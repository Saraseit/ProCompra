"""Módulo de pagos, orden a crédito automática y borrado definitivo."""

import uuid as uuidlib
from datetime import date

import pytest
from pydantic import ValidationError

from app.api import ordenes, pagos as api_pagos, requerimientos
from app.utils import pagos

HOY = date(2026, 11, 4)  # miércoles: la semana cierra el domingo 8


def _orden(id_="o1", tipo_pago="credito", estado="recoleccion", total=1000.0, fecha="2026-10-01", programados=None, **extra):
    return {
        "id": id_, "folio": int(id_[-1]), "fecha": fecha, "estado": estado, "tipo_pago": tipo_pago,
        "total": total, "monto_comprobado": extra.get("monto_comprobado"),
        "proveedor": {"id": "p1", "nombre": "Pinturen", "da_credito": True},
        "pagos": extra.get("pagos", []), "programados": programados or [], "facturas": extra.get("facturas", []),
    }


def _prog(*pares):
    return [{"id": f"c{i}", "fecha": f, "monto": m} for i, (f, m) in enumerate(pares)]


# ── Qué urge esta semana ─────────────────────────────────

def test_la_semana_cierra_el_domingo():
    assert pagos.fin_de_semana(HOY) == date(2026, 11, 8)
    assert pagos.fin_de_semana(date(2026, 11, 8)) == date(2026, 11, 8)
    assert pagos.fin_de_semana(date(2026, 11, 9)) == date(2026, 11, 15)


@pytest.mark.parametrize("fecha, grupo, situacion", [
    ("2026-10-20", "semana", "vencida"),
    ("2026-11-07", "semana", "por_vencer"),
    ("2026-11-08", "semana", "por_vencer"),   # domingo
    ("2026-11-09", "despues", "por_vencer"),  # lunes
    ("2026-11-30", "despues", "vigente"),
])
def test_credito_urge_si_su_fecha_cae_antes_del_domingo(fecha, grupo, situacion):
    r = pagos.pendiente(_orden(programados=_prog((fecha, 1000))), HOY)
    assert (r["grupo"], r["situacion"], r["tipo"]) == (grupo, situacion, "credito")


def test_compra_dividida_solo_pide_lo_de_esta_semana():
    o = _orden(total=1000, programados=_prog(("2026-11-06", 520), ("2026-12-06", 520)))
    r = pagos.pendiente(o, HOY)
    assert (r["grupo"], r["a_pagar"], r["saldo"], r["recargo"]) == ("semana", 520.0, 1040.0, 40.0)


def test_despues_pide_el_siguiente_pago():
    r = pagos.pendiente(_orden(programados=_prog(("2026-11-20", 300), ("2026-12-20", 700))), HOY)
    assert (r["grupo"], r["a_pagar"], r["vencimiento"]) == ("despues", 300.0, "2026-11-20")


def test_contado_autorizada_urge_siempre():
    r = pagos.pendiente(_orden(tipo_pago="transferencia", estado="autorizada", fecha="2026-11-04"), HOY)
    assert (r["grupo"], r["tipo"], r["saldo"], r["a_pagar"]) == ("semana", "contado", 1000.0, 1000.0)


def test_credito_sin_fecha_va_aparte():
    r = pagos.pendiente(_orden(), HOY)
    assert (r["grupo"], r["situacion"], r["a_pagar"]) == ("sin_fecha", "sin_fecha", 1000.0)


def test_por_pagar_omite_liquidadas_y_ordena_por_fecha():
    res = pagos.por_pagar([
        _orden("o1", programados=_prog(("2026-11-05", 1000))),
        _orden("o2", programados=_prog(("2026-10-01", 1000))),
        _orden("o3", programados=_prog(("2026-10-01", 1000)), pagos=[{"monto": 1000}]),
        _orden("o4", tipo_pago="efectivo", estado="autorizada", total=250),
        _orden("o5", programados=_prog(("2026-11-25", 1000)), pagos=[{"monto": 400}]),
        _orden("o6"),
    ], HOY)
    assert [r["id"] for r in res["semana"]["ordenes"]] == ["o2", "o1", "o4"]
    assert res["semana"]["total"] == 2250.0 and res["semana"]["vencido"] == 1000.0
    assert [(r["id"], r["saldo"]) for r in res["despues"]["ordenes"]] == [("o5", 600.0)]
    assert [r["id"] for r in res["sin_fecha"]["ordenes"]] == ["o6"]


def test_historial_agrupa_por_movimiento():
    filas = [
        {"id": "a", "movimiento_id": "m1", "orden_id": "o1", "monto": 300, "fecha_pago": "2026-11-02",
         "metodo": "transferencia", "referencia": "SPEI 1", "created_at": "2", "orden": {"folio": 1}},
        {"id": "b", "movimiento_id": "m1", "orden_id": "o2", "monto": 200.5, "fecha_pago": "2026-11-02",
         "metodo": "transferencia", "referencia": "SPEI 1", "created_at": "2", "orden": {"folio": 2}},
        {"id": "c", "orden_id": "o3", "monto": 50, "fecha_pago": "2026-10-01", "metodo": "efectivo",
         "created_at": "1", "orden": {"folio": 3}},
    ]
    m1, suelto = pagos.agrupar_pagos(filas)
    assert (m1["id"], m1["agrupado"], m1["monto"], [o["folio"] for o in m1["ordenes"]]) == ("m1", True, 500.5, [1, 2])
    assert (suelto["id"], suelto["agrupado"], suelto["monto"]) == ("c", False, 50.0)


# ── Validaciones ─────────────────────────────────────────

UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"


def test_pago_de_varias_ordenes_valida_forma_y_ordenes():
    base = {"fecha_pago": "2026-11-04", "metodo": "transferencia", "referencia": "SPEI 9"}
    ok = api_pagos.PagoVarias(**base, aplicaciones=[{"orden_id": UUID_A, "monto": 10}, {"orden_id": UUID_B, "monto": 5}])
    assert len(ok.aplicaciones) == 2
    with pytest.raises(ValidationError):
        api_pagos.PagoVarias(**base, aplicaciones=[{"orden_id": UUID_A, "monto": 10}, {"orden_id": UUID_A, "monto": 5}])
    with pytest.raises(ValidationError):
        api_pagos.PagoVarias(**{**base, "referencia": ""}, aplicaciones=[{"orden_id": UUID_A, "monto": 10}])
    with pytest.raises(ValidationError):
        api_pagos.PagoVarias(**{**base, "metodo": "credito"}, aplicaciones=[{"orden_id": UUID_A, "monto": 10}])
    with pytest.raises(ValidationError):
        api_pagos.PagoVarias(**base, aplicaciones=[])


@pytest.mark.parametrize("tipo, estado, puede", [
    ("credito", "autorizada", True), ("credito", "cerrada", True), ("credito", "autorizacion", False),
    ("transferencia", "autorizada", True), ("transferencia", "comprobacion", True),
    ("transferencia", "cerrada", False), ("efectivo", "borrador", False),
])
def test_cuando_una_orden_recibe_pagos(tipo, estado, puede):
    assert (ordenes.motivo_sin_pago({"folio": 7, "tipo_pago": tipo, "estado": estado}) is None) is puede


@pytest.mark.parametrize("proveedor, credito", [
    ({"da_credito": True, "dias_credito": None}, True),
    ({"da_credito": False, "dias_credito": 30}, False),   # la marca manda sobre el plazo viejo
    ({"dias_credito": 30}, True),                         # antes de la fase 7
    ({"dias_credito": None}, False),
])
def test_da_credito_segun_la_marca_del_proveedor(proveedor, credito):
    assert ordenes.da_credito(proveedor) is credito


def test_calendario_valida_fechas_y_montos():
    c = ordenes.Calendario(programados=[{"fecha": "2026-11-20T00:00:00", "monto": 5651.87, "notas": " 1 de 2 "}])
    assert (c.programados[0].fecha, c.programados[0].notas) == ("2026-11-20", "1 de 2")
    assert ordenes.Calendario(programados=[]).programados == []
    for malo in ({"fecha": "20/11/2026", "monto": 1}, {"fecha": "2026-11-20", "monto": 0}):
        with pytest.raises(ValidationError):
            ordenes.Calendario(programados=[malo])


def test_cualquiera_puede_poner_fechas_de_pago():
    roles = _roles_de(ordenes.router, "PUT", "/ordenes/{orden_id}/programados")
    assert set(roles) == {"admin", "compras", "almacen", "pagos"}


def _roles_de(router, metodo, ruta):
    ruta_ = next(r for r in router.routes if r.path == ruta and metodo in r.methods)
    for dep in ruta_.dependant.dependencies:
        if dep.call.__name__ == "verificar":
            return dep.call.__closure__[0].cell_contents
    raise AssertionError(f"{ruta} no exige rol")


@pytest.mark.parametrize("router, metodo, ruta", [
    (ordenes.router, "POST", "/ordenes/borrar"),
    (requerimientos.router, "POST", "/requerimientos/borrar"),
    (api_pagos.router, "DELETE", "/pagos/{pago_id}"),
])
def test_borrar_es_solo_de_admin(router, metodo, ruta):
    assert _roles_de(router, metodo, ruta) == ("admin",)


def test_registrar_pagos_es_de_contabilidad():
    assert _roles_de(api_pagos.router, "POST", "/pagos") == ("admin", "pagos")


# ── Borrado definitivo contra una base en memoria con llaves foráneas ──

class _Res:
    def __init__(self, data):
        self.data = data


# tabla -> [(columna, tabla referida, al borrar)]; 'a' = sin acción (bloquea).
LLAVES = {
    "partidas": [("orden_id", "ordenes_compra", "c"), ("req_id", "requerimientos", "a"),
                 ("partida_origen_id", "partidas", "a")],
    "pagos": [("orden_id", "ordenes_compra", "c"), ("movimiento_id", "movimientos_pago", "c")],
    "pagos_programados": [("orden_id", "ordenes_compra", "c")],
    "historial": [("orden_id", "ordenes_compra", "c")],
    "recolecciones": [("orden_id", "ordenes_compra", "c")],
    "notificaciones": [("orden_id", "ordenes_compra", "c")],
    "adjuntos": [("orden_id", "ordenes_compra", "a")],
    "facturas": [("orden_id", "ordenes_compra", "a")],
    "requerimientos": [("orden_id", "ordenes_compra", "a")],
    "ordenes_compra": [("orden_origen_id", "ordenes_compra", "a")],
}


class _Consulta:
    def __init__(self, base, tabla):
        self.base, self.tabla, self.filtros, self.accion, self.datos = base, tabla, [], "select", None
        self.campos = ""

    def select(self, campos="*", **_k): self.campos = campos; return self
    def eq(self, c, v): self.filtros.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.filtros.append(lambda r: r.get(c) in vs); return self
    def update(self, d): self.accion, self.datos = "update", d; return self
    def delete(self): self.accion = "delete"; return self

    def execute(self):
        filas = self.base.tablas.setdefault(self.tabla, [])
        elegidas = [r for r in filas if all(f(r) for f in self.filtros)]
        if self.accion == "update":
            for r in elegidas:
                r.update(self.datos)
        elif self.accion == "delete":
            self.base.borrar(self.tabla, {r["id"] for r in elegidas})
        salida = [dict(r) for r in elegidas]
        if "orden:orden_id" in self.campos:  # único embebido que usa el borrado
            por_id = {o["id"]: o for o in self.base.tablas.get("ordenes_compra", [])}
            for r in salida:
                r["orden"] = por_id.get(r.get("orden_id"))
        return _Res(salida)


class _Base:
    def __init__(self, tablas):
        self.tablas = tablas

    def table(self, nombre):
        return _Consulta(self, nombre)

    def borrar(self, tabla, ids):
        for hija, llaves in LLAVES.items():
            for columna, padre, regla in llaves:
                if padre != tabla:
                    continue
                citan = [r for r in self.tablas.get(hija, []) if r.get(columna) in ids
                         and not (hija == tabla and r["id"] in ids)]
                if citan and regla == "a":
                    raise AssertionError(f"FK: {hija}.{columna} aún apunta a {tabla}")
                if citan:
                    self.borrar(hija, {r["id"] for r in citan})
        self.tablas[tabla] = [r for r in self.tablas.get(tabla, []) if r["id"] not in ids]


def _id():
    return str(uuidlib.uuid4())


@pytest.fixture
def base(monkeypatch):
    o1, o2, o3 = _id(), _id(), _id()
    r1, r2 = _id(), _id()
    p1, p2 = _id(), _id()
    tablas = {
        "ordenes_compra": [{"id": o1, "folio": 1, "estado": "recoleccion"},
                           {"id": o2, "folio": 2, "estado": "autorizada", "orden_origen_id": o1},
                           {"id": o3, "folio": 3, "estado": "cerrada"}],
        "partidas": [{"id": p1, "orden_id": o1, "req_id": r1}, {"id": p2, "orden_id": o2, "req_id": r1, "partida_origen_id": p1},
                     {"id": _id(), "orden_id": o1, "req_id": r2}],
        "requerimientos": [{"id": r1, "estado": "en_orden", "orden_id": o1}, {"id": r2, "estado": "en_orden", "orden_id": o1}],
        "pagos": [{"id": _id(), "orden_id": o1, "movimiento_id": "m1"}, {"id": _id(), "orden_id": o3, "movimiento_id": "m2"}],
        "movimientos_pago": [{"id": "m1"}, {"id": "m2"}],
        "historial": [{"id": _id(), "orden_id": o1}],
        "adjuntos": [{"id": _id(), "orden_id": o1, "ruta": "ordenes/x/a.pdf"}],
        "facturas": [{"id": _id(), "orden_id": o1, "vinculacion": "manual"}],
    }
    b = _Base(tablas)
    borrados = []
    monkeypatch.setattr(ordenes, "supabase", b)
    monkeypatch.setattr(requerimientos, "supabase", b)
    monkeypatch.setattr(ordenes.storage, "borrar_varios", borrados.extend)
    b.ids = {"o1": o1, "o2": o2, "o3": o3, "r1": r1, "r2": r2}
    b.archivos = borrados
    return b


ADMIN = {"id": "u1", "rol": "admin", "nombre": "David"}


def test_borrar_orden_suelta_lo_ligado_y_libera_requerimientos(base):
    r = ordenes.borrar_definitivo(ordenes.BorradoOrdenes(ids=[base.ids["o1"]]), usuario=ADMIN)
    t = base.tablas
    assert r["folios"] == [1] and r["facturas_desvinculadas"] == 1 and r["documentos_borrados"] == 1
    assert {o["folio"] for o in t["ordenes_compra"]} == {2, 3}
    assert t["ordenes_compra"][0]["orden_origen_id"] is None
    assert not t["historial"] and not t["adjuntos"] and base.archivos == ["ordenes/x/a.pdf"]
    assert t["facturas"][0]["orden_id"] is None and t["facturas"][0]["vinculacion"] == "pendiente"
    assert [m["id"] for m in t["movimientos_pago"]] == ["m2"]
    reqs = {q["id"]: q for q in t["requerimientos"]}
    # r2 solo estaba en la orden borrada; r1 sigue en el complemento (#2).
    assert reqs[base.ids["r2"]] == {"id": base.ids["r2"], "estado": "pendiente", "orden_id": None}
    assert reqs[base.ids["r1"]]["estado"] == "en_orden" and reqs[base.ids["r1"]]["orden_id"] == base.ids["o2"]
    assert r["requerimientos_liberados"] == 1


def test_borrar_ordenes_con_sus_requerimientos(base):
    r = ordenes.borrar_definitivo(
        ordenes.BorradoOrdenes(ids=[base.ids["o1"], base.ids["o2"]], con_requerimientos=True), usuario=ADMIN)
    assert r["borradas"] == 2 and r["requerimientos_borrados"] == 2
    assert not base.tablas["requerimientos"] and [o["folio"] for o in base.tablas["ordenes_compra"]] == [3]


def test_borrar_requerimientos_respeta_los_que_estan_en_una_orden(base):
    suelto = _id()
    base.tablas["requerimientos"].append({"id": suelto, "estado": "pendiente", "orden_id": None})
    base.tablas["partidas"].append({"id": _id(), "orden_id": base.ids["o3"], "req_id": suelto})
    r = requerimientos.borrar_definitivo(
        requerimientos.BorradoRequerimientos(ids=[suelto, base.ids["r1"]]), usuario=ADMIN)
    assert r["borrados"] == 1 and [o["id"] for o in r["omitidos"]] == [base.ids["r1"]]
    assert suelto not in {q["id"] for q in base.tablas["requerimientos"]}
