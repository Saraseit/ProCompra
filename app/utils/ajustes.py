"""
app/utils/ajustes.py
--------------------
Cambios a una orden después de creada: edición de partidas y surtido parcial.

Funciones puras (sin acceso a la base) para poder probarlas sin Supabase.
"""

from typing import Optional

IVA = 0.16

# Estados en los que una orden se puede editar o surtir parcialmente.
# La cancelada ya no cuenta; la cerrada se edita (con aviso) pero no se surte.
ESTADOS_EDITABLES = ("borrador", "autorizacion", "autorizada", "pagada", "recoleccion", "comprobacion", "cerrada")
ESTADOS_SURTIDO = ("borrador", "autorizacion", "autorizada", "pagada", "recoleccion", "comprobacion")

# El proveedor solo cambia mientras no se le haya pagado nada.
ESTADOS_CAMBIO_PROVEEDOR = ("borrador", "autorizacion", "autorizada")

CAMPOS_PARTIDA = ("concepto", "cantidad", "unidad", "precio_unitario", "contrato")


def _num(valor) -> float:
    return float(valor or 0)


def _cant(valor) -> str:
    n = _num(valor)
    return f"{n:g}"


def totales(partidas: list[dict]) -> dict:
    subtotal = round(sum(_num(p.get("cantidad")) * _num(p.get("precio_unitario")) for p in partidas), 2)
    iva = round(subtotal * IVA, 2)
    return {"subtotal": subtotal, "iva": iva, "total": round(subtotal + iva, 2)}


def cambio_partida(antes: dict, despues: dict) -> dict:
    """Campos de la partida que realmente cambian (para no escribir de más)."""
    cambios = {}
    for campo in CAMPOS_PARTIDA:
        a, d = antes.get(campo), despues.get(campo)
        if campo in ("cantidad", "precio_unitario"):
            if round(_num(a), 4) != round(_num(d), 4):
                cambios[campo] = d
        elif (a or None) != (d or None):
            cambios[campo] = d
    return cambios


def describir_cambios(antes: list[dict], despues: list[dict]) -> list[str]:
    """Lista legible de lo que cambió entre dos versiones de las partidas.

    `despues` trae `id` en las partidas que ya existían; sin `id` son nuevas.
    """
    por_id = {p["id"]: p for p in antes}
    lineas = []
    vistos = set()
    for d in despues:
        a = por_id.get(d.get("id")) if d.get("id") else None
        if not a:
            lineas.append(f"Agregada: {d['concepto']} ({_cant(d['cantidad'])} {d['unidad']} × ${_num(d['precio_unitario']):,.2f})")
            continue
        vistos.add(a["id"])
        cambios = cambio_partida(a, d)
        if not cambios:
            continue
        partes = []
        if "cantidad" in cambios or "unidad" in cambios:
            partes.append(f"{_cant(a['cantidad'])} {a['unidad']} → {_cant(d['cantidad'])} {d['unidad']}")
        if "precio_unitario" in cambios:
            partes.append(f"precio ${_num(a['precio_unitario']):,.2f} → ${_num(d['precio_unitario']):,.2f}")
        if "concepto" in cambios:
            partes.append(f"concepto «{a['concepto']}» → «{d['concepto']}»")
        if "contrato" in cambios:
            partes.append(f"contrato {a.get('contrato') or '—'} → {d.get('contrato') or '—'}")
        lineas.append(f"{a['concepto']}: " + ", ".join(partes))
    for a in antes:
        if a["id"] not in vistos and a["id"] not in {d.get("id") for d in despues}:
            lineas.append(f"Quitada: {a['concepto']} ({_cant(a['cantidad'])} {a['unidad']})")
    return lineas


class ErrorSurtido(ValueError):
    pass


def plan_surtido(partidas: list[dict], entradas: list[dict], precios: Optional[dict] = None) -> dict:
    """Reparte cada partida entre lo que surte el proveedor y su faltante.

    entradas: [{partida_id, cantidad_surtida, faltante: 'complemento'|'cancelar'}]
    Devuelve qué actualizar en la orden original y qué partidas lleva la
    orden complementaria.
    """
    precios = precios or {}
    por_id = {p["id"]: p for p in partidas}
    ajustes, complemento, canceladas = [], [], []
    for e in entradas:
        p = por_id.get(e["partida_id"])
        if not p:
            raise ErrorSurtido("Una de las partidas no pertenece a esta orden")
        pedida = _num(p["cantidad"])
        surtida = _num(e["cantidad_surtida"])
        if surtida < 0 or surtida > pedida:
            raise ErrorSurtido(f"{p['concepto']}: lo surtido debe estar entre 0 y {_cant(pedida)}")
        faltante = round(pedida - surtida, 4)
        if faltante <= 0:
            continue
        ajustes.append({
            "id": p["id"], "concepto": p["concepto"], "unidad": p["unidad"],
            "cantidad": surtida, "pedida": pedida, "faltante": faltante,
            "cantidad_original": p.get("cantidad_original") or pedida,
        })
        destino = e.get("faltante", "complemento")
        if destino == "complemento":
            precio = precios.get(p["id"], p.get("precio_unitario"))
            if _num(precio) < 0:
                raise ErrorSurtido(f"{p['concepto']}: el precio no puede ser negativo")
            complemento.append({
                "partida_origen_id": p["id"] if surtida > 0 else None,
                "req_id": p.get("req_id"),
                "concepto": p["concepto"],
                "cantidad": faltante,
                "unidad": p["unidad"],
                "precio_unitario": _num(precio),
                "contrato": p.get("contrato"),
            })
        elif destino == "cancelar":
            canceladas.append(p["concepto"])
        else:
            raise ErrorSurtido("El faltante va a una orden complementaria o se cancela")

    if not ajustes:
        raise ErrorSurtido("Ninguna partida tiene faltante: indica cuánto sí surtió el proveedor")
    restantes = [p for p in partidas if p["id"] not in {a["id"] for a in ajustes}]
    if not restantes and all(a["cantidad"] == 0 for a in ajustes):
        raise ErrorSurtido("El proveedor no surte nada de la orden: cancélala o cambia el proveedor desde Editar")
    return {"ajustes": ajustes, "complemento": complemento, "canceladas": canceladas}


def porcentaje(parte: float, todo: float) -> int:
    return round(100 * parte / todo) if todo else 0


def reparto(raiz: dict, hijos: list[dict], actual_id: str) -> dict:
    """Cómo se repartió una partida entre la orden original y sus complementos.

    raiz: partida original (con `cantidad_original` si ya se ajustó) y su `orden`.
    hijos: partidas complementarias de órdenes no canceladas.
    """
    pedida = _num(raiz.get("cantidad_original") or raiz.get("cantidad"))
    partes = []
    for x in [raiz, *hijos]:
        o = x.get("orden") or {}
        cantidad = _num(x.get("cantidad"))
        partes.append({
            "orden_id":   o.get("id"),
            "folio":      o.get("folio"),
            "estado":     o.get("estado"),
            "proveedor":  (o.get("proveedor") or {}).get("nombre"),
            "cantidad":   cantidad,
            "porcentaje": porcentaje(cantidad, pedida),
            "actual":     x.get("id") == actual_id,
        })
    sin_surtir = max(round(pedida - sum(p["cantidad"] for p in partes), 4), 0)
    return {
        "pedida": pedida,
        "partes": partes,
        "sin_surtir": sin_surtir,
        "porcentaje_sin_surtir": porcentaje(sin_surtir, pedida),
    }
