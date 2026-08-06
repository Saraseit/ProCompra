from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
import httpx
from app.config import settings
from app.database import supabase

bearer = HTTPBearer()

_jwks_cache = None

def _obtener_jwks(forzar_refresh: bool = False) -> dict:
    global _jwks_cache
    if forzar_refresh or _jwks_cache is None:
        url = f"{settings.SUPABASE_URL}/auth/v1/.well-known/jwks.json"
        res = httpx.get(url, headers={"apikey": settings.SUPABASE_KEY})
        res.raise_for_status()
        _jwks_cache = res.json()
    return _jwks_cache


def obtener_clave_publica(kid: str = None, forzar_refresh: bool = False):
    from jwt.algorithms import ECAlgorithm
    jwks = _obtener_jwks(forzar_refresh)
    claves = jwks.get("keys", [])
    if not claves:
        raise HTTPException(status_code=401, detail="No hay claves públicas disponibles")

    jwk = next((k for k in claves if k.get("kid") == kid), None) if kid else None
    if jwk is None:
        jwk = claves[0]
    return ECAlgorithm.from_jwk(jwk)


def _decodificar(token: str, forzar_refresh: bool = False) -> dict:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.InvalidTokenError:
        kid = None
    clave = obtener_clave_publica(kid, forzar_refresh=forzar_refresh)
    return jwt.decode(
        token,
        clave,
        algorithms=["ES256"],
        options={"verify_aud": False},
    )


def usuario_actual(
    credentials: HTTPAuthorizationCredentials = Depends(bearer)
) -> dict:
    token = credentials.credentials

    try:
        try:
            payload = _decodificar(token)
        except jwt.InvalidSignatureError:
            # La clave cacheada puede estar desactualizada si Supabase rotó las llaves de firma
            payload = _decodificar(token, forzar_refresh=True)
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Token sin usuario")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Error verificando token")

    perfil = (
        supabase.table("usuarios")
        .select("id, nombre, correo, rol, activo")
        .eq("id", user_id)
        .maybe_single()
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