"""
app/api/propago.py
------------------
ProPago: cajas, catálogo contable y registros para el ERP.

Cada pago sale de una caja. Al registrarlo (desde Pagos, desde la orden o
como pago sin orden) se generan sus renglones para el ERP con el motor de
app/utils/registros.py. Se revisan, se eligen por pago y se descargan en el
Excel de importación. Un renglón exportado ya no se regenera: si hay que
corregirlo se deja un ajuste para aplicarlo a mano en el ERP, que no acepta
cargos negativos.

Sin la migración de la fase 8 el resto de ProCompra sigue igual.
"""

import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from app.auth import requiere_rol
from app.database import supabase, una_fila
from app.roles import ROLES_CONTABILIDAD
from app.utils import propago_inicial as inicial, registros as motor
from app.utils.bandeja import falta_tabla
from app.utils.precios import normalizar

router = APIRouter(prefix="/propago", tags=["ProPago"])

FALTA_MIGRACION = "Falta aplicar la migración 2026-11-11_fase8_propago.sql en Supabase para usar ProPago"
LOTE_IN = 50
PAGINA = 1000
# Mérida no cambia de horario: la fecha del archivo es la del día en Mérida.
ZONA = timezone(timedelta(hours=-6))

TIPOS_CAJA = ("banco", "tarjeta_debito", "tarjeta_credito", "amex", "inversion", "caja_chica_virtual", "fondo_fijo")
TIPOS_CUENTA = ("gasto", "iva", "retencion_isr", "retencion_iva")
METODOS = ("transferencia", "tarjeta_credito", "efectivo", "tarjeta_debito")
NATURALEZAS = ("total", "anticipo", "abono", "saldo")

SELECT_REGISTROS = (
    "*,movimiento:movimiento_id(id,fecha_pago,tipo,monto,referencia,justificacion,notas,poliza,comision,diferencia,"
    "proveedor_texto,caja_id,created_at),orden:orden_id(id,folio,proveedor:proveedor_id(nombre))"
)


def hoy() -> date:
    return datetime.now(ZONA).date()


# ── Infraestructura ───────────────────────────────────

_migracion = {"lista": False, "revisado": 0.0}


def disponible() -> bool:
    """¿Ya se aplicó la fase 8? Mientras no, los pagos se registran como antes."""
    if _migracion["lista"]:
        return True
    if time.time() - _migracion["revisado"] < 30:
        return False
    _migracion["revisado"] = time.time()
    try:
        supabase.table("registros_erp").select("id").limit(1).execute()
        supabase.table("movimientos_pago").select("caja_id").limit(1).execute()
        _migracion["lista"] = True
    except Exception as e:
        if not falta_tabla(e):
            raise
    return _migracion["lista"]


def _exigir():
    if not disponible():
        raise HTTPException(status_code=503, detail=FALTA_MIGRACION)


def _todas(construir) -> list[dict]:
    filas, inicio = [], 0
    while True:
        lote = construir().range(inicio, inicio + PAGINA - 1).execute().data or []
        filas.extend(lote)
        if len(lote) < PAGINA:
            return filas
        inicio += PAGINA


def _en_lotes(valores, tam: int = LOTE_IN):
    valores = list(valores)
    for i in range(0, len(valores), tam):
        yield valores[i:i + tam]


def _bitacora(entidad: str, entidad_id, accion: str, usuario_id: str, antes=None, despues=None) -> None:
    try:
        supabase.table("bitacora").insert({"entidad": entidad, "entidad_id": str(entidad_id) if entidad_id else None,
                                           "accion": accion, "antes": antes, "despues": despues,
                                           "usuario_id": usuario_id}).execute()
    except Exception as e:
        print(f"[PROPAGO] no se guardó la bitácora de {entidad} {entidad_id}: {e}")


def _limpio(v: Optional[str]) -> Optional[str]:
    v = " ".join((v or "").split())
    return v or None


def _uuid(v: str) -> str:
    from app.api.ordenes import _uuid_valido
    try:
        return _uuid_valido(v)
    except ValueError:
        raise HTTPException(status_code=404, detail="No encontrado")


# ── Cajas y permisos ──────────────────────────────────

def _es_admin(usuario: dict) -> bool:
    return usuario["rol"] == "admin"


def cajas_de(usuario: dict, solo_activas: bool = True) -> list[dict]:
    """Cajas que el usuario puede usar, con sus permisos. Admin las ve todas."""
    q = supabase.table("cajas").select("*").order("nombre")
    if solo_activas:
        q = q.eq("activa", True)
    cajas = q.execute().data or []
    if _es_admin(usuario):
        return [{**c, "puede_registrar": True, "puede_exportar": True} for c in cajas]
    permisos = {p["caja_id"]: p for p in (supabase.table("caja_usuarios").select("*")
                                          .eq("usuario_id", usuario["id"]).execute()).data or []}
    return [{**c, "puede_registrar": permisos[c["id"]]["puede_registrar"],
             "puede_exportar": permisos[c["id"]]["puede_exportar"]}
            for c in cajas if c["id"] in permisos]


def hay_cajas() -> bool:
    return bool((supabase.table("cajas").select("id").eq("activa", True).limit(1).execute()).data)


def exigir_caja(usuario: dict, caja_id: str, permiso: str = "puede_registrar") -> dict:
    caja = next((c for c in cajas_de(usuario) if c["id"] == caja_id), None)
    if not caja:
        raise HTTPException(status_code=403, detail="No tienes asignada esa caja")
    if not caja.get(permiso):
        accion = "exportar" if permiso == "puede_exportar" else "registrar pagos"
        raise HTTPException(status_code=403, detail=f"No puedes {accion} en la caja {caja['nombre']}")
    return caja


def propietario(caja: Optional[dict]) -> Optional[str]:
    return motor.mayusculas(caja["nombre"]) if caja else None


# ── Catálogo ──────────────────────────────────────────

def _catalogo() -> dict:
    cuentas = {c["id"]: c for c in (supabase.table("cuentas_contables").select("*").execute()).data or []}
    plantillas = {p["id"]: p for p in (supabase.table("plantillas_registro").select("*").execute()).data or []}
    reglas = (supabase.table("reglas_cuenta").select("*").order("prioridad").execute()).data or []
    # Un parámetro que aún no se guarda toma su valor inicial.
    parametros = {c: v for c, v, _ in inicial.PARAMETROS}
    parametros.update({p["clave"]: p["valor"] for p in (supabase.table("parametros_contables").select("*").execute()).data or []})
    return {"cuentas": cuentas, "plantillas": plantillas, "reglas": reglas, "parametros": parametros}


def _motor(cat: dict = None) -> motor.Motor:
    cat = cat or _catalogo()
    return motor.Motor(cat["reglas"], cat["cuentas"], cat["plantillas"], cat["parametros"])


def _numero(valor, defecto: float) -> float:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return defecto


# ── Generación de renglones ───────────────────────────

SELECT_ORDEN = (
    "id,folio,total,proveedor_id,proveedor:proveedor_id(id,nombre),"
    "partidas(concepto,cantidad,importe,contrato),facturas(total,iva,metodo_pago,serie,folio,historica)"
)


def _factura(facturas: list[dict]) -> Optional[dict]:
    """Las facturas de la orden como una sola: total, IVA y si alguna es PPD."""
    vigentes = [f for f in facturas or [] if not f.get("historica")]
    if not vigentes:
        return None
    primera = vigentes[0]
    return {
        "total": round(sum(float(f.get("total") or 0) for f in vigentes), 2),
        "iva": round(sum(float(f.get("iva") or 0) for f in vigentes), 2),
        "metodo_pago": "PPD" if any((f.get("metodo_pago") or "").upper() == "PPD" for f in vigentes) else primera.get("metodo_pago"),
        "serie": primera.get("serie"),
        "folio": primera.get("folio"),
    }


def _insumos(partidas: list[dict], reglas: list[dict]) -> None:
    """Reconoce el insumo de cada partida, solo si alguna regla lo usa."""
    if not any(r.get("insumo_id") for r in reglas):
        return
    from app.api import precios as api_precios
    try:
        cat = api_precios._catalogo()
        for p in partidas:
            art = api_precios.reconocer(p.get("concepto") or "", cat)
            p["insumo_id"] = art.get("insumo_id") if art else None
    except Exception as e:
        print(f"[PROPAGO] no se reconocieron insumos: {e}")


def _datos_pago(mov: dict, caja: dict, reglas: list[dict]) -> dict:
    """Lo que el motor necesita de un movimiento, leído de la base."""
    aplicaciones = (supabase.table("pagos").select("id,orden_id,monto,naturaleza,created_at")
                    .eq("movimiento_id", mov["id"]).order("created_at").execute()).data or []
    ids = list({a["orden_id"] for a in aplicaciones})
    ordenes, previos, iva_previo = {}, {}, {}
    if ids:
        ordenes = {o["id"]: o for o in (supabase.table("ordenes_compra").select(SELECT_ORDEN)
                                         .in_("id", ids).execute()).data or []}
        # Lo pagado antes de este movimiento: base del IVA proporcional.
        otros = (supabase.table("pagos").select("orden_id,monto,movimiento_id,created_at")
                 .in_("orden_id", ids).execute()).data or []
        anteriores = [p for p in otros if p.get("movimiento_id") != mov["id"]
                      and str(p.get("created_at") or "") < str(mov.get("created_at") or "9999")]
        movs_anteriores = {p["movimiento_id"] for p in anteriores if p.get("movimiento_id")}
        for p in anteriores:
            previos[p["orden_id"]] = previos.get(p["orden_id"], 0) + float(p["monto"] or 0)
        if movs_anteriores:
            for r in (supabase.table("registros_erp").select("orden_id,cargo,movimiento_id")
                      .eq("es_iva", True).in_("orden_id", ids).execute()).data or []:
                if r["movimiento_id"] in movs_anteriores:
                    iva_previo[r["orden_id"]] = iva_previo.get(r["orden_id"], 0) + float(r["cargo"] or 0)

    lista = []
    for a in aplicaciones:
        o = ordenes.get(a["orden_id"]) or {}
        partidas = [dict(p) for p in o.get("partidas") or []]
        _insumos(partidas, reglas)
        lista.append({
            "pago_id": a["id"], "orden_id": a["orden_id"], "monto": float(a["monto"] or 0),
            "naturaleza": a.get("naturaleza"), "orden_folio": o.get("folio"), "proveedor": o.get("proveedor") or {},
            "partidas": partidas, "factura": _factura(o.get("facturas")),
            "pagado_previo": previos.get(a["orden_id"], 0), "iva_previo": iva_previo.get(a["orden_id"], 0),
        })
    lineas = (supabase.table("pago_lineas").select("*").eq("movimiento_id", mov["id"]).order("renglon").execute()).data or []
    _insumos(lineas, reglas)
    return {
        "fecha": mov["fecha_pago"], "plaza": mov.get("plaza") or caja.get("plaza") or "MID",
        "poliza": motor.texto_poliza(caja.get("prefijo_poliza"), mov.get("poliza")),
        "caja_id": caja["id"], "comision": float(mov.get("comision") or 0), "diferencia": float(mov.get("diferencia") or 0),
        "aplicaciones": lista, "lineas": lineas, "proveedor_texto": mov.get("proveedor_texto"),
    }


def _avisar_diferencia(renglones: list[dict], mov: dict, parametros: dict) -> None:
    diferencia = float(mov.get("diferencia") or 0)
    monto = float(mov.get("monto") or 0)
    tope = _numero(parametros.get("aviso_diferencia_monto"), 50)
    tope_pct = _numero(parametros.get("aviso_diferencia_pct"), 0.05)
    if diferencia > 0 and (diferencia > tope or (monto and diferencia / monto > tope_pct)):
        for r in renglones:
            if not r["es_iva"]:
                r["avisos"].append(f"Diferencia de ${diferencia:,.2f} entre el comprobante y el banco")
                break


def calcular(mov: dict, caja: dict, cat: dict) -> list[dict]:
    renglones = _motor(cat).generar(_datos_pago(mov, caja, cat["reglas"]))
    _avisar_diferencia(renglones, mov, cat["parametros"])
    if not motor.cuadra(renglones, mov.get("monto") or 0):
        for r in renglones:
            r["avisos"].append("Los renglones no suman el monto del pago")
            r["estado"] = "pendiente"
    return renglones


def _mov(movimiento_id: str) -> Optional[dict]:
    return una_fila(supabase.table("movimientos_pago").select("*,caja:caja_id(*)").eq("id", movimiento_id))


def generar(movimiento_id: str, forzar: bool = False, cat: dict = None) -> dict:
    """(Re)genera los renglones de un movimiento que aún no se exporta.

    Si alguno se editó a mano no se toca, salvo con `forzar`.
    """
    mov = _mov(movimiento_id)
    if not mov or not mov.get("caja"):
        return {"generados": 0, "motivo": "sin caja"}
    existentes = (supabase.table("registros_erp").select("id,estado,editado_manual")
                  .eq("movimiento_id", movimiento_id).execute()).data or []
    if any(r["estado"] == "exportado" for r in existentes):
        return {"generados": 0, "motivo": "ya exportado"}
    if any(r["editado_manual"] for r in existentes) and not forzar:
        return {"generados": 0, "motivo": "editado a mano"}
    cat = cat or _catalogo()
    renglones = calcular(mov, mov["caja"], cat)
    if existentes:
        supabase.table("registros_erp").delete().eq("movimiento_id", movimiento_id).neq("estado", "exportado").execute()
    filas = [{**r, "movimiento_id": movimiento_id, "caja_id": mov["caja_id"], "propietario": propietario(mov["caja"]),
              "poliza": mov.get("poliza")} for r in renglones]
    if filas:
        supabase.table("registros_erp").insert(filas).execute()
    return {"generados": len(filas)}


def numerar_polizas(caja: dict, fecha: str) -> list[str]:
    """Pólizas del mes de una caja con prefijo (TCB).

    Las exportadas quedan fijas; las demás se numeran en orden cronológico a
    partir de la más alta exportada. Una compra que llega tarde a un mes ya
    exportado toma el siguiente número. Devuelve los movimientos que cambiaron.
    """
    if not caja.get("prefijo_poliza"):
        return []
    d = date.fromisoformat(str(fecha)[:10])
    inicio = d.replace(day=1)
    fin = (inicio + timedelta(days=32)).replace(day=1)
    movs = (supabase.table("movimientos_pago").select("id,fecha_pago,created_at,poliza,poliza_fija")
            .eq("caja_id", caja["id"]).gte("fecha_pago", inicio.isoformat()).lt("fecha_pago", fin.isoformat())
            .execute()).data or []
    cambiados = motor.asignar_polizas(movs)
    for mov_id, _, despues in cambiados:
        supabase.table("movimientos_pago").update({"poliza": despues}).eq("id", mov_id).execute()
    prefijo = caja["prefijo_poliza"]
    for mov_id, antes, despues in cambiados:
        resultado = generar(mov_id)
        if resultado.get("motivo") == "editado a mano" and antes:
            # Los editados a mano conservan su texto; solo cambia el número de póliza.
            viejo, nuevo = motor.texto_poliza(prefijo, antes), motor.texto_poliza(prefijo, despues)
            for r in (supabase.table("registros_erp").select("id,ref").eq("movimiento_id", mov_id).execute()).data or []:
                supabase.table("registros_erp").update({"ref": r["ref"].replace(f"{viejo} - ", f"{nuevo} - "),
                                                        "poliza": despues}).eq("id", r["id"]).execute()
    return [c[0] for c in cambiados]


def tras_registrar(movimiento_id: str) -> dict:
    """Después de guardar un pago: póliza y renglones. Nunca tumba el registro del pago."""
    try:
        mov = _mov(movimiento_id)
        if not mov or not mov.get("caja"):
            return {"generados": 0}
        cambiados = numerar_polizas(mov["caja"], mov["fecha_pago"])
        if movimiento_id in cambiados:
            return {"generados": len((supabase.table("registros_erp").select("id")
                                      .eq("movimiento_id", movimiento_id).execute()).data or [])}
        return generar(movimiento_id)
    except Exception as e:
        print(f"[PROPAGO] no se generaron los renglones del movimiento {movimiento_id}: {e}")
        return {"generados": 0, "error": str(e)}


def antes_de_borrar(movimiento_id: Optional[str], pago_ids: list[str], usuario_id: str) -> Optional[dict]:
    """Al borrar un pago: los renglones ya exportados dejan un ajuste para quitarlos del ERP."""
    if not disponible() or not (movimiento_id or pago_ids):
        return None
    try:
        q = supabase.table("registros_erp").select("*").eq("estado", "exportado")
        exportados = (q.eq("movimiento_id", movimiento_id) if movimiento_id else q.in_("pago_id", pago_ids)).execute().data or []
        for r in exportados:
            supabase.table("ajustes_erp").insert({
                "registro_id": None, "lote_id": r.get("lote_id"), "accion": "eliminar",
                "antes": _resumen(r), "motivo": "Se borró el pago en ProCompra", "creado_por": usuario_id,
            }).execute()
        mov = _mov(movimiento_id) if movimiento_id else None
        return {"caja": mov.get("caja"), "fecha": mov["fecha_pago"]} if mov and mov.get("caja") else None
    except Exception as e:
        print(f"[PROPAGO] no se prepararon los ajustes del pago borrado: {e}")
        return None


def despues_de_borrar(info: Optional[dict]) -> None:
    if info:
        try:
            numerar_polizas(info["caja"], info["fecha"])
        except Exception as e:
            print(f"[PROPAGO] no se renumeraron las pólizas: {e}")


def _resumen(r: dict) -> dict:
    return {k: r.get(k) for k in ("cargo", "cuenta_nombre_erp", "ref", "id_evento", "propietario", "lote_id", "movimiento_id")}


# ── Estado ────────────────────────────────────────────

@router.get("/estado")
def estado(usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    """Si ProPago está activo, las cajas del usuario y lo pendiente."""
    if not disponible():
        return {"disponible": False, "cajas": []}
    por_exportar = (supabase.table("registros_erp").select("id", count="exact").neq("estado", "exportado")
                    .limit(1).execute()).count or 0
    ajustes = (supabase.table("ajustes_erp").select("id", count="exact").eq("estado", "pendiente")
               .limit(1).execute()).count or 0
    cuentas = (supabase.table("cuentas_contables").select("id", count="exact").limit(1).execute()).count or 0
    return {"disponible": True, "cajas": cajas_de(usuario), "es_admin": _es_admin(usuario),
            "por_exportar": por_exportar, "ajustes_pendientes": ajustes, "catalogo_vacio": cuentas == 0}


# ── Cajas ─────────────────────────────────────────────

class CajaDatos(BaseModel):
    nombre: Optional[str] = Field(None, min_length=1, max_length=60)
    tipo: Optional[str] = None
    metodo: Optional[str] = None
    plaza: Optional[str] = None
    prefijo_poliza: Optional[str] = Field(None, max_length=10)
    numero_cuenta: Optional[str] = Field(None, max_length=20)
    responsable_id: Optional[str] = None
    activa: Optional[bool] = None
    notas: Optional[str] = Field(None, max_length=300)

    @field_validator("tipo")
    @classmethod
    def _tipo(cls, v):
        if v is not None and v not in TIPOS_CAJA:
            raise ValueError("tipo de caja no válido")
        return v

    @field_validator("metodo")
    @classmethod
    def _metodo(cls, v):
        if v is not None and v not in METODOS:
            raise ValueError("forma de pago no válida")
        return v

    @field_validator("plaza")
    @classmethod
    def _plaza(cls, v):
        if v is not None and v not in ("MID", "CUN"):
            raise ValueError("la plaza es MID o CUN")
        return v

    @field_validator("nombre", "prefijo_poliza", "numero_cuenta", "notas")
    @classmethod
    def _limpiar(cls, v):
        return _limpio(v)


class PermisoCaja(BaseModel):
    usuario_id: str
    puede_registrar: bool = True
    puede_exportar: bool = True


@router.get("/cajas")
def listar_cajas(todas: bool = False, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    _exigir()
    cajas = cajas_de(usuario, solo_activas=not todas)
    if _es_admin(usuario):
        permisos = (supabase.table("caja_usuarios").select("*").execute()).data or []
        for c in cajas:
            c["usuarios"] = [p for p in permisos if p["caja_id"] == c["id"]]
    return cajas


def _caja_fila(body: CajaDatos) -> dict:
    datos = body.model_dump(exclude_unset=True)
    if "prefijo_poliza" in datos and datos["prefijo_poliza"]:
        datos["prefijo_poliza"] = datos["prefijo_poliza"].upper()
    return datos


@router.post("/cajas", status_code=201)
def crear_caja(body: CajaDatos, usuario=Depends(requiere_rol("admin"))):
    _exigir()
    if not body.nombre:
        raise HTTPException(status_code=400, detail="La caja necesita nombre")
    try:
        caja = supabase.table("cajas").insert(_caja_fila(body)).execute().data[0]
    except Exception as e:
        if "duplicate" in str(e) or "23505" in str(e):
            raise HTTPException(status_code=409, detail="Ya existe una caja con ese nombre")
        raise
    _bitacora("caja", caja["id"], "creada", usuario["id"], despues=caja)
    return caja


@router.patch("/cajas/{caja_id}")
def editar_caja(caja_id: str, body: CajaDatos, usuario=Depends(requiere_rol("admin"))):
    _exigir()
    antes = una_fila(supabase.table("cajas").select("*").eq("id", _uuid(caja_id)))
    if not antes:
        raise HTTPException(status_code=404, detail="Caja no encontrada")
    datos = _caja_fila(body)
    if not datos:
        raise HTTPException(status_code=400, detail="No hay cambios")
    datos["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        caja = supabase.table("cajas").update(datos).eq("id", caja_id).execute().data[0]
    except Exception as e:
        if "duplicate" in str(e) or "23505" in str(e):
            raise HTTPException(status_code=409, detail="Ya existe una caja con ese nombre")
        raise
    _bitacora("caja", caja_id, "editada", usuario["id"], antes=antes, despues=caja)
    return caja


@router.put("/cajas/{caja_id}/usuarios")
def permisos_caja(caja_id: str, body: list[PermisoCaja], usuario=Depends(requiere_rol("admin"))):
    """Quién usa la caja: registrar pagos y descargar sus renglones."""
    _exigir()
    caja_id = _uuid(caja_id)
    supabase.table("caja_usuarios").delete().eq("caja_id", caja_id).execute()
    filas = [{"caja_id": caja_id, **p.model_dump()} for p in body]
    if filas:
        supabase.table("caja_usuarios").insert(filas).execute()
    _bitacora("caja", caja_id, "permisos", usuario["id"], despues=filas)
    return filas


# ── Catálogo: cuentas, plantillas, reglas, parámetros ─

class CuentaDatos(BaseModel):
    clave: Optional[str] = Field(None, min_length=1, max_length=20)
    nombre_erp: Optional[str] = Field(None, min_length=1, max_length=200)
    grupo: Optional[str] = Field(None, max_length=10)
    tipo: Optional[str] = None
    cdv: Optional[bool] = None
    activa: Optional[bool] = None
    verificada_erp: Optional[bool] = None

    @field_validator("tipo")
    @classmethod
    def _tipo(cls, v):
        if v is not None and v not in TIPOS_CUENTA:
            raise ValueError("tipo de cuenta no válido")
        return v

    @field_validator("clave", "nombre_erp", "grupo")
    @classmethod
    def _limpiar(cls, v):
        v = _limpio(v)
        return v.upper() if v else v


class PlantillaDatos(BaseModel):
    codigo: Optional[str] = Field(None, min_length=1, max_length=30)
    plantilla: Optional[str] = Field(None, min_length=1, max_length=300)
    descripcion: Optional[str] = Field(None, max_length=200)
    activa: Optional[bool] = None

    @field_validator("plantilla")
    @classmethod
    def _plantilla(cls, v):
        v = _limpio(v)
        if v and (malos := motor.placeholders_invalidos(v)):
            raise ValueError(f"no existe {{{malos[0]}}}; usa {', '.join('{' + p + '}' for p in motor.PLACEHOLDERS)}")
        return v

    @field_validator("codigo")
    @classmethod
    def _codigo(cls, v):
        v = _limpio(v)
        return v.upper().replace(" ", "_") if v else v


class ReglaDatos(BaseModel):
    nombre: Optional[str] = Field(None, min_length=1, max_length=80)
    prioridad: Optional[int] = Field(None, ge=0, le=999)
    proveedor_id: Optional[str] = None
    caja_id: Optional[str] = None
    plaza: Optional[str] = None
    insumo_id: Optional[str] = None
    palabras: Optional[list[str]] = Field(None, max_length=60)
    palabras_excluir: Optional[list[str]] = Field(None, max_length=60)
    con_cfdi: Optional[bool] = None
    cuenta_id: Optional[str] = None
    categoria_texto: Optional[str] = Field(None, max_length=30)
    fuerza_nd: Optional[bool] = None
    desglosa_iva: Optional[bool] = None
    plantilla_id: Optional[str] = None
    activa: Optional[bool] = None

    @field_validator("palabras", "palabras_excluir")
    @classmethod
    def _palabras(cls, v):
        if v is None:
            return v
        return list(dict.fromkeys(p for p in (normalizar(x) for x in v) if p))

    @field_validator("plaza")
    @classmethod
    def _plaza(cls, v):
        if v not in (None, "", "MID", "CUN"):
            raise ValueError("la plaza es MID o CUN")
        return v or None

    @field_validator("categoria_texto")
    @classmethod
    def _categoria(cls, v):
        return motor.mayusculas(v) or None

    @field_validator("proveedor_id", "caja_id", "insumo_id", "cuenta_id", "plantilla_id")
    @classmethod
    def _ids(cls, v):
        return v or None


class Parametro(BaseModel):
    valor: Optional[str] = Field(None, max_length=3000)


def _guardar(tabla: str, entidad: str, datos: dict, usuario: dict, fila_id: Optional[str] = None) -> dict:
    """Alta o cambio de una fila del catálogo, con bitácora y mensajes legibles."""
    if not datos:
        raise HTTPException(status_code=400, detail="No hay cambios")
    antes = None
    try:
        if fila_id:
            antes = una_fila(supabase.table(tabla).select("*").eq("id", _uuid(fila_id)))
            if not antes:
                raise HTTPException(status_code=404, detail="No encontrado")
            datos["updated_at"] = datetime.now(timezone.utc).isoformat()
            fila = supabase.table(tabla).update(datos).eq("id", fila_id).execute().data[0]
        else:
            fila = supabase.table(tabla).insert(datos).execute().data[0]
    except HTTPException:
        raise
    except Exception as e:
        texto = str(e)
        if "duplicate" in texto or "23505" in texto:
            raise HTTPException(status_code=409, detail="Ya existe otro con esa clave")
        if "23503" in texto:
            raise HTTPException(status_code=400, detail="Hace referencia a algo que no existe")
        raise
    _bitacora(entidad, fila["id"], "editada" if fila_id else "creada", usuario["id"], antes=antes, despues=fila)
    return fila


@router.get("/cuentas")
def listar_cuentas(usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    _exigir()
    return (supabase.table("cuentas_contables").select("*").order("clave").execute()).data or []


@router.post("/cuentas", status_code=201)
def crear_cuenta(body: CuentaDatos, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    _exigir()
    if not body.clave or not body.nombre_erp:
        raise HTTPException(status_code=400, detail="La cuenta necesita clave y nombre del ERP")
    return _guardar("cuentas_contables", "cuenta", body.model_dump(exclude_unset=True), usuario)


@router.patch("/cuentas/{cuenta_id}")
def editar_cuenta(cuenta_id: str, body: CuentaDatos, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    """Una cuenta usada no se borra: se desactiva. Los renglones exportados
    conservan el nombre con que salieron."""
    _exigir()
    return _guardar("cuentas_contables", "cuenta", body.model_dump(exclude_unset=True), usuario, cuenta_id)


@router.get("/plantillas")
def listar_plantillas(usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    _exigir()
    return (supabase.table("plantillas_registro").select("*").order("codigo").execute()).data or []


@router.post("/plantillas", status_code=201)
def crear_plantilla(body: PlantillaDatos, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    _exigir()
    if not body.codigo or not body.plantilla:
        raise HTTPException(status_code=400, detail="La plantilla necesita código y texto")
    return _guardar("plantillas_registro", "plantilla", body.model_dump(exclude_unset=True), usuario)


@router.patch("/plantillas/{plantilla_id}")
def editar_plantilla(plantilla_id: str, body: PlantillaDatos, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    _exigir()
    return _guardar("plantillas_registro", "plantilla", body.model_dump(exclude_unset=True), usuario, plantilla_id)


@router.get("/reglas")
def listar_reglas(usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    _exigir()
    return (supabase.table("reglas_cuenta").select("*,proveedor:proveedor_id(nombre),caja:caja_id(nombre)")
            .order("prioridad").order("nombre").execute()).data or []


@router.post("/reglas", status_code=201)
def crear_regla(body: ReglaDatos, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    _exigir()
    if not body.nombre:
        raise HTTPException(status_code=400, detail="La regla necesita nombre")
    return _guardar("reglas_cuenta", "regla", body.model_dump(exclude_unset=True), usuario)


@router.patch("/reglas/{regla_id}")
def editar_regla(regla_id: str, body: ReglaDatos, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    _exigir()
    return _guardar("reglas_cuenta", "regla", body.model_dump(exclude_unset=True), usuario, regla_id)


@router.delete("/reglas/{regla_id}", status_code=204)
def borrar_regla(regla_id: str, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    _exigir()
    antes = una_fila(supabase.table("reglas_cuenta").select("*").eq("id", _uuid(regla_id)))
    if not antes:
        raise HTTPException(status_code=404, detail="Regla no encontrada")
    supabase.table("reglas_cuenta").delete().eq("id", regla_id).execute()
    _bitacora("regla", regla_id, "borrada", usuario["id"], antes=antes)


class Prueba(BaseModel):
    concepto: str = Field(..., min_length=1, max_length=300)
    proveedor_id: Optional[str] = None
    caja_id: Optional[str] = None
    con_cfdi: bool = False
    importe: float = Field(100, gt=0)


@router.post("/reglas/probar")
def probar_regla(body: Prueba, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    """Qué regla aplica a un concepto, con qué cuenta y cómo queda el texto."""
    _exigir()
    cat = _catalogo()
    caja = una_fila(supabase.table("cajas").select("*").eq("id", body.caja_id)) if body.caja_id else None
    proveedor = una_fila(supabase.table("proveedores").select("id,nombre").eq("id", body.proveedor_id)) if body.proveedor_id else None
    pago = {"fecha": hoy().isoformat(), "plaza": (caja or {}).get("plaza") or "MID",
            "poliza": motor.texto_poliza((caja or {}).get("prefijo_poliza"), 1), "caja_id": body.caja_id,
            "comision": 0, "diferencia": 0, "aplicaciones": [], "lineas": []}
    if proveedor:
        importe = motor.redondear(body.importe)
        factura = {"total": importe, "iva": motor.desglosar(importe)[1], "metodo_pago": "PUE"} if body.con_cfdi else None
        pago["aplicaciones"] = [{"monto": importe, "proveedor": proveedor, "orden_folio": None, "factura": factura,
                                 "partidas": [{"concepto": body.concepto, "cantidad": 1, "importe": importe}]}]
    else:
        pago["lineas"] = [{"concepto": body.concepto, "importe": body.importe, "con_iva": body.con_cfdi}]
    partidas = pago["aplicaciones"][0]["partidas"] if proveedor else pago["lineas"]
    _insumos(partidas, cat["reglas"])
    ctx = {"texto": normalizar(body.concepto), "proveedor_id": body.proveedor_id, "caja_id": body.caja_id,
           "plaza": pago["plaza"], "con_cfdi": body.con_cfdi, "insumo_id": partidas[0].get("insumo_id")}
    resuelto = motor.resolver(cat["reglas"], ctx)
    aplican = [{"id": r["id"], "nombre": r["nombre"], "prioridad": r["prioridad"]}
               for r in cat["reglas"] if motor.regla_aplica(r, ctx)]
    return {"resuelto": resuelto, "aplican": aplican, "renglones": _motor(cat).generar(pago)}


@router.get("/parametros")
def listar_parametros(usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    _exigir()
    guardados = {p["clave"]: p for p in (supabase.table("parametros_contables").select("*").execute()).data or []}
    return [{"clave": c, "valor": guardados.get(c, {}).get("valor", v), "descripcion": d}
            for c, v, d in inicial.PARAMETROS]


@router.put("/parametros/{clave}")
def guardar_parametro(clave: str, body: Parametro, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    _exigir()
    conocido = next((p for p in inicial.PARAMETROS if p[0] == clave), None)
    if not conocido:
        raise HTTPException(status_code=404, detail="Parámetro desconocido")
    valor = (body.valor or "").strip()
    if clave.startswith("cuenta_") and valor and not una_fila(supabase.table("cuentas_contables").select("id").eq("id", _uuid(valor))):
        raise HTTPException(status_code=400, detail="Esa cuenta no existe")
    if clave.startswith("aviso_") and valor:
        try:
            float(valor)
        except ValueError:
            raise HTTPException(status_code=400, detail="Debe ser un número")
    fila = supabase.table("parametros_contables").upsert({"clave": clave, "valor": valor, "descripcion": conocido[2],
                                                          "updated_at": datetime.now(timezone.utc).isoformat()}).execute().data[0]
    _bitacora("parametro", clave, "editado", usuario["id"], despues=fila)
    return fila


def _reglas_iniciales() -> list[dict]:
    """Las reglas por concepto más las de los proveedores y cajas que existan."""
    reglas = list(inicial.REGLAS)
    proveedores = (supabase.table("proveedores").select("id,nombre").eq("activo", True).execute()).data or []
    for textos, datos, etiqueta in inicial.REGLAS_PROVEEDOR:
        for p in proveedores:
            nombre = normalizar(p["nombre"])
            if any(normalizar(t) in nombre for t in textos):
                reglas.append({"nombre": f"{etiqueta} · {p['nombre']}", "palabras": [], "proveedor_id": p["id"], **datos})
    for coincide, etiqueta, datos in inicial.REGLAS_CAJA:
        for c in (supabase.table("cajas").select("*").eq("activa", True).execute()).data or []:
            if coincide(c):
                reglas.append({"nombre": f"{etiqueta} · {c['nombre']}", "caja_id": c["id"], "palabras": [], **datos})
    return reglas


def _fila_regla(r: dict, cuentas: dict, plantillas: dict) -> dict:
    """Todas las filas con las mismas columnas: PostgREST rechaza un lote con llaves distintas."""
    return {
        "nombre": r["nombre"], "prioridad": r.get("prioridad", 50),
        "palabras": r.get("palabras") or [], "palabras_excluir": r.get("palabras_excluir") or [],
        "proveedor_id": r.get("proveedor_id"), "caja_id": r.get("caja_id"), "plaza": r.get("plaza"),
        "con_cfdi": r.get("con_cfdi"),
        "cuenta_id": cuentas[r["cuenta"]]["id"] if r.get("cuenta") in cuentas else None,
        "categoria_texto": r.get("categoria"),
        "plantilla_id": plantillas[r["plantilla"]]["id"] if r.get("plantilla") in plantillas else None,
        "fuerza_nd": bool(r.get("fuerza_nd")), "desglosa_iva": bool(r.get("desglosa_iva")),
    }


@router.post("/catalogo/inicial")
def cargar_inicial(usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    """Carga lo que falte del catálogo de la especificación. No toca lo ya editado."""
    _exigir()
    cuentas = {c["clave"]: c for c in (supabase.table("cuentas_contables").select("id,clave").execute()).data or []}
    nuevas = [{"clave": c, "nombre_erp": n, "grupo": g, "tipo": t, "cdv": cdv, "verificada_erp": v}
              for c, n, g, t, cdv, v in inicial.CUENTAS if c not in cuentas]
    if nuevas:
        for f in supabase.table("cuentas_contables").insert(nuevas).execute().data or []:
            cuentas[f["clave"]] = f

    plantillas = {p["codigo"]: p for p in (supabase.table("plantillas_registro").select("id,codigo").execute()).data or []}
    nuevas_p = [{"codigo": c, "plantilla": p, "descripcion": d} for c, p, d in inicial.PLANTILLAS if c not in plantillas]
    if nuevas_p:
        for f in supabase.table("plantillas_registro").insert(nuevas_p).execute().data or []:
            plantillas[f["codigo"]] = f

    # Solo las reglas que falten por nombre: las ya editadas no se tocan.
    existentes_r = {r["nombre"] for r in (supabase.table("reglas_cuenta").select("nombre").execute()).data or []}
    filas = [_fila_regla(r, cuentas, plantillas) for r in _reglas_iniciales() if r["nombre"] not in existentes_r]
    reglas = len(supabase.table("reglas_cuenta").insert(filas).execute().data or []) if filas else 0

    existentes = {p["clave"] for p in (supabase.table("parametros_contables").select("clave").execute()).data or []}
    parametros = [{"clave": c, "valor": v, "descripcion": d} for c, v, d in inicial.PARAMETROS if c not in existentes]
    if parametros:
        supabase.table("parametros_contables").insert(parametros).execute()
    _bitacora("catalogo", None, "carga inicial", usuario["id"],
              despues={"cuentas": len(nuevas), "plantillas": len(nuevas_p), "reglas": reglas})
    return {"cuentas": len(nuevas), "plantillas": len(nuevas_p), "reglas": reglas, "parametros": len(parametros)}


# ── Pago sin orden ────────────────────────────────────

class LineaSinOrden(BaseModel):
    concepto: str = Field(..., min_length=1, max_length=300)
    cantidad: Optional[float] = Field(None, ge=0)
    contrato: Optional[str] = Field(None, max_length=120)
    importe: float = Field(..., gt=0)
    con_iva: bool = False
    cuenta_id: Optional[str] = None

    @field_validator("concepto", "contrato")
    @classmethod
    def _limpiar(cls, v):
        return _limpio(v)


class PagoSinOrden(BaseModel):
    caja_id: str
    fecha_pago: str
    justificacion: str = Field(..., min_length=3, max_length=500)
    proveedor_texto: Optional[str] = Field(None, max_length=120)
    referencia: Optional[str] = Field(None, max_length=120)
    notas: Optional[str] = Field(None, max_length=500)
    comision: float = Field(0, ge=0)
    diferencia: float = Field(0, ge=0)
    lineas: list[LineaSinOrden] = Field(..., min_length=1, max_length=50)

    @field_validator("fecha_pago")
    @classmethod
    def _fecha(cls, v):
        try:
            return date.fromisoformat(str(v)[:10]).isoformat()
        except ValueError:
            raise ValueError("debe ser una fecha AAAA-MM-DD")

    @field_validator("justificacion", "proveedor_texto", "referencia", "notas")
    @classmethod
    def _limpiar(cls, v):
        return _limpio(v)


@router.post("/sin-orden", status_code=201)
def pago_sin_orden(body: PagoSinOrden, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    """Gasto hecho por urgencia sin orden previa: casetas, DiDi, hidratación."""
    _exigir()
    caja = exigir_caja(usuario, body.caja_id)
    if not body.justificacion:
        raise HTTPException(status_code=400, detail="El pago sin orden necesita justificación")
    monto = motor.redondear(sum(l.importe for l in body.lineas) + body.comision + body.diferencia)
    mov = supabase.table("movimientos_pago").insert({
        "fecha_pago": body.fecha_pago, "metodo": caja["metodo"], "referencia": body.referencia, "notas": body.notas,
        "registrado_por": usuario["id"], "caja_id": caja["id"], "monto": monto, "tipo": "sin_orden",
        "plaza": caja["plaza"], "justificacion": body.justificacion, "proveedor_texto": body.proveedor_texto,
        "comision": motor.redondear(body.comision), "diferencia": motor.redondear(body.diferencia),
    }).execute().data[0]
    try:
        supabase.table("pago_lineas").insert([
            {"movimiento_id": mov["id"], "renglon": n, **l.model_dump(), "importe": motor.redondear(l.importe)}
            for n, l in enumerate(body.lineas, start=1)
        ]).execute()
    except Exception:
        supabase.table("movimientos_pago").delete().eq("id", mov["id"]).execute()
        raise HTTPException(status_code=400, detail="No se pudo registrar el pago")
    resultado = tras_registrar(mov["id"])
    return {"movimiento_id": mov["id"], "monto": monto, **resultado}


# ── Registros ─────────────────────────────────────────

class CambioRegistro(BaseModel):
    cargo: Optional[float] = Field(None, gt=0)
    cuenta_id: Optional[str] = None
    ref: Optional[str] = Field(None, min_length=1, max_length=500)
    contrato: Optional[str] = Field(None, max_length=60)
    motivo: Optional[str] = Field(None, max_length=300)

    @field_validator("ref")
    @classmethod
    def _ref(cls, v):
        return motor.mayusculas(v) if v else v


@router.get("/registros")
def listar_registros(
    estado: str = Query("por_exportar", pattern="^(por_exportar|pendiente|listo|exportado)$"),
    caja_id: Optional[str] = None,
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    limite: int = Query(3000, ge=1, le=10000),
    usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD)),
):
    """Renglones agrupados por pago. Se exporta un pago completo, no renglones sueltos."""
    _exigir()
    mias = {c["id"]: c for c in cajas_de(usuario, solo_activas=False)}
    ids = [caja_id] if caja_id else list(mias)
    if caja_id and caja_id not in mias:
        raise HTTPException(status_code=403, detail="No tienes asignada esa caja")
    if not ids:
        return {"pagos": [], "total": 0}
    q = supabase.table("registros_erp").select(SELECT_REGISTROS).in_("caja_id", ids)
    q = q.neq("estado", "exportado") if estado == "por_exportar" else q.eq("estado", estado)
    filas = q.order("created_at", desc=True).order("renglon").limit(limite).execute().data or []

    pagos: dict[str, dict] = {}
    for r in filas:
        mov = r.pop("movimiento") or {}
        if desde and mov.get("fecha_pago", "") < desde or hasta and mov.get("fecha_pago", "") > hasta:
            continue
        p = pagos.setdefault(r["movimiento_id"], {**mov, "caja": mias.get(r["caja_id"], {}).get("nombre"),
                                                   "caja_id": r["caja_id"], "registros": [], "ordenes": []})
        orden = r.pop("orden") or {}
        if orden.get("folio") and orden["folio"] not in [o["folio"] for o in p["ordenes"]]:
            p["ordenes"].append({"id": orden["id"], "folio": orden["folio"],
                                 "proveedor": (orden.get("proveedor") or {}).get("nombre")})
        p["registros"].append(r)
    lista = []
    for p in pagos.values():
        p["registros"].sort(key=lambda r: r["renglon"])
        p["cuadra"] = motor.cuadra(p["registros"], p.get("monto") or 0)
        p["listo"] = p["cuadra"] and all(r["estado"] == "listo" for r in p["registros"])
        p["exportado"] = all(r["estado"] == "exportado" for r in p["registros"])
        lista.append(p)
    lista.sort(key=lambda p: (p.get("fecha_pago") or "", p.get("created_at") or ""), reverse=True)
    return {"pagos": lista, "total": len(lista)}


@router.patch("/registros/{registro_id}")
def editar_registro(registro_id: str, body: CambioRegistro, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    """Corrige un renglón. Si ya se exportó, queda un ajuste para hacerlo en el ERP."""
    _exigir()
    r = una_fila(supabase.table("registros_erp").select("*").eq("id", _uuid(registro_id)))
    if not r:
        raise HTTPException(status_code=404, detail="Renglón no encontrado")
    exigir_caja(usuario, r["caja_id"])
    cambios = body.model_dump(exclude_unset=True, exclude={"motivo"})
    if "cuenta_id" in cambios:
        cuenta = una_fila(supabase.table("cuentas_contables").select("*").eq("id", _uuid(cambios["cuenta_id"]))) if cambios["cuenta_id"] else None
        if not cuenta or not cuenta["activa"]:
            raise HTTPException(status_code=400, detail="Elige una cuenta activa")
        cambios["cuenta_nombre_erp"] = cuenta["nombre_erp"]
    if "contrato" in cambios:
        lista = motor.contratos(cambios["contrato"])
        cambios["contrato"] = "/".join(lista) or None
        cambios["id_evento"] = motor.id_evento(lista[0]) if lista else 0
    if "cargo" in cambios:
        cambios["cargo"] = motor.redondear(cambios["cargo"])
    if not cambios:
        raise HTTPException(status_code=400, detail="No hay cambios")
    cambios["updated_at"] = datetime.now(timezone.utc).isoformat()

    if r["estado"] == "exportado":
        despues = {**_resumen(r), **{k: v for k, v in cambios.items() if k != "updated_at"}}
        supabase.table("ajustes_erp").insert({
            "registro_id": r["id"], "lote_id": r.get("lote_id"), "accion": "modificar", "antes": _resumen(r),
            "despues": despues, "motivo": body.motivo, "creado_por": usuario["id"],
        }).execute()
    else:
        cuenta_ok = cambios.get("cuenta_id", r["cuenta_id"])
        cambios.update(editado_manual=True, estado="listo" if cuenta_ok else "pendiente",
                       avisos=[a for a in r["avisos"] or [] if not a.startswith("Falta elegir")] if cuenta_ok else r["avisos"])
    fila = supabase.table("registros_erp").update(cambios).eq("id", r["id"]).execute().data[0]
    _bitacora("registro", r["id"], "editado", usuario["id"], antes=_resumen(r), despues=_resumen(fila))
    return fila


@router.post("/movimientos/{movimiento_id}/regenerar")
def regenerar(movimiento_id: str, forzar: bool = False, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    """Vuelve a armar los renglones de un pago con las reglas actuales."""
    _exigir()
    mov = _mov(_uuid(movimiento_id))
    if not mov:
        raise HTTPException(status_code=404, detail="Pago no encontrado")
    if mov.get("caja_id"):
        exigir_caja(usuario, mov["caja_id"])
    resultado = generar(movimiento_id, forzar=forzar)
    if resultado.get("motivo") == "ya exportado":
        raise HTTPException(status_code=409, detail="El pago ya se exportó: corrige sus renglones uno por uno")
    if resultado.get("motivo") == "editado a mano":
        raise HTTPException(status_code=409, detail="Tiene renglones editados a mano; confirma para reemplazarlos")
    return resultado


class Reaplicar(BaseModel):
    confirmar: bool = False


def _firma(r: dict) -> tuple:
    return (motor.redondear(r["cargo"]), r.get("cuenta_id"), r.get("cuenta_nombre_erp"), r["ref"], int(r.get("id_evento") or 0))


@router.post("/reaplicar")
def reaplicar(body: Reaplicar, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    """Compara los renglones por exportar con lo que darían las reglas actuales.

    Sin confirmar solo muestra los cambios. Los editados a mano no se tocan.
    """
    _exigir()
    mias = {c["id"] for c in cajas_de(usuario, solo_activas=False)}
    actuales = [r for r in _todas(lambda: supabase.table("registros_erp").select("*").neq("estado", "exportado")
                                  .order("movimiento_id").order("renglon")) if r["caja_id"] in mias]
    por_mov: dict[str, list] = {}
    for r in actuales:
        por_mov.setdefault(r["movimiento_id"], []).append(r)
    cat, cambios = _catalogo(), []
    for mov_id, filas in list(por_mov.items())[:300]:
        if any(r["editado_manual"] for r in filas):
            continue
        mov = _mov(mov_id)
        if not mov or not mov.get("caja"):
            continue
        nuevos = calcular(mov, mov["caja"], cat)
        if [_firma(r) for r in filas] != [_firma(r) for r in nuevos]:
            cambios.append({"movimiento_id": mov_id, "fecha_pago": mov["fecha_pago"],
                            "antes": [_resumen(r) for r in filas], "despues": [_resumen(r) for r in nuevos]})
    if body.confirmar:
        for c in cambios:
            generar(c["movimiento_id"], cat=cat)
        _bitacora("registros", None, "reglas reaplicadas", usuario["id"], despues={"pagos": len(cambios)})
    return {"cambios": cambios, "aplicado": body.confirmar}


# ── Exportación ───────────────────────────────────────

class Exportar(BaseModel):
    movimientos: list[str] = Field(..., min_length=1, max_length=500)


def _archivo(lote: dict, renglones: list[dict]) -> Response:
    contenido = motor.excel(renglones, date.fromisoformat(str(lote["fecha"])[:10]))
    nombre = f"registros_erp_{lote['fecha']}_{str(lote['id'])[:8]}.xlsx"
    return Response(content=contenido,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"', "X-Lote-Id": str(lote["id"]),
                             "Access-Control-Expose-Headers": "Content-Disposition, X-Lote-Id"})


def _orden_archivo(renglones: list[dict], movs: dict) -> list[dict]:
    def llave(r):
        m = movs.get(r["movimiento_id"]) or {}
        return (r.get("propietario") or "", m.get("poliza") or 0, m.get("fecha_pago") or "", m.get("created_at") or "",
                r["movimiento_id"], r["renglon"])
    return sorted(renglones, key=llave)


@router.post("/exportar")
def exportar(body: Exportar, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    """Descarga el Excel de los pagos elegidos y los marca como exportados.

    La fecha del archivo es la de hoy; las pólizas de esos pagos quedan fijas.
    """
    _exigir()
    ids = list(dict.fromkeys(_uuid(m) for m in body.movimientos))
    movs = {}
    for lote in _en_lotes(ids):
        for m in (supabase.table("movimientos_pago").select("*,caja:caja_id(*)").in_("id", lote).execute()).data or []:
            movs[m["id"]] = m
    if len(movs) != len(ids):
        raise HTTPException(status_code=404, detail="Algún pago ya no existe")
    for m in movs.values():
        if not m.get("caja"):
            raise HTTPException(status_code=400, detail="Hay un pago sin caja")
        exigir_caja(usuario, m["caja_id"], "puede_exportar")

    renglones = []
    for lote in _en_lotes(ids):
        renglones += (supabase.table("registros_erp").select("*").in_("movimiento_id", lote)
                      .neq("estado", "exportado").execute()).data or []
    errores = []
    for mov_id, m in movs.items():
        suyos = [r for r in renglones if r["movimiento_id"] == mov_id]
        if not suyos:
            errores.append(f"El pago del {m['fecha_pago']} no tiene renglones por exportar")
        elif any(r["estado"] != "listo" for r in suyos):
            errores.append(f"El pago del {m['fecha_pago']} de ${float(m['monto'] or 0):,.2f} tiene renglones sin cuenta")
        elif not motor.cuadra(suyos, m.get("monto") or 0):
            errores.append(f"Los renglones del pago del {m['fecha_pago']} no suman ${float(m['monto'] or 0):,.2f}")
    if errores:
        raise HTTPException(status_code=409, detail=". ".join(errores[:5]))

    fecha = hoy()
    total = motor.redondear(sum(float(r["cargo"]) for r in renglones))
    lote = supabase.table("lotes_exportacion").insert({
        "fecha": fecha.isoformat(), "num_renglones": len(renglones), "total": total, "generado_por": usuario["id"],
    }).execute().data[0]
    for mov_id, m in movs.items():
        supabase.table("registros_erp").update({
            "estado": "exportado", "lote_id": lote["id"], "propietario": propietario(m["caja"]),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("movimiento_id", mov_id).neq("estado", "exportado").execute()
        supabase.table("movimientos_pago").update({"poliza_fija": True}).eq("id", mov_id).execute()
        for r in renglones:
            if r["movimiento_id"] == mov_id:
                r["propietario"] = propietario(m["caja"])
    _bitacora("lote", lote["id"], "exportado", usuario["id"], despues={"renglones": len(renglones), "total": total})
    return _archivo(lote, _orden_archivo(renglones, movs))


@router.get("/lotes")
def listar_lotes(limite: int = Query(60, ge=1, le=300), usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    _exigir()
    return (supabase.table("lotes_exportacion").select("*,generado:generado_por(nombre)")
            .order("created_at", desc=True).limit(limite).execute()).data or []


def _lote(lote_id: str) -> dict:
    lote = una_fila(supabase.table("lotes_exportacion").select("*").eq("id", _uuid(lote_id)))
    if not lote:
        raise HTTPException(status_code=404, detail="Lote no encontrado")
    return lote


@router.get("/lotes/{lote_id}/excel")
def descargar_lote(lote_id: str, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    """El mismo archivo otra vez, con la fecha original del lote."""
    _exigir()
    lote = _lote(lote_id)
    renglones = (supabase.table("registros_erp").select("*").eq("lote_id", lote["id"]).execute()).data or []
    ids = list({r["movimiento_id"] for r in renglones})
    movs = {}
    for grupo in _en_lotes(ids):
        movs.update({m["id"]: m for m in (supabase.table("movimientos_pago").select("id,poliza,fecha_pago,created_at")
                                          .in_("id", grupo).execute()).data or []})
    return _archivo(lote, _orden_archivo(renglones, movs))


@router.post("/lotes/{lote_id}/registrado")
def lote_registrado(lote_id: str, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    """Ya se subió al ERP: el lote no se puede anular."""
    _exigir()
    lote = _lote(lote_id)
    if lote["estado"] != "generado":
        raise HTTPException(status_code=409, detail="El lote ya estaba registrado o anulado")
    fila = supabase.table("lotes_exportacion").update({
        "estado": "registrado", "registrado_por": usuario["id"], "registrado_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", lote["id"]).execute().data[0]
    _bitacora("lote", lote["id"], "registrado en el ERP", usuario["id"])
    return fila


@router.post("/lotes/{lote_id}/anular")
def anular_lote(lote_id: str, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    """El ERP rechazó el archivo: sus renglones vuelven a «por exportar». Las pólizas siguen fijas."""
    _exigir()
    lote = _lote(lote_id)
    if lote["estado"] != "generado":
        raise HTTPException(status_code=409, detail="Solo se anula un lote que aún no se registra en el ERP")
    supabase.table("registros_erp").update({"estado": "listo", "lote_id": None,
                                            "updated_at": datetime.now(timezone.utc).isoformat()}).eq("lote_id", lote["id"]).execute()
    fila = supabase.table("lotes_exportacion").update({"estado": "anulado"}).eq("id", lote["id"]).execute().data[0]
    _bitacora("lote", lote["id"], "anulado", usuario["id"])
    return fila


# ── Ajustes en el ERP ─────────────────────────────────

@router.get("/ajustes")
def listar_ajustes(estado: str = Query("pendiente", pattern="^(pendiente|aplicado)$"),
                   usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    _exigir()
    return (supabase.table("ajustes_erp").select("*,creado:creado_por(nombre),lote:lote_id(fecha)")
            .eq("estado", estado).order("created_at", desc=True).limit(300).execute()).data or []


@router.post("/ajustes/{ajuste_id}/aplicado")
def ajuste_aplicado(ajuste_id: str, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    _exigir()
    ajuste = una_fila(supabase.table("ajustes_erp").select("*").eq("id", _uuid(ajuste_id)))
    if not ajuste:
        raise HTTPException(status_code=404, detail="Ajuste no encontrado")
    fila = supabase.table("ajustes_erp").update({
        "estado": "aplicado", "aplicado_por": usuario["id"], "aplicado_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", ajuste["id"]).execute().data[0]
    _bitacora("ajuste", ajuste["id"], "aplicado en el ERP", usuario["id"])
    return fila
