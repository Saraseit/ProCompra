"""
app/api/facturas.py
-------------------
Facturas CFDI de proveedores.

Se cargan por XML, uno o muchos a la vez. Cada factura se vincula sola a su
orden cuando hay exactamente una candidata (mismo RFC del proveedor, total que
cuadra y fecha razonable); si no, queda pendiente para vincularla a mano.
"""

from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, field_validator

from app.auth import requiere_rol
from app.database import supabase, una_fila
from app.api.ordenes import registrar_evento, _uuid_valido
from app.utils import cfdi, storage

router = APIRouter(prefix="/facturas", tags=["Facturas"])

ROLES_FACTURAS = ("admin", "compras", "pagos")
MAXIMO_ARCHIVOS = 200

SELECT_FACTURA = "*,orden:orden_id(id,folio,estado,total)"
SELECT_ORDEN_VINCULABLE = (
    "id,folio,fecha,estado,total,monto_comprobado,proveedor:proveedor_id(id,nombre,rfc)"
)


class Vinculo(BaseModel):
    orden_id: Optional[str] = None

    @field_validator("orden_id")
    @classmethod
    def _valido(cls, v: Optional[str]) -> Optional[str]:
        return _uuid_valido(v) if v else None


def _ordenes_vinculables() -> list[dict]:
    return (
        supabase.table("ordenes_compra")
        .select(SELECT_ORDEN_VINCULABLE)
        .in_("estado", list(cfdi.ESTADOS_FACTURABLES))
        .execute()
    ).data or []


def _ordenes_con_factura() -> set:
    filas = (
        supabase.table("facturas").select("orden_id").not_.is_("orden_id", "null").execute()
    ).data or []
    return {f["orden_id"] for f in filas}


def _completar_rfc_proveedor(orden_id: str, rfc: str) -> None:
    """Si el proveedor no tenía RFC se toma el de su factura: así las
    siguientes facturas de ese proveedor ya se vinculan solas."""
    orden = una_fila(
        supabase.table("ordenes_compra").select("proveedor:proveedor_id(id,rfc)").eq("id", orden_id)
    )
    prov = (orden or {}).get("proveedor") or {}
    if prov.get("id") and not (prov.get("rfc") or "").strip() and rfc:
        supabase.table("proveedores").update({"rfc": rfc}).eq("id", prov["id"]).execute()


def registrar_xml(contenido: bytes, nombre: str, usuario: dict, orden_id: str = None,
                  contexto: dict = None) -> dict:
    """Lee un CFDI, lo guarda y lo vincula. Nunca lanza por un XML malo:
    devuelve el resultado para reportarlo junto con los demás archivos.

    `contexto` permite reutilizar la lista de órdenes en una carga masiva.
    """
    base = {"archivo": nombre}
    try:
        datos = cfdi.leer_cfdi(contenido)
    except cfdi.CfdiInvalido as e:
        return {**base, "estado": "invalida", "mensaje": str(e)}

    base.update({"total": datos["total"], "emisor": datos["nombre_emisor"] or datos["rfc_emisor"]})

    if datos["tipo_comprobante"] and datos["tipo_comprobante"].upper() != "I":
        tipos = {"E": "nota de crédito", "P": "complemento de pago", "T": "traslado", "N": "nómina"}
        return {**base, "estado": "ignorada",
                "mensaje": f"Es un CFDI de {tipos.get(datos['tipo_comprobante'].upper(), 'otro tipo')}; "
                           "solo se vinculan facturas de ingreso"}

    if datos["uuid_fiscal"]:
        previa = una_fila(
            supabase.table("facturas").select("id, orden:orden_id(folio)")
            .eq("uuid_fiscal", datos["uuid_fiscal"])
        )
        if previa:
            folio = (previa.get("orden") or {}).get("folio")
            donde = f" (vinculada a la orden #{folio})" if folio else " (pendiente de vincular)"
            return {**base, "estado": "duplicada", "mensaje": f"Esta factura ya estaba cargada{donde}"}

    if orden_id:
        elegida = una_fila(supabase.table("ordenes_compra").select("id, folio").eq("id", orden_id))
        vinculacion = "manual"
    else:
        if contexto is None:
            contexto = {"ordenes": _ordenes_vinculables(), "facturadas": _ordenes_con_factura()}
        elegida = cfdi.elegir_orden(datos, contexto["ordenes"], contexto["facturadas"])
        vinculacion = "automatica" if elegida else "pendiente"

    ruta = storage.subir("facturas", nombre or f"{datos['uuid_fiscal'] or 'cfdi'}.xml",
                         contenido, "application/xml")
    fila = {
        **datos,
        "orden_id":    elegida["id"] if elegida else None,
        "vinculacion": vinculacion,
        "xml_ruta":    ruta,
        "subido_por":  usuario["id"],
    }
    res = supabase.table("facturas").insert(fila).execute()
    if not res.data:
        storage.borrar(ruta)
        return {**base, "estado": "invalida", "mensaje": "No se pudo guardar la factura"}
    factura = res.data[0]

    if not elegida:
        return {**base, "estado": "pendiente", "factura_id": factura["id"],
                "mensaje": "Sin una orden que coincida con certeza: vincúlala a mano"}

    if contexto is not None:
        contexto["facturadas"].add(elegida["id"])
    _completar_rfc_proveedor(elegida["id"], datos["rfc_emisor"])
    registrar_evento(
        elegida["id"], "Factura vinculada",
        detalle=f"{datos['serie'] or ''}{datos['folio'] or ''} · ${datos['total']:,.2f}"
                f" · {'automática' if vinculacion == 'automatica' else 'manual'}",
        usuario_id=usuario["id"],
    )
    return {**base, "estado": "vinculada", "factura_id": factura["id"], "folio": elegida["folio"],
            "mensaje": f"Vinculada a la orden #{elegida['folio']}"}


@router.post("/xml")
async def cargar_xml(
    archivos: list[UploadFile] = File(...),
    usuario=Depends(requiere_rol(*ROLES_FACTURAS)),
):
    """Carga masiva de XML. Responde un resultado por archivo."""
    if len(archivos) > MAXIMO_ARCHIVOS:
        raise HTTPException(status_code=400, detail=f"Máximo {MAXIMO_ARCHIVOS} archivos por carga")

    contexto = {"ordenes": _ordenes_vinculables(), "facturadas": _ordenes_con_factura()}
    resultados = []
    for archivo in archivos:
        if not (archivo.filename or "").lower().endswith(".xml"):
            resultados.append({"archivo": archivo.filename, "estado": "invalida",
                               "mensaje": "No es un archivo .xml"})
            continue
        try:
            contenido, _ = await storage.leer_archivo(archivo)
        except HTTPException as e:
            resultados.append({"archivo": archivo.filename, "estado": "invalida", "mensaje": e.detail})
            continue
        resultados.append(registrar_xml(contenido, archivo.filename, usuario, contexto=contexto))

    resumen = {}
    for r in resultados:
        resumen[r["estado"]] = resumen.get(r["estado"], 0) + 1
    return {"resultados": resultados, "resumen": resumen}


@router.get("")
def listar_facturas(
    estado: Optional[str] = Query(None, description="pendiente | vinculada"),
    usuario=Depends(requiere_rol(*ROLES_FACTURAS)),
):
    query = supabase.table("facturas").select(SELECT_FACTURA).order("created_at", desc=True).limit(500)
    if estado == "pendiente":
        query = query.is_("orden_id", "null")
    elif estado == "vinculada":
        query = query.not_.is_("orden_id", "null")
    return query.execute().data


@router.get("/{factura_id}/candidatas")
def candidatas(factura_id: str, usuario=Depends(requiere_rol(*ROLES_FACTURAS))):
    """Órdenes sugeridas para vincular a mano, de más a menos probable."""
    factura = una_fila(supabase.table("facturas").select("*").eq("id", factura_id))
    if not factura:
        raise HTTPException(status_code=404, detail="Factura no encontrada")

    rfc = (factura.get("rfc_emisor") or "").upper()
    facturadas = _ordenes_con_factura()
    sugeridas = []
    for o in _ordenes_vinculables():
        rfc_prov = ((o.get("proveedor") or {}).get("rfc") or "").upper().strip()
        mismo_rfc = bool(rfc) and rfc_prov == rfc
        cuadra = (cfdi.total_coincide(factura["total"], o.get("total"))
                  or cfdi.total_coincide(factura["total"], o.get("monto_comprobado")))
        if not (mismo_rfc or (cuadra and not rfc_prov)):
            continue
        puntaje = (2 if mismo_rfc else 0) + (2 if cuadra else 0) \
            + (1 if cfdi.fecha_coincide(factura.get("fecha_emision"), o.get("fecha")) else 0)
        sugeridas.append({**o, "mismo_rfc": mismo_rfc, "total_cuadra": cuadra,
                          "ya_facturada": o["id"] in facturadas, "puntaje": puntaje})
    sugeridas.sort(key=lambda o: (-o["puntaje"], o["ya_facturada"], -(o.get("folio") or 0)))
    return sugeridas[:20]


@router.patch("/{factura_id}")
def vincular(factura_id: str, body: Vinculo, usuario=Depends(requiere_rol(*ROLES_FACTURAS))):
    """Vincula la factura a una orden, o la desvincula con orden_id nulo."""
    factura = una_fila(supabase.table("facturas").select("*").eq("id", factura_id))
    if not factura:
        raise HTTPException(status_code=404, detail="Factura no encontrada")

    if body.orden_id:
        orden = una_fila(supabase.table("ordenes_compra").select("id, folio, estado").eq("id", body.orden_id))
        if not orden:
            raise HTTPException(status_code=404, detail="Orden no encontrada")
        if orden["estado"] == "rechazada":
            raise HTTPException(status_code=409, detail="La orden está cancelada")
        datos = {"orden_id": orden["id"], "vinculacion": "manual"}
    else:
        datos = {"orden_id": None, "vinculacion": "pendiente"}

    res = supabase.table("facturas").update(datos).eq("id", factura_id).execute()

    etiqueta = f"{factura.get('serie') or ''}{factura.get('folio') or ''} · ${float(factura['total']):,.2f}"
    if factura.get("orden_id") and factura["orden_id"] != datos["orden_id"]:
        registrar_evento(factura["orden_id"], "Factura desvinculada", detalle=etiqueta,
                         usuario_id=usuario["id"])
    if datos["orden_id"] and datos["orden_id"] != factura.get("orden_id"):
        _completar_rfc_proveedor(datos["orden_id"], factura.get("rfc_emisor"))
        registrar_evento(datos["orden_id"], "Factura vinculada", detalle=f"{etiqueta} · manual",
                         usuario_id=usuario["id"])
    return res.data[0]


@router.get("/{factura_id}/xml")
def url_xml(factura_id: str, usuario=Depends(requiere_rol(*ROLES_FACTURAS, "almacen"))):
    factura = una_fila(supabase.table("facturas").select("xml_ruta").eq("id", factura_id))
    if not factura or not factura.get("xml_ruta"):
        raise HTTPException(status_code=404, detail="XML no encontrado")
    return {"url": storage.url_firmada(factura["xml_ruta"])}
