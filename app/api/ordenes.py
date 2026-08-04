"""
app/api/ordenes.py
------------------
Rutas para gestionar órdenes de compra.

Endpoints:
    GET    /api/ordenes                     — lista todas (filtrables por estado)
    GET    /api/ordenes/{id}                — detalle completo con partidas e historial
    POST   /api/ordenes                     — generar orden desde requerimientos
    PATCH  /api/ordenes/{id}/estado         — cambiar estado (autorizar, rechazar, etc.)
    POST   /api/ordenes/{id}/pago           — registrar pago
    POST   /api/ordenes/{id}/recoleccion    — registrar recolección o entrega
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.database import supabase

router = APIRouter(prefix="/ordenes", tags=["Órdenes de compra"])


# ── Esquemas ───────────────────────────────────────────
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


# ── Helpers ────────────────────────────────────────────
def registrar_evento(orden_id: str, evento: str, detalle: str = None, usuario_id: str = None):
    """Inserta una línea en la tabla historial."""
    supabase.table("historial").insert({
        "orden_id": orden_id,
        "evento": evento,
        "detalle": detalle,
        "usuario_id": usuario_id,
    }).execute()


# ── Endpoints ─────────────────────────────────────────
@router.get("/")
def listar_ordenes(estado: Optional[str] = None):
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
    res = query.execute()
    return res.data


@router.get("/{orden_id}")
def obtener_orden(orden_id: str):
    """Detalle completo de una orden con partidas, pagos, recolecciones e historial."""
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

    partidas = (
        supabase.table("partidas")
        .select("*")
        .eq("orden_id", orden_id)
        .execute()
    )
    pagos = (
        supabase.table("pagos")
        .select("*")
        .eq("orden_id", orden_id)
        .execute()
    )
    recolecciones = (
        supabase.table("recolecciones")
        .select("*")
        .eq("orden_id", orden_id)
        .execute()
    )
    historial = (
        supabase.table("historial")
        .select("*, usuario:usuario_id ( id, nombre )")
        .eq("orden_id", orden_id)
        .order("created_at", desc=True)
        .execute()
    )

    resultado = orden.data
    resultado["partidas"] = partidas.data
    resultado["pagos"] = pagos.data
    resultado["recolecciones"] = recolecciones.data
    resultado["historial"] = historial.data

    return resultado


@router.post("/", status_code=201)
def crear_orden(orden: OrdenCreate):
    """
    Genera una nueva orden de compra con sus partidas.
    El folio se asigna automáticamente por la secuencia en Postgres.
    Al terminar marca los requerimientos vinculados como 'en_orden'.
    """
    if not orden.partidas:
        raise HTTPException(status_code=400, detail="La orden debe tener al menos una partida")

    # 1 — Crear la orden
    orden_data = {
        "proveedor_id":  orden.proveedor_id,
        "creado_por":    orden.creado_por,
        "tipo_pago":     orden.tipo_pago,
        "observaciones": orden.observaciones,
        "estado":        "borrador",
    }
    res_orden = supabase.table("ordenes_compra").insert(orden_data).execute()
    nueva_orden = res_orden.data[0]
    orden_id = nueva_orden["id"]

    # 2 — Insertar partidas
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

    # 3 — Marcar requerimientos como 'en_orden'
    req_ids = [p.req_id for p in orden.partidas if p.req_id]
    if req_ids:
        supabase.table("requerimientos").update({
            "estado":   "en_orden",
            "orden_id": orden_id,
        }).in_("id", req_ids).execute()

    # 4 — Registrar evento en historial
    registrar_evento(orden_id, "Orden creada", usuario_id=orden.creado_por)

    # 5 — Devolver la orden completa
    return obtener_orden(orden_id)


@router.patch("/{orden_id}/estado")
def cambiar_estado(orden_id: str, body: EstadoUpdate):
    """Cambia el estado de una orden y dispara correo si aplica."""
    estados_validos = {
        "borrador", "autorizacion", "autorizada",
        "pagada", "recoleccion", "cerrada", "rechazada"
    }
    if body.estado not in estados_validos:
        raise HTTPException(status_code=400, detail=f"Estado inválido: {body.estado}")

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
    )

    # Enviar correo de autorización
    #if body.estado == "autorizacion":
     #   from app.utils.email import enviar_correo_autorizacion
     #   orden_completa = obtener_orden(orden_id)
     # enviar_correo_autorizacion(orden_completa)

    return res.data[0]


@router.post("/{orden_id}/pago", status_code=201)
def registrar_pago(orden_id: str, pago: PagoCreate):
    """Registra un pago y notifica al solicitante para recolectar."""
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
        usuario_id=pago.registrado_por,
    )

    # Notificar recolección por correo
    #from app.utils.email import enviar_correo_pago
    #orden_completa = obtener_orden(orden_id)
    #solicitante_correo = orden_completa.get("creador", {}).get("correo", "")
    #if solicitante_correo:
      #  enviar_correo_pago(orden_completa, datos, [solicitante_correo])

    return res.data[0]


@router.post("/{orden_id}/recoleccion", status_code=201)
def registrar_recoleccion(orden_id: str, rec: RecoleccionCreate):
    """Registra recolección o entrega. Si completado=True cierra la orden."""
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
    )

    return res.data[0]