"""
app/api/requerimientos.py
-------------------------
Rutas para gestionar la lista de requerimientos.

Endpoints:
    GET    /api/requerimientos              — lista todos (filtrables por estado)
    GET    /api/requerimientos/{id}         — detalle de uno
    POST   /api/requerimientos              — crear nuevo
    PUT    /api/requerimientos/{id}         — editar
    DELETE /api/requerimientos/{id}         — soft delete (cancelar)
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from app.database import supabase

router = APIRouter(prefix="/requerimientos", tags=["Requerimientos"])


# ── Esquemas ───────────────────────────────────────────
class RequerimientoCreate(BaseModel):
    descripcion: str
    cantidad: float
    unidad: str
    precio_estimado: float = 0
    contrato: Optional[str] = None
    proveedor_sug: Optional[str] = None   # UUID del proveedor
    solicitante_id: str                   # UUID del usuario
    notas: Optional[str] = None
    fecha_requerida: Optional[str] = None # formato YYYY-MM-DD


class RequerimientoUpdate(BaseModel):
    descripcion: Optional[str] = None
    cantidad: Optional[float] = None
    unidad: Optional[str] = None
    precio_estimado: Optional[float] = None
    contrato: Optional[str] = None
    proveedor_sug: Optional[str] = None
    notas: Optional[str] = None
    fecha_requerida: Optional[str] = None
    estado: Optional[str] = None
    orden_id: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────
@router.get("/")
def listar_requerimientos(
    estado: Optional[str] = Query(None, description="pendiente | en_orden | cancelado"),
):
    """
    Lista todos los requerimientos.
    Filtra por estado si se pasa como query param.
    Ejemplo: GET /api/requerimientos?estado=pendiente
    """
    query = (
        supabase.table("requerimientos")
        .select("""
            *,
            proveedor:proveedor_sug ( id, nombre ),
            solicitante:solicitante_id ( id, nombre )
        """)
        .order("created_at", desc=True)
    )

    if estado:
        query = query.eq("estado", estado)

    res = query.execute()
    return res.data


@router.get("/{req_id}")
def obtener_requerimiento(req_id: str):
    """Detalle de un requerimiento por su ID."""
    res = (
        supabase.table("requerimientos")
        .select("""
            *,
            proveedor:proveedor_sug ( id, nombre ),
            solicitante:solicitante_id ( id, nombre )
        """)
        .eq("id", req_id)
        .single()
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Requerimiento no encontrado")
    return res.data


@router.post("/", status_code=201)
def crear_requerimiento(req: RequerimientoCreate):
    """Crea un nuevo requerimiento con estado 'pendiente'."""
    datos = req.model_dump()
    datos["estado"] = "pendiente"

    res = (
        supabase.table("requerimientos")
        .insert(datos)
        .execute()
    )
    return res.data[0]


@router.put("/{req_id}")
def actualizar_requerimiento(req_id: str, req: RequerimientoUpdate):
    """Actualiza campos de un requerimiento existente."""
    datos = {k: v for k, v in req.model_dump().items() if v is not None}

    if not datos:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar")

    res = (
        supabase.table("requerimientos")
        .update(datos)
        .eq("id", req_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Requerimiento no encontrado")
    return res.data[0]


@router.delete("/{req_id}", status_code=204)
def cancelar_requerimiento(req_id: str):
    """
    Soft delete — cambia el estado a 'cancelado'.
    No se borra físicamente para conservar el historial.
    """
    res = (
        supabase.table("requerimientos")
        .update({"estado": "cancelado"})
        .eq("id", req_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Requerimiento no encontrado")