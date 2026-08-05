from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from supabase import create_client
from app.config import settings
from app.database import supabase

bearer = HTTPBearer()

# Cliente separado con anon key para verificar tokens de usuarios
supabase_auth = create_client(
    settings.SUPABASE_URL,
    settings.SUPABASE_ANON_KEY,
)


def usuario_actual(
    credentials: HTTPAuthorizationCredentials = Depends(bearer)
) -> dict:
    token = credentials.credentials

    try:
        res = supabase_auth.auth.get_user(token)
        if not res or not res.user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token inválido o expirado",
            )
        user_id = res.user.id
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token inválido: {str(e)}",
        )

    perfil = (
        supabase.table("usuarios")
        .select("id, nombre, correo, rol, activo")
        .eq("id", user_id)
        .single()
        .execute()
    )

    if not perfil.data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario no registrado en el sistema",
        )

    if not perfil.data.get("activo"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuario inactivo",
        )

    return perfil.data


def requiere_rol(*roles: str):
    def verificar(usuario: dict = Depends(usuario_actual)) -> dict:
        if usuario["rol"] not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Se requiere rol: {', '.join(roles)}",
            )
        return usuario
    return verificar