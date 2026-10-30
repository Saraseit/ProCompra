"""Edición de órdenes, surtido parcial y órdenes complementarias."""

import pytest

from app.api import ajustes as api_ajustes, ordenes, requerimientos
from app.roles import ROLES_OPERACION
from app.utils import ajustes

TELA = {"id": "p1", "concepto": "Tela tergal", "cantidad": 40, "unidad": "MTR", "precio_unitario": 85,
        "contrato": "EV-114", "req_id": "r1"}
HILO = {"id": "p2", "concepto": "Hilo", "cantidad": 2, "unidad": "CAJA", "precio_unitario": 50,
        "contrato": None, "req_id": None}


def test_totales_con_iva():
    assert ajustes.totales([TELA, HILO]) == {"subtotal": 3500.0, "iva": 560.0, "total": 4060.0}


def test_describe_cambios_agregadas_quitadas_y_editadas():
    despues = [
        {**TELA, "cantidad": 20, "precio_unitario": 90},
        {"id": None, "concepto": "Botones", "cantidad": 100, "unidad": "PZA", "precio_unitario": 1.5, "contrato": None},
    ]
    lineas = ajustes.describir_cambios([TELA, HILO], despues)
    assert "Tela tergal: 40 MTR → 20 MTR, precio $85.00 → $90.00" in lineas
    assert any(l.startswith("Agregada: Botones") for l in lineas)
    assert "Quitada: Hilo (2 CAJA)" in lineas


def test_sin_cambios_no_describe_nada():
    assert ajustes.describir_cambios([TELA], [dict(TELA)]) == []


def test_surtido_mitad_y_complemento():
    plan = ajustes.plan_surtido([TELA, HILO], [
        {"partida_id": "p1", "cantidad_surtida": 20, "faltante": "complemento"},
    ], precios={"p1": 95})
    (aj,) = plan["ajustes"]
    assert (aj["cantidad"], aj["faltante"], aj["cantidad_original"]) == (20, 20, 40)
    (comp,) = plan["complemento"]
    assert comp == {"partida_origen_id": "p1", "req_id": "r1", "concepto": "Tela tergal", "cantidad": 20,
                    "unidad": "MTR", "precio_unitario": 95, "contrato": "EV-114"}


def test_surtido_conserva_la_cantidad_original_de_un_ajuste_previo():
    ya_ajustada = {**TELA, "cantidad": 20, "cantidad_original": 40}
    plan = ajustes.plan_surtido([ya_ajustada], [{"partida_id": "p1", "cantidad_surtida": 10, "faltante": "cancelar"}])
    assert plan["ajustes"][0]["cantidad_original"] == 40
    assert plan["canceladas"] == ["Tela tergal"] and plan["complemento"] == []


def test_partida_sin_surtir_no_liga_partida_de_origen():
    plan = ajustes.plan_surtido([TELA, HILO], [{"partida_id": "p2", "cantidad_surtida": 0}])
    assert plan["complemento"][0]["partida_origen_id"] is None


@pytest.mark.parametrize("entradas, mensaje", [
    ([{"partida_id": "p1", "cantidad_surtida": 41}], "entre 0 y 40"),
    ([{"partida_id": "zz", "cantidad_surtida": 1}], "no pertenece"),
    ([{"partida_id": "p1", "cantidad_surtida": 40}], "Ninguna partida tiene faltante"),
    ([{"partida_id": "p1", "cantidad_surtida": 0}, {"partida_id": "p2", "cantidad_surtida": 0}], "no surte nada"),
])
def test_surtido_invalido(entradas, mensaje):
    with pytest.raises(ajustes.ErrorSurtido, match=mensaje):
        ajustes.plan_surtido([TELA, HILO], entradas)


def test_reparto_50_50():
    raiz = {**TELA, "cantidad": 20, "cantidad_original": 40,
            "orden": {"id": "o1", "folio": 100, "estado": "pagada", "proveedor": {"nombre": "Parisina"}}}
    hijo = {"id": "p9", "cantidad": 20, "partida_origen_id": "p1",
            "orden": {"id": "o2", "folio": 101, "estado": "autorizacion", "proveedor": {"nombre": "Tapicero"}}}
    r = ajustes.reparto(raiz, [hijo], actual_id="p9")
    assert [(p["folio"], p["porcentaje"], p["actual"]) for p in r["partes"]] == [(100, 50, False), (101, 50, True)]
    assert r["sin_surtir"] == 0


def test_reparto_con_faltante_cancelado():
    raiz = {**TELA, "cantidad": 30, "cantidad_original": 40, "orden": {"folio": 100}}
    r = ajustes.reparto(raiz, [], actual_id="p1")
    assert (r["sin_surtir"], r["porcentaje_sin_surtir"]) == (10, 25)


def test_editar_fuera_de_borrador_pide_motivo_y_valida_partidas():
    body = api_ajustes.EdicionOrden(partidas=[ordenes.PartidaInput(
        concepto=" Tela ", cantidad=1, unidad="MTR", precio_unitario=10).model_dump()], motivo="  ")
    assert body.motivo is None
    assert body.partidas[0].concepto == "Tela"


def test_surtido_con_complemento_exige_proveedor():
    with pytest.raises(Exception, match="proveedor"):
        api_ajustes.SurtidoParcial(motivo="Solo tenían la mitad",
                                   partidas=[{"partida_id": "p1", "cantidad_surtida": 20}])


def test_surtido_exige_motivo():
    with pytest.raises(Exception):
        api_ajustes.SurtidoParcial(motivo=" ", partidas=[{"partida_id": "p1", "cantidad_surtida": 20,
                                                          "faltante": "cancelar"}])


def test_canceladas_no_se_editan_ni_cerradas_se_surten():
    assert "rechazada" not in ajustes.ESTADOS_EDITABLES
    assert "cerrada" in ajustes.ESTADOS_EDITABLES and "cerrada" not in ajustes.ESTADOS_SURTIDO
    assert set(ajustes.ESTADOS_EDITABLES) <= set(ordenes.ESTADOS_ORDEN)


def test_almacen_crea_requerimientos_y_edita_ordenes():
    assert "almacen" in ROLES_OPERACION
    crear = next(r for r in requerimientos.router.routes if r.path == "/requerimientos" and "POST" in r.methods)
    cierres = [c.cell_contents for d in crear.dependant.dependencies for c in (d.call.__closure__ or ())]
    assert ROLES_OPERACION in cierres
