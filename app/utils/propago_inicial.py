"""
app/utils/propago_inicial.py
----------------------------
Carga inicial del catálogo contable de ProPago (especificación 1.1, §8).

Se inserta desde la pantalla (Catálogo contable → Cargar catálogo inicial),
no con una migración, y solo lo que falte: lo que ya se editó no se toca.
"""

# (clave, nombre_erp, grupo, tipo, cdv, verificada_erp)
CUENTAS = [
    ("A02", "100 - A02 - COSTO POR PERDIDAS", "A", "gasto", False, False),
    ("A03", "100 - A03 - FLETES TERCERIZADOS PROGRAMADOS", "A", "gasto", False, False),
    ("A06", "100 - A06 - FLOTILLA-MANTENIMIENTO", "A", "gasto", False, True),
    ("A07", "100 - A07 - FLOTILLA - ARRENDAMIENTOS", "A", "gasto", False, False),
    ("A09.1", "100 - A09.1 - INSUMOS Y GASTOS EN PREPARACION DE PEDIDOS", "A", "gasto", False, True),
    ("A09.2", "100 - A09.2 - OTROS INSUMOS O SERVICIOS EN PREPARACION DE PEDIDOS", "A", "gasto", False, True),
    ("A10", "100 - A10 - OTROS LOGISTICA Y DISTRIBUCION", "A", "gasto", False, True),
    ("A11.2", "100 - A11.2 - ALIMENTO EN HORAS EXTRAS A PERSONAL EN SERVICIO", "A", "gasto", False, False),
    ("A12", "100 - A12 - TERCERIZACION MONTAJE/DESMONTAJE", "A", "gasto", False, True),
    ("B01", "100 - B01 - SUB-CONTRATACION", "B", "gasto", True, False),
    ("B02", "100 - B02 - PRODUCCION FLORAL", "B", "gasto", True, True),
    ("B04", "100 - B04 - MATERIA PRIMA", "B", "gasto", True, False),
    ("C03", "100 - C03 - MARKETING", "C", "gasto", False, True),
    ("C04", "100 - C04 - VIATICOS Y REEMBOLSOS DE VENTA", "C", "gasto", False, False),
    ("C06", "100 - C06 - SUB-ARRENDAMIENTO", "C", "gasto", True, True),
    ("D02", "100 - D02 - INSUMOS", "D", "gasto", False, True),
    ("D03", "100 - D03 - SUB-CONTRATACION MANTTO GENERAL INVENTARIO", "D", "gasto", False, True),
    ("E06", "100 - E06 - GASTOS GRAL RH", "E", "gasto", False, True),
    ("E09", "100 - E09 - REPARTO DE UTILIDAD", "E", "gasto", False, False),
    ("F02", "100 - F02 - SERVICIOS PUBLICOS", "F", "gasto", False, True),
    ("F03", "100 - F03 - GASTOS DE OFICINA", "F", "gasto", False, True),
    ("F04", "100 - F04 - PERMISOS Y DERECHOS", "F", "gasto", False, False),
    ("F05", "100 - F05 - VIATICOS Y REEMBOLSO OFICINA", "F", "gasto", False, True),
    ("F06", "100 - F06 - MOBILIARIO Y EQUIPO DE OFICINA", "F", "gasto", False, False),
    ("G01", "100 - G01 - EQUIPO NUEVO (MOBILIARIO)", "G", "gasto", False, True),
    ("G02", "100 - G02 - HERRAMIENTAS MAYORES", "G", "gasto", False, False),
    ("G05", "100 - G05 - INSTALACION EN INMUEBLE", "G", "gasto", False, False),
    ("H01", "100 - H01 - IVA POR ACREDITAR", "H", "iva", False, True),
    ("I04", "100 - I04 - INTERESES POR CRÉDITOS Y FINANCIAMIENTOS", "I", "gasto", False, False),
]

# (codigo, plantilla, descripcion)
PLANTILLAS = [
    ("GENERAL", "{VERBO} DE {CONCEPTO}", "La que se usa si ninguna regla pide otra: COMPRA DE para bienes"),
    ("PAGO_GENERAL", "PAGO DE {CONCEPTO}[ A {PROVEEDOR}]", "Servicios, rentas, fabricación, permisos"),
    ("CON_PROVEEDOR", "{VERBO} DE {CONCEPTO}[ A {PROVEEDOR}]", "Agrega el proveedor al final"),
    ("TCB", "COMPRA DE {CONCEPTO}", "Tarjeta de compras"),
    ("LAVANDERIA", "PAGO DE LAVANDERIA DE {ITEMS}[ A {PROVEEDOR}]", "Cantidades y conceptos de la remisión"),
    ("SUBRENTA", "PAGO DE SUBRENTA DE {ITEMS}[ A {PROVEEDOR}]", "Subrentas (C06)"),
    ("DEPTO360", "PAGO DE {CONCEPTO}[ A {PROVEEDOR}][ {DOC}]", "Departamento 360; DOC es la serie y folio de la factura"),
    ("FLETE", "PAGO DE FLETE {CONCEPTO}[ A {PROVEEDOR}]", "Fletes programados"),
    ("REEMBOLSO", "REEMBOLSO A {PROVEEDOR} POR {CONCEPTO}", "En pagos sin orden, PROVEEDOR es la persona"),
    ("CAJA_CUN", "COMPRA DE {CONCEPTO}[ EN {PROVEEDOR}]", "Tickets del fondo de Cancún"),
    ("MYD_CUN", "PAGO {CONCEPTO}", "Montaje y desmontaje en Cancún: «MYD 2 PAX EVENTO …»"),
]

def _r(nombre, prioridad, palabras, cuenta=None, categoria=None, plantilla=None, excluir=(), **extras):
    return {"nombre": nombre, "prioridad": prioridad, "palabras": list(palabras), "palabras_excluir": list(excluir),
            "cuenta": cuenta, "categoria": categoria, "plantilla": plantilla, **extras}


# Reglas de cuenta por concepto (reglas de registro ERP, sección 8). La cuenta
# que se elija en el pago siempre manda; esto solo la propone.
REGLAS = [
    _r("Mermas y reposiciones a proveedores", 8, ["MERMA", "MERMAS", "PERDIDA", "REPOSICION", "REPOSICION DE FUNDAS"], "A02",
       plantilla="PAGO_GENERAL"),
    _r("Renta de unidades", 8, ["RENTA DE CAMIONETA", "RENTA DE UNIDAD", "RENTA DE CAMION", "CAMIONETA ESTACA", "HILUX"], "A07",
       plantilla="PAGO_GENERAL", excluir=["GASOLINA", "COMBUSTIBLE"]),
    _r("Subrentas", 10, ["SUBRENTA", "RENTA DE"], "C06", plantilla="SUBRENTA"),
    _r("Retiro de utilidad", 10, ["RETIRO DE UTILIDAD", "REPARTO DE UTILIDAD"], "E09"),
    _r("Montaje y desmontaje", 10, ["MYD", "PAX", "MONTAJE", "DESMONTAJE"], "A12", plantilla="MYD_CUN"),
    _r("Fletes", 10, ["FLETE", "FLETES"], "A03", "#LOG", "FLETE"),
    _r("Lavandería", 10, ["LAVANDERIA", "LAVADO"], "D03", "LAVADO", "LAVANDERIA"),
    _r("Intereses y cargos financieros", 10, ["INTERES", "INTERESES", "FLEX PRICING", "PAGO EXTEMPORANEO"], "I04",
       plantilla="PAGO_GENERAL"),
    _r("Florista y limpieza de flores", 12, ["AUXILIAR FLORISTA", "FLORISTA", "LIMPIEZA DE FLORES"], "B02", plantilla="PAGO_GENERAL"),
    _r("Herramientas mayores y lámparas de bodega", 14, ["LIJADORA", "TALADRO", "DESTORNILLADOR DE IMPACTO",
                                                         "INSTALACION DE LAMPARAS", "CAMBIO DE LAMPARAS"], "G02"),
    _r("Instalaciones en inmueble", 14, ["CLIMA", "MINISPLIT", "AIRE ACONDICIONADO"], "G05", plantilla="PAGO_GENERAL"),
    _r("Mobiliario y equipo de oficina", 14, ["REFLECTOR", "REFLECTORES"], "F06"),
    _r("Tickets de Cancún sin factura", 15, [], plaza="CUN", con_cfdi=False, fuerza_nd=True),
    _r("Gastos de oficina", 15, ["GARRAFON", "GARRAFONES", "PAPELERIA", "TONER", "JARDINERIA", "PASTO", "GOTERA", "GOTERAS",
                                 "LIMPIEZA", "SUSCRIPCION CLAUDE", "CLAUDE AI"], "F03"),
    _r("Personal y RRHH", 16, ["CUMPLEANOS", "FESTEJO", "FESTEJOS", "PIBES", "PREMIO", "PREMIOS", "SERIGRAFIA", "PLAYERAS",
                               "UNIFORME", "UNIFORMES", "COMEDOR", "PERCOLADORA", "REEMBOLSO MEDICO"], "E06"),
    _r("Marketing", 16, ["OBSEQUIO", "OBSEQUIOS", "DULCES PARA CLIENTE", "PASTEL PARA CLIENTE", "CONGRESO", "FOLLETO", "FOLLETOS",
                         "TARJETAS DE PRESENTACION", "HOSTING", "SUSCRIPCION", "FAM TRIP"], "C03"),
    _r("Viáticos de dirección", 18, ["TREN MAYA", "HOSPEDAJE", "VUELO", "VUELOS", "VIATICOS"], "F05", plantilla="PAGO_GENERAL",
       excluir=["FAM TRIP"]),
    _r("Telas", 20, ["TELA", "TELAS", "TERGAL", "TAFETAN", "MANTA", "BOMBAY", "MAGALY", "LICRA", "TAPICERIA"], "A09.1", "TELA",
       excluir=["SERVILLETA", "SERVILLETAS"]),
    _r("Pintura", 20, ["PINTURA", "THINNER", "TINTA", "TINTAS", "MANCHADOR", "BERELINTE", "AEROSOL", "AEROSOLES"], "A09.1", "PINTURA"),
    _r("Flores", 20, ["FLOR", "FLORES", "ESPUMA FLORAL", "OASIS", "ARREGLO FLORAL", "EUCALIPTO", "HORTENSIAS", "RANUNCULOS",
                      "ROSAS", "LISIANTHUS"], "B02"),
    _r("Hidratación y alimentos en servicio", 20, ["HIELO", "AGUA", "HIDRATACION", "REFRESCO", "REFRESCOS", "BEBIDA", "BEBIDAS",
                                                   "ALIMENTO", "ALIMENTOS", "COMIDA"], "A11.2", excluir=["GARRAFON", "GARRAFONES"]),
    _r("Insumos de preparación", 30, ["HILO", "LISTON", "SILICON", "LENTEJUELA", "ALFILER", "ALFILERES", "TORNILLO", "TORNILLOS",
                                      "TORNILLERIA", "PIJA", "PIJAS", "ESCUADRA", "ESCUADRAS", "GRAPA", "GRAPAS", "LIJA", "LIJAS",
                                      "MASKING", "ESTOPA", "CONTACTO", "CONTACTOS", "SOPORTE", "SOPORTES", "MEMORIA USB", "ESPUMA"],
       "A09.2"),
    _r("Traslados, casetas y combustible", 30, ["UBER", "DIDI", "CASETA", "CASETAS", "PEAJE", "GASOLINA", "COMBUSTIBLE",
                                                "TABULADOR"], "A10", plantilla="PAGO_GENERAL"),
    _r("Producción tercerizada", 30, ["VINIL", "VINILES", "HERRERIA", "CARPINTERIA", "TORRE", "TORRES", "ESTRUCTURA", "ESTRUCTURAS",
                                      "FRENTE DE BARRA", "FABRICACION", "REPARACION DE TARIMA"], "B01", plantilla="PAGO_GENERAL"),
    _r("Equipo nuevo de inventario", 30, ["EQUIPO NUEVO", "COPAS METALICAS"], "G01"),
    _r("Servicios públicos", 40, ["TELEFONO", "TELEFONIA", "TELMEX", "TELCEL", "INTERNET", "CFE", "ENERGIA ELECTRICA", "BASURA"],
       "F02", plantilla="PAGO_GENERAL"),
    _r("Permisos de unidades", 40, ["PERMISO", "PERMISOS", "BOB"], "F04", plantilla="PAGO_GENERAL"),
    _r("Mantenimiento de unidades", 40, ["FRENOS", "AFINACION", "SERVICIO DE UNIDAD", "LLANTA", "LLANTAS"], "A06",
       plantilla="PAGO_GENERAL"),
]

# Reglas por proveedor: se crean solo para los proveedores dados de alta cuyo
# nombre contenga alguno de estos textos.
REGLAS_PROVEEDOR = [
    (["DILGO", "DON GUILLERMO", "PASTOR", "VERO LARA", "LEPAYU", "RENTIT"],
     {"prioridad": 9, "cuenta": "C06", "plantilla": "SUBRENTA"}, "Subrenta"),
    (["WILBERT", "LAVANDERIA 100"], {"prioridad": 9, "cuenta": "D03", "categoria": "LAVADO", "plantilla": "LAVANDERIA"}, "Lavandería"),
    (["DEPARTAMENTO 360"], {"prioridad": 9, "plantilla": "DEPTO360"}, "Texto de Departamento 360"),
]

# Reglas por caja: se crean para las cajas que existan de ese tipo.
REGLAS_CAJA = [
    (lambda c: c.get("tipo") == "amex", "IVA desglosado en AMEX",
     {"prioridad": 5, "desglosa_iva": True,
      "palabras": ["COMBUSTIBLE", "GASOLINA", "TELMEX", "CFE", "GROWTH INSTITUTE", "CAPACITACION", "VIVAAEROBUS",
                   "CITY EXPRESS", "FLEX PRICING"]}),
    (lambda c: bool(c.get("prefijo_poliza")), "Texto de la tarjeta de compras", {"prioridad": 5, "plantilla": "TCB"}),
    (lambda c: c.get("tipo") == "fondo_fijo", "Texto del fondo de Cancún", {"prioridad": 5, "plantilla": "CAJA_CUN", "fuerza_nd": True}),
]

# (clave, valor, descripcion)
PARAMETROS = [
    ("cuenta_comisiones", "", "Cuenta para comisiones (OXXO, SPEI). Vacía: se suman al renglón principal."),
    ("cuenta_diferencias", "", "Cuenta para propinas y redondeos. Vacía: se suman al renglón principal."),
    ("aviso_diferencia_monto", "50", "Avisa si la diferencia entre comprobante y banco pasa de este monto."),
    ("aviso_diferencia_pct", "0.05", "…o de este porcentaje del pago (0.05 = 5 %)."),
    ("plantillas_agrupan", "LAVANDERIA,DEPTO360",
     "Plantillas que juntan en un renglón los conceptos del mismo contrato (códigos separados por coma). "
     "Las demás van una partida por renglón."),
    ("correcciones", "THONER=TONER, REMBOLSO=REEMBOLSO, CLOBO=GLOBO, EUCALIPO=EUCALIPTO, HORTENCIAS=HORTENSIAS, "
                     "RANUCULOS=RANUNCULOS, SUPERVICION=SUPERVISION",
     "Ortografía que se corrige sola en el texto: MAL=BIEN, separadas por coma."),
]
