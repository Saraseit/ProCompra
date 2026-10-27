"""Lectura de CFDI y vinculación automática con órdenes."""

import pytest

from app.utils import cfdi

CFDI_40 = b"""<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
  Version="4.0" Serie="A" Folio="1234" Fecha="2026-10-20T10:15:00" SubTotal="1000.00" Total="1160.00"
  Moneda="MXN" MetodoPago="PUE" FormaPago="03" TipoDeComprobante="I">
  <cfdi:Emisor Rfc="TEL010101ABC" Nombre="TELAS DEL SURESTE" RegimenFiscal="601"/>
  <cfdi:Receptor Rfc="MES151124J83" Nombre="MINIMAL ESTUDIO" UsoCFDI="G03"/>
  <cfdi:Conceptos><cfdi:Concepto Cantidad="10" Descripcion="Tela tergal" Importe="1000.00"/></cfdi:Conceptos>
  <cfdi:Impuestos TotalImpuestosTrasladados="160.00"/>
  <cfdi:Complemento>
    <tfd:TimbreFiscalDigital Version="1.1" UUID="ab12cd34-0000-4000-8000-1234567890ef"/>
  </cfdi:Complemento>
</cfdi:Comprobante>"""


def _orden(id_, rfc="TEL010101ABC", total=1160.0, estado="comprobacion", fecha="2026-10-18", **extra):
    return {"id": id_, "folio": int(id_[-1]), "fecha": fecha, "estado": estado, "total": total,
            "monto_comprobado": extra.get("monto_comprobado"),
            "proveedor": {"rfc": rfc}}


def test_lee_cfdi_40():
    f = cfdi.leer_cfdi(CFDI_40)
    assert f["uuid_fiscal"] == "AB12CD34-0000-4000-8000-1234567890EF"
    assert f["rfc_emisor"] == "TEL010101ABC"
    assert f["rfc_receptor"] == "MES151124J83"
    assert (f["subtotal"], f["iva"], f["total"]) == (1000.0, 160.0, 1160.0)
    assert (f["serie"], f["folio"], f["tipo_comprobante"]) == ("A", "1234", "I")


def test_lee_cfdi_33():
    xml = CFDI_40.replace(b"cfd/4", b"cfd/3").replace(b'Version="4.0"', b'Version="3.3"')
    assert cfdi.leer_cfdi(xml)["total"] == 1160.0


@pytest.mark.parametrize("contenido", [b"no es xml", b"<factura total='1'/>",
                                       b'<cfdi:Comprobante xmlns:cfdi="x" Total="1"/>'])
def test_rechaza_lo_que_no_es_cfdi(contenido):
    with pytest.raises(cfdi.CfdiInvalido):
        cfdi.leer_cfdi(contenido)


def test_vincula_si_hay_una_sola_candidata():
    f = cfdi.leer_cfdi(CFDI_40)
    ordenes = [_orden("o1"), _orden("o2", rfc="OTRO010101XX0"), _orden("o3", total=5000)]
    assert cfdi.elegir_orden(f, ordenes, set())["id"] == "o1"


def test_usa_el_monto_comprobado_si_el_total_cambio():
    f = cfdi.leer_cfdi(CFDI_40)
    ordenes = [_orden("o1", total=1200.0, monto_comprobado=1160.0)]
    assert cfdi.elegir_orden(f, ordenes, set())["id"] == "o1"


def test_no_vincula_si_hay_ambiguedad():
    f = cfdi.leer_cfdi(CFDI_40)
    assert cfdi.elegir_orden(f, [_orden("o1"), _orden("o2")], set()) is None


def test_no_vincula_ordenes_ya_facturadas():
    f = cfdi.leer_cfdi(CFDI_40)
    ordenes = [_orden("o1"), _orden("o2")]
    assert cfdi.elegir_orden(f, ordenes, {"o1"})["id"] == "o2"


def test_no_vincula_fuera_de_fecha_ni_en_borrador():
    f = cfdi.leer_cfdi(CFDI_40)
    assert cfdi.elegir_orden(f, [_orden("o1", fecha="2026-01-01")], set()) is None
    assert cfdi.elegir_orden(f, [_orden("o1", estado="borrador")], set()) is None


def test_tolera_centavos_de_redondeo():
    assert cfdi.total_coincide(1160.00, 1160.60)
    assert not cfdi.total_coincide(1160.00, 1175.00)
