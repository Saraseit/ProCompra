"""
app/api/proveedores.py
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from app.database import supabase
from app.auth import usuario_actual, requiere_rol

router = APIRouter(prefix="/proveedores", tags=["Proveedores"])


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
    nombre: Optional[str] = None


@router.get("/")
def listar_proveedores(usuario = Depends(usuario_actual)):
    res = (
        supabase.table("proveedores")
        .select("*")
        .eq("activo", True)
        .order("nombre")
        .execute()
    )
    return res.data


@router.get("/{proveedor_id}")
def obtener_proveedor(proveedor_id: str, usuario = Depends(usuario_actual)):
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
def crear_proveedor(
    proveedor: ProveedorCreate,
    usuario = Depends(requiere_rol("admin", "compras"))
):
    res = (
        supabase.table("proveedores")
        .insert(proveedor.model_dump())
        .execute()
    )
    return res.data[0]


@router.put("/{proveedor_id}")
def actualizar_proveedor(
    proveedor_id: str,
    proveedor: ProveedorUpdate,
    usuario = Depends(requiere_rol("admin", "compras"))
):
    datos = {k: v for k, v in proveedor.model_dump().items() if v is not None}
    if not datos:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar")
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
def desactivar_proveedor(
    proveedor_id: str,
    usuario = Depends(requiere_rol("admin"))
):
    res = (
        supabase.table("proveedores")
        .update({"activo": False})
        .eq("id", proveedor_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")