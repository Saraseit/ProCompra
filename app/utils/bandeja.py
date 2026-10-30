"""
app/utils/bandeja.py
--------------------
Avisos dentro de ProCompra (campana del menú). A diferencia del correo, no
dependen de Resend: se guardan en la tabla `notificaciones`, uno por
destinatario, para que cada quien los marque como leídos.
"""

from app.database import supabase
from app.roles import ROLES_ADMIN


def falta_tabla(error: Exception) -> bool:
    """El error viene de una columna o tabla que aún no crea la migración."""
    texto = str(error)
    return any(c in texto for c in ("42703", "42P01", "PGRST204", "PGRST205", "does not exist"))


def destinatarios_finanzas(excepto: str = None) -> list[str]:
    filas = (
        supabase.table("usuarios").select("id")
        .in_("rol", list(ROLES_ADMIN)).eq("activo", True).execute()
    ).data or []
    return [f["id"] for f in filas if f["id"] != excepto]


def avisar(usuarios: list[str], titulo: str, detalle: str = None, orden_id: str = None,
           tipo: str = "orden", creado_por: str = None) -> int:
    """Deja un aviso a cada usuario. Nunca rompe la operación que lo origina."""
    if not usuarios:
        return 0
    filas = [
        {"usuario_id": u, "orden_id": orden_id, "tipo": tipo, "titulo": titulo,
         "detalle": detalle, "creado_por": creado_por}
        for u in usuarios
    ]
    try:
        supabase.table("notificaciones").insert(filas).execute()
        return len(filas)
    except Exception as e:
        print(f"[BANDEJA] no se guardó el aviso «{titulo}»: {e}")
        return 0


def avisar_finanzas(titulo: str, detalle: str = None, orden_id: str = None,
                    tipo: str = "orden", actor: str = None) -> int:
    """Admin y contabilidad, salvo quien hizo el cambio."""
    try:
        usuarios = destinatarios_finanzas(excepto=actor)
    except Exception as e:
        print(f"[BANDEJA] no se pudieron leer los destinatarios: {e}")
        return 0
    return avisar(usuarios, titulo, detalle, orden_id, tipo, actor)
