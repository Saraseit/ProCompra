"""
app/api/proveedores.py
----------------------
Rutas para consultar y gestionar el catálogo de proveedores.

Endpoints:
    GET  /api/proveedores          — lista todos los activos
    GET  /api/proveedores/{id}     — detalle de uno
    POST /api/proveedores          — crear nuevo
    PUT  /api/proveedores/{id}     — editar
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.database import supabase

router = APIRouter(prefix="/proveedores", tags=["Proveedores"])


# ── Esquema de datos ───────────────────────────────────
class ProveedorBase(BaseModel):
    nombre: str
    rfc: Optional[str] = None
    correo: Optional[str] = None
    telefono: Optional[str] = None
    direccion: Optional[str] = None
    cuenta_bancaria: Optional[str] = None

class ProveedorCreate(ProveedorBase):
    pass

class ProveedorUpdate(ProveedorBase):
    nombre: Optional[str] = None  # en update todo es opcional


# ── Endpoints ─────────────────────────────────────────
@router.get("/")
def listar_proveedores():
    """Devuelve todos los proveedores activos, ordenados por nombre."""
    res = (
        supabase.table("proveedores")
        .select("*")
        .eq("activo", True)
        .order("nombre")
        .execute()
    )
    return res.data


@router.get("/{proveedor_id}")
def obtener_proveedor(proveedor_id: str):
    """Devuelve el detalle de un proveedor por su ID."""
    res = (
        supabase.table("proveedores")
        .select("*")
        .eq("id", proveedor_id)
        .single()
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")
    return res.data


@router.post("/", status_code=201)
def crear_proveedor(proveedor: ProveedorCreate):
    """Crea un nuevo proveedor en el catálogo."""
    res = (
        supabase.table("proveedores")
        .insert(proveedor.model_dump())
        .execute()
    )
    return res.data[0]


@router.put("/{proveedor_id}")
def actualizar_proveedor(proveedor_id: str, proveedor: ProveedorUpdate):
    """Actualiza los datos de un proveedor existente."""
    # Quitar campos None para no sobreescribir con vacíos
    datos = {k: v for k, v in proveedor.model_dump().items() if v is not None}

    res = (
        supabase.table("proveedores")
        .update(datos)
        .eq("id", proveedor_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")
    return res.data[0]


@router.delete("/{proveedor_id}", status_code=204)
def desactivar_proveedor(proveedor_id: str):
    """
    Soft delete — marca el proveedor como inactivo.
    No se borra físicamente para conservar el historial de órdenes.
    """
    res = (
        supabase.table("proveedores")
        .update({"activo": False})
        .eq("id", proveedor_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")