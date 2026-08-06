"""
app/api/usuarios.py
-------------------
Endpoints de gestión de usuarios — solo admin.

GET    /api/usuarios          — listar todos
POST   /api/usuarios          — crear usuario (Auth + tabla)
PUT    /api/usuarios/{id}     — editar nombre, rol
DELETE /api/usuarios/{id}     — desactivar (soft delete)
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr
from typing import Optional
from app.database import supabase
from app.auth import requiere_rol

router = APIRouter(prefix="/usuarios", tags=["Usuarios"])


class UsuarioCreate(BaseModel):
    nombre: str
    correo: str
    password: str
    rol: str = "solicitante"


class UsuarioUpdate(BaseModel):
    nombre: Optional[str] = None
    rol: Optional[str] = None
    activo: Optional[bool] = None


ROLES_VALIDOS = {"admin", "compras", "almacen", "pagos"}


@router.get("")
def listar_usuarios(usuario=Depends(requiere_rol("admin"))):
    res = (
        supabase.table("usuarios")
        .select("id, nombre, correo, rol, activo, created_at")
        .order("nombre")
        .execute()
    )
    return res.data


@router.post("", status_code=201)
def crear_usuario(
    data: UsuarioCreate,
    usuario=Depends(requiere_rol("admin")),
):
    if data.rol not in ROLES_VALIDOS:
        raise HTTPException(status_code=400, detail=f"Rol inválido. Válidos: {', '.join(ROLES_VALIDOS)}")

    # 1 — Crear en Supabase Auth
    try:
        res_auth = supabase.auth.admin.create_user({
            "email": data.correo,
            "password": data.password,
            "email_confirm": True,  # activo de inmediato sin correo de confirmación
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error creando usuario en Auth: {str(e)}")

    if not res_auth or not res_auth.user:
        raise HTTPException(status_code=400, detail="No se pudo crear el usuario en Auth")

    user_id = res_auth.user.id

    # 2 — Insertar en tabla pública
    try:
        res_perfil = (
            supabase.table("usuarios")
            .insert({
                "id": user_id,
                "nombre": data.nombre,
                "correo": data.correo,
                "rol": data.rol,
                "activo": True,
            })
            .execute()
        )
    except Exception as e:
        # Si falla la inserción, borrar el usuario de Auth para no dejar inconsistencias
        try:
            supabase.auth.admin.delete_user(user_id)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Error guardando perfil: {str(e)}")

    return res_perfil.data[0]


@router.put("/{usuario_id}")
def actualizar_usuario(
    usuario_id: str,
    data: UsuarioUpdate,
    usuario=Depends(requiere_rol("admin")),
):
    if data.rol and data.rol not in ROLES_VALIDOS:
        raise HTTPException(status_code=400, detail=f"Rol inválido. Válidos: {', '.join(ROLES_VALIDOS)}")

    datos = {k: v for k, v in data.model_dump().items() if v is not None}
    if not datos:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar")

    res = (
        supabase.table("usuarios")
        .update(datos)
        .eq("id", usuario_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    return res.data[0]


@router.delete("/{usuario_id}", status_code=204)
def desactivar_usuario(
    usuario_id: str,
    usuario=Depends(requiere_rol("admin")),
):
    # No permitir que el admin se desactive a sí mismo
    if usuario_id == usuario["id"]:
        raise HTTPException(status_code=400, detail="No puedes desactivarte a ti mismo")

    res = (
        supabase.table("usuarios")
        .update({"activo": False})
        .eq("id", usuario_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")