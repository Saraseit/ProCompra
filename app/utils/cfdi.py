"""
app/utils/cfdi.py
-----------------
Lectura de facturas CFDI (versiones 3.3 y 4.0) y su vinculación con órdenes.

Solo se usa la biblioteca estándar: del XML basta con el comprobante, el
emisor, el receptor y el timbre fiscal. No se valida la firma del SAT; para
eso está el portal del SAT o un PAC.
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Optional

NS_TIMBRE = "http://www.sat.gob.mx/TimbreFiscalDigital"

# Holguras para la vinculación automática.
TOLERANCIA_PESOS = 1.00        # redondeos de centavos entre ticket y factura
TOLERANCIA_RELATIVA = 0.005    # 0.5 % para totales grandes
DIAS_ANTES = 7                 # una factura rara vez es anterior a la orden
DIAS_DESPUES = 60              # ni llega dos meses después

# Estados en los que una orden puede recibir factura.
ESTADOS_FACTURABLES = ("autorizada", "pagada", "recoleccion", "comprobacion", "cerrada")


class CfdiInvalido(ValueError):
    pass


def _local(tag: str) -> str:
    """Nombre del nodo sin el namespace: '{http://...}Emisor' -> 'Emisor'."""
    return tag.rsplit("}", 1)[-1]


def _numero(valor: Optional[str]) -> Optional[float]:
    if valor in (None, ""):
        return None
    try:
        return round(float(valor), 2)
    except ValueError:
        return None


def _hijo(nodo, nombre: str):
    for h in nodo:
        if _local(h.tag) == nombre:
            return h
    return None


def leer_cfdi(contenido: bytes) -> dict:
    """Extrae los datos de un CFDI. Lanza CfdiInvalido si no lo es."""
    try:
        raiz = ET.fromstring(contenido)
    except ET.ParseError as e:
        raise CfdiInvalido(f"El archivo no es un XML válido ({e})")

    if _local(raiz.tag) != "Comprobante":
        raise CfdiInvalido("El XML no es un CFDI (no tiene nodo Comprobante)")

    # Los atributos no llevan namespace; algunos emisores escriben 'total' en minúsculas (3.2).
    attr = {k.lower(): v for k, v in raiz.attrib.items()}
    emisor = _hijo(raiz, "Emisor")
    receptor = _hijo(raiz, "Receptor")
    if emisor is None:
        raise CfdiInvalido("El CFDI no tiene emisor")

    total = _numero(attr.get("total"))
    if total is None:
        raise CfdiInvalido("El CFDI no tiene total")

    uuid_fiscal = None
    complemento = _hijo(raiz, "Complemento")
    if complemento is not None:
        for nodo in complemento.iter():
            if _local(nodo.tag) == "TimbreFiscalDigital":
                uuid_fiscal = (nodo.attrib.get("UUID") or "").upper() or None
                break

    iva = None
    impuestos = _hijo(raiz, "Impuestos")
    if impuestos is not None:
        iva = _numero(impuestos.attrib.get("TotalImpuestosTrasladados"))

    fecha = None
    if attr.get("fecha"):
        try:
            fecha = datetime.fromisoformat(attr["fecha"]).isoformat()
        except ValueError:
            fecha = None

    return {
        "uuid_fiscal":      uuid_fiscal,
        "rfc_emisor":       (emisor.attrib.get("Rfc") or emisor.attrib.get("rfc") or "").upper().strip(),
        "nombre_emisor":    emisor.attrib.get("Nombre") or emisor.attrib.get("nombre"),
        "rfc_receptor":     ((receptor.attrib.get("Rfc") or receptor.attrib.get("rfc") or "").upper().strip()
                             if receptor is not None else None),
        "serie":            attr.get("serie"),
        "folio":            attr.get("folio"),
        "fecha_emision":    fecha,
        "subtotal":         _numero(attr.get("subtotal")),
        "iva":              iva,
        "total":            total,
        "moneda":           attr.get("moneda"),
        "metodo_pago":      attr.get("metodopago"),
        "forma_pago":       attr.get("formapago"),
        "tipo_comprobante": attr.get("tipodecomprobante"),
    }


def total_coincide(factura_total: float, monto: Optional[float]) -> bool:
    if monto is None:
        return False
    tolerancia = max(TOLERANCIA_PESOS, abs(monto) * TOLERANCIA_RELATIVA)
    return abs(float(factura_total) - float(monto)) <= tolerancia


def fecha_coincide(fecha_emision: Optional[str], fecha_orden: Optional[str]) -> bool:
    if not fecha_emision or not fecha_orden:
        return True  # sin fecha no se descarta: decide el total
    try:
        emision = datetime.fromisoformat(fecha_emision).date()
        orden = datetime.fromisoformat(str(fecha_orden)).date()
    except ValueError:
        return True
    return orden - timedelta(days=DIAS_ANTES) <= emision <= orden + timedelta(days=DIAS_DESPUES)


def candidatas(factura: dict, ordenes: list[dict]) -> list[dict]:
    """Órdenes que pueden corresponder a la factura.

    `ordenes` trae el RFC del proveedor embebido en `proveedor.rfc`. Una orden
    es candidata si el RFC coincide, el total de la factura cuadra con el
    total autorizado o con el monto comprobado, y la fecha es razonable.
    """
    rfc = (factura.get("rfc_emisor") or "").upper()
    resultado = []
    for o in ordenes:
        rfc_prov = ((o.get("proveedor") or {}).get("rfc") or "").upper().strip()
        if not rfc or rfc_prov != rfc:
            continue
        if o.get("estado") not in ESTADOS_FACTURABLES:
            continue
        if not (total_coincide(factura["total"], o.get("total"))
                or total_coincide(factura["total"], o.get("monto_comprobado"))):
            continue
        if not fecha_coincide(factura.get("fecha_emision"), o.get("fecha")):
            continue
        resultado.append(o)
    return resultado


def elegir_orden(factura: dict, ordenes: list[dict], ya_facturadas: set) -> Optional[dict]:
    """Devuelve la orden a vincular solo si hay exactamente una candidata libre.

    Con cero o varias candidatas la factura queda pendiente para vincularla a
    mano: es preferible pedir una confirmación a vincular la orden equivocada.
    """
    libres = [o for o in candidatas(factura, ordenes) if o["id"] not in ya_facturadas]
    return libres[0] if len(libres) == 1 else None
