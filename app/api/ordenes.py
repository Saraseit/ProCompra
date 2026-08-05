"""
app/api/ordenes.py
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from app.database import supabase
from app.auth import usuario_actual, requiere_rol

router = APIRouter(prefix="/ordenes", tags=["Órdenes de compra"])


class PartidaInput(BaseModel):
    req_id: Optional[str] = None
    concepto: str
    cantidad: float
    unidad: str
    precio_unitario: float
    contrato: Optional[str] = None


class OrdenCreate(BaseModel):
    proveedor_id: str
    creado_por: str
    tipo_pago: str = "transferencia"
    observaciones: Optional[str] = None
    partidas: list[PartidaInput]


class EstadoUpdate(BaseModel):
    estado: str
    detalle: Optional[str] = None


class PagoCreate(BaseModel):
    fecha_pago: str
    referencia: str
    metodo: str
    monto: float
    comprobante_url: Optional[str] = None
    registrado_por: str
    notas: Optional[str] = None


class RecoleccionCreate(BaseModel):
    tipo: str = "recoleccion"
    fecha_programada: Optional[str] = None
    responsable: Optional[str] = None
    notas: Optional[str] = None
    completado: bool = False
    fecha_real: Optional[str] = None


def registrar_evento(orden_id: str, evento: str, detalle: str = None, usuario_id: str = None):
    supabase.table("historial").insert({
        "orden_id": orden_id,
        "evento": evento,
        "detalle": detalle,
        "usuario_id": usuario_id,
    }).execute()


@router.get("/")
def listar_ordenes(
    estado: Optional[str] = None,
    usuario = Depends(usuario_actual),
):
    query = (
        supabase.table("ordenes_compra")
        .select("""
            *,
            proveedor:proveedor_id ( id, nombre, correo, telefono ),
            creador:creado_por ( id, nombre )
        """)
        .order("created_at", desc=True)
    )
    if estado:
        query = query.eq("estado", estado)

    # Solicitantes solo ven sus propias órdenes
    if usuario["rol"] == "solicitante":
        query = query.eq("creado_por", usuario["id"])

    res = query.execute()
    return res.data


@router.get("/{orden_id}")
def obtener_orden(orden_id: str, usuario = Depends(usuario_actual)):
    orden = (
        supabase.table("ordenes_compra")
        .select("""
            *,
            proveedor:proveedor_id ( * ),
            creador:creado_por ( id, nombre, correo )
        """)
        .eq("id", orden_id)
        .single()
        .execute()
    )
    if not orden.data:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    partidas     = supabase.table("partidas").select("*").eq("orden_id", orden_id).execute()
    pagos        = supabase.table("pagos").select("*").eq("orden_id", orden_id).execute()
    recolecciones= supabase.table("recolecciones").select("*").eq("orden_id", orden_id).execute()
    historial    = (
        supabase.table("historial")
        .select("*, usuario:usuario_id ( id, nombre )")
        .eq("orden_id", orden_id)
        .order("created_at", desc=True)
        .execute()
    )

    resultado = orden.data
    resultado["partidas"]      = partidas.data
    resultado["pagos"]         = pagos.data
    resultado["recolecciones"] = recolecciones.data
    resultado["historial"]     = historial.data
    return resultado


@router.post("/", status_code=201)
def crear_orden(
    orden: OrdenCreate,
    usuario = Depends(requiere_rol("admin", "compras")),
):
    if not orden.partidas:
        raise HTTPException(status_code=400, detail="La orden debe tener al menos una partida")

    orden_data = {
        "proveedor_id":  orden.proveedor_id,
        "creado_por":    orden.creado_por,
        "tipo_pago":     orden.tipo_pago,
        "observaciones": orden.observaciones,
        "estado":        "borrador",
    }
    res_orden   = supabase.table("ordenes_compra").insert(orden_data).execute()
    nueva_orden = res_orden.data[0]
    orden_id    = nueva_orden["id"]

    partidas_data = [
        {
            "orden_id":        orden_id,
            "req_id":          p.req_id,
            "concepto":        p.concepto,
            "cantidad":        p.cantidad,
            "unidad":          p.unidad,
            "precio_unitario": p.precio_unitario,
            "contrato":        p.contrato,
        }
        for p in orden.partidas
    ]
    supabase.table("partidas").insert(partidas_data).execute()

    req_ids = [p.req_id for p in orden.partidas if p.req_id]
    if req_ids:
        supabase.table("requerimientos").update({
            "estado":   "en_orden",
            "orden_id": orden_id,
        }).in_("id", req_ids).execute()

    registrar_evento(orden_id, "Orden creada", usuario_id=usuario["id"])
    return obtener_orden(orden_id, usuario)


@router.patch("/{orden_id}/estado")
def cambiar_estado(
    orden_id: str,
    body: EstadoUpdate,
    usuario = Depends(usuario_actual),
):
    estados_validos = {
        "borrador", "autorizacion", "autorizada",
        "pagada", "recoleccion", "cerrada", "rechazada"
    }
    if body.estado not in estados_validos:
        raise HTTPException(status_code=400, detail=f"Estado inválido: {body.estado}")

    # Solo pagos o admin pueden autorizar o rechazar
    if body.estado in ("autorizada", "rechazada"):
        if usuario["rol"] not in ("admin", "pagos"):
            raise HTTPException(status_code=403, detail="Solo el área de pagos puede autorizar o rechazar")

    res = (
        supabase.table("ordenes_compra")
        .update({"estado": body.estado})
        .eq("id", orden_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    registrar_evento(
        orden_id,
        evento=f"Estado cambiado a '{body.estado}'",
        detalle=body.detalle,
        usuario_id=usuario["id"],
    )

    # Enviar correo si el nuevo estado es 'autorizacion'
    # if body.estado == "autorizacion":
    #     from app.utils.email import enviar_correo_autorizacion
    #     orden_completa = obtener_orden(orden_id, usuario)
    #     enviar_correo_autorizacion(orden_completa)

    return res.data[0]


@router.post("/{orden_id}/pago", status_code=201)
def registrar_pago(
    orden_id: str,
    pago: PagoCreate,
    usuario = Depends(requiere_rol("admin", "pagos")),
):
    datos = pago.model_dump()
    datos["orden_id"] = orden_id

    res = supabase.table("pagos").insert(datos).execute()

    supabase.table("ordenes_compra").update(
        {"estado": "pagada"}
    ).eq("id", orden_id).execute()

    registrar_evento(
        orden_id,
        evento="Pago registrado",
        detalle=f"Ref: {pago.referencia} · {pago.metodo} · ${pago.monto:,.2f}",
        usuario_id=usuario["id"],
    )

    # Notificar recolección por correo
    # from app.utils.email import enviar_correo_pago
    # orden_completa = obtener_orden(orden_id, usuario)
    # solicitante_correo = orden_completa.get("creador", {}).get("correo", "")
    # if solicitante_correo:
    #     enviar_correo_pago(orden_completa, datos, [solicitante_correo])

    return res.data[0]


@router.post("/{orden_id}/recoleccion", status_code=201)
def registrar_recoleccion(
    orden_id: str,
    rec: RecoleccionCreate,
    usuario = Depends(usuario_actual),
):
    datos = rec.model_dump()
    datos["orden_id"] = orden_id

    res = supabase.table("recolecciones").insert(datos).execute()

    nuevo_estado = "cerrada" if rec.completado else "recoleccion"
    supabase.table("ordenes_compra").update(
        {"estado": nuevo_estado}
    ).eq("id", orden_id).execute()

    registrar_evento(
        orden_id,
        evento="Recolección cerrada" if rec.completado else f"Recolección programada ({rec.tipo})",
        detalle=rec.notas,
        usuario_id=usuario["id"],
    )
    return res.data[0]


@router.delete("/{orden_id}", status_code=204)
def eliminar_orden(
    orden_id: str,
    usuario = Depends(requiere_rol("admin")),
):
    """Soft delete — solo admin puede eliminar órdenes."""
    res = (
        supabase.table("ordenes_compra")
        .update({"estado": "rechazada"})
        .eq("id", orden_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    registrar_evento(
        orden_id,
        evento="Orden eliminada por administrador",
        usuario_id=usuario["id"],
    )