"""
app/utils/email.py
------------------
Envío de correos usando Resend.
https://resend.com — gratis hasta 3,000 correos/mes

Uso:
    from app.utils.email import enviar
    (los avisos del flujo de órdenes están en app/utils/notificaciones.py)

PUNTO DE CONEXIÓN:
    Para cambiar el proveedor de correo (SendGrid, SES, etc.)
    solo reemplaza las funciones send_* manteniendo la misma firma.
"""

import html
import resend
from app.config import settings

resend.api_key = settings.RESEND_API_KEY

# Correo desde el que se envían los mensajes. El remitente de pruebas de
# Resend (onboarding@resend.dev) solo entrega al dueño de la cuenta: para
# avisar al equipo hay que verificar un dominio en Resend y fijar
# EMAIL_REMITENTE, p. ej. "ProCompra <compras@minimalcuatrocero.com>".
REMITENTE = settings.EMAIL_REMITENTE


# ── Helpers ────────────────────────────────────────────
def _esc(valor) -> str:
    """Escapa valores controlados por el usuario antes de insertarlos en HTML."""
    return html.escape(str(valor)) if valor is not None else ""


def _formatear_dinero(monto: float) -> str:
    return f"${monto:,.2f} MXN"


def _html_partidas(partidas: list[dict]) -> str:
    """Genera las filas de la tabla de partidas para el correo."""
    filas = ""
    for p in partidas:
        importe = p.get("importe") or p.get("cantidad", 0) * p.get("precio_unitario", 0)
        filas += f"""
        <tr>
            <td style="padding:8px;border-bottom:1px solid #eee">{_esc(p.get('cantidad'))} {_esc(p.get('unidad'))}</td>
            <td style="padding:8px;border-bottom:1px solid #eee">{_esc(p.get('concepto'))}</td>
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


# ── Envío ──────────────────────────────────────────────
def enviar(destinatarios: list[str], asunto: str, titulo: str, contenido: str) -> bool:
    """Envía un correo con la plantilla de ProCompra.

    Nunca lanza: un correo que falla no debe tumbar la operación que lo
    originó. Sin RESEND_API_KEY configurada simplemente no envía.
    """
    destinatarios = sorted({d for d in destinatarios if d})
    if not settings.RESEND_API_KEY or not destinatarios:
        return False
    try:
        resend.Emails.send({
            "from": REMITENTE,
            "to": destinatarios,
            "subject": asunto,
            "html": _plantilla_base(titulo, contenido),
        })
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {asunto}: {e}")
        return False
