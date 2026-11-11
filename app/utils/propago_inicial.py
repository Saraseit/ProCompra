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
    ("GENERAL", "{VERBO} DE {CONCEPTO}", "La que se usa si ninguna regla pide otra"),
    ("CON_PROVEEDOR", "{VERBO} DE {CONCEPTO}[ A {PROVEEDOR}]", "Agrega el proveedor al final"),
    ("TCB", "COMPRA DE {CONCEPTO}", "Tarjeta de compras"),
    ("LAVANDERIA", "PAGO DE LAVANDERIA DE {ITEMS} A {PROVEEDOR}", "Cantidades y conceptos de la remisión"),
    ("SUBRENTA", "PAGO DE SUBRENTA DE {ITEMS} A {PROVEEDOR}", "Subrentas (C06)"),
    ("DEPTO360", "PAGO DE {CONCEPTO} A {PROVEEDOR}[ {DOC}]", "Departamento 360; DOC es la serie y folio de la factura"),
    ("FLETE", "PAGO DE FLETE {CONCEPTO} A {PROVEEDOR}", "Fletes programados"),
    ("REEMBOLSO", "REEMBOLSO A {PROVEEDOR} POR {CONCEPTO}", "En pagos sin orden, PROVEEDOR es la persona"),
    ("CAJA_CUN", "COMPRA DE {CONCEPTO}[ EN {PROVEEDOR}]", "Tickets del fondo de Cancún"),
    ("MYD_CUN", "PAGO {CONCEPTO}", "Montaje y desmontaje en Cancún: «MYD 2 PAX EVENTO …»"),
]

# (nombre, prioridad, palabras, palabras_excluir, cuenta, categoria, plantilla, extras)
REGLAS = [
    ("Subrentas", 10, ["SUBRENTA", "RENTA DE PISTA"], [], "C06", None, "SUBRENTA", {}),
    ("Retiro de utilidad", 10, ["RETIRO DE UTILIDAD", "REPARTO DE UTILIDAD"], [], "E09", None, None, {}),
    ("Montaje y desmontaje", 10, ["MYD", "PAX", "MONTAJE", "DESMONTAJE"], [], "A12", None, "MYD_CUN", {}),
    ("Fletes", 10, ["FLETE", "FLETES"], [], "A03", "#LOG", "FLETE", {}),
    ("Lavandería", 10, ["LAVANDERIA", "LAVADO"], [], "D03", "LAVADO", "LAVANDERIA", {}),
    ("Tickets de Cancún sin factura", 15, [], [], None, None, None,
     {"plaza": "CUN", "con_cfdi": False, "fuerza_nd": True}),
    ("Gastos de oficina", 15, ["GARRAFON", "GARRAFONES", "PAPELERIA", "TONER", "LIMPIEZA"], [], "F03", None, None, {}),
    ("Telas", 20, ["TELA", "TELAS", "TERGAL", "TAFETAN", "MANTA", "BOMBAY", "MAGALY", "LICRA"], ["SERVILLETA", "SERVILLETAS"],
     "A09.1", "TELA", None, {}),
    ("Pintura", 20, ["PINTURA", "THINNER", "TINTA", "MANCHADOR", "BERELINTE"], [], "A09.1", "PINTURA", None, {}),
    ("Flores", 20, ["FLOR", "FLORES", "ESPUMA FLORAL", "OASIS"], [], "B02", None, None, {}),
    ("Hidratación y alimentos en servicio", 20, ["HIELO", "AGUA", "HIDRATACION", "REFRESCO", "REFRESCOS", "ALIMENTO", "ALIMENTOS", "COMIDA"],
     ["GARRAFON", "GARRAFONES"], "A11.2", None, None, {}),
    ("Insumos de preparación", 30, ["HILO", "LISTON", "SILICON", "TORNILLO", "TORNILLOS", "TORNILLERIA", "MASKING", "ESTOPA",
                                    "CONTACTO", "CONTACTOS", "SOPORTE", "SOPORTES"], [], "A09.2", None, None, {}),
    ("Traslados, casetas y combustible", 30, ["UBER", "DIDI", "CASETA", "CASETAS", "GASOLINA", "COMBUSTIBLE"], [], "A10", None, None, {}),
    ("Producción tercerizada", 30, ["VINIL", "HERRERIA", "CARPINTERIA", "TORRE", "TORRES", "ESTRUCTURA", "ESTRUCTURAS", "FABRICACION"],
     [], "B01", None, None, {}),
    ("Servicios públicos", 40, ["TELEFONO", "TELMEX", "TELCEL", "INTERNET", "CFE", "ENERGIA ELECTRICA", "BASURA"], [], "F02", None, None, {}),
    ("Personal", 40, ["FESTEJO", "PREMIO", "PREMIOS", "UNIFORME", "UNIFORMES"], [], "E06", None, None, {}),
    ("Marketing", 40, ["OBSEQUIO", "OBSEQUIOS", "HOSTING", "SUSCRIPCION"], [], "C03", None, None, {}),
    ("Permisos de unidades", 40, ["PERMISO", "PERMISOS", "BOB"], [], "F04", None, None, {}),
    ("Mantenimiento de unidades", 40, ["FRENOS", "AFINACION", "LLANTA", "LLANTAS"], [], "A06", None, None, {}),
    ("Renta de unidades", 40, ["RENTA DE CAMIONETA", "RENTA DE UNIDAD", "RENTA DE CAMION"], [], "A07", None, None, {}),
    ("Mermas", 50, ["MERMA", "MERMAS", "PERDIDA"], [], "A02", None, None, {}),
]

# (clave, valor, descripcion)
PARAMETROS = [
    ("cuenta_comisiones", "", "Cuenta para comisiones (OXXO, SPEI). Vacía: se suman al renglón principal."),
    ("cuenta_diferencias", "", "Cuenta para propinas y redondeos. Vacía: se suman al renglón principal."),
    ("aviso_diferencia_monto", "50", "Avisa si la diferencia entre comprobante y banco pasa de este monto."),
    ("aviso_diferencia_pct", "0.05", "…o de este porcentaje del pago (0.05 = 5 %)."),
]
