"""
app/utils/email.py
------------------
Envío de correos usando Resend.
https://resend.com — gratis hasta 3,000 correos/mes

Uso:
    from app.utils.email import enviar_correo_autorizacion, enviar_correo_pago

PUNTO DE CONEXIÓN:
    Para cambiar el proveedor de correo (SendGrid, SES, etc.)
    solo reemplaza las funciones send_* manteniendo la misma firma.
"""

import resend
from app.config import settings

resend.api_key = settings.RESEND_API_KEY

# Correo desde el que se envían los mensajes
# En Resend debes verificar este dominio primero
REMITENTE = "ProCompra <onboarding@resend.dev>"

# Destinatarios fijos que autorizan pagos
AUTORIZADORES = ["dahmc2312@gmail.com"]


# ── Helpers ────────────────────────────────────────────
def _formatear_dinero(monto: float) -> str:
    return f"${monto:,.2f} MXN"


def _html_partidas(partidas: list[dict]) -> str:
    """Genera las filas de la tabla de partidas para el correo."""
    filas = ""
    for p in partidas:
        importe = p.get("importe") or p.get("cantidad", 0) * p.get("precio_unitario", 0)
        filas += f"""
        <tr>
            <td style="padding:8px;border-bottom:1px solid #eee">{p.get('cantidad')} {p.get('unidad')}</td>
            <td style="padding:8px;border-bottom:1px solid #eee">{p.get('concepto')}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;text-align:right">
                {_formatear_dinero(p.get('precio_unitario', 0))}
            </td>
            <td style="padding:8px;border-bottom:1px solid #eee;text-align:right">
                {_formatear_dinero(importe)}
            </td>
        </tr>
        """
    return filas


def _plantilla_base(titulo: str, contenido: str) -> str:
    """Envuelve el contenido en un HTML limpio con el estilo de ProCompra."""
    return f"""
    <!DOCTYPE html>
    <html lang="es">
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
    <body style="margin:0;padding:0;background:#F4F1EA;font-family:'Helvetica Neue',Arial,sans-serif">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr><td align="center" style="padding:32px 16px">
          <table width="600" cellpadding="0" cellspacing="0"
                 style="background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08)">

            <!-- Header -->
            <tr><td style="background:#26241D;padding:24px 32px">
              <span style="color:#fff;font-size:22px;font-weight:700;letter-spacing:-0.5px">
                MINIMAL 4.0
              </span>
              <span style="color:#A8A395;font-size:13px;margin-left:12px">ProCompra</span>
            </td></tr>

            <!-- Título -->
            <tr><td style="padding:28px 32px 0">
              <h1 style="margin:0;font-size:20px;color:#26241D;font-weight:700">{titulo}</h1>
            </td></tr>

            <!-- Contenido -->
            <tr><td style="padding:20px 32px">{contenido}</td></tr>

            <!-- Footer -->
            <tr><td style="padding:20px 32px;border-top:1px solid #EEEBE3;color:#A8A395;font-size:12px">
              MINIMAL ESTUDIO S.A. DE C.V. · RFC: MES151124J83<br>
              Calle 4-D 338 por 48 y 49, San Camilo, Kanasín, Yucatán<br>
              compras@minimalcuatrocero.com · 991 102 4893
            </td></tr>

          </table>
        </td></tr>
      </table>
    </body>
    </html>
    """


# ── Correos ────────────────────────────────────────────
def enviar_correo_autorizacion(orden: dict) -> bool:
    """
    Envía el correo de solicitud de autorización de pago
    a los autorizadores definidos en AUTORIZADORES.
    """
    folio      = orden.get("folio")
    proveedor  = orden.get("proveedor", {}).get("nombre", "—")
    total      = orden.get("total", 0)
    tipo_pago  = orden.get("tipo_pago", "—").replace("_", " ").upper()
    partidas   = orden.get("partidas", [])
    cuenta     = orden.get("proveedor", {}).get("cuenta_bancaria") or "—"
    rfc        = orden.get("proveedor", {}).get("rfc") or "—"
    obs        = orden.get("observaciones") or "—"

    contenido = f"""
    <p style="color:#5A5648;margin:0 0 20px">
        Se solicita autorización para el siguiente pago a proveedor.
    </p>

    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#F9F7F2;border-radius:8px;padding:16px;margin-bottom:20px">
        <tr>
            <td style="padding:4px 0;color:#8A8577;font-size:13px;width:140px">Orden</td>
            <td style="padding:4px 0;font-weight:700;font-size:15px">#{folio}</td>
        </tr>
        <tr>
            <td style="padding:4px 0;color:#8A8577;font-size:13px">Proveedor</td>
            <td style="padding:4px 0;font-weight:600">{proveedor}</td>
        </tr>
        <tr>
            <td style="padding:4px 0;color:#8A8577;font-size:13px">RFC</td>
            <td style="padding:4px 0">{rfc}</td>
        </tr>
        <tr>
            <td style="padding:4px 0;color:#8A8577;font-size:13px">Cuenta bancaria</td>
            <td style="padding:4px 0">{cuenta}</td>
        </tr>
        <tr>
            <td style="padding:4px 0;color:#8A8577;font-size:13px">Tipo de pago</td>
            <td style="padding:4px 0">{tipo_pago}</td>
        </tr>
        <tr>
            <td style="padding:4px 0;color:#8A8577;font-size:13px">Observaciones</td>
            <td style="padding:4px 0">{obs}</td>
        </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px">
        <thead>
            <tr style="background:#F4F1EA">
                <th style="padding:10px 8px;text-align:left;font-size:12px;color:#6B6659">CANT / UNIDAD</th>
                <th style="padding:10px 8px;text-align:left;font-size:12px;color:#6B6659">CONCEPTO</th>
                <th style="padding:10px 8px;text-align:right;font-size:12px;color:#6B6659">P. UNIT</th>
                <th style="padding:10px 8px;text-align:right;font-size:12px;color:#6B6659">IMPORTE</th>
            </tr>
        </thead>
        <tbody>{_html_partidas(partidas)}</tbody>
        <tfoot>
            <tr>
                <td colspan="3" style="padding:10px 8px;text-align:right;color:#6B6659;font-size:13px">
                    Subtotal
                </td>
                <td style="padding:10px 8px;text-align:right">
                    {_formatear_dinero(orden.get('subtotal', 0))}
                </td>
            </tr>
            <tr>
                <td colspan="3" style="padding:4px 8px;text-align:right;color:#6B6659;font-size:13px">
                    IVA 16%
                </td>
                <td style="padding:4px 8px;text-align:right">
                    {_formatear_dinero(orden.get('iva', 0))}
                </td>
            </tr>
            <tr style="border-top:2px solid #26241D">
                <td colspan="3"
                    style="padding:12px 8px;text-align:right;font-weight:700;font-size:15px">
                    TOTAL A PAGAR
                </td>
                <td style="padding:12px 8px;text-align:right;font-weight:700;font-size:15px;color:#C4462B">
                    {_formatear_dinero(total)}
                </td>
            </tr>
        </tfoot>
    </table>

    <p style="color:#5A5648;font-size:13px">
        Para autorizar ingresa al sistema y cambia el estado de la orden a
        <strong>Autorizada</strong>, o responde este correo con tu confirmación.
    </p>
    """

    try:
        resend.Emails.send({
            "from": REMITENTE,
            "to": AUTORIZADORES,
            "subject": f"Autorización de pago — OC #{folio} · {proveedor}",
            "html": _plantilla_base(f"Solicitud de autorización — OC #{folio}", contenido),
        })
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] autorizacion OC #{folio}: {e}")
        return False


def enviar_correo_pago(orden: dict, pago: dict, destinatarios: list[str]) -> bool:
    """
    Notifica al solicitante y almacén que el pago fue realizado
    y que deben recolectar los insumos o programar la entrega.
    """
    folio     = orden.get("folio")
    proveedor = orden.get("proveedor", {})
    partidas  = orden.get("partidas", [])

    lista_material = "".join(
        f"<li style='padding:4px 0'>{p.get('cantidad')} {p.get('unidad')} — {p.get('concepto')}</li>"
        for p in partidas
    )

    contenido = f"""
    <p style="color:#5A5648;margin:0 0 20px">
        Se realizó el pago de la orden <strong>#{folio}</strong>.
        Favor de recolectar los insumos o programar la entrega con el proveedor.
    </p>

    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#F9F7F2;border-radius:8px;padding:16px;margin-bottom:20px">
        <tr>
            <td style="padding:4px 0;color:#8A8577;font-size:13px;width:140px">Proveedor</td>
            <td style="padding:4px 0;font-weight:600">{proveedor.get('nombre','—')}</td>
        </tr>
        <tr>
            <td style="padding:4px 0;color:#8A8577;font-size:13px">Dirección</td>
            <td style="padding:4px 0">{proveedor.get('direccion','—')}</td>
        </tr>
        <tr>
            <td style="padding:4px 0;color:#8A8577;font-size:13px">Teléfono</td>
            <td style="padding:4px 0">{proveedor.get('telefono','—')}</td>
        </tr>
        <tr>
            <td style="padding:4px 0;color:#8A8577;font-size:13px">Fecha de pago</td>
            <td style="padding:4px 0">{pago.get('fecha_pago','—')}</td>
        </tr>
        <tr>
            <td style="padding:4px 0;color:#8A8577;font-size:13px">Referencia</td>
            <td style="padding:4px 0;font-weight:600">{pago.get('referencia','—')}</td>
        </tr>
        <tr>
            <td style="padding:4px 0;color:#8A8577;font-size:13px">Monto pagado</td>
            <td style="padding:4px 0;font-weight:700;color:#2E6B4F">
                {_formatear_dinero(pago.get('monto', 0))}
            </td>
        </tr>
    </table>

    <p style="color:#26241D;font-weight:600;margin:0 0 8px">Material a recibir:</p>
    <ul style="margin:0 0 20px;padding-left:20px;color:#5A5648;font-size:14px">
        {lista_material}
    </ul>

    <p style="color:#5A5648;font-size:13px">
        Registra en el sistema la fecha de recolección o entrega
        y el nombre del responsable.
    </p>
    """

    try:
        resend.Emails.send({
            "from": REMITENTE,
            "to": destinatarios,
            "subject": f"Pago realizado — OC #{folio} · recolectar / programar entrega",
            "html": _plantilla_base(f"Pago realizado — OC #{folio}", contenido),
        })
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] pago OC #{folio}: {e}")
        return False