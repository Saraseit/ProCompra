"""
app/api/ordenes.py
"""

import time
from datetime import date, datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional
from uuid import UUID
from app.database import supabase, una_fila
from app.auth import usuario_actual, requiere_rol
from app.roles import ROLES_ADMIN, ROLES_OPERACION, ROLES_CONTABILIDAD, ROLES_TODOS
from app.utils.pdf import generar_pdf_orden
from app.utils import ajustes, credito, notificaciones, storage
from app.utils.bandeja import falta_tabla
from app.api import precios

router = APIRouter(prefix="/ordenes", tags=["Órdenes de compra"])

# Valores del enum metodo_pago en Postgres. Agregar uno aquí exige antes un
# ALTER TYPE metodo_pago ADD VALUE en la base, o el insert falla.
METODOS_PAGO = ("transferencia", "tarjeta_credito", "tarjeta_debito", "efectivo", "credito")

# Formas en que contabilidad entrega dinero o tarjeta a quien hará la compra.
METODOS_CON_ENTREGA = ("efectivo", "tarjeta_credito", "tarjeta_debito")

ESTADOS_ORDEN = (
    "borrador", "autorizacion", "autorizada",
    "pagada", "recoleccion", "comprobacion", "cerrada", "rechazada",
)

# Cambios permitidos por PATCH /estado: destino -> (orígenes, roles, motivo obligatorio).
# 'borrador' desde 'autorizacion' es la devolución para corrección; 'rechazada'
# es la cancelación (también la usa el borrado lógico de admin).
TRANSICIONES = {
    "autorizacion": ({"borrador"}, ROLES_OPERACION, False),
    "autorizada":   ({"autorizacion"}, ROLES_CONTABILIDAD, False),
    "borrador":     ({"autorizacion"}, ROLES_CONTABILIDAD, True),
    "rechazada":    ({"borrador", "autorizacion", "autorizada"}, ROLES_CONTABILIDAD, True),
}

EVENTOS_ESTADO = {
    "autorizacion": "Enviada a autorización",
    "autorizada":   "Autorizada",
    "borrador":     "Devuelta para corrección",
    "rechazada":    "Cancelada",
}

# Adjuntos que cuentan como comprobante de la compra.
TIPOS_COMPROBANTE = ("ticket", "factura")

# PostgREST rechaza los saltos de línea dentro de select: si la cadena es
# multilínea las relaciones anidadas se pierden sin error. Van en una sola línea.
SELECT_LISTADO = (
    "*,proveedor:proveedor_id(id,nombre,correo,telefono),"
    "creador:creado_por(id,nombre),partidas(concepto),adjuntos(count)"
)
SELECT_DETALLE = (
    "*,proveedor:proveedor_id(*),creador:creado_por(id,nombre,correo)"
)
SELECT_ADJUNTO = "*,subido:subido_por(id,nombre)"
SELECT_FACTURA_ORDEN = (
    "id,uuid_fiscal,serie,folio,total,fecha_emision,nombre_emisor,rfc_emisor,vinculacion"
)


def _texto_requerido(valor: str) -> str:
    limpio = (valor or "").strip()
    if not limpio:
        raise ValueError("no puede quedar vacío")
    return limpio


def _texto_opcional(valor: Optional[str]) -> Optional[str]:
    limpio = (valor or "").strip()
    return limpio or None


def _uuid_valido(valor: str) -> str:
    try:
        return str(UUID(str(valor)))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("no es un identificador válido")


def _metodo_valido(v: str) -> str:
    if v not in METODOS_PAGO:
        raise ValueError(f"debe ser uno de: {', '.join(METODOS_PAGO)}")
    return v


class PartidaInput(BaseModel):
    req_id: Optional[str] = None
    concepto: str
    cantidad: float = Field(gt=0)
    unidad: str
    precio_unitario: float = Field(ge=0)
    contrato: Optional[str] = None

    @field_validator("concepto", "unidad")
    @classmethod
    def _no_vacio(cls, v: str) -> str:
        return _texto_requerido(v)

    @field_validator("req_id")
    @classmethod
    def _req_id_valido(cls, v: Optional[str]) -> Optional[str]:
        return _uuid_valido(v) if v else None


class OrdenCreate(BaseModel):
    proveedor_id: str
    tipo_pago: str = "transferencia"
    observaciones: Optional[str] = None
    partidas: list[PartidaInput] = Field(min_length=1)

    @field_validator("proveedor_id")
    @classmethod
    def _proveedor_valido(cls, v: str) -> str:
        return _uuid_valido(v)

    @field_validator("tipo_pago")
    @classmethod
    def _tipo_pago_valido(cls, v: str) -> str:
        return _metodo_valido(v)


class OrdenUpdate(BaseModel):
    tipo_pago: Optional[str] = None
    observaciones: Optional[str] = None

    @field_validator("tipo_pago")
    @classmethod
    def _tipo_pago_valido(cls, v: Optional[str]) -> Optional[str]:
        return _metodo_valido(v) if v is not None else None


class EstadoUpdate(BaseModel):
    estado: str
    detalle: Optional[str] = None

    @field_validator("detalle")
    @classmethod
    def _detalle_limpio(cls, v: Optional[str]) -> Optional[str]:
        return _texto_opcional(v)


class DatosPago(BaseModel):
    """Cómo se pagó: lo común a un pago de una orden y a uno de varias.

    - transferencia: exige referencia.
    - efectivo / tarjeta: exige a quién se le entregó.
    - 'credito' no es una forma de pagar: es una condición de la orden.
    """
    fecha_pago: str
    metodo: str
    referencia: Optional[str] = None
    entregado_a: Optional[str] = None
    comprobante_url: Optional[str] = None
    notas: Optional[str] = None

    @field_validator("referencia", "entregado_a", "notas")
    @classmethod
    def _limpiar(cls, v: Optional[str]) -> Optional[str]:
        return _texto_opcional(v)

    @field_validator("metodo")
    @classmethod
    def _metodo_valido(cls, v: str) -> str:
        _metodo_valido(v)
        if v == "credito":
            raise ValueError("el crédito no es un pago: autoriza la orden a crédito "
                             "y registra el pago cuando se liquide")
        return v

    @model_validator(mode="after")
    def _datos_por_metodo(self):
        if self.metodo == "transferencia" and not self.referencia:
            raise ValueError("la transferencia necesita referencia")
        if self.metodo in METODOS_CON_ENTREGA and not self.entregado_a:
            raise ValueError("indica a quién se le entregó el efectivo o la tarjeta")
        return self


class PagoCreate(DatosPago):
    """Pago a proveedor o entrega de recursos para una sola orden."""
    monto: float = Field(gt=0)


class PagoProgramado(BaseModel):
    """Una fecha del calendario de pagos de una compra a crédito."""
    fecha: str
    monto: float = Field(gt=0)
    notas: Optional[str] = None

    @field_validator("fecha")
    @classmethod
    def _fecha_valida(cls, v: str) -> str:
        try:
            return date.fromisoformat(str(v)[:10]).isoformat()
        except ValueError:
            raise ValueError("debe ser una fecha AAAA-MM-DD")

    @field_validator("notas")
    @classmethod
    def _limpiar(cls, v: Optional[str]) -> Optional[str]:
        return _texto_opcional(v)


class Calendario(BaseModel):
    """El calendario completo; una lista vacía quita todas las fechas."""
    programados: list[PagoProgramado] = Field(default_factory=list, max_length=36)


class BorradoOrdenes(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    con_requerimientos: bool = False

    @field_validator("ids")
    @classmethod
    def _ids_validos(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(_uuid_valido(i) for i in v))


class RecoleccionCreate(BaseModel):
    tipo: str = "recoleccion"
    fecha_programada: Optional[str] = None
    responsable: Optional[str] = None
    notas: Optional[str] = None
    completado: bool = False
    fecha_real: Optional[str] = None


class ComprobacionInput(BaseModel):
    monto_comprobado: float = Field(gt=0)
    cambio_devuelto: Optional[float] = Field(default=None, ge=0)
    notas: Optional[str] = None

    @field_validator("notas")
    @classmethod
    def _limpiar(cls, v: Optional[str]) -> Optional[str]:
        return _texto_opcional(v)


def registrar_evento(orden_id: str, evento: str, detalle: str = None, usuario_id: str = None):
    supabase.table("historial").insert({
        "orden_id": orden_id,
        "evento": evento,
        "detalle": detalle,
        "usuario_id": usuario_id,
    }).execute()


def da_credito(proveedor: Optional[dict]) -> bool:
    """¿El proveedor nos da crédito? Antes de la fase 7 se deducía de dias_credito."""
    proveedor = proveedor or {}
    if "da_credito" in proveedor:
        return bool(proveedor["da_credito"])
    return bool(proveedor.get("dias_credito"))


_calendario = {"listo": False, "revisado": 0.0}


def calendario_disponible() -> bool:
    """¿Ya existe pagos_programados (fase 7)? Sin ella toda deuda queda sin fecha."""
    if _calendario["listo"]:
        return True
    if time.time() - _calendario["revisado"] < 30:
        return False
    _calendario["revisado"] = time.time()
    try:
        supabase.table("pagos_programados").select("id").limit(1).execute()
        _calendario["listo"] = True
    except Exception as e:
        if not falta_tabla(e):
            raise
    return _calendario["listo"]


# Embebido del calendario para los listados de crédito y pagos.
EMBED_PROGRAMADOS = ",programados:pagos_programados(id,fecha,monto,notas)"


def con_programados(select: str) -> str:
    return select + EMBED_PROGRAMADOS if calendario_disponible() else select


# Estados en que una orden recibe pagos: la de crédito en cuanto se autoriza
# y hasta después de cerrada; la de contado hasta su comprobación.
ESTADOS_PAGO_CREDITO = ("autorizada", "recoleccion", "comprobacion", "cerrada")
ESTADOS_PAGO_CONTADO = ("autorizada", "pagada", "recoleccion", "comprobacion")


def motivo_sin_pago(orden: dict) -> Optional[str]:
    """Por qué la orden no puede recibir un pago (None si sí puede)."""
    if orden["tipo_pago"] == "credito":
        if orden["estado"] not in ESTADOS_PAGO_CREDITO:
            return f"La orden #{orden.get('folio')} aún no está autorizada"
    elif orden["estado"] not in ESTADOS_PAGO_CONTADO:
        return f"La orden #{orden.get('folio')} no está autorizada: solo se pagan órdenes autorizadas"
    return None


def despues_del_pago(orden: dict, pago: dict, usuario_id: str, nota: Optional[str] = None) -> None:
    """Avanza la orden de contado a pagada y deja el pago en su historial."""
    a_credito = orden["tipo_pago"] == "credito"
    if orden["estado"] == "autorizada" and not a_credito:
        # La forma real de pago la decide contabilidad al procesarlo.
        supabase.table("ordenes_compra").update(
            {"estado": "pagada", "tipo_pago": pago["metodo"]}
        ).eq("id", orden["id"]).execute()

    monto = float(pago["monto"])
    if pago["metodo"] in METODOS_CON_ENTREGA:
        evento = f"Recursos entregados a {pago.get('entregado_a')}"
        detalle = f"{pago['metodo']} · ${monto:,.2f}" + (f" · {pago['referencia']}" if pago.get("referencia") else "")
    else:
        evento = "Pago a crédito registrado" if a_credito else "Pago registrado"
        detalle = f"Ref: {pago.get('referencia')} · {pago['metodo']} · ${monto:,.2f}"
    if nota:
        detalle += f" · {nota}"
    registrar_evento(orden["id"], evento, detalle=detalle, usuario_id=usuario_id)


def _orden_minima(orden_id: str, campos: str = "id, folio, estado, tipo_pago, total, creado_por") -> dict:
    orden = una_fila(supabase.table("ordenes_compra").select(campos).eq("id", orden_id))
    if not orden:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    return orden


def _liberar_requerimientos(orden_id: str) -> None:
    """Al cancelar una orden sus requerimientos vuelven a pendientes.

    Sin esto quedaban como 'en_orden' apuntando a una orden cancelada y
    desaparecían de la lista de pendientes sin que nadie lo notara.
    """
    supabase.table("requerimientos").update({
        "estado": "pendiente",
        "orden_id": None,
    }).eq("orden_id", orden_id).eq("estado", "en_orden").execute()


def orden_completa(orden_id: str) -> Optional[dict]:
    orden = una_fila(
        supabase.table("ordenes_compra")
        .select(SELECT_DETALLE)
        .eq("id", orden_id)
    )
    if not orden:
        return None

    tabla = lambda nombre, sel="*": supabase.table(nombre).select(sel).eq("orden_id", orden_id)
    orden["partidas"]      = tabla("partidas").execute().data
    orden["pagos"]         = tabla("pagos").order("created_at").execute().data
    orden["recolecciones"] = tabla("recolecciones").order("created_at").execute().data
    orden["adjuntos"]      = tabla("adjuntos", SELECT_ADJUNTO).order("created_at").execute().data
    orden["facturas"]      = tabla("facturas", SELECT_FACTURA_ORDEN).order("created_at").execute().data
    orden["programados"]   = (
        tabla("pagos_programados", "id,fecha,monto,notas").order("fecha").execute().data
        if calendario_disponible() else []
    )
    orden["calendario"]    = credito.movimiento(orden, date.today()) if orden.get("tipo_pago") == "credito" else None
    orden["historial"]     = (
        supabase.table("historial")
        .select("*,usuario:usuario_id(id,nombre)")
        .eq("orden_id", orden_id)
        .order("created_at", desc=True)
        .execute()
    ).data
    _agregar_vinculos(orden)
    return orden


# Orden ligada (origen o complemento) tal como se muestra en el detalle.
SELECT_ORDEN_LIGADA = "id,folio,estado,total,proveedor:proveedor_id(nombre)"
SELECT_PARTIDA_LIGADA = (
    "id,cantidad,cantidad_original,partida_origen_id,"
    "orden:orden_id(id,folio,estado,proveedor:proveedor_id(nombre))"
)


def _agregar_vinculos(orden: dict) -> None:
    """Orden de origen, complementos y reparto del surtido de cada partida.

    Usa columnas de la migración de la fase 4; sin ella la orden se muestra
    sin vínculos en lugar de fallar.
    """
    orden["origen"] = None
    orden["complementos"] = []
    try:
        if orden.get("orden_origen_id"):
            orden["origen"] = una_fila(
                supabase.table("ordenes_compra").select(SELECT_ORDEN_LIGADA).eq("id", orden["orden_origen_id"])
            )
        orden["complementos"] = (
            supabase.table("ordenes_compra").select(SELECT_ORDEN_LIGADA)
            .eq("orden_origen_id", orden["id"]).neq("estado", "rechazada").order("folio").execute()
        ).data or []

        propia = {"id": orden["id"], "folio": orden.get("folio"), "estado": orden.get("estado"),
                  "proveedor": {"nombre": (orden.get("proveedor") or {}).get("nombre")}}
        raices = {p["id"]: {**p, "orden": propia} for p in orden["partidas"] if p.get("cantidad_original") is not None}
        origenes = [p["partida_origen_id"] for p in orden["partidas"] if p.get("partida_origen_id")]
        if origenes:
            for r in supabase.table("partidas").select(SELECT_PARTIDA_LIGADA).in_("id", origenes).execute().data or []:
                raices[r["id"]] = r
        if not raices:
            return
        hijos = [
            h for h in supabase.table("partidas").select(SELECT_PARTIDA_LIGADA)
            .in_("partida_origen_id", list(raices)).execute().data or []
            if (h.get("orden") or {}).get("estado") != "rechazada"
        ]
        for p in orden["partidas"]:
            raiz_id = p["id"] if p["id"] in raices else p.get("partida_origen_id")
            if raiz_id in raices:
                p["surtido"] = ajustes.reparto(
                    raices[raiz_id], [h for h in hijos if h["partida_origen_id"] == raiz_id], p["id"]
                )
    except Exception as e:
        if not falta_tabla(e):
            print(f"[VINCULOS] orden {orden.get('id')}: {e}")


def _avisar(tareas: BackgroundTasks, funcion, orden_id: str, *args) -> None:
    """Programa un correo con la orden completa, sin retrasar la respuesta."""
    def _tarea():
        try:
            orden = orden_completa(orden_id)
            if orden:
                funcion(orden, *args)
        except Exception as e:
            print(f"[AVISO ERROR] {funcion.__name__} orden {orden_id}: {e}")
    tareas.add_task(_tarea)


@router.get("")
def listar_ordenes(
    estado: Optional[str] = None,
    usuario=Depends(usuario_actual),
):
    query = (
        supabase.table("ordenes_compra")
        .select(SELECT_LISTADO)
        .order("created_at", desc=True)
    )
    if estado:
        if estado not in ESTADOS_ORDEN:
            raise HTTPException(status_code=400, detail=f"Estado inválido: {estado}")
        query = query.eq("estado", estado)
    else:
        # Las canceladas (estado 'rechazada') no aparecen en el listado general.
        # Siguen accesibles pidiendo ?estado=rechazada.
        query = query.neq("estado", "rechazada")

    res = query.execute()
    return res.data


@router.get("/{orden_id}")
def obtener_orden(orden_id: str, usuario=Depends(usuario_actual)):
    orden = orden_completa(orden_id)
    if not orden:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    return orden


@router.get("/{orden_id}/pdf")
def descargar_pdf(orden_id: str, usuario=Depends(usuario_actual)):
    """Devuelve la orden como PDF. Respeta la misma visibilidad que el detalle."""
    orden = obtener_orden(orden_id, usuario)
    buffer = generar_pdf_orden(orden)
    nombre = f"orden-{orden.get('folio')}.pdf"
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@router.post("", status_code=201)
def crear_orden(
    orden: OrdenCreate,
    usuario=Depends(requiere_rol(*ROLES_OPERACION)),
):
    proveedor = una_fila(
        supabase.table("proveedores")
        .select("*")
        .eq("id", orden.proveedor_id)
    )
    if not proveedor:
        raise HTTPException(status_code=400, detail="El proveedor no existe")
    if not proveedor.get("activo"):
        raise HTTPException(status_code=400, detail="El proveedor está desactivado")

    # Con quien nos da crédito la orden nace a crédito: no espera un pago
    # previo y su saldo se lleva en Crédito y en Pagos.
    a_credito = da_credito(proveedor)
    orden_data = {
        "proveedor_id":  orden.proveedor_id,
        "creado_por":    usuario["id"],
        "tipo_pago":     "credito" if a_credito else orden.tipo_pago,
        "observaciones": orden.observaciones,
        "estado":        "borrador",
    }
    res_orden = supabase.table("ordenes_compra").insert(orden_data).execute()
    if not res_orden.data:
        raise HTTPException(status_code=400, detail="No se pudo crear la orden")
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
    try:
        res_partidas = supabase.table("partidas").insert(partidas_data).execute()
        if not res_partidas.data:
            raise ValueError("insert de partidas sin resultado")
    except Exception:
        # Sin partidas la orden queda en blanco y con folio consumido: se descarta.
        supabase.table("ordenes_compra").delete().eq("id", orden_id).execute()
        raise HTTPException(status_code=400, detail="No se pudieron crear las partidas de la orden")

    req_ids = [p.req_id for p in orden.partidas if p.req_id]
    if req_ids:
        supabase.table("requerimientos").update({
            "estado":   "en_orden",
            "orden_id": orden_id,
        }).in_("id", req_ids).execute()

    registrar_evento(orden_id, "Orden creada",
                     detalle="A crédito: la fecha de pago se captura en la orden cuando se conozca"
                     if a_credito else None,
                     usuario_id=usuario["id"])
    return obtener_orden(orden_id, usuario)


@router.put("/{orden_id}")
def actualizar_orden(
    orden_id: str,
    body: OrdenUpdate,
    usuario=Depends(requiere_rol(*ROLES_TODOS)),
):
    """En borrador, operación edita tipo de pago y observaciones.

    Ya autorizada, contabilidad puede cambiar la forma de pago: es quien
    decide al final si se paga por transferencia, efectivo, tarjeta o crédito.
    """
    orden = _orden_minima(orden_id)
    datos = {k: v for k, v in body.model_dump().items() if v is not None}
    if not datos:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar")

    if orden["estado"] == "borrador":
        if usuario["rol"] not in ROLES_OPERACION:
            raise HTTPException(status_code=403, detail="Solo almacén o compras editan un borrador")
    elif orden["estado"] == "autorizada":
        if usuario["rol"] not in ROLES_CONTABILIDAD:
            raise HTTPException(status_code=403, detail="Solo contabilidad cambia la forma de pago")
        if set(datos) != {"tipo_pago"}:
            raise HTTPException(status_code=400, detail="En una orden autorizada solo se cambia la forma de pago")
    else:
        raise HTTPException(status_code=400, detail="Esta orden ya no se puede editar")

    res = (
        supabase.table("ordenes_compra")
        .update(datos)
        .eq("id", orden_id)
        .execute()
    )
    if "tipo_pago" in datos and datos["tipo_pago"] != orden["tipo_pago"]:
        registrar_evento(orden_id, "Forma de pago cambiada",
                         detalle=f"{orden['tipo_pago']} → {datos['tipo_pago']}",
                         usuario_id=usuario["id"])
    else:
        registrar_evento(orden_id, "Orden actualizada", usuario_id=usuario["id"])
    return res.data[0]


@router.patch("/{orden_id}/estado")
def cambiar_estado(
    orden_id: str,
    body: EstadoUpdate,
    tareas: BackgroundTasks,
    usuario=Depends(requiere_rol(*ROLES_TODOS)),
):
    regla = TRANSICIONES.get(body.estado)
    if not regla:
        raise HTTPException(status_code=400, detail=f"Estado inválido: {body.estado}")
    origenes, roles, motivo_requerido = regla

    if usuario["rol"] not in roles:
        raise HTTPException(status_code=403, detail="Tu rol no puede hacer este cambio")
    if motivo_requerido and not body.detalle:
        raise HTTPException(status_code=400, detail="Indica el motivo")

    orden = _orden_minima(orden_id)
    if orden["estado"] not in origenes:
        raise HTTPException(
            status_code=409,
            detail=f"La orden está en '{orden['estado']}' y no puede pasar a '{body.estado}'",
        )

    datos = {"estado": body.estado}
    if body.estado == "borrador":
        datos["motivo_devolucion"] = body.detalle
    elif body.estado == "autorizacion":
        datos["motivo_devolucion"] = None

    res = (
        supabase.table("ordenes_compra")
        .update(datos)
        .eq("id", orden_id)
        .execute()
    )
    if body.estado == "rechazada":
        _liberar_requerimientos(orden_id)

    registrar_evento(orden_id, EVENTOS_ESTADO[body.estado],
                     detalle=body.detalle, usuario_id=usuario["id"])

    if body.estado == "autorizacion":
        _avisar(tareas, notificaciones.por_autorizar, orden_id)
        tareas.add_task(precios.revisar_orden, orden_id, usuario["id"])
    elif body.estado == "autorizada":
        _avisar(tareas, notificaciones.resultado_autorizacion, orden_id, "autorizada")
    elif body.estado == "borrador":
        _avisar(tareas, notificaciones.resultado_autorizacion, orden_id, "devuelta", body.detalle)
    elif body.estado == "rechazada":
        _avisar(tareas, notificaciones.resultado_autorizacion, orden_id, "cancelada", body.detalle)
    return res.data[0]


@router.post("/{orden_id}/pago", status_code=201)
def registrar_pago(
    orden_id: str,
    pago: PagoCreate,
    usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD)),
):
    orden = _orden_minima(orden_id)
    # La compra a crédito no espera el pago: se liquida cuando toque.
    motivo = motivo_sin_pago(orden)
    if motivo:
        raise HTTPException(status_code=409, detail=motivo)

    datos = pago.model_dump()
    datos["orden_id"]       = orden_id
    datos["registrado_por"] = usuario["id"]

    res = supabase.table("pagos").insert(datos).execute()
    if not res.data:
        raise HTTPException(status_code=400, detail="No se pudo registrar el pago")

    despues_del_pago(orden, datos, usuario["id"])
    return res.data[0]


@router.put("/{orden_id}/programados")
def guardar_calendario(
    orden_id: str,
    body: Calendario,
    usuario=Depends(requiere_rol(*ROLES_TODOS)),
):
    """Fechas y montos en que se pagará una compra a crédito.

    Lo captura quien lo sepa (quien negoció con el proveedor o contabilidad) y
    se puede cambiar cuando se difiere. Si el proveedor cobra recargo por
    diferir, la suma del calendario puede ser mayor que la orden.
    """
    if not calendario_disponible():
        raise HTTPException(status_code=503, detail="Falta activar la actualización de la base de datos "
                                                    "(fase 7) para capturar fechas de pago")
    orden = _orden_minima(orden_id)
    if orden["tipo_pago"] != "credito":
        raise HTTPException(status_code=400, detail="Solo las órdenes a crédito llevan calendario de pagos")
    if orden["estado"] == "rechazada":
        raise HTTPException(status_code=409, detail="La orden está cancelada")

    supabase.table("pagos_programados").delete().eq("orden_id", orden_id).execute()
    filas = [
        {"orden_id": orden_id, "fecha": p.fecha, "monto": round(p.monto, 2), "notas": p.notas,
         "creado_por": usuario["id"]}
        for p in sorted(body.programados, key=lambda p: p.fecha)
    ]
    if filas:
        supabase.table("pagos_programados").insert(filas).execute()

    total = sum(f["monto"] for f in filas)
    detalle = "\n".join(
        f"{date.fromisoformat(f['fecha']).strftime('%d/%m/%Y')} · ${f['monto']:,.2f}" + (f" · {f['notas']}" if f["notas"] else "")
        for f in filas
    ) or "Sin fechas: queda pendiente de programar"
    if len(filas) > 1:
        detalle += f"\nTotal ${total:,.2f} en {len(filas)} pagos"
    registrar_evento(orden_id, "Fechas de pago actualizadas", detalle=detalle, usuario_id=usuario["id"])
    return orden_completa(orden_id)["calendario"]


@router.post("/{orden_id}/recoleccion", status_code=201)
def registrar_recoleccion(
    orden_id: str,
    rec: RecoleccionCreate,
    tareas: BackgroundTasks,
    usuario=Depends(requiere_rol(*ROLES_TODOS)),
):
    orden = _orden_minima(orden_id)
    permitida = orden["estado"] in ("pagada", "recoleccion") or (
        orden["estado"] == "autorizada" and orden["tipo_pago"] == "credito"
    )
    if not permitida:
        raise HTTPException(
            status_code=409,
            detail="La logística se programa cuando la orden está pagada (o autorizada a crédito)",
        )

    datos = rec.model_dump()
    datos["orden_id"] = orden_id
    if rec.completado and not rec.fecha_real:
        datos["fecha_real"] = date.today().isoformat()

    res = supabase.table("recolecciones").insert(datos).execute()
    if not res.data:
        raise HTTPException(status_code=400, detail="No se pudo registrar la recolección")

    # Recibido el material falta el ticket o la factura: la orden pasa a
    # comprobación y se cierra cuando contabilidad la revisa.
    nuevo_estado = "comprobacion" if rec.completado else "recoleccion"
    supabase.table("ordenes_compra").update(
        {"estado": nuevo_estado}
    ).eq("id", orden_id).execute()

    registrar_evento(
        orden_id,
        evento="Material recibido" if rec.completado else f"Logística programada ({rec.tipo})",
        detalle=rec.notas,
        usuario_id=usuario["id"],
    )
    return res.data[0]


@router.post("/{orden_id}/comprobacion")
def registrar_comprobacion(
    orden_id: str,
    body: ComprobacionInput,
    tareas: BackgroundTasks,
    usuario=Depends(requiere_rol(*ROLES_TODOS)),
):
    """Monto real de la compra según el ticket o la factura adjuntos."""
    orden = _orden_minima(orden_id)
    if orden["estado"] != "comprobacion":
        raise HTTPException(status_code=409, detail="La orden no está en comprobación")

    adjuntos = (
        supabase.table("adjuntos").select("id", count="exact")
        .eq("orden_id", orden_id).in_("tipo", list(TIPOS_COMPROBANTE)).execute()
    )
    facturas = (
        supabase.table("facturas").select("id", count="exact")
        .eq("orden_id", orden_id).execute()
    )
    if not (adjuntos.count or facturas.count):
        raise HTTPException(
            status_code=400,
            detail="Adjunta el ticket o la factura antes de registrar la comprobación",
        )

    datos = {
        "monto_comprobado":   round(body.monto_comprobado, 2),
        "cambio_devuelto":    round(body.cambio_devuelto, 2) if body.cambio_devuelto is not None else None,
        "notas_comprobacion": body.notas,
        "comprobada_por":     usuario["id"],
        "comprobada_at":      datetime.now(timezone.utc).isoformat(),
    }
    res = supabase.table("ordenes_compra").update(datos).eq("id", orden_id).execute()

    detalle = f"Monto real ${body.monto_comprobado:,.2f}"
    if body.cambio_devuelto:
        detalle += f" · cambio devuelto ${body.cambio_devuelto:,.2f}"
    registrar_evento(orden_id, "Comprobación registrada", detalle=detalle, usuario_id=usuario["id"])
    _avisar(tareas, notificaciones.comprobacion_lista, orden_id)
    return res.data[0]


@router.post("/{orden_id}/cerrar")
def cerrar_orden(
    orden_id: str,
    usuario=Depends(requiere_rol(*ROLES_CONTABILIDAD)),
):
    orden = _orden_minima(orden_id, "id, estado, monto_comprobado")
    if orden["estado"] != "comprobacion":
        raise HTTPException(status_code=409, detail="Solo se cierran órdenes en comprobación")
    if orden.get("monto_comprobado") is None:
        raise HTTPException(status_code=400, detail="Falta registrar la comprobación del gasto")

    res = (
        supabase.table("ordenes_compra")
        .update({"estado": "cerrada"})
        .eq("id", orden_id)
        .execute()
    )
    registrar_evento(orden_id, "Orden cerrada", detalle="Comprobación revisada por contabilidad",
                     usuario_id=usuario["id"])
    return res.data[0]


@router.delete("/{orden_id}", status_code=204)
def eliminar_orden(
    orden_id: str,
    usuario=Depends(requiere_rol(*ROLES_ADMIN)),
):
    """Soft delete — solo admin puede eliminar órdenes.

    Marca la orden como cancelada ('rechazada'), estado que el listado general
    omite, y devuelve sus requerimientos a pendientes.
    """
    actual = una_fila(
        supabase.table("ordenes_compra")
        .select("estado")
        .eq("id", orden_id)
    )
    if not actual:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    estado = actual["estado"]
    if estado == "rechazada":
        raise HTTPException(status_code=409, detail="La orden ya está cancelada")
    if estado in ("pagada", "recoleccion", "comprobacion", "cerrada"):
        raise HTTPException(
            status_code=409,
            detail="No se puede eliminar una orden que ya fue pagada",
        )

    supabase.table("ordenes_compra").update(
        {"estado": "rechazada"}
    ).eq("id", orden_id).execute()
    _liberar_requerimientos(orden_id)
    registrar_evento(orden_id, "Orden eliminada", usuario_id=usuario["id"])


# ── Borrado definitivo (solo admin) ──────────────────────────────────────

def _lotes(valores, n: int = 50):
    """Trozos para filtros in_(): una URL de PostgREST con cientos de ids falla."""
    valores = list(valores)
    for i in range(0, len(valores), n):
        yield valores[i:i + n]


def _filas(tabla: str, campos: str, columna: str, valores) -> list[dict]:
    filas = []
    for lote in _lotes(valores):
        filas += supabase.table(tabla).select(campos).in_(columna, lote).execute().data or []
    return filas


def _reubicar_requerimientos(req_ids: set, borradas: set) -> int:
    """Los requerimientos de las órdenes borradas pasan a la orden viva que
    todavía los surte (un complemento) o vuelven a pendientes.

    Devuelve cuántos volvieron a pendientes.
    """
    liberados = 0
    for req in _filas("requerimientos", "id, estado, orden_id", "id", req_ids):
        if req["estado"] == "cancelado":
            if req.get("orden_id") in borradas:
                supabase.table("requerimientos").update({"orden_id": None}).eq("id", req["id"]).execute()
            continue
        vivas = (
            supabase.table("partidas").select("orden:orden_id(id,estado)").eq("req_id", req["id"]).execute()
        ).data or []
        destino = next((
            o["id"] for o in ((p.get("orden") or {}) for p in vivas)
            if o.get("id") and o["id"] not in borradas and o.get("estado") != "rechazada"
        ), None)
        if destino:
            datos = {"estado": "en_orden", "orden_id": destino}
        else:
            datos = {"estado": "pendiente", "orden_id": None}
            liberados += 1
        supabase.table("requerimientos").update(datos).eq("id", req["id"]).execute()
    return liberados


@router.post("/borrar")
def borrar_definitivo(
    body: BorradoOrdenes,
    usuario=Depends(requiere_rol("admin")),
):
    """Borra las órdenes por completo, para limpiar capturas de prueba o errores.

    Se van sus partidas, pagos, logística, historial, avisos y documentos.
    Las facturas no se borran: quedan sin vincular. Los requerimientos vuelven
    a pendientes, salvo que se pida borrarlos también.
    """
    ordenes = _filas("ordenes_compra", "id, folio", "id", body.ids)
    if not ordenes:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    ids = [o["id"] for o in ordenes]
    borradas = set(ids)

    partidas = _filas("partidas", "id, req_id", "orden_id", ids)
    req_ids = {p["req_id"] for p in partidas if p.get("req_id")} | {
        r["id"] for r in _filas("requerimientos", "id", "orden_id", ids)
    }
    adjuntos = _filas("adjuntos", "id, ruta", "orden_id", ids)
    movimientos = {p.get("movimiento_id") for p in _filas("pagos", "*", "orden_id", ids)} - {None}

    # Primero se suelta todo lo que apunta a estas órdenes sin borrarse con ellas.
    for lote in _lotes([p["id"] for p in partidas]):
        supabase.table("partidas").update({"partida_origen_id": None}).in_("partida_origen_id", lote).execute()
    desvinculadas = 0
    for lote in _lotes(ids):
        supabase.table("ordenes_compra").update({"orden_origen_id": None}).in_("orden_origen_id", lote).execute()
        res = supabase.table("facturas").update(
            {"orden_id": None, "vinculacion": "pendiente"}
        ).in_("orden_id", lote).execute()
        desvinculadas += len(res.data or [])
        supabase.table("adjuntos").delete().in_("orden_id", lote).execute()

    liberados = 0
    if body.con_requerimientos:
        for lote in _lotes(ids):
            supabase.table("requerimientos").update({"orden_id": None}).in_("orden_id", lote).execute()
    else:
        liberados = _reubicar_requerimientos(req_ids, borradas)

    # Partidas, pagos, logística, historial y avisos se borran en cascada.
    for lote in _lotes(ids):
        supabase.table("ordenes_compra").delete().in_("id", lote).execute()

    borrados_req = 0
    if body.con_requerimientos and req_ids:
        for lote in _lotes(req_ids):
            supabase.table("partidas").update({"req_id": None}).in_("req_id", lote).execute()
            res = supabase.table("requerimientos").delete().in_("id", lote).execute()
            borrados_req += len(res.data or [])

    # Un pago de varias órdenes que se quedó sin ninguna desaparece también.
    if movimientos:
        quedan = {p["movimiento_id"] for p in _filas("pagos", "movimiento_id", "movimiento_id", movimientos)}
        for lote in _lotes(movimientos - quedan):
            supabase.table("movimientos_pago").delete().in_("id", lote).execute()

    storage.borrar_varios([a["ruta"] for a in adjuntos if a.get("ruta")])

    folios = sorted(o["folio"] for o in ordenes if o.get("folio") is not None)
    print(f"[BORRADO] {usuario.get('nombre') or usuario['id']} borró las órdenes {folios}"
          f" (requerimientos {'borrados' if body.con_requerimientos else 'liberados'})")
    return {
        "borradas": len(ids),
        "folios": folios,
        "requerimientos_borrados": borrados_req,
        "requerimientos_liberados": liberados,
        "facturas_desvinculadas": desvinculadas,
        "documentos_borrados": len(adjuntos),
    }
