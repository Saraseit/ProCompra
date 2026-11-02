"""Histórico de precios: lectura de renglones, agrupación, referencia y avisos."""

from datetime import date, timedelta

import pytest

from app.api import precios as api_precios
from app.utils import cfdi, precios

HOY = date(2026, 11, 2)

CFDI_RENGLONES = b"""<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
  Version="4.0" Folio="77" Fecha="2026-09-10T09:00:00" SubTotal="1450.00" Descuento="50.00" Total="1624.00"
  Moneda="MXN" TipoDeComprobante="I">
  <cfdi:Emisor Rfc="TEL010101ABC" Nombre="TELAS DEL SURESTE"/>
  <cfdi:Receptor Rfc="MES151124J83"/>
  <cfdi:Conceptos>
    <cfdi:Concepto ClaveProdServ="11162100" Cantidad="10" ClaveUnidad="MTR" Unidad="Metro"
      Descripcion="TELA TERGAL NGO 1.50 MTS" ValorUnitario="85.00" Importe="850.00" Descuento="50.00"/>
    <cfdi:Concepto ClaveProdServ="78101800" Cantidad="1" ClaveUnidad="E48" Unidad="Servicio"
      Descripcion="FLETE LOCAL" ValorUnitario="600.00" Importe="600.00"/>
    <cfdi:Concepto Cantidad="0" Descripcion="Renglon sin cantidad" ValorUnitario="5" Importe="0"/>
  </cfdi:Conceptos>
  <cfdi:Complemento><tfd:TimbreFiscalDigital UUID="11111111-2222-4333-8444-555555555555"/></cfdi:Complemento>
</cfdi:Comprobante>"""

CFDI_DOLARES = b"""<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" Version="4.0" Fecha="2026-09-10T09:00:00"
  SubTotal="10.00" Total="11.60" Moneda="USD" TipoCambio="18.50" TipoDeComprobante="I">
  <cfdi:Emisor Rfc="USA010101ABC"/><cfdi:Receptor Rfc="MES151124J83"/>
  <cfdi:Conceptos><cfdi:Concepto Cantidad="2" Descripcion="Cinta" ValorUnitario="5.00" Importe="10.00"/></cfdi:Conceptos>
</cfdi:Comprobante>"""

CFDI_32 = b"""<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/3" version="3.2" fecha="2016-03-01T10:00:00"
  subTotal="100.00" total="116.00" tipoDeComprobante="ingreso">
  <cfdi:Emisor rfc="ABC010101XYZ"/><cfdi:Receptor rfc="MES151124J83"/>
  <cfdi:Conceptos><cfdi:Concepto cantidad="4" unidad="PZA" descripcion="Bisagra" valorUnitario="25.00" importe="100.00"/></cfdi:Conceptos>
</cfdi:Comprobante>"""


def _punto(dias, precio, unidad="MTR", proveedor="Parisina", cantidad=1):
    return {"fecha": str(HOY - timedelta(days=dias)), "precio": precio, "unidad": unidad,
            "proveedor": proveedor, "cantidad": cantidad}


# ── Lectura del CFDI ──────────────────────────────────

def test_lee_renglones_con_precio_neto_de_descuento():
    r = cfdi.leer_cfdi(CFDI_RENGLONES)["conceptos"]
    assert len(r) == 2  # el renglón sin cantidad se omite
    tela, flete = r
    assert tela["descripcion"] == "TELA TERGAL NGO 1.50 MTS"
    assert tela["clave_sat"] == "11162100" and tela["clave_unidad"] == "MTR"
    assert tela["valor_unitario"] == 85.0
    assert tela["precio_unitario"] == 80.0  # (850 − 50) / 10
    assert flete["renglon"] == 2 and flete["precio_unitario"] == 600.0


def test_convierte_a_pesos_con_tipo_de_cambio():
    (cinta,) = cfdi.leer_cfdi(CFDI_DOLARES)["conceptos"]
    assert cinta["precio_unitario"] == pytest.approx(92.5)
    assert cinta["importe"] == pytest.approx(185.0)


def test_lee_renglones_de_cfdi_32_en_minusculas():
    (bisagra,) = cfdi.leer_cfdi(CFDI_32)["conceptos"]
    assert bisagra["descripcion"] == "Bisagra" and bisagra["precio_unitario"] == 25.0


def test_filas_de_renglones_llevan_texto_normalizado_y_unidad_estandar():
    datos = cfdi.leer_cfdi(CFDI_RENGLONES)
    factura = {"id": "f1", "fecha_emision": datos["fecha_emision"], "rfc_emisor": datos["rfc_emisor"],
               "nombre_emisor": datos["nombre_emisor"]}
    tela, flete = api_precios.filas_conceptos(factura, datos["conceptos"])
    assert tela["texto_norm"] == "TELA TERGAL NGO 1.50 MTS"
    assert tela["unidad"] == "MTR" and flete["unidad"] == "SERVICIO"
    assert tela["factura_id"] == "f1" and tela["rfc_emisor"] == "TEL010101ABC"


# ── Textos y unidades ─────────────────────────────────

def test_normalizar_quita_acentos_y_signos_y_respeta_decimales():
    assert precios.normalizar("  Pintura vinílica, blanca 19 L. ") == "PINTURA VINILICA BLANCA 19 L"
    assert precios.normalizar("Tela 1,50 mts") == "TELA 1.50 MTS"
    assert precios.normalizar("Paño #3 (rojo)") == "PANO 3 ROJO"


def test_abreviaturas_se_reconocen_como_parecidas():
    assert precios.similitud("TELA TERGAL NGO 1.50", "Tela tergal negra 1.50") >= precios.PARECIDO_RECONOCER
    assert precios.similitud("PINTURA VINIL BCO 19LT", "Pintura vinílica blanca 19LT") >= precios.PARECIDO_SUGERENCIA


def test_medidas_distintas_bajan_el_parecido():
    misma = precios.similitud("Tela tergal negra 1.50", "TELA TERGAL NEGRA 1.50")
    otra = precios.similitud("Tela tergal negra 1.50", "TELA TERGAL NEGRA 2.80")
    assert misma == 1.0 and otra < misma


def test_textos_sin_relacion_no_se_parecen():
    assert precios.similitud("FLETE LOCAL", "Tela tergal negra") < precios.PARECIDO_SUGERENCIA


def test_indice_solo_compara_textos_con_palabras_en_comun():
    indice = precios.IndiceTextos([{"texto": "TELA TERGAL NEGRA"}, {"texto": "FLETE LOCAL"}])
    assert [c["texto"] for c in indice.buscar("TERGAL NGO")] == ["TELA TERGAL NEGRA"]
    assert indice.buscar("CINTA DOBLE CARA") == []


@pytest.mark.parametrize("unidad,clave,esperada", [
    ("Metro", "MTR", "MTR"),
    ("Pieza", "H87", "PZA"),
    ("Metros", "H87", "MTR"),      # clave genérica: manda el texto
    ("Servicio", "E48", "SERVICIO"),
    (None, "KGM", "KILO"),
    ("GALON", None, "GALON"),
    ("PZA", None, "PZA"),
])
def test_unidad_estandar(unidad, clave, esperada):
    assert precios.unidad_estandar(unidad, clave) == esperada


# ── Referencia ────────────────────────────────────────

def test_referencia_es_la_mediana_de_6_meses():
    puntos = [_punto(10, 85), _punto(40, 84), _punto(70, 101), _punto(100, 82), _punto(400, 60)]
    s = precios.estadisticas(puntos, HOY)
    assert s["referencia"] == 84.5 and s["ventana_meses"] == 6
    assert s["compras_ventana"] == 4 and s["mas_barata"]["precio"] == 82
    assert s["ultima"]["precio"] == 85


def test_con_pocas_compras_se_amplia_a_12_meses():
    puntos = [_punto(20, 90), _punto(250, 80), _punto(300, 70)]
    s = precios.estadisticas(puntos, HOY)
    assert s["ventana_meses"] == 12 and s["referencia"] == 80


def test_sin_3_compras_no_hay_referencia():
    s = precios.estadisticas([_punto(5, 90), _punto(30, 80)], HOY)
    assert s["referencia"] is None
    assert precios.evaluar(120, "MTR", s)["nivel"] == "sin_historial"


def test_otras_unidades_no_se_mezclan():
    puntos = [_punto(5, 85), _punto(15, 86), _punto(25, 84), _punto(35, 2100, unidad="ROLLO")]
    s = precios.estadisticas(puntos, HOY)
    assert s["unidad"] == "MTR" and s["referencia"] == 85 and s["otras_unidades"] == 1


def test_por_proveedor_ordena_del_mas_barato():
    puntos = [_punto(5, 90, proveedor="Centro"), _punto(15, 80, proveedor="Parisina"),
              _punto(25, 82, proveedor="Parisina")]
    filas = precios.por_proveedor(puntos, HOY)
    assert [f["proveedor"] for f in filas] == ["Parisina", "Centro"]
    assert filas[0]["compras"] == 2 and filas[0]["ultimo"] == 80


def test_cambio_anual():
    puntos = [_punto(10, 110), _punto(30, 110), _punto(365, 100)]
    assert precios.estadisticas(puntos, HOY)["cambio_anual"] == pytest.approx(0.10)


# ── Avisos ────────────────────────────────────────────

STATS = {"referencia": 84.5, "unidad": "MTR"}


@pytest.mark.parametrize("precio,unidad,nivel", [
    (101, "MTR", "alto"),
    (97.2, "MTR", "alto"),     # +15 % justo
    (70, "MTR", "bajo"),
    (90, "MTR", "normal"),
    (90, "ROLLO", "unidad_distinta"),
    (0, "MTR", "sin_precio"),
])
def test_evaluar(precio, unidad, nivel):
    assert precios.evaluar(precio, unidad, STATS)["nivel"] == nivel


def test_porcentaje_legible():
    assert precios.porcentaje(0.2) == "+20%"
    assert precios.porcentaje(-0.18) == "−18%"


def test_factura_contra_lo_cotizado():
    conceptos = [
        {"descripcion": "PINTURA VINILICA BLANCA 19L", "precio_unitario": 1390, "unidad": "CUBETA"},
        {"descripcion": "FLETE", "precio_unitario": 300, "unidad": "SERVICIO"},
        {"descripcion": "Brocha 4 pulgadas", "precio_unitario": 61, "unidad": "PZA"},
    ]
    partidas = [
        {"concepto": "Pintura vinílica blanca 19L", "precio_unitario": 1290, "unidad": "CUBETA"},
        {"concepto": "Brocha 4 pulgadas", "precio_unitario": 60, "unidad": "PZA"},
    ]
    (d,) = api_precios.comparar_factura(conceptos, partidas)
    assert d["concepto"] == "Pintura vinílica blanca 19L"
    assert d["diferencia"] == pytest.approx(0.0775, abs=1e-3)


def test_rfc_de_la_empresa_es_el_mas_frecuente():
    xmls = [{"rfc_emisor": "PROV1", "rfc_receptor": "EMPRESA"},
            {"rfc_emisor": "PROV2", "rfc_receptor": "EMPRESA"},
            {"rfc_emisor": "EMPRESA", "rfc_receptor": "CLIENTE"}]
    assert precios.rfc_empresa_probable(xmls) == "EMPRESA"


# ── Permisos ──────────────────────────────────────────

def _roles(path, metodo):
    ruta = next(r for r in api_precios.router.routes if r.path == path and metodo in r.methods)
    return [c.cell_contents for d in ruta.dependant.dependencies for c in (d.call.__closure__ or ())]


def test_permisos():
    assert api_precios.ROLES_VER in _roles("/precios/articulo", "GET")
    assert api_precios.ROLES_VER in _roles("/precios/evaluar", "POST")
    assert api_precios.ROLES_CLASIFICAR in _roles("/precios/importar", "POST")
    assert api_precios.ROLES_CLASIFICAR in _roles("/precios/clasificar", "POST")
    assert "almacen" in api_precios.ROLES_VER and "almacen" not in api_precios.ROLES_CLASIFICAR
