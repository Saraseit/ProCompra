"""
app/api/ajustes.py
------------------
Cambios a una orden después de creada:

- PUT  /ordenes/{id}/partidas  edita partidas (y proveedor) en cualquier estado
  salvo cancelada. Fuera de borrador exige motivo y avisa a admin y contabilidad.
- POST /ordenes/{id}/surtido   registra que el proveedor no surtió todo: la
  orden se ajusta a lo surtido y el faltante se cancela o pasa a una orden
  complementaria con otro proveedor, ligada a la original.
"""

from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from app.auth import requiere_rol
from app.database import supabase, una_fila
from app.roles import ROLES_OPERACION
from app.api.ordenes import (
    PartidaInput, METODOS_PAGO, registrar_evento, obtener_orden, _avisar, _texto_opcional, _uuid_valido,
)
from app.utils import ajustes, bandeja, notificaciones

router = APIRouter(prefix="/ordenes", tags=["Ajustes de órdenes"])

FALTA_MIGRACION = (
    "Falta aplicar la migración 2026-10-30_fase4_ajustes.sql en Supabase "
    "para registrar surtidos parciales"
)


class PartidaEdicion(PartidaInput):
    id: Optional[str] = None

    @field_validator("id")
    @classmethod
    def _id_valido(cls, v: Optional[str]) -> Optional[str]:
        return _uuid_valido(v) if v else None


class EdicionOrden(BaseModel):
    partidas: list[PartidaEdicion] = Field(min_length=1)
    motivo: Optional[str] = None
    proveedor_id: Optional[str] = None
    observaciones: Optional[str] = None

    @field_validator("motivo", "observaciones")
    @classmethod
    def _limpiar(cls, v: Optional[str]) -> Optional[str]:
        return _texto_opcional(v)

    @field_validator("proveedor_id")
    @classmethod
    def _proveedor(cls, v: Optional[str]) -> Optional[str]:
        return _uuid_valido(v) if v else None


class SurtidoPartida(BaseModel):
    partida_id: str
    cantidad_surtida: float = Field(ge=0)
    faltante: Literal["complemento", "cancelar"] = "complemento"


class Complemento(BaseModel):
    proveedor_id: str
    tipo_pago: str = "transferencia"
    precios: dict[str, float] = {}
    enviar_a_autorizacion: bool = True

    @field_validator("proveedor_id")
    @classmethod
    def _proveedor(cls, v: str) -> str:
        return _uuid_valido(v)

    @field_validator("tipo_pago")
    @classmethod
    def _pago(cls, v: str) -> str:
        if v not in METODOS_PAGO:
            raise ValueError(f"debe ser uno de: {', '.join(METODOS_PAGO)}")
        return v


class SurtidoParcial(BaseModel):
    partidas: list[SurtidoPartida] = Field(min_length=1)
    motivo: str
    complemento: Optional[Complemento] = None

    @field_validator("motivo")
    @classmethod
    def _motivo(cls, v: str) -> str:
        limpio = (v or "").strip()
        if not limpio:
            raise ValueError("explica qué pasó con el proveedor")
        return limpio

    @model_validator(mode="after")
    def _complemento_si_hace_falta(self):
        if any(p.faltante == "complemento" for p in self.partidas) and not self.complemento:
            raise ValueError("elige el proveedor de la orden complementaria")
        return self


def _orden(orden_id: str) -> dict:
    orden = una_fila(
        supabase.table("ordenes_compra")
        .select("id, folio, estado, tipo_pago, total, proveedor_id, creado_por, proveedor:proveedor_id(id,nombre)")
        .eq("id", orden_id)
    )
    if not orden:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    return orden


def _proveedor_activo(proveedor_id: str) -> dict:
    prov = una_fila(supabase.table("proveedores").select("id, nombre, activo").eq("id", proveedor_id))
    if not prov:
        raise HTTPException(status_code=400, detail="El proveedor no existe")
    if not prov.get("activo"):
        raise HTTPException(status_code=400, detail="El proveedor está desactivado")
    return prov


def _guardar_totales(orden_id: str, partidas: list[dict]) -> dict:
    t = ajustes.totales(partidas)
    supabase.table("ordenes_compra").update(t).eq("id", orden_id).execute()
    return t


def _liberar_si_huerfano(req_ids: set) -> None:
    """Un requerimiento cuya partida se quitó vuelve a pendientes si ya no está en otra orden viva."""
    for req_id in req_ids:
        vivas = (
            supabase.table("partidas").select("id, orden:orden_id(estado)").eq("req_id", req_id).execute()
        ).data or []
        if not any((p.get("orden") or {}).get("estado") != "rechazada" for p in vivas):
            supabase.table("requerimientos").update(
                {"estado": "pendiente", "orden_id": None}
            ).eq("id", req_id).execute()


def _dinero(n) -> str:
    return f"${float(n or 0):,.2f}"


@router.put("/{orden_id}/partidas")
def editar_orden(
    orden_id: str,
    body: EdicionOrden,
    tareas: BackgroundTasks,
    usuario=Depends(requiere_rol(*ROLES_OPERACION)),
):
    orden = _orden(orden_id)
    estado = orden["estado"]
    if estado not in ajustes.ESTADOS_EDITABLES:
        raise HTTPException(status_code=409, detail="Una orden cancelada ya no se edita")
    borrador = estado == "borrador"
    if not borrador and not body.motivo:
        raise HTTPException(status_code=400, detail="Explica el motivo del cambio: se avisa a administración y contabilidad")

    antes = supabase.table("partidas").select("*").eq("orden_id", orden_id).order("created_at").execute().data or []
    por_id = {p["id"]: p for p in antes}
    nuevas = [p.model_dump() for p in body.partidas]
    ajenas = [p["id"] for p in nuevas if p["id"] and p["id"] not in por_id]
    if ajenas:
        raise HTTPException(status_code=400, detail="Una de las partidas no pertenece a esta orden")

    cambios_orden = {}
    prov_nuevo = None
    if body.proveedor_id and body.proveedor_id != orden["proveedor_id"]:
        if estado not in ajustes.ESTADOS_CAMBIO_PROVEEDOR:
            raise HTTPException(status_code=409, detail="El proveedor ya no se cambia en esta etapa: registra un surtido parcial y manda el faltante a otra orden")
        pagos = supabase.table("pagos").select("id").eq("orden_id", orden_id).limit(1).execute().data
        if pagos:
            raise HTTPException(status_code=409, detail="Ya hay pagos a este proveedor: registra un surtido parcial en lugar de cambiarlo")
        prov_nuevo = _proveedor_activo(body.proveedor_id)
        cambios_orden["proveedor_id"] = body.proveedor_id
    if body.observaciones is not None:
        cambios_orden["observaciones"] = body.observaciones

    lineas = ajustes.describir_cambios(antes, nuevas)
    if prov_nuevo:
        lineas.insert(0, f"Proveedor: {(orden.get('proveedor') or {}).get('nombre', '—')} → {prov_nuevo['nombre']}")
    if not lineas and not cambios_orden:
        raise HTTPException(status_code=400, detail="No hay cambios que guardar")

    quedan = {p["id"] for p in nuevas if p["id"]}
    quitadas = [p for p in antes if p["id"] not in quedan]
    for p in nuevas:
        datos = {k: p[k] for k in ajustes.CAMPOS_PARTIDA}
        if p["id"]:
            cambio = ajustes.cambio_partida(por_id[p["id"]], datos)
            if cambio:
                supabase.table("partidas").update(cambio).eq("id", p["id"]).execute()
        else:
            supabase.table("partidas").insert({**datos, "orden_id": orden_id, "req_id": p.get("req_id")}).execute()
    if quitadas:
        supabase.table("partidas").delete().in_("id", [p["id"] for p in quitadas]).execute()
        _liberar_si_huerfano({p["req_id"] for p in quitadas if p.get("req_id")})

    total_antes = float(orden.get("total") or 0)
    t = _guardar_totales(orden_id, nuevas)
    if cambios_orden:
        supabase.table("ordenes_compra").update(cambios_orden).eq("id", orden_id).execute()
    if round(t["total"], 2) != round(total_antes, 2):
        lineas.append(f"Total {_dinero(total_antes)} → {_dinero(t['total'])}")

    detalle = "\n".join(([f"Motivo: {body.motivo}"] if body.motivo else []) + lineas)
    registrar_evento(orden_id, "Orden modificada", detalle=detalle, usuario_id=usuario["id"])

    if not borrador:
        titulo = f"{usuario['nombre']} modificó la orden #{orden['folio']}"
        bandeja.avisar_finanzas(titulo, detalle, orden_id, tipo="modificacion", actor=usuario["id"])
        _avisar(tareas, notificaciones.orden_modificada, orden_id, titulo, body.motivo, lineas)
    return obtener_orden(orden_id, usuario)


@router.post("/{orden_id}/surtido")
def surtido_parcial(
    orden_id: str,
    body: SurtidoParcial,
    tareas: BackgroundTasks,
    usuario=Depends(requiere_rol(*ROLES_OPERACION)),
):
    try:
        supabase.table("partidas").select("cantidad_original, partida_origen_id").limit(1).execute()
        supabase.table("ordenes_compra").select("orden_origen_id").limit(1).execute()
    except Exception as e:
        if bandeja.falta_tabla(e):
            raise HTTPException(status_code=503, detail=FALTA_MIGRACION)
        raise

    orden = _orden(orden_id)
    if orden["estado"] not in ajustes.ESTADOS_SURTIDO:
        raise HTTPException(status_code=409, detail="El surtido parcial se registra antes de cerrar la orden")

    partidas = supabase.table("partidas").select("*").eq("orden_id", orden_id).order("created_at").execute().data or []
    try:
        plan = ajustes.plan_surtido(
            partidas, [p.model_dump() for p in body.partidas],
            body.complemento.precios if body.complemento else None,
        )
    except ajustes.ErrorSurtido as e:
        raise HTTPException(status_code=400, detail=str(e))

    prov_comp = _proveedor_activo(body.complemento.proveedor_id) if plan["complemento"] else None

    # 1. La orden original se queda con lo que sí surtió su proveedor.
    for a in plan["ajustes"]:
        if a["cantidad"] > 0:
            supabase.table("partidas").update(
                {"cantidad": a["cantidad"], "cantidad_original": a["cantidad_original"]}
            ).eq("id", a["id"]).execute()
        else:
            supabase.table("partidas").delete().eq("id", a["id"]).execute()
    restantes = supabase.table("partidas").select("*").eq("orden_id", orden_id).execute().data or []
    total_antes = float(orden.get("total") or 0)
    t = _guardar_totales(orden_id, restantes)

    # 2. El faltante, a una orden nueva ligada a la original.
    nueva = None
    if plan["complemento"]:
        estado_nuevo = "autorizacion" if body.complemento.enviar_a_autorizacion else "borrador"
        res = supabase.table("ordenes_compra").insert({
            "proveedor_id":    prov_comp["id"],
            "creado_por":      usuario["id"],
            "tipo_pago":       body.complemento.tipo_pago,
            "estado":          estado_nuevo,
            "orden_origen_id": orden_id,
            "observaciones":   f"Complemento de la orden #{orden['folio']}: {body.motivo}",
        }).execute()
        nueva = res.data[0]
        supabase.table("partidas").insert(
            [{**p, "orden_id": nueva["id"]} for p in plan["complemento"]]
        ).execute()
        _guardar_totales(nueva["id"], plan["complemento"])
        registrar_evento(nueva["id"], "Orden creada",
                         detalle=f"Complemento de la orden #{orden['folio']} ({orden['proveedor']['nombre']}): {body.motivo}",
                         usuario_id=usuario["id"])
        if estado_nuevo == "autorizacion":
            registrar_evento(nueva["id"], "Enviada a autorización", usuario_id=usuario["id"])
            _avisar(tareas, notificaciones.por_autorizar, nueva["id"])

    # 3. Historial y avisos.
    lineas = [
        f"{a['concepto']}: surtió {a['cantidad']:g} de {a['pedida']:g} {a['unidad']} "
        f"({ajustes.porcentaje(a['cantidad'], a['pedida'])}%)"
        for a in plan["ajustes"]
    ]
    if nueva:
        lineas.append(f"Faltante enviado a la orden #{nueva['folio']} con {prov_comp['nombre']}")
    if plan["canceladas"]:
        lineas.append("Faltante cancelado: " + ", ".join(plan["canceladas"]))
    lineas.append(f"Total {_dinero(total_antes)} → {_dinero(t['total'])}")
    detalle = "\n".join([f"Motivo: {body.motivo}"] + lineas)
    registrar_evento(orden_id, "Surtido parcial", detalle=detalle, usuario_id=usuario["id"])

    titulo = f"Surtido parcial en la orden #{orden['folio']}"
    if nueva:
        titulo += f" · complemento #{nueva['folio']} con {prov_comp['nombre']}"
    bandeja.avisar_finanzas(titulo, detalle, orden_id, tipo="surtido", actor=usuario["id"])
    _avisar(tareas, notificaciones.orden_modificada, orden_id, titulo, body.motivo, lineas)

    return {
        "orden": obtener_orden(orden_id, usuario),
        "complemento": {"id": nueva["id"], "folio": nueva["folio"]} if nueva else None,
    }
