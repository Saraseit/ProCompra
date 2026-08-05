from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from supabase import create_client
from app.config import settings
from app.database import supabase

bearer = HTTPBearer()

supabase_auth = create_client(
    settings.SUPABASE_URL,
    settings.SUPABASE_ANON_KEY,
)


def usuario_actual(
    credentials: HTTPAuthorizationCredentials = Depends(bearer)
) -> dict:
    token = credentials.credentials

    # 1 — Verificar token con Supabase Auth
    try:
        res = supabase_auth.auth.get_user(token)
    except Exception as e:
        print(f"[AUTH] Error al verificar token: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=401, detail=f"Error verificando token: {str(e)}")

    if not res or not res.user:
        print(f"[AUTH] Token sin usuario")
        raise HTTPException(status_code=401, detail="Token inválido o expirado")

    user_id = res.user.id
    print(f"[AUTH] Token válido — user_id: {user_id}")

    # 2 — Buscar perfil en tabla usuarios
    try:
        perfil = (
            supabase.table("usuarios")
            .select("id, nombre, correo, rol, activo")
            .eq("id", user_id)
            .single()
            .execute()
        )
    except Exception as e:
        print(f"[AUTH] Error al buscar perfil: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error buscando perfil: {str(e)}")

    print(f"[AUTH] Perfil encontrado: {perfil.data}")

    if not perfil.data:
        print(f"[AUTH] Usuario {user_id} no está en tabla usuarios")
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