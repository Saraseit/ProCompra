"""
app/database.py
"""

from supabase import create_client, Client
from app.config import settings

supabase: Client = create_client(
    settings.SUPABASE_URL,
    settings.SUPABASE_KEY,
)


def una_fila(query):
    """Ejecuta la consulta y devuelve la fila, o None si no existe.

    maybe_single().execute() devuelve None cuando no hay filas, no una
    respuesta con .data vacío: leerlo directo lanza AttributeError y el
    endpoint responde 500 en lugar del 404 que corresponde.
    """
    res = query.maybe_single().execute()
    return res.data if res else None