"""
app/api/requerimientos.py
"""

from datetime import date

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from uuid import UUID
from app.database import supabase, una_fila
from app.auth import usuario_actual, requiere_rol
from app.roles import ROLES_OPERACION

router = APIRouter(prefix="/requerimientos", tags=["Requerimientos"])

ESTADOS_REQ = ("pendiente", "en_orden", "cancelado")

# PostgREST rechaza los saltos de línea dentro de select: si la cadena es
# multilínea las relaciones anidadas se pierden sin error. Va en una sola línea.
SELECT_REQ = (
    "*,proveedor:proveedor_sug(id,nombre),solicitante:solicitante_id(id,nombre)"
)


def _texto_requerido(valor: str) -> str:
    limpio = (valor or "").strip()
    if not limpio:
        raise ValueError("no puede quedar vacío")
    return limpio


def _fecha_valida(valor: Optional[str]) -> Optional[str]:
    if not valor:
        return None
    try:
        return date.fromisoformat(str(valor)[:10]).isoformat()
    except ValueError:
        raise ValueError("debe ser una fecha AAAA-MM-DD")


def _uuid_valido(valor: str) -> str:
    try:
        return str(UUID(str(valor)))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("no es un identificador válido")


class RequerimientoCreate(BaseModel):
    descripcion: str
    cantidad: float = Field(gt=0)
    unidad: str
    precio_estimado: float = Field(default=0, ge=0)
    contrato: Optional[str] = None
    proveedor_sug: Optional[str] = None
    notas: Optional[str] = None
    fecha_requerida: Optional[str] = None

    @field_validator("descripcion", "unidad")
    @classmethod
    def _no_vacio(cls, v: str) -> str:
        return _texto_requerido(v)

    @field_validator("proveedor_sug")
    @classmethod
    def _proveedor_valido(cls, v: Optional[str]) -> Optional[str]:
        return _uuid_valido(v) if v else None

    @field_validator("fecha_requerida")
    @classmethod
    def _fecha(cls, v: Optional[str]) -> Optional[str]:
        return _fecha_valida(v)


class RequerimientoUpdate(BaseModel):
    descripcion: Optional[str] = None
    cantidad: Optional[float] = Field(default=None, gt=0)
    unidad: Optional[str] = None
    precio_estimado: Optional[float] = Field(default=None, ge=0)
    contrato: Optional[str] = None
    proveedor_sug: Optional[str] = None
    notas: Optional[str] = None
    fecha_requerida: Optional[str] = None
    estado: Optional[str] = None
    orden_id: Optional[str] = None

    @field_validator("descripcion", "unidad")
    @classmethod
    def _no_vacio(cls, v: Optional[str]) -> Optional[str]:
        return _texto_requerido(v) if v is not None else None

    @field_validator("fecha_requerida")
    @classmethod
    def _fecha(cls, v: Optional[str]) -> Optional[str]:
        return _fecha_valida(v)

    @field_validator("estado")
    @classmethod
    def _estado_valido(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ESTADOS_REQ:
            raise ValueError(f"debe ser uno de: {', '.join(ESTADOS_REQ)}")
        return v


@router.get("")
def listar_requerimientos(
    estado: Optional[str] = Query(None),
    usuario = Depends(usuario_actual),
):
    query = (
        supabase.table("requerimientos")
        .select(SELECT_REQ)
        .order("created_at", desc=True)
    )
    if estado:
        query = query.eq("estado", estado)

    # Solicitantes solo ven sus propios requerimientos
    if usuario["rol"] == "solicitante":
        query = query.eq("solicitante_id", usuario["id"])

    res = query.execute()
    return res.data


@router.get("/{req_id}")
def obtener_requerimiento(req_id: str, usuario = Depends(usuario_actual)):
    req = una_fila(
        supabase.table("requerimientos")
        .select(SELECT_REQ)
        .eq("id", req_id)
    )
    if not req:
        raise HTTPException(status_code=404, detail="Requerimiento no encontrado")

    # Solicitantes solo pueden ver sus propios requerimientos
    if usuario["rol"] == "solicitante" and req.get("solicitante_id") != usuario["id"]:
        raise HTTPException(status_code=404, detail="Requerimiento no encontrado")

    return req


@router.post("", status_code=201)
def crear_requerimiento(
    req: RequerimientoCreate,
    usuario = Depends(requiere_rol(*ROLES_OPERACION)),
):
    datos = req.model_dump()
    datos["estado"] = "pendiente"
    datos["solicitante_id"] = usuario["id"]
    res = (
        supabase.table("requerimientos")
        .insert(datos)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=400, detail="No se pudo crear el requerimiento")
    return res.data[0]


@router.put("/{req_id}")
def actualizar_requerimiento(
    req_id: str,
    req: RequerimientoUpdate,
    usuario = Depends(requiere_rol(*ROLES_OPERACION)),
):
    datos = {k: v for k, v in req.model_dump().items() if v is not None}
    # La fecha requerida sí se puede quitar: un null explícito la borra.
    if "fecha_requerida" in req.model_fields_set:
        datos["fecha_requerida"] = req.fecha_requerida
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
def cancelar_requerimiento(
    req_id: str,
    usuario = Depends(usuario_actual),
):
    query = (
        supabase.table("requerimientos")
        .update({"estado": "cancelado"})
        .eq("id", req_id)
    )
    # Solicitantes solo pueden cancelar sus propios requerimientos
    if usuario["rol"] == "solicitante":
        query = query.eq("solicitante_id", usuario["id"])
    res = query.execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Requerimiento no encontrado")