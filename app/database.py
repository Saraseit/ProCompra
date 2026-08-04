"""
app/database.py
---------------
Conexión a Supabase. Exporta un cliente listo para usar.

Uso en cualquier archivo:
    from app.database import supabase
    result = supabase.table("proveedores").select("*").execute()
"""

from supabase import create_client, Client
from app.config import settings

supabase: Client = create_client(
    settings.SUPABASE_URL,
    settings.SUPABASE_KEY,
)