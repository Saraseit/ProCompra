"""
app/utils/pagos.py
------------------
Qué órdenes falta pagar y cuáles urgen esta semana.

Funciones puras (sin acceso a la base) para poder probarlas sin Supabase.
"""

from datetime import date, timedelta

from app.utils import credito

# Diferencias de redondeo menores a un centavo no son saldo.
TOLERANCIA = credito.TOLERANCIA


def fin_de_semana(hoy: date) -> date:
    """Domingo de la semana en curso (la semana empieza en lunes)."""
    return hoy + timedelta(days=6 - hoy.weekday())


def _proveedor(orden: dict) -> dict:
    prov = orden.get("proveedor") or {}
    return {k: prov.get(k) for k in ("id", "nombre", "rfc", "cuenta_bancaria")}


def pendiente(orden: dict, hoy: date) -> dict:
    """Renglón por pagar de una orden, a crédito o de contado.

    La de contado (autorizada, esperando su pago) urge siempre: sin el pago no
    se compra. La de crédito urge si tiene un pago programado vencido o que
    cae antes del domingo; sin ninguna fecha queda aparte, para capturarla.
    `a_pagar` es lo que toca ahora: lo de esta semana, o el siguiente pago.
    """
    domingo = fin_de_semana(hoy).isoformat()
    a_credito = orden.get("tipo_pago") == "credito"
    if a_credito:
        m = credito.movimiento(orden, hoy)
        abiertas = [c for c in m["cuotas"] if c["pendiente"] > TOLERANCIA]
        semana = [c for c in abiertas if c["fecha"] and c["fecha"] <= domingo]
        if semana:
            grupo, a_pagar = "semana", sum(c["pendiente"] for c in semana)
        elif abiertas:
            grupo, a_pagar = "despues", abiertas[0]["pendiente"]
        else:
            grupo, a_pagar = "sin_fecha", m["saldo"]
    else:
        pagos = orden.get("pagos") or []
        importe = round(float(orden.get("total") or 0), 2)
        pagado = round(sum(float(p.get("monto") or 0) for p in pagos), 2)
        facturas = orden.get("facturas") or []
        m = {
            "id": orden["id"], "folio": orden.get("folio"), "fecha": orden.get("fecha"),
            "estado": orden.get("estado"),
            "factura": ", ".join(f"{f.get('serie') or ''}{f.get('folio') or 's/f'}" for f in facturas) or None,
            "importe": importe, "pagado": pagado, "saldo": max(round(importe - pagado, 2), 0.0),
            "vencimiento": None, "dias": None, "situacion": "contado",
            "vencido": 0.0, "recargo": 0.0, "sin_fecha": 0.0, "cuotas": [],
        }
        grupo, a_pagar = "semana", m["saldo"]

    return {
        **{k: m.get(k) for k in ("id", "folio", "fecha", "estado", "factura", "importe", "recargo", "pagado",
                                 "saldo", "vencido", "sin_fecha", "vencimiento", "dias", "situacion", "cuotas")},
        "a_pagar": round(min(a_pagar, m["saldo"]), 2),
        "tipo": "credito" if a_credito else "contado",
        "tipo_pago": orden.get("tipo_pago"),
        "proveedor": _proveedor(orden),
        "grupo": grupo,
    }


def por_pagar(ordenes: list[dict], hoy: date) -> dict:
    """Separa lo de esta semana, lo que puede esperar y lo que no tiene fecha."""
    renglones = [r for r in (pendiente(o, hoy) for o in ordenes) if r["saldo"] > TOLERANCIA]
    # Primero lo vencido, luego por fecha; el contado (sin fecha) al final de su grupo.
    orden_de = lambda r: (r["vencimiento"] or "9999-12-31", r["folio"] or 0)

    def grupo(clave: str) -> dict:
        filas = sorted((r for r in renglones if r["grupo"] == clave), key=orden_de)
        return {
            "ordenes": filas,
            "total": round(sum(r["a_pagar"] for r in filas), 2),
            "saldo": round(sum(r["saldo"] for r in filas), 2),
            "vencido": round(sum(r["vencido"] for r in filas), 2),
        }

    return {
        "hoy": hoy.isoformat(),
        "fin_semana": fin_de_semana(hoy).isoformat(),
        "semana": grupo("semana"),
        "despues": grupo("despues"),
        "sin_fecha": grupo("sin_fecha"),
    }


def agrupar_pagos(filas: list[dict]) -> list[dict]:
    """Junta los renglones de pago que salieron de un mismo movimiento.

    Un pago registrado antes del módulo de pagos (sin movimiento) es un
    movimiento de una sola orden.
    """
    movimientos: dict[str, dict] = {}
    for p in filas:
        clave = p.get("movimiento_id") or f"p:{p['id']}"
        if clave not in movimientos:
            movimientos[clave] = {
                "id": p.get("movimiento_id") or p["id"],
                "agrupado": bool(p.get("movimiento_id")),
                **{k: p.get(k) for k in ("fecha_pago", "metodo", "referencia", "entregado_a", "notas", "created_at")},
                "registrado": p.get("registrado"),
                "monto": 0.0,
                "ordenes": [],
            }
        mov = movimientos[clave]
        orden = p.get("orden") or {}
        mov["monto"] = round(mov["monto"] + float(p.get("monto") or 0), 2)
        mov["ordenes"].append({
            "pago_id": p["id"], "orden_id": p.get("orden_id"), "monto": float(p.get("monto") or 0),
            "folio": orden.get("folio"), "estado": orden.get("estado"),
            "proveedor": (orden.get("proveedor") or {}).get("nombre"),
        })
    return sorted(movimientos.values(),
                  key=lambda m: (str(m["fecha_pago"] or ""), str(m["created_at"] or "")), reverse=True)
