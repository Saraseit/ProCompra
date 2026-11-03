"""Estado de cuenta de proveedores a crédito: saldos y vencimientos."""

from datetime import date

import pytest

from app.api import proveedores
from app.utils import credito

HOY = date(2026, 10, 27)


def _orden(id_="o1", total=1000.0, fecha="2026-10-01", proveedor="p1", programados=None, **extra):
    return {
        "id": id_, "folio": int(id_[-1]), "fecha": fecha, "estado": extra.get("estado", "comprobacion"),
        "total": total, "monto_comprobado": extra.get("monto_comprobado"),
        "proveedor_id": proveedor,
        "proveedor": {"id": proveedor, "nombre": f"Proveedor {proveedor}", "da_credito": True},
        "pagos": extra.get("pagos", []),
        "programados": programados or [],
        "facturas": extra.get("facturas", []),
    }


def _prog(*pares):
    return [{"id": f"c{i}", "fecha": f, "monto": m} for i, (f, m) in enumerate(pares)]


def test_sin_calendario_la_orden_queda_sin_fecha():
    m = credito.movimiento(_orden(facturas=[{"serie": "A", "folio": "9"}]), HOY)
    assert (m["situacion"], m["vencimiento"], m["sin_fecha"], m["saldo"]) == ("sin_fecha", None, 1000.0, 1000.0)
    assert m["factura"] == "A9"


def test_lo_comprobado_reemplaza_al_total():
    m = credito.movimiento(_orden(monto_comprobado=950.0), HOY)
    assert m["importe"] == 950.0


@pytest.mark.parametrize("fecha, situacion", [
    ("2026-10-21", "vencida"),
    ("2026-10-31", "por_vencer"),   # dentro de 7 días
    ("2026-12-01", "vigente"),
])
def test_situacion_segun_la_fecha_programada(fecha, situacion):
    m = credito.movimiento(_orden(programados=_prog((fecha, 1000))), HOY)
    assert (m["situacion"], m["vencimiento"]) == (situacion, fecha)


def test_lo_pagado_cubre_las_fechas_en_orden():
    # Dividida en 2 pagos con recargo: la deuda es lo programado (1,040), no la orden.
    o = _orden(programados=_prog(("2026-11-20", 520), ("2026-10-20", 520)),
               pagos=[{"fecha_pago": "2026-10-19", "monto": 520}])
    m = credito.movimiento(o, HOY)
    assert (m["importe"], m["recargo"], m["saldo"], m["vencimiento"], m["proximo_pago"]) == (1040.0, 40.0, 520.0, "2026-11-20", 520.0)
    assert [c["estado"] for c in m["cuotas"]] == ["pagada", "pendiente"]
    assert m["situacion"] == "vigente" and m["vencido"] == 0


def test_pago_parcial_de_una_fecha_vencida():
    o = _orden(programados=_prog(("2026-10-20", 600), ("2026-11-30", 400)),
               pagos=[{"fecha_pago": "2026-10-20", "monto": 250.5}])
    m = credito.movimiento(o, HOY)
    assert (m["pagado"], m["saldo"], m["vencido"], m["situacion"]) == (250.5, 749.5, 349.5, "vencida")


def test_programar_solo_una_parte_deja_el_resto_sin_fecha():
    m = credito.movimiento(_orden(programados=_prog(("2026-11-30", 400))), HOY)
    assert (m["importe"], m["sin_fecha"], m["proximo_pago"]) == (1000.0, 600.0, 400.0)


def test_orden_pagada_completa_queda_liquidada_aunque_este_vencida():
    o = _orden(programados=_prog(("2026-10-01", 1000)),
               pagos=[{"fecha_pago": "2026-10-20", "monto": 600}, {"fecha_pago": "2026-10-15", "monto": 400}])
    m = credito.movimiento(o, HOY)
    assert (m["saldo"], m["situacion"]) == (0.0, "liquidada")
    assert [p["fecha_pago"] for p in m["pagos"]] == ["2026-10-15", "2026-10-20"]


def test_estado_de_cuenta_agrupa_y_ordena_por_vencido():
    ordenes = [
        _orden("o1", 1000, proveedor="p1", programados=_prog(("2026-12-01", 1000))),
        _orden("o2", 500, proveedor="p2", programados=_prog(("2026-10-11", 500))),
        _orden("o3", 300, proveedor="p2"),
        _orden("o4", 200, proveedor="p2", programados=_prog(("2026-10-02", 200)),
               pagos=[{"fecha_pago": "2026-10-02", "monto": 200}]),
    ]
    r = credito.estados_de_cuenta(ordenes, HOY)
    p2, p1 = r["proveedores"]
    assert p2["id"] == "p2" and p1["id"] == "p1"
    assert (p2["saldo"], p2["vencido"], p2["por_vencer"]) == (800.0, 500.0, 0.0)
    assert (p2["ordenes_abiertas"], p2["ordenes_vencidas"], p2["ordenes_sin_fecha"]) == (2, 1, 1)
    assert p2["proximo_vencimiento"] == "2026-10-11"
    assert (p2["cargos"], p2["abonos"]) == (1000.0, 200.0)
    assert r["totales"] == {"saldo": 1800.0, "vencido": 500.0, "por_vencer": 0.0,
                            "ordenes_abiertas": 3, "ordenes_vencidas": 1, "ordenes_sin_fecha": 1}


def test_los_estados_de_deuda_son_estados_de_orden():
    from app.api import ordenes
    assert set(credito.ESTADOS_DEUDA) <= set(ordenes.ESTADOS_ORDEN)


@pytest.mark.parametrize("dias", [-1, 366])
def test_dias_de_credito_fuera_de_rango_se_rechazan(dias):
    with pytest.raises(Exception):
        proveedores.ProveedorCreate(nombre="X", dias_credito=dias)


def test_alta_sin_dias_de_credito_no_manda_la_columna():
    assert "dias_credito" not in proveedores.ProveedorCreate(nombre="X").model_dump(exclude_none=True)


def test_alta_sin_marcar_credito_no_manda_da_credito():
    # Antes de la fase 7 la columna no existe: no se manda si no se eligió.
    assert "da_credito" not in proveedores.ProveedorCreate(nombre="X").model_dump(exclude_none=True)
    assert proveedores.ProveedorCreate(nombre="X", da_credito=True).da_credito is True


# ── Fecha requerida (vista semanal) ───────────────────────

def test_fecha_requerida_se_normaliza():
    from app.api import requerimientos
    r = requerimientos.RequerimientoCreate(descripcion="X", cantidad=1, unidad="PZA",
                                           fecha_requerida="2026-11-03T00:00:00")
    assert r.fecha_requerida == "2026-11-03"


def test_fecha_requerida_invalida_se_rechaza():
    from app.api import requerimientos
    with pytest.raises(Exception):
        requerimientos.RequerimientoUpdate(fecha_requerida="03/11/2026")


def test_fecha_requerida_vacia_cuenta_como_borrarla():
    from app.api import requerimientos
    r = requerimientos.RequerimientoUpdate(fecha_requerida="")
    assert r.fecha_requerida is None and "fecha_requerida" in r.model_fields_set
