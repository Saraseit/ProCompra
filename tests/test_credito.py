"""Estado de cuenta de proveedores a crédito: saldos y vencimientos."""

from datetime import date

import pytest

from app.api import proveedores
from app.utils import credito

HOY = date(2026, 10, 27)


def _orden(id_="o1", total=1000.0, fecha="2026-10-01", proveedor="p1", dias=30, **extra):
    return {
        "id": id_, "folio": int(id_[-1]), "fecha": fecha, "estado": extra.get("estado", "comprobacion"),
        "total": total, "monto_comprobado": extra.get("monto_comprobado"),
        "proveedor_id": proveedor,
        "proveedor": {"id": proveedor, "nombre": f"Proveedor {proveedor}", "dias_credito": dias},
        "pagos": extra.get("pagos", []),
        "recolecciones": extra.get("recolecciones", []),
        "facturas": extra.get("facturas", []),
    }


def test_el_plazo_corre_desde_la_factura():
    o = _orden(facturas=[{"fecha_emision": "2026-10-10T12:00:00", "serie": "A", "folio": "9"}],
               recolecciones=[{"completado": True, "fecha_real": "2026-10-05"}])
    m = credito.movimiento(o, 30, HOY)
    assert (m["fecha_base"], m["origen_base"]) == ("2026-10-10", "factura")
    assert m["vencimiento"] == "2026-11-09"
    assert m["factura"] == "A9"


def test_sin_factura_el_plazo_corre_desde_la_recepcion():
    o = _orden(recolecciones=[{"completado": False, "fecha_real": None},
                              {"completado": True, "fecha_real": "2026-10-05"}])
    assert credito.fecha_base(o) == (date(2026, 10, 5), "recepcion")


def test_sin_factura_ni_recepcion_cuenta_la_fecha_de_la_orden():
    assert credito.fecha_base(_orden()) == (date(2026, 10, 1), "orden")


def test_lo_comprobado_reemplaza_al_total():
    m = credito.movimiento(_orden(monto_comprobado=950.0), 30, HOY)
    assert m["importe"] == 950.0


@pytest.mark.parametrize("dias, situacion", [
    (20, "vencida"),      # venció el 21 de octubre
    (30, "por_vencer"),   # vence el 31, dentro de 7 días
    (60, "vigente"),
    (None, "sin_plazo"),
])
def test_situacion_segun_el_plazo(dias, situacion):
    assert credito.movimiento(_orden(), dias, HOY)["situacion"] == situacion


def test_orden_pagada_completa_queda_liquidada_aunque_este_vencida():
    o = _orden(pagos=[{"fecha_pago": "2026-10-20", "monto": 600}, {"fecha_pago": "2026-10-15", "monto": 400}])
    m = credito.movimiento(o, 5, HOY)
    assert (m["saldo"], m["situacion"]) == (0.0, "liquidada")
    assert [p["fecha_pago"] for p in m["pagos"]] == ["2026-10-15", "2026-10-20"]


def test_pago_parcial_deja_saldo():
    m = credito.movimiento(_orden(pagos=[{"fecha_pago": "2026-10-20", "monto": 250.5}]), 30, HOY)
    assert (m["pagado"], m["saldo"]) == (250.5, 749.5)


def test_estado_de_cuenta_agrupa_y_ordena_por_vencido():
    ordenes = [
        _orden("o1", 1000, proveedor="p1", dias=60),
        _orden("o2", 500, proveedor="p2", dias=10),
        _orden("o3", 300, proveedor="p2", dias=10, fecha="2026-10-25"),
        _orden("o4", 200, proveedor="p2", dias=10, pagos=[{"fecha_pago": "2026-10-02", "monto": 200}]),
    ]
    r = credito.estados_de_cuenta(ordenes, HOY)
    p2, p1 = r["proveedores"]
    assert p2["id"] == "p2" and p1["id"] == "p1"
    assert (p2["saldo"], p2["vencido"], p2["por_vencer"]) == (800.0, 500.0, 0.0)
    assert (p2["ordenes_abiertas"], p2["ordenes_vencidas"]) == (2, 1)
    assert p2["proximo_vencimiento"] == "2026-10-11"
    assert (p2["cargos"], p2["abonos"]) == (1000.0, 200.0)
    assert r["totales"] == {"saldo": 1800.0, "vencido": 500.0, "por_vencer": 0.0,
                            "ordenes_abiertas": 3, "ordenes_vencidas": 1}


def test_los_estados_de_deuda_son_estados_de_orden():
    from app.api import ordenes
    assert set(credito.ESTADOS_DEUDA) <= set(ordenes.ESTADOS_ORDEN)


@pytest.mark.parametrize("dias", [-1, 366])
def test_dias_de_credito_fuera_de_rango_se_rechazan(dias):
    with pytest.raises(Exception):
        proveedores.ProveedorCreate(nombre="X", dias_credito=dias)


def test_alta_sin_dias_de_credito_no_manda_la_columna():
    assert "dias_credito" not in proveedores.ProveedorCreate(nombre="X").model_dump(exclude_none=True)


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
