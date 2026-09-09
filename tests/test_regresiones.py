"""
Guardas contra los fallos silenciosos que ya llegaron a producción.

Ninguna prueba toca la base: todas verifican las reglas que se rompieron
sin dar error, que es justo lo que las hacía difíciles de detectar.
Correr con:  pytest -q
"""

import pytest
from pydantic import ValidationError

from app.api import ordenes, proveedores, requerimientos, reportes

UUID_VALIDO = "11111111-1111-1111-1111-111111111111"


def _partida(**cambios):
    base = {"concepto": "Tela tergal", "cantidad": 2, "unidad": "MTR", "precio_unitario": 35}
    base.update(cambios)
    return base


# ── Embeds de PostgREST ───────────────────────────────────
# Un salto de línea dentro de select() hace que PostgREST descarte las
# relaciones anidadas sin lanzar error: las órdenes llegaban sin proveedor.

@pytest.mark.parametrize("nombre, cadena", [
    ("ordenes.SELECT_LISTADO",      ordenes.SELECT_LISTADO),
    ("ordenes.SELECT_DETALLE",      ordenes.SELECT_DETALLE),
    ("requerimientos.SELECT_REQ",   requerimientos.SELECT_REQ),
    ("reportes.SELECT_GASTO",       reportes.SELECT_GASTO),
])
def test_select_sin_saltos_de_linea(nombre, cadena):
    assert "\n" not in cadena, f"{nombre} tiene salto de línea: perdería los embeds"
    assert "(" in cadena, f"{nombre} debería declarar al menos una relación anidada"


# ── Métodos de pago ───────────────────────────────────────
# El frontend ofrecía personas_morales y gastos_generales, que no existen
# en el enum metodo_pago de Postgres y hacían fallar el insert.

def test_metodos_de_pago_son_los_del_enum():
    assert set(ordenes.METODOS_PAGO) == {"transferencia", "tarjeta_credito", "efectivo"}


def test_metodos_de_pago_coinciden_con_los_de_reportes():
    assert set(reportes.ETIQUETAS_PAGO) == set(ordenes.METODOS_PAGO)


@pytest.mark.parametrize("metodo", ["personas_morales", "gastos_generales", "tarjeta_debito", ""])
def test_tipo_de_pago_inexistente_se_rechaza(metodo):
    with pytest.raises(ValidationError):
        ordenes.OrdenCreate(proveedor_id=UUID_VALIDO, tipo_pago=metodo, partidas=[_partida()])


# ── Órdenes en blanco ─────────────────────────────────────

def test_orden_sin_partidas_se_rechaza():
    with pytest.raises(ValidationError):
        ordenes.OrdenCreate(proveedor_id=UUID_VALIDO, partidas=[])


@pytest.mark.parametrize("cambios", [
    {"concepto": ""},
    {"concepto": "   "},
    {"unidad": "  "},
    {"cantidad": 0},
    {"cantidad": -1},
    {"precio_unitario": -0.01},
])
def test_partida_incompleta_se_rechaza(cambios):
    with pytest.raises(ValidationError):
        ordenes.OrdenCreate(proveedor_id=UUID_VALIDO, partidas=[_partida(**cambios)])


@pytest.mark.parametrize("proveedor", ["", "   ", "no-es-uuid", "12345"])
def test_proveedor_mal_formado_se_rechaza(proveedor):
    with pytest.raises(ValidationError):
        ordenes.OrdenCreate(proveedor_id=proveedor, partidas=[_partida()])


def test_orden_valida_recorta_los_textos():
    orden = ordenes.OrdenCreate(
        proveedor_id=UUID_VALIDO,
        partidas=[_partida(concepto="  Tela tergal  ", unidad=" MTR ")],
    )
    assert orden.partidas[0].concepto == "Tela tergal"
    assert orden.partidas[0].unidad == "MTR"


# ── Requerimientos ────────────────────────────────────────

@pytest.mark.parametrize("cambios", [
    {"descripcion": ""},
    {"descripcion": "    "},
    {"unidad": ""},
    {"cantidad": 0},
    {"precio_estimado": -1},
])
def test_requerimiento_incompleto_se_rechaza(cambios):
    datos = {"descripcion": "Cable", "cantidad": 1, "unidad": "PZA"}
    datos.update(cambios)
    with pytest.raises(ValidationError):
        requerimientos.RequerimientoCreate(**datos)


def test_requerimiento_valido_recorta_la_descripcion():
    req = requerimientos.RequerimientoCreate(descripcion="  Cable  ", cantidad=1, unidad="PZA")
    assert req.descripcion == "Cable"


# ── Pagos ─────────────────────────────────────────────────

@pytest.mark.parametrize("cambios", [
    {"monto": 0},
    {"monto": -5},
    {"referencia": "  "},
    {"metodo": "personas_morales"},
])
def test_pago_invalido_se_rechaza(cambios):
    datos = {"fecha_pago": "2026-09-09", "referencia": "REF1",
             "metodo": "transferencia", "monto": 100}
    datos.update(cambios)
    with pytest.raises(ValidationError):
        ordenes.PagoCreate(**datos)


# ── Nombres de proveedor ──────────────────────────────────

@pytest.mark.parametrize("a, b", [
    ("COMEX", "comex"),
    ("COMEX", "  Comex  "),
    ("La Piramide", "LA   PIRAMIDE"),
])
def test_nombres_equivalentes_se_normalizan_igual(a, b):
    assert proveedores._normalizar(a) == proveedores._normalizar(b)


def test_nombres_distintos_no_colisionan():
    assert proveedores._normalizar("COMEX") != proveedores._normalizar("COMEX SUR")


@pytest.mark.parametrize("nombre", ["", "   "])
def test_proveedor_sin_nombre_se_rechaza(nombre):
    with pytest.raises(ValidationError):
        proveedores.ProveedorCreate(nombre=nombre)


# ── Estados ───────────────────────────────────────────────

def test_estados_de_orden_cubren_el_enum():
    assert set(ordenes.ESTADOS_ORDEN) == {
        "borrador", "autorizacion", "autorizada",
        "pagada", "recoleccion", "cerrada", "rechazada",
    }


def test_los_estados_de_gasto_son_estados_validos():
    assert set(reportes.ESTADOS_GASTO) <= set(ordenes.ESTADOS_ORDEN)
