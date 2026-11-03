"""
app/api/pagos.py
----------------
Módulo de pagos de contabilidad: qué falta pagar (esta semana y después),
registrar un pago que cubre una o varias órdenes, y el historial de pagos.

Un pago de varias órdenes es un movimiento (tabla movimientos_pago) con un
renglón en pagos por cada orden: así el saldo de cada orden, el estado de
cuenta de crédito y el resumen de la orden siguen leyendo pagos como antes.
Sin la migración de la fase 6 se registra igual, solo que sin agruparse.
"""

import time
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.auth import requiere_rol
from app.database import supabase, una_fila
from app.roles import ROLES_CONTABILIDAD
from app.utils import credito, pagos as cuentas
from app.utils.bandeja import falta_tabla
from app.api.ordenes import (
    DatosPago, ESTADOS_PAGO_CREDITO, _uuid_valido, con_programados, despues_del_pago, motivo_sin_pago,
    registrar_evento,
)

router = APIRouter(prefix="/pagos", tags=["Pagos"])

# Una sola línea: PostgREST pierde los embebidos si la cadena es multilínea.
SELECT_POR_PAGAR = (
    "id,folio,fecha,estado,tipo_pago,total,monto_comprobado,proveedor_id,proveedor:proveedor_id(*),"
    "pagos(fecha_pago,monto,metodo,referencia),recolecciones(completado,fecha_real),"
    "facturas(serie,folio,fecha_emision)"
)
SELECT_A_PAGAR = "id,folio,estado,tipo_pago,total,monto_comprobado,pagos(monto)"
SELECT_HISTORIAL = (
    "*,orden:orden_id(id,folio,estado,proveedor:proveedor_id(nombre)),registrado:registrado_por(id,nombre)"
)

_migracion = {"lista": False, "revisado": 0.0}


def agrupa() -> bool:
    """¿Ya existe movimientos_pago? Sin ella cada orden lleva su pago suelto."""
    if _migracion["lista"]:
        return True
    if time.time() - _migracion["revisado"] < 30:
        return False
    _migracion["revisado"] = time.time()
    try:
        supabase.table("movimientos_pago").select("id").limit(1).execute()
        supabase.table("pagos").select("movimiento_id").limit(1).execute()
        _migracion["lista"] = True
    except Exception as e:
        if not falta_tabla(e):
            raise
    return _migracion["lista"]


class Aplicacion(BaseModel):
    orden_id: str
    monto: float = Field(gt=0)

    @field_validator("orden_id")
    @classmethod
    def _orden_valida(cls, v: str) -> str:
        return _uuid_valido(v)


class PagoVarias(DatosPago):
    """Un pago (transferencia, efectivo, tarjeta) repartido entre órdenes."""
    aplicaciones: list[Aplicacion] = Field(min_length=1, max_length=100)

    @field_validator("aplicaciones")
    @classmethod
    def _sin_repetir(cls, v: list[Aplicacion]) -> list[Aplicacion]:
        if len({a.orden_id for a in v}) != len(v):
            raise ValueError("una orden aparece dos veces en el pago")
        return v


@router.get("/pendientes")
def pendientes(usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    """Órdenes con saldo: esta semana (vencido, lo que cae antes del domingo, contado),
    después, y las de crédito que aún no tienen fecha de pago."""
    a_credito = (
        supabase.table("ordenes_compra").select(con_programados(SELECT_POR_PAGAR))
        .eq("tipo_pago", "credito").in_("estado", list(ESTADOS_PAGO_CREDITO)).execute()
    ).data or []
    contado = (
        supabase.table("ordenes_compra").select(SELECT_POR_PAGAR)
        .eq("estado", "autorizada").neq("tipo_pago", "credito").execute()
    ).data or []
    return {**cuentas.por_pagar(a_credito + contado, date.today()), "agrupa": agrupa()}


@router.post("", status_code=201)
def registrar(body: PagoVarias, usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD))):
    ids = [a.orden_id for a in body.aplicaciones]
    ordenes = {
        o["id"]: o for o in
        (supabase.table("ordenes_compra").select(con_programados(SELECT_A_PAGAR)).in_("id", ids).execute()).data or []
    }
    if len(ordenes) != len(ids):
        raise HTTPException(status_code=404, detail="Alguna de las órdenes ya no existe")

    errores = []
    for a in body.aplicaciones:
        orden = ordenes[a.orden_id]
        motivo = motivo_sin_pago(orden)
        if motivo:
            errores.append(motivo)
        elif orden["tipo_pago"] == "credito":
            # La deuda incluye el recargo si el calendario suma más que la orden.
            saldo = credito.calendario(orden, date.today())["saldo"]
            if a.monto > saldo + credito.TOLERANCIA:
                errores.append(f"A la orden #{orden['folio']} le quedan ${max(saldo, 0):,.2f}: "
                               f"no se le pueden aplicar ${a.monto:,.2f}")
    if errores:
        raise HTTPException(status_code=409, detail=". ".join(errores))

    comunes = body.model_dump(exclude={"aplicaciones"})
    movimiento_id = None
    if agrupa():
        res = supabase.table("movimientos_pago").insert({
            **{k: comunes.get(k) for k in ("fecha_pago", "metodo", "referencia", "entregado_a", "notas")},
            "registrado_por": usuario["id"],
        }).execute()
        movimiento_id = res.data[0]["id"] if res.data else None

    filas = [
        {**comunes, "orden_id": a.orden_id, "monto": round(a.monto, 2), "registrado_por": usuario["id"],
         **({"movimiento_id": movimiento_id} if movimiento_id else {})}
        for a in body.aplicaciones
    ]
    try:
        res = supabase.table("pagos").insert(filas).execute()
        if not res.data:
            raise ValueError("insert de pagos sin resultado")
    except Exception:
        if movimiento_id:
            supabase.table("movimientos_pago").delete().eq("id", movimiento_id).execute()
        raise HTTPException(status_code=400, detail="No se pudo registrar el pago")

    total = round(sum(f["monto"] for f in filas), 2)
    nota = f"pago de ${total:,.2f} repartido en {len(filas)} órdenes" if len(filas) > 1 else None
    for fila in filas:
        despues_del_pago(ordenes[fila["orden_id"]], fila, usuario["id"], nota=nota)
    return {"movimiento_id": movimiento_id, "ordenes": len(filas), "monto": total}


@router.get("")
def historial(
    limite: int = Query(100, ge=1, le=500),
    usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD)),
):
    """Pagos registrados, del más reciente al más antiguo, agrupados por movimiento."""
    filas = (
        supabase.table("pagos").select(SELECT_HISTORIAL)
        .order("fecha_pago", desc=True).order("created_at", desc=True)
        .limit(limite * 4).execute()
    ).data or []
    for f in filas:
        f["registrado"] = (f.get("registrado") or {}).get("nombre")
    return {"pagos": cuentas.agrupar_pagos(filas)[:limite], "agrupa": agrupa()}


@router.delete("/{pago_id}")
def borrar(pago_id: str, usuario=Depends(requiere_rol("admin"))):
    """Borra un pago capturado por error: el movimiento completo o un pago suelto.

    Una orden de contado que solo estaba pagada (sin logística) regresa a
    autorizada para que se vuelva a pagar.
    """
    try:
        pago_id = _uuid_valido(pago_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Pago no encontrado")

    filas, es_movimiento = [], False
    if agrupa():
        filas = (supabase.table("pagos").select("*").eq("movimiento_id", pago_id).execute()).data or []
        es_movimiento = bool(filas) or bool(
            una_fila(supabase.table("movimientos_pago").select("id").eq("id", pago_id))
        )
    if not es_movimiento:
        filas = (supabase.table("pagos").select("*").eq("id", pago_id).execute()).data or []
    if not filas and not es_movimiento:
        raise HTTPException(status_code=404, detail="Pago no encontrado")

    for lote in range(0, len(filas), 50):
        supabase.table("pagos").delete().in_("id", [f["id"] for f in filas[lote:lote + 50]]).execute()
    if es_movimiento:
        supabase.table("movimientos_pago").delete().eq("id", pago_id).execute()

    for orden_id in {f["orden_id"] for f in filas}:
        orden = una_fila(supabase.table("ordenes_compra").select("id, estado, pagos(id)").eq("id", orden_id))
        if not orden:
            continue
        if orden["estado"] == "pagada" and not orden.get("pagos"):
            supabase.table("ordenes_compra").update({"estado": "autorizada"}).eq("id", orden_id).execute()
        monto = sum(float(f["monto"] or 0) for f in filas if f["orden_id"] == orden_id)
        pago = next(f for f in filas if f["orden_id"] == orden_id)
        registrar_evento(orden_id, "Pago eliminado",
                         detalle=f"{pago['metodo']} · ${monto:,.2f} del {pago['fecha_pago']}"
                                 + (f" · {pago['referencia']}" if pago.get("referencia") else ""),
                         usuario_id=usuario["id"])
    return {"borrados": len(filas), "ordenes": len({f["orden_id"] for f in filas})}
