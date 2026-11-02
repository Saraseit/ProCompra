"""Importación del histórico contra una base en memoria (sin Supabase)."""

import asyncio
import io
import uuid as uuidlib

import pytest
from fastapi import UploadFile

from app.api import precios as api_precios
from app.utils import precios


class _Res:
    def __init__(self, data, count=None):
        self.data, self.count = data, count


class _Consulta:
    def __init__(self, base, tabla):
        self.base, self.tabla = base, tabla
        self.filtros, self.accion, self.datos, self.contar = [], "select", None, False
        self.negar = False

    @property
    def not_(self):
        self.negar = True
        return self

    def _filtro(self, f):
        self.filtros.append((f, self.negar))
        self.negar = False
        return self

    def select(self, *_a, count=None):
        self.contar = count == "exact"
        return self

    def eq(self, c, v): return self._filtro(lambda r: r.get(c) == v)
    def neq(self, c, v): return self._filtro(lambda r: r.get(c) != v)
    def in_(self, c, vs): return self._filtro(lambda r: r.get(c) in vs)
    def is_(self, c, v): return self._filtro(lambda r: r.get(c) is None)
    def order(self, *_a, **_k): return self
    def limit(self, *_a): return self
    def range(self, *_a): return self

    def insert(self, filas):
        self.accion, self.datos = "insert", filas if isinstance(filas, list) else [filas]
        return self

    def update(self, d):
        self.accion, self.datos = "update", d
        return self

    def _coinciden(self, filas):
        return [r for r in filas if all(f(r) != n for f, n in self.filtros)]

    def execute(self):
        filas = self.base.setdefault(self.tabla, [])
        if self.accion == "insert":
            nuevas = []
            for d in self.datos:
                if self.tabla == "facturas" and d.get("uuid_fiscal") and any(
                        r["uuid_fiscal"] == d["uuid_fiscal"] for r in filas):
                    raise Exception("duplicate key value violates unique constraint")
                fila = {"id": str(uuidlib.uuid4()), **d}
                if self.tabla == "factura_conceptos":
                    alias = next((a for a in self.base.get("insumo_alias", []) if a["texto_norm"] == d["texto_norm"]), None)
                    fila.setdefault("insumo_id", alias["insumo_id"] if alias else None)
                    fila.setdefault("ignorado", alias["ignorar"] if alias else False)
                nuevas.append(fila)
            filas.extend(nuevas)
            return _Res(nuevas)
        elegidas = self._coinciden(filas)
        if self.accion == "update":
            for r in elegidas:
                r.update(self.datos)
        return _Res(elegidas, len(elegidas) if self.contar else None)


class _Base:
    def __init__(self):
        self.tablas = {}

    def table(self, nombre):
        return _Consulta(self.tablas, nombre)


def _xml(uuid, emisor="TEL010101ABC", receptor="MES151124J83", tipo="I", descripcion="TELA TERGAL NGO 1.50", precio="85.00"):
    return f"""<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
      Version="4.0" Fecha="2026-05-10T10:00:00" SubTotal="850.00" Total="986.00" Moneda="MXN" TipoDeComprobante="{tipo}">
      <cfdi:Emisor Rfc="{emisor}" Nombre="TELAS"/><cfdi:Receptor Rfc="{receptor}"/>
      <cfdi:Conceptos><cfdi:Concepto Cantidad="10" ClaveUnidad="MTR" Descripcion="{descripcion}" ValorUnitario="{precio}" Importe="{float(precio) * 10:.2f}"/>
      <cfdi:Concepto Cantidad="1" ClaveUnidad="E48" Descripcion="FLETE" ValorUnitario="100" Importe="100"/></cfdi:Conceptos>
      <cfdi:Complemento><tfd:TimbreFiscalDigital UUID="{uuid}"/></cfdi:Complemento>
    </cfdi:Comprobante>""".encode()


def _archivo(nombre, contenido):
    return UploadFile(file=io.BytesIO(contenido), filename=nombre)


@pytest.fixture
def base(monkeypatch):
    b = _Base()
    monkeypatch.setattr(api_precios, "supabase", b)
    monkeypatch.setattr(api_precios, "_migracion", {"lista": True, "revisado": 0})
    return b


def _importar(archivos, rfc="MES151124J83"):
    return asyncio.run(api_precios.importar(archivos=archivos, rfc_empresa=rfc, usuario={"id": "u1", "rol": "admin"}))


def test_importa_compras_y_descarta_ventas_y_otros_tipos(base):
    r = _importar([
        _archivo("a.xml", _xml("U1")),
        _archivo("b.xml", _xml("U2", emisor="MES151124J83", receptor="CLIENTE01")),
        _archivo("c.xml", _xml("U3", tipo="E")),
        _archivo("d.xml", b"<no-es-cfdi/>"),
        _archivo("e.pdf", b"%PDF"),
        _archivo("f.xml", _xml("U1")),
    ])
    assert r["resumen"] == {"importada": 1, "no_compra": 1, "ignorada": 1, "invalida": 2, "duplicada": 1}
    assert r["renglones"] == 2
    (f,) = base.tablas["facturas"]
    assert f["historica"] is True and f.get("orden_id") is None
    textos = {c["texto_norm"]: c for c in base.tablas["factura_conceptos"]}
    assert textos["TELA TERGAL NGO 1.50"]["unidad"] == "MTR"
    assert textos["TELA TERGAL NGO 1.50"]["precio_unitario"] == 85.0
    assert textos["FLETE"]["unidad"] == "SERVICIO"


def test_segunda_carga_no_duplica_y_reconoce_textos_clasificados(base):
    _importar([_archivo("a.xml", _xml("U1"))])
    base.tablas["insumo_alias"] = [{"texto_norm": "TELA TERGAL NGO 1.50", "insumo_id": "i1", "ignorar": False},
                                   {"texto_norm": "FLETE", "insumo_id": None, "ignorar": True}]
    r = _importar([_archivo("a.xml", _xml("U1")), _archivo("b.xml", _xml("U9", precio="90"))])
    assert r["resumen"] == {"duplicada": 1, "importada": 1}
    assert r["reconocidos"] == 2
    nuevos = [c for c in base.tablas["factura_conceptos"] if c["precio_unitario"] == 90.0]
    assert nuevos[0]["insumo_id"] == "i1"


def test_completa_renglones_de_una_factura_ya_cargada_sin_ellos(base):
    base.tablas["facturas"] = [{"id": "f-vieja", "uuid_fiscal": "U5", "historica": False}]
    r = _importar([_archivo("a.xml", _xml("U5"))])
    assert r["resumen"] == {"completada": 1}
    assert {c["factura_id"] for c in base.tablas["factura_conceptos"]} == {"f-vieja"}


def test_sin_rfc_de_empresa_entra_todo_lo_de_ingreso(base):
    r = _importar([_archivo("b.xml", _xml("U2", emisor="MES151124J83", receptor="CLIENTE01"))], rfc="")
    assert r["resumen"] == {"importada": 1}


def test_estadisticas_con_lo_importado(base):
    for n, p in enumerate(["80", "85", "84", "101"]):
        _importar([_archivo(f"{n}.xml", _xml(f"U{n}", precio=p))])
    puntos = [{"fecha": c["fecha"], "precio": c["precio_unitario"], "unidad": c["unidad"], "cantidad": c["cantidad"]}
              for c in base.tablas["factura_conceptos"] if c["texto_norm"] != "FLETE"]
    from datetime import date
    s = precios.estadisticas(puntos, date(2026, 11, 2))
    assert s["referencia"] == 84.5 and s["ventana_meses"] == 6
