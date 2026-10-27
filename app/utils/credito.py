"""
app/utils/credito.py
--------------------
Estado de cuenta de las compras a crédito: saldo por orden y vencimientos.

Funciones puras (sin acceso a la base) para poder probarlas sin Supabase.
"""

from datetime import date, timedelta
from typing import Optional

# Órdenes a crédito que ya son deuda con el proveedor. En 'autorizada' el
# material puede no haber llegado, pero la compra ya está comprometida.
ESTADOS_DEUDA = ("autorizada", "recoleccion", "comprobacion", "cerrada")

# Una orden "por vencer" vence dentro de estos días.
DIAS_POR_VENCER = 7

# Diferencias de redondeo menores a un centavo no son saldo.
TOLERANCIA = 0.01


def _fecha(valor) -> Optional[date]:
    if not valor:
        return None
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        return None


def fecha_base(orden: dict) -> tuple[date, str]:
    """Desde cuándo corre el plazo de crédito.

    La factura manda; si aún no hay, cuenta desde que se recibió el material
    y, en último caso, desde la fecha de la orden.
    """
    facturas = [f for f in (_fecha(x.get("fecha_emision")) for x in orden.get("facturas") or []) if f]
    if facturas:
        return min(facturas), "factura"
    recibidas = [
        f for f in (_fecha(r.get("fecha_real")) for r in orden.get("recolecciones") or [] if r.get("completado"))
        if f
    ]
    if recibidas:
        return min(recibidas), "recepcion"
    return _fecha(orden.get("fecha")) or date.today(), "orden"


def importe(orden: dict) -> float:
    """Lo que se le debe al proveedor: lo comprobado si ya se comprobó, si no el total."""
    comprobado = orden.get("monto_comprobado")
    return float(comprobado if comprobado is not None else orden.get("total") or 0)


def movimiento(orden: dict, dias_credito: Optional[int], hoy: date) -> dict:
    """Renglón del estado de cuenta para una orden a crédito."""
    pagos = sorted(orden.get("pagos") or [], key=lambda p: str(p.get("fecha_pago") or ""))
    cargo = round(importe(orden), 2)
    pagado = round(sum(float(p.get("monto") or 0) for p in pagos), 2)
    saldo = round(cargo - pagado, 2)
    base, origen = fecha_base(orden)
    vence = base + timedelta(days=dias_credito) if dias_credito is not None else None
    dias = (vence - hoy).days if vence else None

    if saldo <= TOLERANCIA:
        situacion = "liquidada"
    elif vence is None:
        situacion = "sin_plazo"
    elif dias < 0:
        situacion = "vencida"
    elif dias <= DIAS_POR_VENCER:
        situacion = "por_vencer"
    else:
        situacion = "vigente"

    facturas = orden.get("facturas") or []
    return {
        "id":            orden["id"],
        "folio":         orden.get("folio"),
        "fecha":         orden.get("fecha"),
        "estado":        orden.get("estado"),
        "factura":       ", ".join(f"{f.get('serie') or ''}{f.get('folio') or 's/f'}" for f in facturas) or None,
        "importe":       cargo,
        "pagado":        pagado,
        "saldo":         max(saldo, 0.0),
        "fecha_base":    base.isoformat(),
        "origen_base":   origen,
        "vencimiento":   vence.isoformat() if vence else None,
        "dias":          dias,
        "situacion":     situacion,
        "pagos": [
            {k: p.get(k) for k in ("fecha_pago", "monto", "metodo", "referencia")}
            for p in pagos
        ],
    }


def estados_de_cuenta(ordenes: list[dict], hoy: date) -> dict:
    """Agrupa las órdenes a crédito por proveedor y suma saldos y vencimientos.

    Cada orden trae su proveedor embebido en `proveedor` (con `dias_credito`).
    """
    por_proveedor: dict[str, dict] = {}
    for orden in ordenes:
        prov = orden.get("proveedor") or {}
        clave = prov.get("id") or orden.get("proveedor_id")
        if clave not in por_proveedor:
            por_proveedor[clave] = {
                "id":           clave,
                "nombre":       prov.get("nombre") or "—",
                "rfc":          prov.get("rfc"),
                "telefono":     prov.get("telefono"),
                "dias_credito": prov.get("dias_credito"),
                "ordenes":      [],
            }
        cuenta = por_proveedor[clave]
        cuenta["ordenes"].append(movimiento(orden, cuenta["dias_credito"], hoy))

    cuentas = []
    for cuenta in por_proveedor.values():
        movs = sorted(cuenta["ordenes"], key=lambda m: (m["vencimiento"] or "9999", m["folio"] or 0))
        abiertas = [m for m in movs if m["situacion"] != "liquidada"]
        vencimientos = [m["vencimiento"] for m in abiertas if m["vencimiento"]]
        cuenta.update({
            "ordenes":             movs,
            "cargos":              round(sum(m["importe"] for m in movs), 2),
            "abonos":              round(sum(m["pagado"] for m in movs), 2),
            "saldo":               round(sum(m["saldo"] for m in abiertas), 2),
            "vencido":             round(sum(m["saldo"] for m in abiertas if m["situacion"] == "vencida"), 2),
            "por_vencer":          round(sum(m["saldo"] for m in abiertas if m["situacion"] == "por_vencer"), 2),
            "ordenes_abiertas":    len(abiertas),
            "ordenes_vencidas":    sum(1 for m in abiertas if m["situacion"] == "vencida"),
            "proximo_vencimiento": min(vencimientos) if vencimientos else None,
        })
        cuentas.append(cuenta)

    # Primero quien tiene deuda vencida, luego por saldo.
    cuentas.sort(key=lambda c: (-c["vencido"], -c["saldo"], c["nombre"]))
    suma = lambda campo: round(sum(c[campo] for c in cuentas), 2)
    return {
        "hoy":             hoy.isoformat(),
        "dias_por_vencer": DIAS_POR_VENCER,
        "totales": {
            "saldo":            suma("saldo"),
            "vencido":          suma("vencido"),
            "por_vencer":       suma("por_vencer"),
            "ordenes_abiertas": sum(c["ordenes_abiertas"] for c in cuentas),
            "ordenes_vencidas": sum(c["ordenes_vencidas"] for c in cuentas),
        },
        "proveedores": cuentas,
    }
