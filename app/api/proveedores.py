"""
app/api/proveedores.py
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from app.database import supabase, una_fila
from app.auth import usuario_actual, requiere_rol
from app.roles import ROLES_ADMIN, ROLES_OPERACION

router = APIRouter(prefix="/proveedores", tags=["Proveedores"])


def _normalizar(nombre: str) -> str:
    """Nombre comparable: sin acentos de espaciado ni distinción de mayúsculas."""
    return " ".join((nombre or "").split()).casefold()


def _nombre_ocupado(nombre: str, excluir_id: Optional[str] = None) -> Optional[dict]:
    """Busca un proveedor con el mismo nombre, incluidos los desactivados.

    La comparación se hace en Python y no con ilike porque el nombre del
    proveedor puede traer % o _, que en un LIKE serían comodines.
    """
    objetivo = _normalizar(nombre)
    filas = supabase.table("proveedores").select("id, nombre, activo").execute().data or []
    for fila in filas:
        if _normalizar(fila["nombre"]) == objetivo and fila["id"] != excluir_id:
            return fila
    return None


def _rechazar_si_duplicado(nombre: str, excluir_id: Optional[str] = None) -> None:
    choque = _nombre_ocupado(nombre, excluir_id)
    if not choque:
        return
    detalle = f'Ya existe un proveedor llamado "{choque["nombre"]}"'
    if not choque["activo"]:
        detalle += ", aunque está desactivado. Reactívalo en lugar de crear otro"
    raise HTTPException(status_code=409, detail=detalle)


class ProveedorBase(BaseModel):
    nombre: str
    rfc: Optional[str] = None
    correo: Optional[str] = None
    telefono: Optional[str] = None
    direccion: Optional[str] = None
    cuenta_bancaria: Optional[str] = None
    # Plazo de crédito que da el proveedor. Vacío = no da crédito o no se ha pactado.
    dias_credito: Optional[int] = Field(default=None, ge=0, le=365)

    @field_validator("nombre")
    @classmethod
    def _nombre_no_vacio(cls, v):
        limpio = " ".join((v or "").split())
        if not limpio:
            raise ValueError("el nombre del proveedor es obligatorio")
        return limpio

class ProveedorCreate(ProveedorBase):
    pass

class ProveedorUpdate(ProveedorBase):
    nombre: Optional[str] = None

    @field_validator("nombre")
    @classmethod
    def _nombre_no_vacio(cls, v):
        if v is None:
            return None
        limpio = " ".join(v.split())
        if not limpio:
            raise ValueError("el nombre del proveedor es obligatorio")
        return limpio


@router.get("")
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
    proveedor = una_fila(
        supabase.table("proveedores")
        .select("*")
        .eq("id", proveedor_id)
    )
    if not proveedor:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")
    return proveedor


@router.post("", status_code=201)
def crear_proveedor(
    proveedor: ProveedorCreate,
    usuario = Depends(requiere_rol(*ROLES_OPERACION))
):
    _rechazar_si_duplicado(proveedor.nombre)
    res = (
        supabase.table("proveedores")
        .insert(proveedor.model_dump(exclude_none=True))
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=400, detail="No se pudo crear el proveedor")
    return res.data[0]


@router.put("/{proveedor_id}")
def actualizar_proveedor(
    proveedor_id: str,
    proveedor: ProveedorUpdate,
    usuario = Depends(requiere_rol(*ROLES_OPERACION))
):
    datos = {k: v for k, v in proveedor.model_dump().items() if v is not None}
    # dias_credito sí se puede borrar: un null explícito quita el plazo.
    if "dias_credito" in proveedor.model_fields_set:
        datos["dias_credito"] = proveedor.dias_credito
    if not datos:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar")
    if "nombre" in datos:
        _rechazar_si_duplicado(datos["nombre"], excluir_id=proveedor_id)
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
    usuario = Depends(requiere_rol(*ROLES_ADMIN))
):
    res = (
        supabase.table("proveedores")
        .update({"activo": False})
        .eq("id", proveedor_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")