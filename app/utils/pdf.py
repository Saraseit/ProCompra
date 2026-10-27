"""
app/utils/pdf.py
----------------
Genera el PDF de una orden de compra con reportlab.

Se eligió reportlab sobre WeasyPrint porque no necesita GTK/Pango como
paquetes del sistema, que Railway no trae en la imagen por defecto.
"""

from datetime import date
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

TINTA      = colors.HexColor("#26241D")
TENUE      = colors.HexColor("#8A8577")
BORDE      = colors.HexColor("#E3DFD5")
FONDO      = colors.HexColor("#F4F1EA")
ACENTO     = colors.HexColor("#C4462B")

MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre")

ETIQUETAS_PAGO = {
    "transferencia":   "Transferencia",
    "tarjeta_credito": "Tarjeta de crédito",
    "tarjeta_debito":  "Tarjeta de débito",
    "efectivo":        "Efectivo",
    "credito":         "Crédito del proveedor",
}

ETIQUETAS_ESTADO = {
    "borrador":     "Borrador",
    "autorizacion": "En autorización",
    "autorizada":   "Autorizada",
    "pagada":       "Pagada",
    "recoleccion":  "Por recolectar",
    "comprobacion": "Por comprobar",
    "cerrada":      "Cerrada",
    "rechazada":    "Cancelada",
}


def _dinero(valor) -> str:
    return f"${float(valor or 0):,.2f}"


def _fecha_larga(valor) -> str:
    if not valor:
        return "—"
    try:
        d = date.fromisoformat(str(valor)[:10])
    except ValueError:
        return str(valor)
    return f"{d.day} de {MESES[d.month - 1]} de {d.year}"


def _estilos():
    base = getSampleStyleSheet()["Normal"]
    return {
        "normal": ParagraphStyle("n", parent=base, fontSize=9, leading=12, textColor=TINTA),
        "tenue":  ParagraphStyle("t", parent=base, fontSize=8, leading=11, textColor=TENUE),
        "titulo": ParagraphStyle("ti", parent=base, fontSize=20, leading=23,
                                 textColor=TINTA, fontName="Helvetica-Bold"),
        "marca":  ParagraphStyle("m", parent=base, fontSize=9, leading=12,
                                 textColor=ACENTO, fontName="Helvetica-Bold"),
        "rotulo": ParagraphStyle("r", parent=base, fontSize=7, leading=10,
                                 textColor=TENUE, fontName="Helvetica-Bold"),
        "derecha": ParagraphStyle("d", parent=base, fontSize=9, leading=12,
                                  textColor=TINTA, alignment=TA_RIGHT),
    }


def _bloque_datos(titulo: str, lineas: list[tuple[str, str]], est, ancho) -> Table:
    filas = [[Paragraph(titulo, est["rotulo"]), ""]]
    for etiqueta, valor in lineas:
        filas.append([
            Paragraph(etiqueta, est["tenue"]),
            Paragraph(valor or "—", est["normal"]),
        ])
    t = Table(filas, colWidths=[ancho * 0.34, ancho * 0.66])
    t.setStyle(TableStyle([
        ("SPAN",         (0, 0), (1, 0)),
        ("BACKGROUND",   (0, 0), (-1, 0), FONDO),
        ("BOX",          (0, 0), (-1, -1), 0.6, BORDE),
        ("LINEBELOW",    (0, 0), (-1, 0), 0.6, BORDE),
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING",   (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def generar_pdf_orden(orden: dict) -> BytesIO:
    """Arma el PDF de una orden ya cargada con proveedor, creador y partidas."""
    buffer = BytesIO()
    folio = orden.get("folio")
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Orden de compra {folio}",
        author="ProCompra · Minimal 4.0",
    )
    est = _estilos()
    ancho = doc.width
    hist = []

    # ── Encabezado ────────────────────────────────────────
    encabezado = Table(
        [[
            [Paragraph("MINIMAL 4.0", est["marca"]),
             Paragraph("ORDEN DE COMPRA", est["titulo"])],
            [Paragraph(f"<b>Folio #{folio}</b>", est["derecha"]),
             Paragraph(_fecha_larga(orden.get("fecha")), est["derecha"]),
             Paragraph(ETIQUETAS_ESTADO.get(orden.get("estado"), orden.get("estado") or "—"),
                       est["derecha"])],
        ]],
        colWidths=[ancho * 0.55, ancho * 0.45],
    )
    encabezado.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    hist.append(encabezado)
    hist.append(Spacer(1, 6))
    linea = Table([[""]], colWidths=[ancho], rowHeights=[1.4])
    linea.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), TINTA)]))
    hist.append(linea)
    hist.append(Spacer(1, 12))

    # ── Proveedor y receptor ──────────────────────────────
    prov = orden.get("proveedor") or {}
    creador = orden.get("creador") or {}
    col = (ancho - 8) / 2
    bloques = Table(
        [[
            _bloque_datos("PROVEEDOR", [
                ("Nombre",   prov.get("nombre")),
                ("RFC",      prov.get("rfc")),
                ("Dirección", prov.get("direccion")),
                ("Teléfono", prov.get("telefono")),
                ("Correo",   prov.get("correo")),
                ("Cuenta",   prov.get("cuenta_bancaria")),
            ], est, col),
            _bloque_datos("RECEPTOR", [
                ("Empresa",   "Minimal 4.0"),
                ("Ciudad",    "Mérida, Yucatán"),
                ("Solicitó",  creador.get("nombre")),
                ("Correo",    creador.get("correo")),
                ("Tipo de pago", ETIQUETAS_PAGO.get(orden.get("tipo_pago"),
                                                    orden.get("tipo_pago"))),
            ], est, col),
        ]],
        colWidths=[col, col],
    )
    bloques.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (-1, 0), (-1, 0), 0),
    ]))
    hist.append(bloques)
    hist.append(Spacer(1, 14))

    # ── Partidas ──────────────────────────────────────────
    filas = [["CANT.", "UNIDAD", "CONCEPTO", "P. UNITARIO", "IMPORTE"]]
    for p in orden.get("partidas") or []:
        cantidad = float(p.get("cantidad") or 0)
        filas.append([
            f"{cantidad:g}",
            p.get("unidad") or "—",
            Paragraph(p.get("concepto") or "—", est["normal"]),
            _dinero(p.get("precio_unitario")),
            _dinero(p.get("importe")),
        ])

    anchos = [ancho * 0.09, ancho * 0.12, ancho * 0.47, ancho * 0.16, ancho * 0.16]
    tabla = Table(filas, colWidths=anchos, repeatRows=1)
    tabla.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), FONDO),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.HexColor("#6B6659")),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 7),
        ("FONTSIZE",      (0, 1), (-1, -1), 9),
        ("TEXTCOLOR",     (0, 1), (-1, -1), TINTA),
        ("ALIGN",         (0, 0), (1, -1), "CENTER"),
        ("ALIGN",         (3, 0), (-1, -1), "RIGHT"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("BOX",           (0, 0), (-1, -1), 0.6, BORDE),
        ("INNERGRID",     (0, 0), (-1, -1), 0.4, BORDE),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    hist.append(tabla)
    hist.append(Spacer(1, 10))

    # ── Totales ───────────────────────────────────────────
    totales = Table(
        [
            ["Subtotal", _dinero(orden.get("subtotal"))],
            ["IVA 16%",  _dinero(orden.get("iva"))],
            ["TOTAL",    _dinero(orden.get("total"))],
        ],
        colWidths=[ancho * 0.19, ancho * 0.16],
        hAlign="RIGHT",
    )
    totales.setStyle(TableStyle([
        ("ALIGN",      (0, 0), (-1, -1), "RIGHT"),
        ("FONTSIZE",   (0, 0), (-1, -1), 9),
        ("TEXTCOLOR",  (0, 0), (0, 1), TENUE),
        ("FONTNAME",   (0, 2), (-1, 2), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 2), (-1, 2), 11),
        ("LINEABOVE",  (0, 2), (-1, 2), 0.8, TINTA),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    hist.append(totales)

    # ── Observaciones ─────────────────────────────────────
    if orden.get("observaciones"):
        hist.append(Spacer(1, 14))
        hist.append(KeepTogether([
            Paragraph("OBSERVACIONES", est["rotulo"]),
            Spacer(1, 4),
            Paragraph(orden["observaciones"], est["normal"]),
        ]))

    # ── Firmas ────────────────────────────────────────────
    hist.append(Spacer(1, 30))
    firmas = Table(
        [["", "", ""],
         [Paragraph("Solicitó", est["tenue"]),
          Paragraph("Autorizó", est["tenue"]),
          Paragraph("Recibió", est["tenue"])]],
        colWidths=[ancho / 3] * 3,
        rowHeights=[26, 14],
    )
    firmas.setStyle(TableStyle([
        ("LINEBELOW",   (0, 0), (-1, 0), 0.6, BORDE),
        ("ALIGN",       (0, 0), (-1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 18),
        ("RIGHTPADDING", (0, 0), (-1, -1), 18),
    ]))
    hist.append(firmas)

    def pie(canvas, documento):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(TENUE)
        canvas.drawString(
            documento.leftMargin, 10 * mm,
            f"ProCompra · Minimal 4.0 · Orden #{folio}",
        )
        canvas.drawRightString(
            documento.pagesize[0] - documento.rightMargin, 10 * mm,
            f"Página {canvas.getPageNumber()}",
        )
        canvas.restoreState()

    doc.build(hist, onFirstPage=pie, onLaterPages=pie)
    buffer.seek(0)
    return buffer
