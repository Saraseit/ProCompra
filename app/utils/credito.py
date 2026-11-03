"""
app/utils/credito.py
--------------------
Estado de cuenta de las compras a crédito: saldo por orden y vencimientos.

Cada compra a crédito tiene su propio calendario de pagos (pagos_programados):
una fecha o varias (una compra dividida en 2 o 3 pagos), cada una con su
monto. El plazo no es del proveedor porque cambia en cada compra. La suma del
calendario puede ser mayor que la orden cuando diferir trae recargo; mientras
no se captura ninguna fecha, la orden queda "sin fecha de pago".

Funciones puras (sin acceso a la base) para poder probarlas sin Supabase.
"""

from datetime import date
from typing import Optional

# Órdenes a crédito que ya son deuda con el proveedor. En 'autorizada' el
# material puede no haber llegado, pero la compra ya está comprometida.
ESTADOS_DEUDA = ("autorizada", "recoleccion", "comprobacion", "cerrada")

# Una orden "por vencer" tiene un pago programado dentro de estos días.
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


def importe(orden: dict) -> float:
    """Lo que se le debe al proveedor: lo comprobado si ya se comprobó, si no el total."""
    comprobado = orden.get("monto_comprobado")
    return float(comprobado if comprobado is not None else orden.get("total") or 0)


def calendario(orden: dict, hoy: date) -> dict:
    """Reparte lo pagado sobre los pagos programados, del más antiguo al más nuevo.

    La deuda es el importe de la orden o lo programado, lo que sea mayor: el
    recargo por diferir sube la deuda, pero programar solo una parte no la baja.
    """
    pagado = round(sum(float(p.get("monto") or 0) for p in orden.get("pagos") or []), 2)
    programados = sorted(orden.get("programados") or [], key=lambda p: (str(p.get("fecha") or ""), str(p.get("id") or "")))
    programado = round(sum(float(p.get("monto") or 0) for p in programados), 2)
    deuda = round(max(importe(orden), programado), 2)

    restante = pagado
    cuotas = []
    for n, p in enumerate(programados, start=1):
        monto = round(float(p.get("monto") or 0), 2)
        cubierto = round(min(monto, max(restante, 0)), 2)
        restante = round(restante - cubierto, 2)
        pendiente = round(monto - cubierto, 2)
        fecha = _fecha(p.get("fecha"))
        cuotas.append({
            "id": p.get("id"), "numero": n, "fecha": fecha.isoformat() if fecha else None,
            "monto": monto, "notas": p.get("notas"), "pendiente": max(pendiente, 0.0),
            "estado": "pagada" if pendiente <= TOLERANCIA
                      else "vencida" if fecha and fecha < hoy else "pendiente",
        })
    return {
        "deuda": deuda,
        "pagado": pagado,
        "saldo": max(round(deuda - pagado, 2), 0.0),
        "programado": programado,
        "recargo": max(round(programado - importe(orden), 2), 0.0),
        # Lo que falta pagar y no tiene fecha todavía.
        "sin_fecha": max(round(deuda - programado - max(restante, 0), 2), 0.0),
        "cuotas": cuotas,
    }


def movimiento(orden: dict, hoy: date) -> dict:
    """Renglón del estado de cuenta para una orden a crédito."""
    pagos = sorted(orden.get("pagos") or [], key=lambda p: str(p.get("fecha_pago") or ""))
    cal = calendario(orden, hoy)
    abiertas = [c for c in cal["cuotas"] if c["pendiente"] > TOLERANCIA]
    proxima = abiertas[0] if abiertas else None
    vence = _fecha(proxima["fecha"]) if proxima else None
    dias = (vence - hoy).days if vence else None

    if cal["saldo"] <= TOLERANCIA:
        situacion = "liquidada"
    elif vence is None:
        situacion = "sin_fecha"
    elif dias < 0:
        situacion = "vencida"
    elif dias <= DIAS_POR_VENCER:
        situacion = "por_vencer"
    else:
        situacion = "vigente"

    facturas = orden.get("facturas") or []
    return {
        "id":           orden["id"],
        "folio":        orden.get("folio"),
        "fecha":        orden.get("fecha"),
        "estado":       orden.get("estado"),
        "factura":      ", ".join(f"{f.get('serie') or ''}{f.get('folio') or 's/f'}" for f in facturas) or None,
        "importe":      cal["deuda"],
        "recargo":      cal["recargo"],
        "pagado":       cal["pagado"],
        "saldo":        cal["saldo"],
        "vencido":      round(sum(c["pendiente"] for c in abiertas if c["estado"] == "vencida"), 2),
        "sin_fecha":    cal["sin_fecha"],
        "vencimiento":  vence.isoformat() if vence else None,
        "proximo_pago": proxima["pendiente"] if proxima else None,
        "dias":         dias,
        "situacion":    situacion,
        "cuotas":       cal["cuotas"],
        "pagos": [
            {k: p.get(k) for k in ("fecha_pago", "monto", "metodo", "referencia")}
            for p in pagos
        ],
    }


def estados_de_cuenta(ordenes: list[dict], hoy: date) -> dict:
    """Agrupa las órdenes a crédito por proveedor y suma saldos y vencimientos."""
    por_proveedor: dict[str, dict] = {}
    for orden in ordenes:
        prov = orden.get("proveedor") or {}
        clave = prov.get("id") or orden.get("proveedor_id")
        if clave not in por_proveedor:
            por_proveedor[clave] = {
                "id":       clave,
                "nombre":   prov.get("nombre") or "—",
                "rfc":      prov.get("rfc"),
                "telefono": prov.get("telefono"),
                "ordenes":  [],
            }
        por_proveedor[clave]["ordenes"].append(movimiento(orden, hoy))

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
            "vencido":             round(sum(m["vencido"] for m in abiertas), 2),
            "por_vencer":          round(sum(m["proximo_pago"] or 0 for m in abiertas if m["situacion"] == "por_vencer"), 2),
            "ordenes_abiertas":    len(abiertas),
            "ordenes_vencidas":    sum(1 for m in abiertas if m["situacion"] == "vencida"),
            "ordenes_sin_fecha":   sum(1 for m in abiertas if m["situacion"] == "sin_fecha"),
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
            "saldo":             suma("saldo"),
            "vencido":           suma("vencido"),
            "por_vencer":        suma("por_vencer"),
            "ordenes_abiertas":  sum(c["ordenes_abiertas"] for c in cuentas),
            "ordenes_vencidas":  sum(c["ordenes_vencidas"] for c in cuentas),
            "ordenes_sin_fecha": sum(c["ordenes_sin_fecha"] for c in cuentas),
        },
        "proveedores": cuentas,
    }
