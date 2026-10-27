"""
app/utils/notificaciones.py
---------------------------
Avisos por correo en los momentos del flujo en que alguien tiene que actuar.

Se ejecutan como BackgroundTasks de FastAPI: la respuesta al usuario no
espera al correo y un fallo de envío no afecta la operación.
"""

from app.config import settings
from app.database import supabase
from app.utils.email import enviar, _esc, _formatear_dinero, _html_partidas

ROLES_CONTABILIDAD = ("admin", "pagos")


def _correos_por_rol(roles) -> list[str]:
    filas = (
        supabase.table("usuarios")
        .select("correo")
        .in_("rol", list(roles))
        .eq("activo", True)
        .execute()
    ).data or []
    return [f["correo"] for f in filas]


def _correo_de(usuario_id: str) -> list[str]:
    if not usuario_id:
        return []
    filas = (
        supabase.table("usuarios")
        .select("correo, activo")
        .eq("id", usuario_id)
        .execute()
    ).data or []
    return [f["correo"] for f in filas if f.get("activo")]


def _boton(texto: str) -> str:
    url = (settings.FRONTEND_URL or "").rstrip("/")
    if not url or "localhost" in url:
        return ""
    return (
        f'<p style="margin:24px 0 0"><a href="{_esc(url)}" '
        'style="background:#26241D;color:#fff;text-decoration:none;padding:11px 18px;'
        f'border-radius:9px;font-weight:600;font-size:14px">{_esc(texto)}</a></p>'
    )


def _resumen(orden: dict) -> str:
    prov = orden.get("proveedor") or {}
    return f"""
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#F9F7F2;border-radius:8px;padding:16px;margin-bottom:16px">
      <tr><td style="padding:4px 0;color:#8A8577;font-size:13px;width:140px">Orden</td>
          <td style="padding:4px 0;font-weight:700">#{_esc(orden.get('folio'))}</td></tr>
      <tr><td style="padding:4px 0;color:#8A8577;font-size:13px">Proveedor</td>
          <td style="padding:4px 0;font-weight:600">{_esc(prov.get('nombre') or '—')}</td></tr>
      <tr><td style="padding:4px 0;color:#8A8577;font-size:13px">Total</td>
          <td style="padding:4px 0;font-weight:700">{_formatear_dinero(float(orden.get('total') or 0))}</td></tr>
    </table>
    """


def por_autorizar(orden: dict) -> None:
    """Contabilidad: hay una orden esperando autorización."""
    folio = orden.get("folio")
    partidas = orden.get("partidas") or []
    contenido = f"""
    <p style="color:#5A5648;margin:0 0 16px">
      {_esc((orden.get('creador') or {}).get('nombre') or 'Almacén')} envió una orden a autorización.
    </p>
    {_resumen(orden)}
    <table width="100%" cellpadding="0" cellspacing="0" style="font-size:13px">
      <tbody>{_html_partidas(partidas)}</tbody>
    </table>
    {_boton('Revisar en ProCompra')}
    """
    enviar(
        _correos_por_rol(ROLES_CONTABILIDAD),
        f"Por autorizar — OC #{folio} · {(orden.get('proveedor') or {}).get('nombre', '')}",
        f"Orden #{folio} por autorizar",
        contenido,
    )


def resultado_autorizacion(orden: dict, resultado: str, motivo: str = None) -> None:
    """Quien creó la orden: fue autorizada, devuelta o cancelada."""
    folio = orden.get("folio")
    textos = {
        "autorizada": ("autorizada", "Ya puedes coordinar la compra y la logística."),
        "devuelta":   ("devuelta para corrección", "Corrígela y vuelve a enviarla a autorización."),
        "cancelada":  ("cancelada", "Sus requerimientos regresaron a la lista de pendientes."),
    }
    estado, siguiente = textos[resultado]
    motivo_html = (
        f'<p style="color:#B03A3A;margin:0 0 16px"><strong>Motivo:</strong> {_esc(motivo)}</p>'
        if motivo else ""
    )
    contenido = f"""
    <p style="color:#5A5648;margin:0 0 16px">La orden <strong>#{_esc(folio)}</strong> fue {estado}.</p>
    {motivo_html}
    {_resumen(orden)}
    <p style="color:#5A5648;font-size:13px">{siguiente}</p>
    {_boton('Abrir ProCompra')}
    """
    enviar(
        _correo_de(orden.get("creado_por")),
        f"OC #{folio} {estado}",
        f"Orden #{folio} {estado}",
        contenido,
    )


def comprobacion_lista(orden: dict) -> None:
    """Contabilidad: almacén ya subió el ticket o la factura."""
    folio = orden.get("folio")
    contenido = f"""
    <p style="color:#5A5648;margin:0 0 16px">
      Se registró la comprobación de la orden <strong>#{_esc(folio)}</strong>
      por {_formatear_dinero(float(orden.get('monto_comprobado') or 0))}.
      Revisa el ticket o factura y cierra la orden.
    </p>
    {_resumen(orden)}
    {_boton('Revisar comprobación')}
    """
    enviar(
        _correos_por_rol(ROLES_CONTABILIDAD),
        f"Comprobación lista — OC #{folio}",
        f"Orden #{folio} lista para cerrar",
        contenido,
    )
