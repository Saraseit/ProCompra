"""
app/api/precios.py
------------------
Histórico de precios de insumos.

Cada factura CFDI trae sus renglones (conceptos) con precio unitario. Se
guardan en factura_conceptos y se agrupan por insumo: un insumo del catálogo
junta las distintas formas en que cada proveedor lo nombra (insumo_alias).
Lo que aún no se clasifica se agrupa por su texto normalizado, así que el
histórico sirve desde la primera carga, antes de clasificar nada.

Los XML anteriores a ProCompra se importan como facturas históricas: no se
vinculan a órdenes ni aparecen en «Facturas sin vincular».
"""

import time
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, field_validator

from app.auth import requiere_rol
from app.database import supabase, una_fila
from app.roles import ROLES_OPERACION
from app.utils import cfdi, precios, storage
from app.utils.bandeja import avisar_finanzas, falta_tabla

router = APIRouter(prefix="/precios", tags=["Precios de insumos"])

# Ven precios y avisos todos los que capturan compras; clasifican e importan
# quienes manejan facturas.
ROLES_VER = ROLES_OPERACION
ROLES_CLASIFICAR = ("admin", "pagos", "compras")

MAXIMO_ARCHIVOS = 100       # por envío; la pantalla manda la carga en tandas
LOTE_IN = 50                # valores por filtro in_(): la URL tiene límite
PAGINA = 1000               # filas por página de PostgREST
SEGUNDOS_CACHE = 120

FALTA_MIGRACION = (
    "Falta aplicar la migración 2026-11-02_fase5_precios.sql en Supabase "
    "para usar el histórico de precios"
)

COLUMNAS_ARTICULO = (
    "clave,insumo_id,texto_norm,insumo_nombre,insumo_unidad,descripcion,unidad,compras,"
    "proveedores,ultimo_proveedor,primera,ultima,ultimo_precio,mediana,gastado,busqueda"
)
SELECT_CONCEPTO = (
    "id,fecha,precio_unitario,cantidad,unidad,descripcion,texto_norm,rfc_emisor,nombre_emisor,"
    "factura:factura_id(id,serie,folio,uuid_fiscal,historica,orden:orden_id(id,folio))"
)
SELECT_PARTIDA = (
    "id,concepto,unidad,cantidad,precio_unitario,"
    "orden:orden_id(id,folio,fecha,estado,proveedor:proveedor_id(nombre))"
)


# ── Infraestructura ───────────────────────────────────

def _todas(construir) -> list[dict]:
    """Todas las filas de una consulta, página por página (PostgREST corta en 1000)."""
    filas, inicio = [], 0
    while True:
        lote = construir().range(inicio, inicio + PAGINA - 1).execute().data or []
        filas.extend(lote)
        if len(lote) < PAGINA:
            return filas
        inicio += PAGINA


def _en_lotes(valores: list, tam: int = LOTE_IN):
    valores = list(valores)
    for i in range(0, len(valores), tam):
        yield valores[i:i + tam]


_migracion = {"lista": False, "revisado": 0.0}


def disponible() -> bool:
    """¿Ya se aplicó la migración? Mientras no, el resto de ProCompra sigue igual."""
    if _migracion["lista"]:
        return True
    if time.time() - _migracion["revisado"] < 30:
        return False
    _migracion["revisado"] = time.time()
    try:
        supabase.table("factura_conceptos").select("id").limit(1).execute()
        supabase.table("facturas").select("historica").limit(1).execute()
        _migracion["lista"] = True
    except Exception as e:
        if not falta_tabla(e):
            raise
    return _migracion["lista"]


def _exigir_migracion():
    if not disponible():
        raise HTTPException(status_code=503, detail=FALTA_MIGRACION)


# Catálogo en memoria: la vista agrega toda la tabla, y reconocer una
# descripción compara contra todos los textos conocidos.
_cache: dict = {"t": 0.0}


def invalidar() -> None:
    _cache.clear()
    _cache["t"] = 0.0


def _catalogo() -> dict:
    if _cache.get("articulos") is not None and time.time() - _cache["t"] < SEGUNDOS_CACHE:
        return _cache
    articulos = _todas(lambda: supabase.table("precios_articulos").select(COLUMNAS_ARTICULO).order("clave"))
    alias = _todas(lambda: supabase.table("insumo_alias").select("texto_norm,insumo_id,ignorar").order("texto_norm"))
    insumos = _todas(lambda: supabase.table("insumos").select("id,nombre,unidad,clave_sat,notas").order("nombre"))
    for a in articulos:
        a["nombre"] = a["insumo_nombre"] or precios.nombre_sugerido(a["descripcion"])
        a["busqueda"] = (a.get("busqueda") or "").upper()

    del_catalogo = [{"texto": precios.normalizar(i["nombre"]), "insumo_id": i["id"]} for i in insumos]
    del_catalogo += [{"texto": a["texto_norm"], "insumo_id": a["insumo_id"]} for a in alias if a["insumo_id"]]
    sueltos = [{"texto": a["texto_norm"], "clave": a["clave"]} for a in articulos if not a["insumo_id"]]

    _cache.clear()
    _cache.update(
        t=time.time(),
        articulos=articulos,
        por_clave={a["clave"]: a for a in articulos},
        alias={a["texto_norm"]: a for a in alias},
        insumos={i["id"]: i for i in insumos},
        nombres={precios.normalizar(i["nombre"]): i["id"] for i in insumos},
        indice_catalogo=precios.IndiceTextos(del_catalogo),
        indice_sueltos=precios.IndiceTextos(sueltos),
        indice_todo=precios.IndiceTextos(del_catalogo + sueltos),
        stats={},
    )
    return _cache


def _nombres_proveedor() -> dict:
    """RFC → nombre como está dado de alta en ProCompra (más corto que la razón social)."""
    filas = _todas(lambda: supabase.table("proveedores").select("rfc,nombre").order("nombre"))
    return {(f["rfc"] or "").upper().strip(): f["nombre"] for f in filas if f.get("rfc")}


# ── Artículos: insumo del catálogo o texto sin clasificar ─────────

def _articulo_insumo(cat: dict, insumo_id: str, exacto: bool = True, parecido: float = None) -> Optional[dict]:
    i = cat["insumos"].get(insumo_id)
    if not i:
        return None
    return {"clave": insumo_id, "insumo_id": insumo_id, "texto_norm": None, "nombre": i["nombre"],
            "unidad": i["unidad"], "exacto": exacto, "parecido": parecido}


def _articulo_texto(cat: dict, clave: str, exacto: bool = True, parecido: float = None) -> Optional[dict]:
    a = cat["por_clave"].get(clave)
    if not a:
        return None
    return {"clave": clave, "insumo_id": None, "texto_norm": a["texto_norm"], "nombre": a["nombre"],
            "unidad": None, "exacto": exacto, "parecido": parecido}


def reconocer(descripcion: str, cat: dict = None) -> Optional[dict]:
    """¿A qué artículo del histórico corresponde una descripción capturada?

    Primero lo exacto (texto ya clasificado, nombre de un insumo, texto sin
    clasificar idéntico); si no, el más parecido por encima del umbral.
    """
    cat = cat or _catalogo()
    tn = precios.normalizar(descripcion)
    if not tn:
        return None
    alias = cat["alias"].get(tn)
    if alias:
        return None if alias["ignorar"] else _articulo_insumo(cat, alias["insumo_id"])
    if tn in cat["nombres"]:
        return _articulo_insumo(cat, cat["nombres"][tn])
    if f"t:{tn}" in cat["por_clave"]:
        return _articulo_texto(cat, f"t:{tn}")
    mejor = cat["indice_todo"].buscar(tn, minimo=precios.PARECIDO_RECONOCER, limite=1)
    if not mejor:
        return None
    m = mejor[0]
    if m.get("insumo_id"):
        return _articulo_insumo(cat, m["insumo_id"], exacto=False, parecido=m["parecido"])
    return _articulo_texto(cat, m["clave"], exacto=False, parecido=m["parecido"])


def _clave_valida(clave: str, cat: dict) -> dict:
    if clave.startswith("t:"):
        art = _articulo_texto(cat, clave)
    else:
        art = _articulo_insumo(cat, clave)
    if not art:
        raise HTTPException(status_code=404, detail="No hay compras registradas de ese artículo")
    return art


def _conceptos_de(art: dict) -> list[dict]:
    if art["insumo_id"]:
        return _todas(lambda: supabase.table("factura_conceptos").select(SELECT_CONCEPTO)
                      .eq("insumo_id", art["insumo_id"]).order("fecha").order("id"))
    return _todas(lambda: supabase.table("factura_conceptos").select(SELECT_CONCEPTO)
                  .eq("texto_norm", art["texto_norm"]).is_("insumo_id", "null")
                  .eq("ignorado", False).order("fecha").order("id"))


def _puntos(conceptos: list[dict], nombres: dict = None) -> list[dict]:
    nombres = nombres if nombres is not None else _nombres_proveedor()
    puntos = []
    for c in conceptos:
        f = c.get("factura") or {}
        orden = f.get("orden") or {}
        puntos.append({
            "fecha":       c["fecha"],
            "precio":      float(c["precio_unitario"]),
            "cantidad":    float(c["cantidad"]),
            "unidad":      c["unidad"],
            "proveedor":   nombres.get((c.get("rfc_emisor") or "").upper()) or c.get("nombre_emisor") or c.get("rfc_emisor"),
            "descripcion": c["descripcion"],
            "texto_norm":  c["texto_norm"],
            "factura":     ((f.get("serie") or "") + (f.get("folio") or "")) or (f.get("uuid_fiscal") or "")[:8],
            "historica":   f.get("historica"),
            "orden_folio": orden.get("folio"),
            "orden_id":    orden.get("id"),
        })
    return puntos


def estadisticas_de(art: dict, cat: dict = None) -> dict:
    cat = cat or _catalogo()
    guardadas = cat["stats"].get(art["clave"])
    if guardadas is not None:
        return guardadas
    stats = precios.estadisticas(_puntos(_conceptos_de(art)), date.today(), art.get("unidad"))
    cat["stats"][art["clave"]] = stats
    return stats


def evaluar_renglon(descripcion: str, unidad: Optional[str], precio, cat: dict = None) -> dict:
    cat = cat or _catalogo()
    art = reconocer(descripcion, cat)
    if not art:
        return {"articulo": None, "nivel": "sin_historial", "diferencia": None}
    stats = estadisticas_de(art, cat)
    return {
        "articulo":      art,
        "referencia":    stats["referencia"],
        "ventana_meses": stats["ventana_meses"],
        "compras":       stats["compras"],
        "unidad":        stats["unidad"],
        "mas_barata":    stats["mas_barata"],
        "ultima":        stats["ultima"],
        **precios.evaluar(precio, unidad, stats),
    }


def _cotizado(textos: set[str]) -> list[dict]:
    """Precios cotizados en órdenes de compra para esos textos (exactos o muy parecidos)."""
    textos = {t for t in textos if t}
    if not textos:
        return []
    tokens = {t for texto in textos for t in texto.split() if len(t) >= 3}
    partidas = _todas(lambda: supabase.table("partidas").select(SELECT_PARTIDA).order("created_at").order("id"))
    puntos = []
    for p in partidas:
        orden = p.get("orden") or {}
        precio = float(p.get("precio_unitario") or 0)
        if orden.get("estado") == "rechazada" or precio <= 0:
            continue
        tn = precios.normalizar(p["concepto"])
        if tn not in textos:
            if not tokens.intersection(tn.split()):
                continue
            if max(precios.similitud(tn, t) for t in textos) < precios.PARECIDO_RECONOCER:
                continue
        puntos.append({
            "fecha":     orden.get("fecha"),
            "precio":    precio,
            "cantidad":  float(p.get("cantidad") or 0),
            "unidad":    precios.unidad_estandar(p.get("unidad")),
            "concepto":  p["concepto"],
            "proveedor": (orden.get("proveedor") or {}).get("nombre"),
            "folio":     orden.get("folio"),
            "estado":    orden.get("estado"),
            "orden_id":  orden.get("id"),
        })
    return puntos


# ── Guardar renglones de una factura ──────────────────

def filas_conceptos(factura: dict, conceptos: list[dict]) -> list[dict]:
    return [{
        **c,
        "factura_id":    factura["id"],
        "texto_norm":    precios.normalizar(c["descripcion"]) or "SIN DESCRIPCION",
        "unidad":        precios.unidad_estandar(c.get("unidad"), c.get("clave_unidad")),
        "fecha":         factura.get("fecha_emision"),
        "rfc_emisor":    factura.get("rfc_emisor"),
        "nombre_emisor": factura.get("nombre_emisor"),
    } for c in conceptos]


def guardar_conceptos(factura: dict, conceptos: list[dict]) -> list[dict]:
    """Guarda los renglones de una factura ya registrada. Nunca rompe la carga."""
    if not conceptos or not disponible():
        return []
    try:
        res = supabase.table("factura_conceptos").insert(filas_conceptos(factura, conceptos)).execute()
        invalidar()
        return res.data or []
    except Exception as e:
        print(f"[PRECIOS] no se guardaron los renglones de la factura {factura.get('id')}: {e}")
        return []


# ── Avisos automáticos ────────────────────────────────

def _pesos(n) -> str:
    return f"${float(n):,.2f}"


def revisar_orden(orden_id: str, actor: str = None) -> None:
    """Al enviar una orden a autorización: avisa a contabilidad si algún precio se sale de lo normal."""
    try:
        if not disponible():
            return
        orden = una_fila(supabase.table("ordenes_compra")
                         .select("folio,partidas(concepto,unidad,precio_unitario)").eq("id", orden_id))
        if not orden:
            return
        cat = _catalogo()
        lineas, niveles = [], set()
        for p in orden.get("partidas") or []:
            r = evaluar_renglon(p["concepto"], p.get("unidad"), p.get("precio_unitario"), cat)
            if r["nivel"] in ("alto", "bajo"):
                niveles.add(r["nivel"])
                lineas.append(f"{p['concepto']} a {_pesos(p['precio_unitario'])} "
                              f"({precios.porcentaje(r['diferencia'])}). Referencia {_pesos(r['referencia'])}.")
        if not lineas:
            return
        if niveles == {"alto"}:
            titulo = f"Precio alto en la orden #{orden['folio']}"
        elif niveles == {"bajo"}:
            titulo = f"Precio bajo en la orden #{orden['folio']}: confirma la unidad"
        else:
            titulo = f"Precios fuera de lo normal en la orden #{orden['folio']}"
        detalle = " ".join(lineas[:4]) + (f" Y {len(lineas) - 4} más." if len(lineas) > 4 else "")
        avisar_finanzas(titulo, detalle, orden_id=orden_id, tipo="precio", actor=actor)
    except Exception as e:
        print(f"[PRECIOS] no se revisaron los precios de la orden {orden_id}: {e}")


def comparar_factura(conceptos: list[dict], partidas: list[dict]) -> list[dict]:
    """Renglones de la factura cuyo precio difiere de lo cotizado en la orden."""
    diferencias = []
    for c in conceptos:
        tn = c.get("texto_norm") or precios.normalizar(c["descripcion"])
        mejor, puntaje = None, 0.0
        for p in partidas:
            s = precios.similitud(tn, p["concepto"])
            if s > puntaje:
                mejor, puntaje = p, s
        if not mejor or puntaje < precios.PARECIDO_RECONOCER:
            continue
        cotizado = float(mejor.get("precio_unitario") or 0)
        facturado = float(c["precio_unitario"])
        u_fac, u_cot = c.get("unidad"), precios.unidad_estandar(mejor.get("unidad"))
        if cotizado <= 0 or (u_fac and u_cot and u_fac != u_cot):
            continue
        diferencia = facturado / cotizado - 1
        if abs(diferencia) >= precios.UMBRAL_FACTURA and abs(facturado - cotizado) >= 1:
            diferencias.append({"concepto": mejor["concepto"], "facturado": facturado,
                                "cotizado": cotizado, "diferencia": round(diferencia, 4)})
    return diferencias


def revisar_factura(factura_id: str, orden_id: str, actor: str = None) -> None:
    """Al vincular una factura: avisa si lo facturado no coincide con lo cotizado."""
    try:
        if not disponible():
            return
        conceptos = (supabase.table("factura_conceptos")
                     .select("descripcion,texto_norm,precio_unitario,unidad")
                     .eq("factura_id", factura_id).execute()).data or []
        orden = una_fila(supabase.table("ordenes_compra")
                         .select("folio,partidas(concepto,unidad,precio_unitario)").eq("id", orden_id))
        if not conceptos or not orden:
            return
        diferencias = comparar_factura(conceptos, orden.get("partidas") or [])
        if not diferencias:
            return
        partes = [f"{d['concepto']}: facturado {_pesos(d['facturado'])}, cotizado {_pesos(d['cotizado'])} "
                  f"({precios.porcentaje(d['diferencia'])})" for d in diferencias[:4]]
        avisar_finanzas("La factura no coincide con lo cotizado",
                        f"Orden #{orden['folio']} · " + "; ".join(partes) + ".",
                        orden_id=orden_id, tipo="precio", actor=actor)
    except Exception as e:
        print(f"[PRECIOS] no se comparó la factura {factura_id}: {e}")


# ── Consultas ─────────────────────────────────────────

def _contar(tabla: str, filtros) -> int:
    return filtros(supabase.table(tabla).select("id", count="exact").limit(1)).execute().count or 0


@router.get("/resumen")
def resumen(usuario=Depends(requiere_rol(*ROLES_VER))):
    if not disponible():
        return {"disponible": False}
    cat = _catalogo()
    sueltos = [a for a in cat["articulos"] if not a["insumo_id"]]
    fechas = [a["primera"] for a in cat["articulos"] if a["primera"]] + \
             [a["ultima"] for a in cat["articulos"] if a["ultima"]]
    return {
        "disponible":            True,
        "facturas":              _contar("facturas", lambda q: q),
        "facturas_historicas":   _contar("facturas", lambda q: q.eq("historica", True)),
        "renglones":             _contar("factura_conceptos", lambda q: q),
        "renglones_clasificados": _contar("factura_conceptos", lambda q: q.not_.is_("insumo_id", "null")),
        "renglones_ignorados":   _contar("factura_conceptos", lambda q: q.eq("ignorado", True)),
        "insumos":               len(cat["insumos"]),
        "textos_por_clasificar": len(sueltos),
        "desde":                 min(fechas) if fechas else None,
        "hasta":                 max(fechas) if fechas else None,
        "puede_clasificar":      usuario["rol"] in ROLES_CLASIFICAR,
        "umbral_alto":           precios.UMBRAL_ALTO,
        "umbral_bajo":           precios.UMBRAL_BAJO,
        "minimo_compras":        precios.MINIMO_COMPRAS,
    }


def _filtrar(articulos: list[dict], q: str) -> list[dict]:
    palabras = precios.normalizar(q).split()
    if not palabras:
        return articulos
    return [a for a in articulos if all(p in a["busqueda"] for p in palabras)]


ORDENES_LISTA = {
    "compras":  lambda a: (-(a["compras"] or 0), a["nombre"]),
    "gastado":  lambda a: (-float(a["gastado"] or 0), a["nombre"]),
    "reciente": lambda a: (-(date.fromisoformat(str(a["ultima"])[:10]).toordinal() if a["ultima"] else 0), a["nombre"]),
    "nombre":   lambda a: (a["nombre"].upper(),),
}


@router.get("/articulos")
def articulos(
    q: str = "",
    tipo: str = Query("todos", pattern="^(todos|insumos|sin_clasificar)$"),
    orden: str = Query("compras", pattern="^(compras|gastado|reciente|nombre)$"),
    offset: int = Query(0, ge=0),
    limite: int = Query(50, ge=1, le=200),
    usuario=Depends(requiere_rol(*ROLES_VER)),
):
    """Todo lo comprado, agrupado por insumo (o por texto si aún no se clasifica)."""
    _exigir_migracion()
    lista = _filtrar(_catalogo()["articulos"], q)
    if tipo == "insumos":
        lista = [a for a in lista if a["insumo_id"]]
    elif tipo == "sin_clasificar":
        lista = [a for a in lista if not a["insumo_id"]]
    lista = sorted(lista, key=ORDENES_LISTA[orden])
    return {"total": len(lista), "articulos": [_sin_busqueda(a) for a in lista[offset:offset + limite]]}


def _sin_busqueda(a: dict) -> dict:
    return {k: v for k, v in a.items() if k != "busqueda"}


@router.get("/articulo")
def articulo(clave: str = Query(..., min_length=3, max_length=220),
             usuario=Depends(requiere_rol(*ROLES_VER))):
    """Historial completo de un artículo: gráfica, proveedores y compras."""
    _exigir_migracion()
    cat = _catalogo()
    art = _clave_valida(clave, cat)
    nombres = _nombres_proveedor()
    puntos = _puntos(_conceptos_de(art), nombres)
    stats = precios.estadisticas(puntos, date.today(), art.get("unidad"))
    cat["stats"][art["clave"]] = stats

    textos: dict[str, int] = {}
    for p in puntos:
        textos[p["texto_norm"]] = textos.get(p["texto_norm"], 0) + 1
    if art["insumo_id"]:
        for a in cat["alias"].values():
            if a["insumo_id"] == art["insumo_id"]:
                textos.setdefault(a["texto_norm"], 0)
        insumo = cat["insumos"][art["insumo_id"]]
        art = {**art, "clave_sat": insumo.get("clave_sat"), "notas": insumo.get("notas")}

    unidad = stats["unidad"]
    for p in puntos:
        p["comparable"] = not unidad or not p["unidad"] or p["unidad"] == unidad
    cotizado = _cotizado(set(textos) | ({precios.normalizar(art["nombre"])} if art["insumo_id"] else set()))
    for c in cotizado:
        c["comparable"] = not unidad or not c["unidad"] or c["unidad"] == unidad

    return {
        "articulo":  {**art, "textos": [{"texto_norm": t, "compras": n}
                                        for t, n in sorted(textos.items(), key=lambda kv: -kv[1])]},
        "stats":     stats,
        "facturado": puntos,
        "cotizado":  cotizado,
        "umbral":    {"alto": precios.UMBRAL_ALTO, "bajo": precios.UMBRAL_BAJO},
    }


@router.get("/insumos")
def listar_insumos(q: str = "", usuario=Depends(requiere_rol(*ROLES_VER))):
    """Catálogo de insumos (para elegir a cuál pertenece un texto)."""
    _exigir_migracion()
    cat = _catalogo()
    palabras = precios.normalizar(q).split()
    filas = []
    for i in cat["insumos"].values():
        a = cat["por_clave"].get(i["id"]) or {}
        nombre = precios.normalizar(i["nombre"])
        if palabras and not all(p in nombre or p in (a.get("busqueda") or "") for p in palabras):
            continue
        filas.append({**i, "compras": a.get("compras") or 0, "ultimo_precio": a.get("ultimo_precio"),
                      "ultima": a.get("ultima")})
    filas.sort(key=lambda f: f["nombre"].upper())
    return filas


@router.get("/por-clasificar")
def por_clasificar(
    q: str = "",
    offset: int = Query(0, ge=0),
    limite: int = Query(30, ge=1, le=100),
    usuario=Depends(requiere_rol(*ROLES_VER)),
):
    """Textos sin clasificar, de los más comprados a los menos, con sugerencia de insumo."""
    _exigir_migracion()
    cat = _catalogo()
    grupos = sorted(_filtrar([a for a in cat["articulos"] if not a["insumo_id"]], q),
                    key=lambda a: (-(a["compras"] or 0), a["texto_norm"]))
    pagina = []
    for g in grupos[offset:offset + limite]:
        sugerencias, vistos = [], set()
        for s in cat["indice_catalogo"].buscar(g["texto_norm"], limite=6):
            if s["insumo_id"] in vistos:
                continue
            vistos.add(s["insumo_id"])
            i = cat["insumos"].get(s["insumo_id"])
            if i:
                sugerencias.append({"insumo_id": i["id"], "nombre": i["nombre"], "unidad": i["unidad"],
                                    "parecido": s["parecido"]})
        parecidos = [s for s in cat["indice_sueltos"].buscar(g["texto_norm"], minimo=0.7, limite=30)
                     if s["clave"] != g["clave"]]
        pagina.append({**_sin_busqueda(g), "sugerencias": sugerencias[:3], "parecidos": len(parecidos)})
    return {"total": len(grupos), "grupos": pagina}


@router.get("/similares")
def similares(texto: str = Query(..., min_length=1, max_length=220),
              usuario=Depends(requiere_rol(*ROLES_VER))):
    """Otros textos sin clasificar parecidos: para clasificarlos juntos."""
    _exigir_migracion()
    cat = _catalogo()
    tn = precios.normalizar(texto)
    filas = []
    for s in cat["indice_sueltos"].buscar(tn, limite=40):
        a = cat["por_clave"].get(s["clave"])
        if a and a["texto_norm"] != tn:
            filas.append({**_sin_busqueda(a), "parecido": s["parecido"]})
    return filas


@router.get("/ignorados")
def ignorados(usuario=Depends(requiere_rol(*ROLES_VER))):
    _exigir_migracion()
    return sorted(t for t, a in _catalogo()["alias"].items() if a["ignorar"])


# ── Clasificación ─────────────────────────────────────

class Clasificacion(BaseModel):
    textos: list[str] = Field(..., min_length=1, max_length=500)
    insumo_id: Optional[str] = None
    ignorar: bool = False

    @field_validator("textos")
    @classmethod
    def _normalizados(cls, v: list[str]) -> list[str]:
        limpios = sorted({precios.normalizar(t) for t in v} - {""})
        if not limpios:
            raise ValueError("indica al menos un texto")
        return limpios


class InsumoNuevo(BaseModel):
    nombre: str = Field(..., min_length=1, max_length=120)
    unidad: Optional[str] = Field(None, max_length=20)
    clave_sat: Optional[str] = Field(None, max_length=20)
    notas: Optional[str] = Field(None, max_length=500)
    textos: list[str] = Field(default_factory=list, max_length=500)


class InsumoCambio(BaseModel):
    nombre: Optional[str] = Field(None, min_length=1, max_length=120)
    unidad: Optional[str] = Field(None, max_length=20)
    clave_sat: Optional[str] = Field(None, max_length=20)
    notas: Optional[str] = Field(None, max_length=500)


class Union(BaseModel):
    destino_id: str


def _asignar(textos: list[str], insumo_id: Optional[str], ignorar: bool, usuario_id: str) -> int:
    """Clasifica los textos: a un insumo, como «no es insumo», o los regresa a sin clasificar."""
    if insumo_id or ignorar:
        supabase.table("insumo_alias").upsert(
            [{"texto_norm": t, "insumo_id": insumo_id, "ignorar": ignorar, "creado_por": usuario_id}
             for t in textos],
            on_conflict="texto_norm",
        ).execute()
    else:
        for lote in _en_lotes(textos):
            supabase.table("insumo_alias").delete().in_("texto_norm", lote).execute()

    cambiados = 0
    for lote in _en_lotes(textos):
        res = (supabase.table("factura_conceptos")
               .update({"insumo_id": insumo_id, "ignorado": ignorar})
               .in_("texto_norm", lote).execute())
        cambiados += len(res.data or [])
    invalidar()
    return cambiados


def _insumo(insumo_id: str) -> dict:
    fila = una_fila(supabase.table("insumos").select("*").eq("id", insumo_id))
    if not fila:
        raise HTTPException(status_code=404, detail="Insumo no encontrado")
    return fila


@router.post("/clasificar")
def clasificar(body: Clasificacion, usuario=Depends(requiere_rol(*ROLES_CLASIFICAR))):
    _exigir_migracion()
    if body.insumo_id and body.ignorar:
        raise HTTPException(status_code=400, detail="Elige un insumo o «no es insumo», no ambos")
    if body.insumo_id:
        _insumo(body.insumo_id)
    renglones = _asignar(body.textos, body.insumo_id, body.ignorar, usuario["id"])
    return {"textos": len(body.textos), "renglones": renglones}


@router.post("/insumos", status_code=201)
def crear_insumo(body: InsumoNuevo, usuario=Depends(requiere_rol(*ROLES_CLASIFICAR))):
    _exigir_migracion()
    nombre = " ".join(body.nombre.split())
    existente = una_fila(supabase.table("insumos").select("id,nombre").ilike("nombre", nombre))
    if existente:
        raise HTTPException(status_code=409, detail=f"Ya existe el insumo «{existente['nombre']}»")
    res = supabase.table("insumos").insert({
        "nombre": nombre,
        "unidad": (body.unidad or "").strip().upper() or None,
        "clave_sat": (body.clave_sat or "").strip() or None,
        "notas": (body.notas or "").strip() or None,
        "creado_por": usuario["id"],
    }).execute()
    insumo = res.data[0]
    textos = sorted(({precios.normalizar(t) for t in body.textos} | {precios.normalizar(nombre)}) - {""})
    renglones = _asignar(textos, insumo["id"], False, usuario["id"])
    return {**insumo, "renglones": renglones}


@router.patch("/insumos/{insumo_id}")
def editar_insumo(insumo_id: str, body: InsumoCambio, usuario=Depends(requiere_rol(*ROLES_CLASIFICAR))):
    _exigir_migracion()
    _insumo(insumo_id)
    datos = {k: (" ".join(v.split()) if isinstance(v, str) else v)
             for k, v in body.model_dump(exclude_unset=True).items()}
    if "unidad" in datos:
        datos["unidad"] = (datos["unidad"] or "").upper() or None
    if not datos:
        raise HTTPException(status_code=400, detail="No hay cambios")
    if datos.get("nombre"):
        otro = una_fila(supabase.table("insumos").select("id,nombre").ilike("nombre", datos["nombre"]).neq("id", insumo_id))
        if otro:
            raise HTTPException(status_code=409, detail=f"Ya existe el insumo «{otro['nombre']}»")
    datos["updated_at"] = datetime.now(timezone.utc).isoformat()
    res = supabase.table("insumos").update(datos).eq("id", insumo_id).execute()
    if datos.get("nombre"):
        _asignar([precios.normalizar(datos["nombre"])], insumo_id, False, usuario["id"])
    invalidar()
    return res.data[0]


@router.post("/insumos/{insumo_id}/unir")
def unir_insumos(insumo_id: str, body: Union, usuario=Depends(requiere_rol(*ROLES_CLASIFICAR))):
    """Junta dos insumos que resultaron ser el mismo: el origen desaparece."""
    _exigir_migracion()
    if insumo_id == body.destino_id:
        raise HTTPException(status_code=400, detail="Elige un insumo distinto")
    origen, destino = _insumo(insumo_id), _insumo(body.destino_id)
    supabase.table("insumo_alias").update({"insumo_id": destino["id"]}).eq("insumo_id", origen["id"]).execute()
    supabase.table("factura_conceptos").update({"insumo_id": destino["id"]}).eq("insumo_id", origen["id"]).execute()
    supabase.table("insumos").delete().eq("id", origen["id"]).execute()
    invalidar()
    return {"destino": destino, "unido": origen["nombre"]}


@router.delete("/insumos/{insumo_id}", status_code=204)
def borrar_insumo(insumo_id: str, usuario=Depends(requiere_rol(*ROLES_CLASIFICAR))):
    """Quita el insumo del catálogo; sus textos vuelven a «Por clasificar»."""
    _exigir_migracion()
    _insumo(insumo_id)
    supabase.table("factura_conceptos").update({"insumo_id": None}).eq("insumo_id", insumo_id).execute()
    supabase.table("insumos").delete().eq("id", insumo_id).execute()
    invalidar()


# ── Avisos al capturar ────────────────────────────────

class RenglonEvaluar(BaseModel):
    descripcion: str = Field(..., max_length=500)
    unidad: Optional[str] = Field(None, max_length=40)
    precio: Optional[float] = None


class Evaluacion(BaseModel):
    renglones: list[RenglonEvaluar] = Field(..., min_length=1, max_length=60)


@router.post("/evaluar")
def evaluar(body: Evaluacion, usuario=Depends(requiere_rol(*ROLES_VER))):
    """Compara precios capturados con su referencia (formulario y partidas de la orden)."""
    if not disponible():
        return {"disponible": False, "resultados": []}
    cat = _catalogo()
    return {"disponible": True, "resultados": [
        evaluar_renglon(r.descripcion, r.unidad, r.precio, cat) for r in body.renglones
    ]}


# ── Importación del histórico ─────────────────────────

def _rfcs(texto: Optional[str]) -> set[str]:
    return {r.strip().upper() for r in (texto or "").replace(";", ",").split(",") if r.strip()}


def _existentes(uuids: list[str]) -> dict:
    """Facturas ya cargadas por UUID, con si ya tienen sus renglones."""
    previas = {}
    for lote in _en_lotes(uuids):
        for f in (supabase.table("facturas").select("id,uuid_fiscal,historica")
                  .in_("uuid_fiscal", lote).execute()).data or []:
            previas[f["uuid_fiscal"]] = {**f, "con_renglones": False}
    ids = {f["id"]: u for u, f in previas.items()}
    for lote in _en_lotes(list(ids)):
        for c in (supabase.table("factura_conceptos").select("factura_id")
                  .in_("factura_id", lote).execute()).data or []:
            previas[ids[c["factura_id"]]]["con_renglones"] = True
    return previas


@router.post("/importar")
async def importar(
    archivos: list[UploadFile] = File(...),
    rfc_empresa: str = Form(""),
    usuario=Depends(requiere_rol(*ROLES_CLASIFICAR)),
):
    """Importa XML anteriores como facturas históricas y guarda sus renglones.

    Solo entran facturas de ingreso recibidas por la empresa (rfc_empresa,
    uno o varios separados por coma): las emitidas por la empresa son ventas
    y no deben mezclarse con precios de compra. Las repetidas se detectan por
    UUID; si una ya estaba cargada sin renglones, se le agregan.
    """
    _exigir_migracion()
    if len(archivos) > MAXIMO_ARCHIVOS:
        raise HTTPException(status_code=400, detail=f"Máximo {MAXIMO_ARCHIVOS} archivos por envío")
    empresa = _rfcs(rfc_empresa)

    resultados, leidas = [], []
    for archivo in archivos:
        nombre = archivo.filename or "archivo.xml"
        base = {"archivo": nombre}
        if not nombre.lower().endswith(".xml"):
            resultados.append({**base, "estado": "invalida", "mensaje": "No es un archivo .xml"})
            continue
        contenido = await archivo.read()
        if not contenido or len(contenido) > storage.TAMANO_MAXIMO:
            resultados.append({**base, "estado": "invalida", "mensaje": "Archivo vacío o de más de 10 MB"})
            continue
        try:
            datos = cfdi.leer_cfdi(contenido)
        except cfdi.CfdiInvalido as e:
            resultados.append({**base, "estado": "invalida", "mensaje": str(e)})
            continue
        if (datos["tipo_comprobante"] or "I").upper() != "I":
            resultados.append({**base, "estado": "ignorada", "mensaje": "No es factura de ingreso (nota de crédito, pago, nómina o traslado)"})
            continue
        if empresa and datos["rfc_receptor"] not in empresa:
            emitida = datos["rfc_emisor"] in empresa
            resultados.append({**base, "estado": "no_compra",
                               "mensaje": "Emitida por la empresa (es una venta)" if emitida
                               else f"Recibida por otro RFC ({datos['rfc_receptor'] or 'sin RFC'})"})
            continue
        leidas.append((base, datos))

    # Repetidas dentro del mismo envío.
    vistas, unicas = set(), []
    for base, datos in leidas:
        u = datos["uuid_fiscal"]
        if u and u in vistas:
            resultados.append({**base, "estado": "duplicada", "mensaje": "Repetida en esta misma carga"})
            continue
        vistas.add(u)
        unicas.append((base, datos))

    previas = _existentes([d["uuid_fiscal"] for _, d in unicas if d["uuid_fiscal"]])
    nuevas, completar = [], []
    for base, datos in unicas:
        previa = previas.get(datos["uuid_fiscal"]) if datos["uuid_fiscal"] else None
        if previa and previa["con_renglones"]:
            resultados.append({**base, "estado": "duplicada", "mensaje": "Ya estaba cargada"})
        elif previa:
            completar.append((base, datos, previa))
        else:
            nuevas.append((base, datos))

    renglones = []
    if nuevas:
        filas = [{**{k: v for k, v in d.items() if k != "conceptos"},
                  "historica": True, "vinculacion": "pendiente", "subido_por": usuario["id"]}
                 for _, d in nuevas]
        try:
            insertadas = supabase.table("facturas").insert(filas).execute().data or []
        except Exception:
            # Una fila mala no debe tumbar el lote: se reintenta una por una.
            insertadas = []
            for f in filas:
                try:
                    insertadas += supabase.table("facturas").insert(f).execute().data or []
                except Exception as e:
                    print(f"[PRECIOS] no se importó {f.get('uuid_fiscal')}: {e}")
        por_uuid = {f["uuid_fiscal"]: f for f in insertadas if f.get("uuid_fiscal")}
        sin_uuid = [f for f in insertadas if not f.get("uuid_fiscal")]
        for base, datos in nuevas:
            factura = por_uuid.get(datos["uuid_fiscal"]) if datos["uuid_fiscal"] else (sin_uuid.pop(0) if sin_uuid else None)
            if not factura:
                resultados.append({**base, "estado": "invalida", "mensaje": "No se pudo guardar"})
                continue
            renglones += filas_conceptos(factura, datos["conceptos"])
            resultados.append({**base, "estado": "importada", "renglones": len(datos["conceptos"]),
                               "mensaje": f"{len(datos['conceptos'])} renglones"})

    for base, datos, previa in completar:
        factura = {"id": previa["id"], "fecha_emision": datos["fecha_emision"],
                   "rfc_emisor": datos["rfc_emisor"], "nombre_emisor": datos["nombre_emisor"]}
        renglones += filas_conceptos(factura, datos["conceptos"])
        resultados.append({**base, "estado": "completada", "renglones": len(datos["conceptos"]),
                           "mensaje": "Ya estaba cargada; se agregaron sus renglones"})

    guardados = []
    for lote in _en_lotes(renglones, 500):
        try:
            guardados += supabase.table("factura_conceptos").insert(lote).execute().data or []
        except Exception as e:
            print(f"[PRECIOS] no se guardó un lote de renglones: {e}")
    if renglones:
        invalidar()

    resumen_estados: dict[str, int] = {}
    for r in resultados:
        resumen_estados[r["estado"]] = resumen_estados.get(r["estado"], 0) + 1
    return {
        "resultados": resultados,
        "resumen":    resumen_estados,
        "renglones":  len(guardados),
        "reconocidos": sum(1 for g in guardados if g.get("insumo_id") or g.get("ignorado")),
        "fallidos":   len(renglones) - len(guardados),
    }
