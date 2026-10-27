"""
app/api/adjuntos.py
-------------------
Documentos de una orden: cotización, comprobante de pago, ticket o factura.

Un XML de factura subido desde la orden no se guarda como adjunto común:
se lee como CFDI y queda registrado en facturas, vinculado a esa orden.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.auth import usuario_actual
from app.roles import ROLES_ADMIN
from app.database import supabase, una_fila
from app.api.ordenes import registrar_evento
from app.api.facturas import registrar_xml
from app.utils import storage

router = APIRouter(tags=["Adjuntos"])

TIPOS_ADJUNTO = {
    "cotizacion":       "Cotización",
    "comprobante_pago": "Comprobante de pago",
    "ticket":           "Ticket",
    "factura":          "Factura",
    "otro":             "Documento",
}


@router.post("/ordenes/{orden_id}/adjuntos", status_code=201)
async def subir_adjunto(
    orden_id: str,
    tipo: str = Form(...),
    archivo: UploadFile = File(...),
    usuario=Depends(usuario_actual),
):
    if tipo not in TIPOS_ADJUNTO:
        raise HTTPException(status_code=400, detail=f"Tipo de documento inválido: {tipo}")

    orden = una_fila(supabase.table("ordenes_compra").select("id, estado").eq("id", orden_id))
    if not orden:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    if orden["estado"] == "rechazada":
        raise HTTPException(status_code=409, detail="La orden está cancelada")

    contenido, mime = await storage.leer_archivo(archivo)

    if mime in ("application/xml", "text/xml"):
        if tipo != "factura":
            raise HTTPException(status_code=400, detail="Un XML solo se acepta como factura (CFDI)")
        resultado = registrar_xml(contenido, archivo.filename, usuario, orden_id=orden_id)
        if resultado["estado"] in ("invalida", "ignorada", "duplicada"):
            raise HTTPException(status_code=400, detail=resultado["mensaje"])
        return {"factura": resultado}

    ruta = storage.subir(f"ordenes/{orden_id}", archivo.filename, contenido, mime)
    res = supabase.table("adjuntos").insert({
        "orden_id":       orden_id,
        "tipo":           tipo,
        "nombre_archivo": archivo.filename or "archivo",
        "ruta":           ruta,
        "mime":           mime,
        "tamano":         len(contenido),
        "subido_por":     usuario["id"],
    }).execute()
    if not res.data:
        storage.borrar(ruta)
        raise HTTPException(status_code=400, detail="No se pudo registrar el documento")

    registrar_evento(orden_id, f"{TIPOS_ADJUNTO[tipo]} adjuntada",
                     detalle=archivo.filename, usuario_id=usuario["id"])
    return res.data[0]


@router.get("/adjuntos/{adjunto_id}/url")
def url_adjunto(adjunto_id: str, usuario=Depends(usuario_actual)):
    adjunto = una_fila(supabase.table("adjuntos").select("ruta, nombre_archivo").eq("id", adjunto_id))
    if not adjunto:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    return {"url": storage.url_firmada(adjunto["ruta"])}


@router.delete("/adjuntos/{adjunto_id}", status_code=204)
def borrar_adjunto(adjunto_id: str, usuario=Depends(usuario_actual)):
    adjunto = una_fila(
        supabase.table("adjuntos")
        .select("id, ruta, tipo, nombre_archivo, subido_por, orden_id, orden:orden_id(estado)")
        .eq("id", adjunto_id)
    )
    if not adjunto:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    if usuario["rol"] not in ROLES_ADMIN and adjunto["subido_por"] != usuario["id"]:
        raise HTTPException(status_code=403, detail="Solo quien lo subió, admin o contabilidad pueden quitarlo")
    if (adjunto.get("orden") or {}).get("estado") == "cerrada":
        raise HTTPException(status_code=409, detail="La orden está cerrada: sus documentos ya no se quitan")

    supabase.table("adjuntos").delete().eq("id", adjunto_id).execute()
    storage.borrar(adjunto["ruta"])
    registrar_evento(adjunto["orden_id"], f"{TIPOS_ADJUNTO.get(adjunto['tipo'], 'Documento')} eliminada",
                     detalle=adjunto["nombre_archivo"], usuario_id=usuario["id"])
