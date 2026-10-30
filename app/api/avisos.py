"""
app/api/avisos.py
-----------------
Bandeja de avisos de cada usuario (la campana del menú).
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import usuario_actual
from app.database import supabase
from app.utils.bandeja import falta_tabla

router = APIRouter(prefix="/notificaciones", tags=["Avisos"])

SELECT_AVISO = "id,tipo,titulo,detalle,leida_at,created_at,orden_id,orden:orden_id(folio,estado),autor:creado_por(nombre)"


class Leer(BaseModel):
    ids: Optional[list[str]] = None


@router.get("")
def mis_avisos(usuario=Depends(usuario_actual)):
    try:
        avisos = (
            supabase.table("notificaciones").select(SELECT_AVISO)
            .eq("usuario_id", usuario["id"]).order("created_at", desc=True).limit(40).execute()
        ).data or []
        no_leidas = (
            supabase.table("notificaciones").select("id", count="exact")
            .eq("usuario_id", usuario["id"]).is_("leida_at", "null").execute()
        ).count or 0
    except Exception as e:
        # Sin la migración de la fase 4 la bandeja simplemente está vacía.
        if not falta_tabla(e):
            raise
        return {"avisos": [], "no_leidas": 0, "disponible": False}
    return {"avisos": avisos, "no_leidas": no_leidas, "disponible": True}


@router.post("/leer", status_code=204)
def marcar_leidas(body: Leer, usuario=Depends(usuario_actual)):
    """Marca como leídos los avisos indicados, o todos si no se indica ninguno."""
    query = (
        supabase.table("notificaciones").update({"leida_at": datetime.now(timezone.utc).isoformat()})
        .eq("usuario_id", usuario["id"]).is_("leida_at", "null")
    )
    if body.ids:
        query = query.in_("id", body.ids)
    try:
        query.execute()
    except Exception as e:
        if not falta_tabla(e):
            raise
