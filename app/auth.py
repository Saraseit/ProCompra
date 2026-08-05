from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config import settings
from app.database import supabase

bearer = HTTPBearer()


def usuario_actual(
    credentials: HTTPAuthorizationCredentials = Depends(bearer)
) -> dict:
    token = credentials.credentials

    try:
        # Usar el cliente con service_role para verificar el token
        res = supabase.auth.get_user(token)
    except Exception as e:
        print(f"[AUTH ERROR] {type(e).__name__}: {str(e)[:300]}")
        raise HTTPException(status_code=401, detail="Error verificando token")

    if not res or not res.user:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")

    user_id = res.user.id

    perfil = (
        supabase.table("usuarios")
        .select("id, nombre, correo, rol, activo")
        .eq("id", user_id)
        .single()
        .execute()
    )

    if not perfil.data:
        raise HTTPException(status_code=401, detail="Usuario no registrado en el sistema")

    if not perfil.data.get("activo"):
        raise HTTPException(status_code=403, detail="Usuario inactivo")

    return perfil.data


def requiere_rol(*roles: str):
    def verificar(usuario: dict = Depends(usuario_actual)) -> dict:
        if usuario["rol"] not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"Se requiere rol: {', '.join(roles)}",
            )
        return usuario
    return verificar