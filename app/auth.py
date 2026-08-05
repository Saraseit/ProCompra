from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
import httpx
from app.config import settings
from app.database import supabase

bearer = HTTPBearer()

# Obtener la clave pública de Supabase
_public_key_cache = None

def obtener_clave_publica():
    global _public_key_cache
    if _public_key_cache:
        return _public_key_cache
    
    # Supabase expone su clave pública en este endpoint
    url = f"{settings.SUPABASE_URL}/auth/v1/.well-known/jwks.json"
    try:
        res = httpx.get(url, headers={"apikey": settings.SUPABASE_KEY})
        res.raise_for_status()
        jwks = res.json()
        # Convertir la primera clave JWK a formato PEM
        from jwt.algorithms import ECAlgorithm
        clave = ECAlgorithm.from_jwk(jwks["keys"][0])
        _public_key_cache = clave
        return clave
    except Exception as e:
        print(f"[AUTH] Error obteniendo clave pública: {e}")
        raise HTTPException(status_code=500, detail="Error obteniendo clave pública")


def usuario_actual(
    credentials: HTTPAuthorizationCredentials = Depends(bearer)
) -> dict:
    token = credentials.credentials

    try:
        clave = obtener_clave_publica()
        payload = jwt.decode(
            token,
            clave,
            algorithms=["ES256"],
            options={"verify_aud": False},
        )
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Token sin usuario")
        print(f"[AUTH] OK — user_id: {user_id}")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado")
    except jwt.InvalidTokenError as e:
        print(f"[AUTH ERROR] {str(e)}")
        raise HTTPException(status_code=401, detail="Token inválido")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[AUTH ERROR] {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=401, detail="Error verificando token")

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