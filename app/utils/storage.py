"""
app/utils/storage.py
--------------------
Archivos en el bucket privado "adjuntos" de Supabase Storage.

El bucket no es público: el frontend nunca recibe la ruta directa, solo una
URL firmada de pocos minutos que genera el backend tras validar el acceso.
"""

import re
import uuid

from fastapi import HTTPException, UploadFile

from app.database import supabase

BUCKET = "adjuntos"
TAMANO_MAXIMO = 10 * 1024 * 1024  # 10 MB, igual que el límite del bucket
MINUTOS_URL = 5

TIPOS_PERMITIDOS = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/xml": ".xml",
    "text/xml": ".xml",
}

# Algunos navegadores mandan el XML como application/octet-stream: se decide
# por extensión en ese caso.
POR_EXTENSION = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".xml": "application/xml",
}


def nombre_seguro(nombre: str) -> str:
    """Deja solo caracteres seguros para una ruta de Storage."""
    base = (nombre or "archivo").strip().replace(" ", "_")
    base = re.sub(r"[^A-Za-z0-9._-]", "", base) or "archivo"
    return base[-80:]


def tipo_mime(archivo: UploadFile) -> str:
    nombre = (archivo.filename or "").lower()
    extension = nombre[nombre.rfind("."):] if "." in nombre else ""
    mime = (archivo.content_type or "").split(";")[0].strip().lower()
    if mime in TIPOS_PERMITIDOS:
        return mime
    if extension in POR_EXTENSION:
        return POR_EXTENSION[extension]
    raise HTTPException(
        status_code=400,
        detail="Formato no permitido. Usa PDF, imagen (JPG, PNG, WEBP) o XML.",
    )


async def leer_archivo(archivo: UploadFile) -> tuple[bytes, str]:
    """Lee el archivo validando tamaño y formato. Devuelve (contenido, mime)."""
    mime = tipo_mime(archivo)
    contenido = await archivo.read()
    if not contenido:
        raise HTTPException(status_code=400, detail=f"El archivo {archivo.filename} está vacío")
    if len(contenido) > TAMANO_MAXIMO:
        raise HTTPException(status_code=400, detail=f"{archivo.filename} supera los 10 MB")
    return contenido, mime


def subir(carpeta: str, nombre: str, contenido: bytes, mime: str) -> str:
    """Sube el archivo y devuelve su ruta dentro del bucket."""
    ruta = f"{carpeta}/{uuid.uuid4().hex[:12]}-{nombre_seguro(nombre)}"
    try:
        supabase.storage.from_(BUCKET).upload(
            path=ruta,
            file=contenido,
            file_options={"content-type": mime, "upsert": "false"},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo guardar el archivo: {e}")
    return ruta


def url_firmada(ruta: str, descarga: str = None) -> str:
    opciones = {"download": descarga} if descarga else {}
    try:
        res = supabase.storage.from_(BUCKET).create_signed_url(ruta, MINUTOS_URL * 60, opciones)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo abrir el archivo: {e}")
    url = res.get("signedURL") or res.get("signedUrl")
    if not url:
        raise HTTPException(status_code=502, detail="Storage no devolvió la URL del archivo")
    return url


def borrar(ruta: str) -> None:
    """Borra el binario. Si falla no se interrumpe: la fila ya se eliminó."""
    try:
        supabase.storage.from_(BUCKET).remove([ruta])
    except Exception:
        pass
