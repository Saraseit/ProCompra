"""Casos de prueba de la especificación de ProPago (§12) contra el motor."""

from datetime import date
from io import BytesIO

import pytest

from app.utils import registros as R

CUENTAS = {
    "a091": {"id": "a091", "clave": "A09.1", "nombre_erp": "100 - A09.1 - INSUMOS Y GASTOS EN PREPARACION DE PEDIDOS", "tipo": "gasto", "cdv": False},
    "a10": {"id": "a10", "clave": "A10", "nombre_erp": "100 - A10 - OTROS LOGISTICA Y DISTRIBUCION", "tipo": "gasto", "cdv": False},
    "a03": {"id": "a03", "clave": "A03", "nombre_erp": "100 - A03 - FLETES TERCERIZADOS PROGRAMADOS", "tipo": "gasto", "cdv": False},
    "a112": {"id": "a112", "clave": "A11.2", "nombre_erp": "100 - A11.2 - ALIMENTO", "tipo": "gasto", "cdv": False},
    "c06": {"id": "c06", "clave": "C06", "nombre_erp": "100 - C06 - SUB-ARRENDAMIENTO", "tipo": "gasto", "cdv": True},
    "d03": {"id": "d03", "clave": "D03", "nombre_erp": "100 - D03 - SUB-CONTRATACION MANTTO GENERAL INVENTARIO", "tipo": "gasto", "cdv": False},
    "f02": {"id": "f02", "clave": "F02", "nombre_erp": "100 - F02 - SERVICIOS PUBLICOS", "tipo": "gasto", "cdv": False},
    "g01": {"id": "g01", "clave": "G01", "nombre_erp": "100 - G01 - EQUIPO NUEVO (MOBILIARIO)", "tipo": "gasto", "cdv": False},
    "h01": {"id": "h01", "clave": "H01", "nombre_erp": "100 - H01 - IVA POR ACREDITAR", "tipo": "iva", "cdv": False},
}
PLANTILLAS = {
    "gen": {"id": "gen", "codigo": "GENERAL", "plantilla": "{VERBO} DE {CONCEPTO}"},
    "tcb": {"id": "tcb", "codigo": "TCB", "plantilla": "COMPRA DE {CONCEPTO}"},
    "lav": {"id": "lav", "codigo": "LAVANDERIA", "plantilla": "PAGO DE LAVANDERIA DE {ITEMS} A {PROVEEDOR}"},
    "sub": {"id": "sub", "codigo": "SUBRENTA", "plantilla": "PAGO DE SUBRENTA DE {ITEMS} A {PROVEEDOR}"},
    "fle": {"id": "fle", "codigo": "FLETE", "plantilla": "PAGO DE FLETE {CONCEPTO} A {PROVEEDOR}"},
    "cun": {"id": "cun", "codigo": "CAJA_CUN", "plantilla": "COMPRA DE {CONCEPTO}[ EN {PROVEEDOR}]"},
    "d360": {"id": "d360", "codigo": "DEPTO360", "plantilla": "PAGO DE {CONCEPTO} A {PROVEEDOR}[ {DOC}]"},
    "pag": {"id": "pag", "codigo": "PAGO_GENERAL", "plantilla": "PAGO DE {CONCEPTO}[ A {PROVEEDOR}]"},
}
REGLAS = [
    {"id": "r-sub", "nombre": "Subrenta", "prioridad": 10, "proveedor_id": "dilgos", "cuenta_id": "c06", "plantilla_id": "sub"},
    {"id": "r-sub2", "nombre": "Subrenta 2", "prioridad": 10, "proveedor_id": "guillermo", "cuenta_id": "c06", "plantilla_id": "sub"},
    {"id": "r-lav", "nombre": "Lavandería", "prioridad": 10, "proveedor_id": "wilbert", "cuenta_id": "d03",
     "categoria_texto": "LAVADO", "plantilla_id": "lav"},
    {"id": "r-fle", "nombre": "Fletes", "prioridad": 10, "palabras": ["FLETE"], "cuenta_id": "a03",
     "categoria_texto": "#LOG", "plantilla_id": "fle"},
    {"id": "r-d360", "nombre": "Depto 360", "prioridad": 30, "proveedor_id": "d360", "cuenta_id": "g01", "plantilla_id": "d360"},
    {"id": "r-cun", "nombre": "Cancún sin factura", "prioridad": 15, "plaza": "CUN", "con_cfdi": False, "fuerza_nd": True,
     "plantilla_id": "cun"},
    {"id": "r-tcb", "nombre": "Tarjeta de compras", "prioridad": 5, "caja_id": "tcb", "plantilla_id": "tcb"},
    {"id": "r-tela", "nombre": "Telas", "prioridad": 20, "palabras": ["TELA", "TAFETAN"], "palabras_excluir": ["SERVILLETAS"],
     "cuenta_id": "a091", "categoria_texto": "TELA"},
    {"id": "r-hielo", "nombre": "Hidratación", "prioridad": 20, "palabras": ["HIELO", "AGUA"], "cuenta_id": "a112"},
    {"id": "r-didi", "nombre": "Traslados", "prioridad": 30, "palabras": ["DIDI", "UBER"], "cuenta_id": "a10", "plantilla_id": "pag"},
    {"id": "r-cfe", "nombre": "CFE", "prioridad": 40, "palabras": ["CFE"], "cuenta_id": "f02", "desglosa_iva": True, "plantilla_id": "pag"},
]
MOTOR = R.Motor(REGLAS, CUENTAS, PLANTILLAS)


def pago(**kw):
    return {"fecha": "2026-10-13", "plaza": "MID", "poliza": None, "caja_id": "bbva", "comision": 0, "diferencia": 0,
            "aplicaciones": [], "lineas": [], **kw}


def aplicacion(monto, partidas, proveedor=("p", "PROVEEDOR"), factura=None, **kw):
    return {"pago_id": "a1", "orden_id": "o1", "orden_folio": 900, "monto": monto, "partidas": partidas,
            "proveedor": {"id": proveedor[0], "nombre": proveedor[1]}, "factura": factura, **kw}


# ── Dinero y textos ───────────────────────────────────

def test_repartir_deja_el_residuo_en_el_ultimo():
    partes = R.repartir(3006.90, [1] * 7)
    assert partes == [429.56] * 6 + [429.54]
    assert R.centavos(sum(partes)) == R.centavos(3006.90)


def test_desglose_cuadra_al_centavo():
    assert R.desglosar(9846.00) == (8487.93, 1358.07)


@pytest.mark.parametrize("texto, esperado", [
    ("17095/17100", ["17095", "17100"]),
    ("C16620-15821", ["16620", "15821"]),
    ("bodega", ["BODEGA"]),
    ("CDIRECTO", ["DIRECTO"]),
    ("0", []),
    (None, []),
])
def test_contratos(texto, esperado):
    assert R.contratos(texto) == esperado


def test_plantilla_omite_tramos_sin_dato_y_no_duplica_verbo():
    assert R.aplicar_plantilla("{VERBO} DE {CONCEPTO}[ A {PROVEEDOR}]", {"VERBO": "COMPRA", "CONCEPTO": "hilo"}) == "COMPRA DE HILO"
    assert R.aplicar_plantilla("{VERBO} DE {CONCEPTO}", {"VERBO": "PAGO", "CONCEPTO": "pago de CFE"}) == "PAGO DE CFE"
    assert R.placeholders_invalidos("PAGO DE {ITEMS} A {TIENDA}") == ["TIENDA"]


def test_mayusculas_sin_acentos_conserva_la_enie():
    assert R.mayusculas("Lavandería 100 años") == "LAVANDERIA 100 AÑOS"


def test_regla_de_cuenta_y_regla_modificadora_se_combinan():
    r = R.resolver(REGLAS, {"texto": "HIELO 5 KG", "plaza": "CUN", "con_cfdi": False})
    assert (r["cuenta_id"], r["fuerza_nd"], r["plantilla_id"], r["regla_id"]) == ("a112", True, "cun", "r-hielo")


# ── Casos de la especificación ───────────────────────

def test_cp01_tcb_con_dos_contratos_y_cfdi():
    p = pago(caja_id="tcb", poliza="TCB12", aplicaciones=[aplicacion(900.16, [
        {"concepto": "Tela tafetán negro", "cantidad": 3, "importe": 291.00, "contrato": "17095"},
        {"concepto": "Tela tafetán crudo", "cantidad": 5, "importe": 485.00, "contrato": "17100"},
    ], factura={"total": 900.16, "iva": 124.16, "metodo_pago": "PUE"})])
    r = MOTOR.generar(p)
    assert [(x["cargo"], x["cuenta_id"], x["ref"], x["id_evento"]) for x in r] == [
        (291.00, "a091", "MID - TCB12 - TELA - COMPRA DE TELA TAFETAN NEGRO C17095 EL 13/10/26", 17095),
        (485.00, "a091", "MID - TCB12 - TELA - COMPRA DE TELA TAFETAN CRUDO C17100 EL 13/10/26", 17100),
        (124.16, "h01", "MID - TCB12 - TELA - COMPRA DE TELA TAFETAN NEGRO, TAFETAN CRUDO C17095/17100 EL 13/10/26", 17095),
    ]
    assert R.cuadra(r, 900.16)


def test_cp02_partes_iguales_con_residuo():
    p = pago(caja_id="tcb", poliza="TCB11", aplicaciones=[aplicacion(3488.00, [
        {"concepto": "Tela tergal minimat", "importe": 3006.90, "contrato": "17163/17066/17100/17232/17168/17252/17152"},
    ], factura={"total": 3488.00, "iva": 481.10, "metodo_pago": "PUE"})])
    r = MOTOR.generar(p)
    assert [x["cargo"] for x in r] == [429.56] * 6 + [429.54, 481.10]
    assert r[6]["id_evento"] == 17152 and r[-1]["es_iva"]
    assert R.cuadra(r, 3488.00)


def test_cp03_desglose_calculado_cfe():
    p = pago(fecha="2026-09-04", lineas=[{"concepto": "Servicio de energía eléctrica CFE", "importe": 9846.00}])
    r = MOTOR.generar(p)
    assert [(x["cargo"], x["cuenta_id"], x["ref"]) for x in r] == [
        (8487.93, "f02", "MID - PAGO DE SERVICIO DE ENERGIA ELECTRICA CFE EL 04/09/26"),
        (1358.07, "h01", "MID - PAGO DE SERVICIO DE ENERGIA ELECTRICA CFE EL 04/09/26"),
    ]


def test_cp04_lavanderia_agrupada():
    p = pago(fecha="2026-09-19", aplicaciones=[aplicacion(356.00, [
        {"concepto": "Asiento marfil", "cantidad": 19, "importe": 133, "contrato": "17036"},
        {"concepto": "Respaldo marfil", "cantidad": 19, "importe": 133, "contrato": "17036"},
        {"concepto": "Funda Henson", "cantidad": 6, "importe": 90, "contrato": "17036"},
    ], proveedor=("wilbert", "Wilbert Vera"))])
    (r,) = MOTOR.generar(p)
    assert (r["cargo"], r["cuenta_id"], r["id_evento"]) == (356.00, "d03", 17036)
    assert r["ref"] == ("MID - ND - LAVADO - PAGO DE LAVANDERIA DE 19 ASIENTO MARFIL, 19 RESPALDO MARFIL, "
                        "6 FUNDA HENSON A WILBERT VERA C17036 EL 19/09/26")


def test_cp05_subrenta_con_iva_y_h01_sin_cdv():
    p = pago(fecha="2026-10-30", aplicaciones=[aplicacion(278.40, [
        {"concepto": "Servilletas tela rosa", "cantidad": 30, "importe": 240.00, "contrato": "17300"},
    ], proveedor=("dilgos", "Dilgos"), factura={"total": 278.40, "iva": 38.40, "metodo_pago": "PUE"})])
    r = MOTOR.generar(p)
    assert [(x["cargo"], x["cuenta_id"], x["ref"]) for x in r] == [
        (240.00, "c06", "[CDV] - MID - PAGO DE SUBRENTA DE 30 SERVILLETAS TELA ROSA A DILGOS C17300 EL 30/10/26"),
        (38.40, "h01", "MID - PAGO DE SUBRENTA DE 30 SERVILLETAS TELA ROSA A DILGOS C17300 EL 30/10/26"),
    ]


def test_cp06_subrenta_sin_iva():
    p = pago(fecha="2026-10-09", aplicaciones=[aplicacion(3080.00, [
        {"concepto": "Servilletas lino arena", "cantidad": 77, "importe": 3080.00, "contrato": "17024"},
    ], proveedor=("guillermo", "Don Guillermo"))])
    (r,) = MOTOR.generar(p)
    assert r["ref"] == "[CDV] - MID - ND - PAGO DE SUBRENTA DE 77 SERVILLETAS LINO ARENA A DON GUILLERMO C17024 EL 09/10/26"


def test_cp07_flete_con_dos_contratos():
    p = pago(fecha="2026-10-17", aplicaciones=[aplicacion(10900.00, [
        {"concepto": "Redondo evento Touche, Tekik de Regil", "importe": 10900, "contrato": "16620-15821"},
    ], proveedor=("logix", "Logix Plus"))])
    # La regla de fletes busca la palabra en el concepto; aquí el proveedor no la trae.
    motor = R.Motor(REGLAS + [{"id": "r-logix", "nombre": "Logix", "prioridad": 5, "proveedor_id": "logix",
                               "cuenta_id": "a03", "categoria_texto": "#LOG", "plantilla_id": "fle"}], CUENTAS, PLANTILLAS)
    r = motor.generar(p)
    assert [(x["cargo"], x["id_evento"]) for x in r] == [(5450.00, 16620), (5450.00, 15821)]
    assert r[0]["ref"] == ("MID - ND - #LOG - PAGO DE FLETE REDONDO EVENTO TOUCHE, TEKIK DE REGIL A LOGIX PLUS "
                           "C16620 EL 17/10/26")


def test_contrato_que_no_tiene_5_digitos_avisa():
    (r,) = MOTOR.generar(pago(lineas=[{"concepto": "Hilo", "importe": 10, "contrato": "1582"}]))
    assert "El contrato 1582 no tiene 5 dígitos" in r["avisos"]


def test_cp08_caja_cancun_ticket_sin_cfdi():
    p = pago(fecha="2026-10-18", plaza="CUN", proveedor_texto="Minisuper GM", lineas=[
        {"concepto": "Hielo 5 kg y agua E Pura 10 lt evento Majestic", "importe": 100.50, "contrato": "17101"}])
    (r,) = MOTOR.generar(p)
    assert (r["cargo"], r["cuenta_id"]) == (100.50, "a112")
    assert r["ref"] == "CUN - ND - COMPRA DE HIELO 5 KG Y AGUA E PURA 10 LT EVENTO MAJESTIC EN MINISUPER GM C17101 EL 18/10/26"


def test_cp09_abono_a_remision():
    p = pago(fecha="2026-10-12", aplicaciones=[aplicacion(30000.00, [
        {"concepto": "Fabricación de silla modelo Tequis", "importe": 98935.75, "contrato": "16614"},
    ], proveedor=("d360", "Departamento 360"), naturaleza="abono")])
    (r,) = MOTOR.generar(p)
    assert r["cargo"] == 30000.00 and " - ND - " in r["ref"] and r["ref"].endswith("C16614 EL 12/10/26")


def test_cp10_gasto_sin_orden():
    p = pago(fecha="2026-10-30", lineas=[{"concepto": "DiDi para hacer depósitos y cambiar dinero en el banco", "importe": 142}])
    (r,) = MOTOR.generar(p)
    assert (r["cargo"], r["cuenta_id"], r["id_evento"]) == (142.00, "a10", 0)
    assert r["ref"] == "MID - ND - PAGO DE DIDI PARA HACER DEPOSITOS Y CAMBIAR DINERO EN EL BANCO EL 30/10/26"


def test_cp13_factura_ppd_en_dos_abonos():
    factura = {"total": 11600.00, "iva": 1600.00, "metodo_pago": "PPD", "serie": "FAC", "folio": "0155"}
    partidas = [{"concepto": "Mantenimiento de sillas", "importe": 10000}]
    r1 = MOTOR.generar(pago(aplicaciones=[aplicacion(5000, partidas, ("d360", "Departamento 360"), factura)]))
    r2 = MOTOR.generar(pago(aplicaciones=[aplicacion(6600, partidas, ("d360", "Departamento 360"), factura,
                                                     pagado_previo=5000, iva_previo=689.66)]))
    assert [x["cargo"] for x in r1] == [4310.34, 689.66]
    assert [x["cargo"] for x in r2] == [5689.66, 910.34]
    assert "FAC0155" in r1[0]["ref"]
    assert "Factura PPD: falta el complemento de pago de este abono" in r1[0]["avisos"]


def test_cp14_comision_sumada_al_principal():
    r = MOTOR.generar(pago(comision=17, lineas=[{"concepto": "Compra Temu soportes", "importe": 350}]))
    assert [x["cargo"] for x in r] == [367.00]
    r = MOTOR.generar(pago(comision=17, lineas=[{"concepto": "Compra Temu soportes", "importe": 350, "con_iva": True}]))
    assert [x["cargo"] for x in r] == [318.72, 48.28]


def test_comision_en_cuenta_propia_si_se_configura():
    motor = R.Motor(REGLAS, CUENTAS, PLANTILLAS, {"cuenta_comisiones": "a10"})
    r = motor.generar(pago(comision=17, lineas=[{"concepto": "Hilo", "importe": 350}]))
    assert [(x["cargo"], x["cuenta_id"]) for x in r] == [(350.00, None), (17.00, "a10")]


def test_cp16_propina_va_al_principal():
    r = MOTOR.generar(pago(diferencia=17.50, lineas=[{"concepto": "Comida del personal", "importe": 482.50}]))
    assert [x["cargo"] for x in r] == [500.00]


def test_cp18_pue_en_abonos_avisa():
    factura = {"total": 11600.00, "iva": 1600.00, "metodo_pago": "PUE"}
    r = MOTOR.generar(pago(aplicaciones=[aplicacion(5000, [{"concepto": "Sillas", "importe": 10000}], factura=factura)]))
    assert "Factura PUE pagada en abonos: debió emitirse PPD" in r[0]["avisos"]


def test_sin_cuenta_queda_pendiente():
    (r,) = MOTOR.generar(pago(lineas=[{"concepto": "Algo raro", "importe": 10}]))
    assert r["estado"] == "pendiente" and r["avisos"][0] == "Falta elegir la cuenta"


def test_anticipo_cambia_el_verbo():
    (r,) = MOTOR.generar(pago(aplicaciones=[aplicacion(5000, [{"concepto": "Tela tergal", "importe": 14329}], naturaleza="anticipo")]))
    assert "PAGO DE ANTICIPO DE TELA TERGAL" in r["ref"]


def test_excel_con_el_formato_del_erp():
    from openpyxl import load_workbook
    contenido = R.excel([{"cargo": 260, "cuenta_nombre_erp": "100 - F03 - GASTOS DE OFICINA",
                          "ref": "MID - ND - COMPRA DE 13 GARRAFONES DE AGUA", "id_evento": 0, "propietario": "DAVID"}],
                        date(2026, 11, 6))
    hoja = load_workbook(BytesIO(contenido)).active
    assert [c.value for c in hoja[1]] == list(R.ENCABEZADOS)
    fila = [c.value for c in hoja[2]]
    assert fila[1:] == ["EFECTIVO", 260, "100 - F03 - GASTOS DE OFICINA", "MID - ND - COMPRA DE 13 GARRAFONES DE AGUA", 0, "DAVID"]
    assert fila[0].date() == date(2026, 11, 6) and hoja["A2"].number_format == "dd-mmm-yy"


def test_cp15_poliza_de_compra_tardia_toma_el_siguiente_numero():
    movs = [{"id": f"m{n}", "fecha_pago": f"2026-10-{n:02d}", "created_at": "2026-10-31", "poliza": n, "poliza_fija": True}
            for n in range(1, 13)]
    movs.append({"id": "tarde", "fecha_pago": "2026-10-05", "created_at": "2026-11-03", "poliza": None, "poliza_fija": False})
    assert R.asignar_polizas(movs) == [("tarde", None, 13)]
    assert R.texto_poliza("tcb", 13) == "TCB13"


def test_polizas_no_exportadas_se_numeran_por_fecha():
    movs = [{"id": "b", "fecha_pago": "2026-11-09", "poliza": 1, "poliza_fija": False},
            {"id": "a", "fecha_pago": "2026-11-02", "poliza": None, "poliza_fija": False}]
    assert R.asignar_polizas(movs) == [("a", None, 1), ("b", 1, 2)]


# ── Permisos ──────────────────────────────────────────

def _roles(path, metodo):
    from app.api import propago
    ruta = next(r for r in propago.router.routes if r.path == path and metodo in r.methods)
    return [c.cell_contents for d in ruta.dependant.dependencies for c in (d.call.__closure__ or ())]


def test_permisos_propago():
    from app.roles import ROLES_CONTABILIDAD
    assert ("admin",) in _roles("/propago/cajas", "POST")
    assert ("admin",) in _roles("/propago/cajas/{caja_id}/usuarios", "PUT")
    assert ROLES_CONTABILIDAD in _roles("/propago/exportar", "POST")
    assert ROLES_CONTABILIDAD in _roles("/propago/reglas", "POST")
    assert ROLES_CONTABILIDAD in _roles("/propago/sin-orden", "POST")


# ── Reglas de registro ERP (sección 3 y 6) ───────────

def test_fuera_de_lavanderia_cada_partida_va_en_su_renglon():
    p = pago(aplicaciones=[aplicacion(300, [
        {"concepto": "Hilo blanco", "importe": 100, "contrato": "17036"},
        {"concepto": "Hilo negro", "importe": 200, "contrato": "17036"},
    ])])
    r = MOTOR.generar(p)
    assert [(x["cargo"], x["ref"]) for x in r] == [
        (100.00, "MID - ND - COMPRA DE HILO BLANCO C17036 EL 13/10/26"),
        (200.00, "MID - ND - COMPRA DE HILO NEGRO C17036 EL 13/10/26"),
    ]


def test_subrenta_respeta_renta_de():
    p = pago(fecha="2026-10-30", aplicaciones=[aplicacion(500, [
        {"concepto": "Renta de pista", "importe": 500, "contrato": "17300"},
    ], proveedor=("dilgos", "Dilgos"))])
    (r,) = MOTOR.generar(p)
    assert r["ref"] == "[CDV] - MID - ND - PAGO DE RENTA DE PISTA A DILGOS C17300 EL 30/10/26"


def test_saldo_repite_el_texto_cambiando_la_palabra():
    p = pago(fecha="2026-10-09", aplicaciones=[aplicacion(1000, [
        {"concepto": "Servilletas lino arena", "cantidad": 77, "importe": 3080, "contrato": "17024"},
    ], proveedor=("guillermo", "Don Guillermo"), naturaleza="saldo")])
    (r,) = MOTOR.generar(p)
    assert r["ref"] == ("[CDV] - MID - ND - PAGO DE SALDO DE SUBRENTA DE 77 SERVILLETAS LINO ARENA A DON GUILLERMO "
                        "C17024 EL 09/10/26")


def test_corrige_la_ortografia_evidente():
    motor = R.Motor(REGLAS, CUENTAS, PLANTILLAS, {"correcciones": "THONER=TONER, REMBOLSO=REEMBOLSO"})
    (r,) = motor.generar(pago(lineas=[{"concepto": "Thoner para impresora", "importe": 850}]))
    assert r["ref"] == "MID - ND - COMPRA DE TONER PARA IMPRESORA EL 13/10/26"


def test_reglas_iniciales_salen_con_las_mismas_columnas():
    # PostgREST rechaza un lote cuyas filas tienen llaves distintas: así falló la primera carga.
    from app.api import propago
    from app.utils import propago_inicial as inicial
    cuentas = {c[0]: {"id": c[0]} for c in inicial.CUENTAS}
    plantillas = {p[0]: {"id": p[0]} for p in inicial.PLANTILLAS}
    filas = [propago._fila_regla(r, cuentas, plantillas) for r in inicial.REGLAS]
    assert len({tuple(sorted(f)) for f in filas}) == 1
    assert all(f["cuenta_id"] or f["fuerza_nd"] or f["desglosa_iva"] or f["plantilla_id"] for f in filas)
    assert len({f["nombre"] for f in filas}) == len(filas)
