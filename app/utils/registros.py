"""
app/utils/registros.py
----------------------
Motor de ProPago: de un pago a sus renglones para el ERP.

Funciones puras (sin acceso a la base) para poder probarlas sin Supabase.

Un renglón es una fila del archivo de importación: importe, cuenta, texto
(REF), contrato (ID_EVENTO). El texto sigue la gramática acordada:

    [CDV] - {PLAZA} - [{POLIZA} - ][ND - ][{CATEGORIA} - ]{CUERPO}[ C{CONTRATO}] EL {DD/MM/YY}

- [CDV] solo si la cuenta del renglón lo lleva; el IVA (H01) nunca.
- ND si el grupo no tiene renglón de IVA.
- Importes a 2 decimales; el IVA siempre por diferencia para cuadrar al centavo.
"""

import re
import unicodedata
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from app.utils.precios import normalizar

TASA_IVA = Decimal("0.16")
TOLERANCIA = 0.005
CONTRATOS_ESPECIALES = ("DIRECTO", "BODEGA", "MONTAJE", "MANTENIMIENTO", "PROTOTIPO")
PLACEHOLDERS = ("VERBO", "CONCEPTO", "ITEMS", "PROVEEDOR", "FOLIO", "DOC")
VERBOS = ("COMPRA", "PAGO", "REEMBOLSO", "RETIRO")
SUSTANTIVOS = ("SUBRENTA", "RENTA", "FABRICACION", "MANTENIMIENTO", "SERVICIO", "LAVANDERIA", "FLETE")
AGRUPAN = "LAVANDERIA,DEPTO360"

PLANTILLA_GENERAL = "{VERBO} DE {CONCEPTO}"


# ── Dinero ────────────────────────────────────────────

def _dec(valor) -> Decimal:
    return Decimal(str(valor or 0))


def centavos(valor) -> int:
    return int((_dec(valor) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def redondear(valor) -> float:
    return float(_dec(valor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def repartir(total, pesos: list[float]) -> list[float]:
    """Reparte `total` en proporción a `pesos`; el residuo de redondeo va al último.

    Así la suma de las partes es exactamente el total (3,006.90 / 7 →
    429.56 × 6 + 429.54).
    """
    if not pesos:
        return []
    total_c = centavos(total)
    suma = sum(max(float(p), 0) for p in pesos)
    if suma <= 0:
        pesos, suma = [1.0] * len(pesos), float(len(pesos))
    partes, acumulado = [], 0
    for p in pesos[:-1]:
        c = int((Decimal(total_c) * _dec(max(float(p), 0)) / _dec(suma)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        partes.append(c)
        acumulado += c
    partes.append(total_c - acumulado)
    return [c / 100 for c in partes]


def desglosar(total) -> tuple[float, float]:
    """Subtotal e IVA desde un total con IVA al 16 %; el IVA por diferencia."""
    subtotal = redondear(_dec(total) / (1 + TASA_IVA))
    return subtotal, redondear(_dec(total) - _dec(subtotal))


def iva_de_abono(abono, total_factura, iva_factura, iva_previo, liquida: bool) -> float:
    """IVA proporcional de un abono a una factura.

    El último abono (el que liquida) toma el IVA que falta, para que la suma
    de los IVA sea exactamente el de la factura.
    """
    if not total_factura or not iva_factura:
        return 0.0
    if liquida:
        iva = redondear(_dec(iva_factura) - _dec(iva_previo))
    else:
        iva = redondear(_dec(abono) * _dec(iva_factura) / _dec(total_factura))
    return min(max(iva, 0.0), redondear(abono))


# ── Textos ────────────────────────────────────────────

def mayusculas(texto: Optional[str]) -> str:
    """Mayúsculas sin acentos; la Ñ se conserva (PEÑAFIEL, AÑOS)."""
    t = (texto or "").upper().replace("Ñ", "\0")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).replace("\0", "Ñ")
    return " ".join(t.split())


def fecha_corta(valor) -> str:
    d = valor if isinstance(valor, date) else date.fromisoformat(str(valor)[:10])
    return d.strftime("%d/%m/%y")


def contratos(texto: Optional[str]) -> list[str]:
    """Contratos de una captura: «17095/17100», «C16620-15821», «bodega», «0»."""
    vistos = []
    for parte in re.split(r"[/,;\-\s]+", mayusculas(texto)):
        parte = parte.strip()
        if re.fullmatch(r"C\d+", parte):
            parte = parte[1:]
        if parte.startswith("C") and parte[1:] in CONTRATOS_ESPECIALES:
            parte = parte[1:]
        if not parte or parte.strip("0") == "":
            continue
        if parte not in vistos:
            vistos.append(parte)
    return vistos


def id_evento(contrato: Optional[str]) -> int:
    return int(contrato) if contrato and contrato.isdigit() else 0


def texto_contratos(lista: list[str]) -> str:
    return "C" + "/".join(lista) if lista else ""


def placeholders_invalidos(plantilla: str) -> list[str]:
    return sorted({p for p in re.findall(r"\{([^{}]*)\}", plantilla or "") if p not in PLACEHOLDERS})


def aplicar_plantilla(plantilla: str, valores: dict) -> str:
    """Llena la plantilla. Un tramo entre corchetes se omite si le falta un dato:
    «{VERBO} DE {CONCEPTO}[ A {PROVEEDOR}]» sin proveedor deja «COMPRA DE …»."""
    def opcional(m):
        tramo = m.group(1)
        nombres = re.findall(r"\{([^{}]*)\}", tramo)
        return tramo if all(valores.get(n) for n in nombres) else ""

    texto = re.sub(r"\[([^\[\]]*)\]", opcional, plantilla or PLANTILLA_GENERAL)
    texto = re.sub(r"\{([^{}]*)\}", lambda m: str(valores.get(m.group(1)) or ""), texto)
    texto = mayusculas(texto)
    # Si el concepto ya empieza con un verbo no se duplica: «PAGO DE PAGO DE CFE».
    texto = re.sub(rf"^(?:{'|'.join(VERBOS)})(?: DE [A-Z]+)? DE (?=(?:{'|'.join(VERBOS)})\b)", "", texto)
    # Si el concepto ya dice qué es, se respeta su palabra: «PAGO DE SUBRENTA DE RENTA DE PISTA» → «PAGO DE RENTA DE PISTA».
    texto = re.sub(rf"^((?:{'|'.join(VERBOS)}) DE )(?:{'|'.join(SUSTANTIVOS)}) DE (?=(?:{'|'.join(SUSTANTIVOS)}) DE )", r"\1", texto)
    # «PAGO DE FLETE FLETE REDONDO» cuando el concepto ya trae la palabra de la plantilla.
    texto = re.sub(r"\b([A-Z]{3,}) \1\b", r"\1", texto)
    # Sin proveedor no queda un «A» o «EN» colgando al final.
    texto = re.sub(r"(?:\s+(?:A|EN|POR|DE))+$", "", texto.strip(" -"))
    return texto.strip(" -")


def con_naturaleza(cuerpo: str, naturaleza: Optional[str]) -> str:
    """Anticipo y saldo: el mismo texto del registro cambiando solo esa palabra.
    «PAGO DE SUBRENTA DE …» → «PAGO DE ANTICIPO DE SUBRENTA DE …»."""
    palabra = {"anticipo": "ANTICIPO", "saldo": "SALDO"}.get(naturaleza)
    if not palabra or cuerpo.startswith(f"PAGO DE {palabra}"):
        return cuerpo
    resto = re.sub(rf"^(?:{'|'.join(VERBOS)}) DE ", "", cuerpo)
    return f"PAGO DE {palabra} DE {resto}"


def corrector(texto: Optional[str]) -> dict:
    """«THONER=TONER, REMBOLSO=REEMBOLSO» → {THONER: TONER, …}."""
    pares = {}
    for par in (texto or "").split(","):
        if "=" in par:
            mal, bien = (mayusculas(x) for x in par.split("=", 1))
            if mal and bien:
                pares[mal] = bien
    return pares


def corregir(texto: str, pares: dict) -> str:
    for mal, bien in pares.items():
        texto = re.sub(rf"\b{re.escape(mal)}\b", bien, texto)
    return texto


def armar_ref(*, cdv: bool, plaza: str, poliza: Optional[str], nd: bool, categoria: Optional[str],
              cuerpo: str, contratos_: list[str], fecha) -> str:
    partes = []
    if cdv:
        partes.append("[CDV]")
    partes.append(plaza or "MID")
    if poliza:
        partes.append(poliza)
    if nd:
        partes.append("ND")
    if categoria:
        partes.append(mayusculas(categoria))
    partes.append(cuerpo)
    texto = " - ".join(p for p in partes if p)
    if contratos_:
        texto += " " + texto_contratos(contratos_)
    return f"{texto} EL {fecha_corta(fecha)}"


def texto_poliza(prefijo: Optional[str], numero: Optional[int]) -> Optional[str]:
    return f"{mayusculas(prefijo)}{numero:02d}" if prefijo and numero else None


def asignar_polizas(movimientos: list[dict]) -> list[tuple]:
    """Números de póliza de los pagos de un mes: [(id, antes, después)] de los que cambian.

    Los exportados (poliza_fija) no se tocan. Los demás van en orden
    cronológico después del más alto exportado, así que una compra que llega
    tarde a un mes ya exportado toma el siguiente número.
    """
    siguiente = max([m.get("poliza") or 0 for m in movimientos if m.get("poliza_fija")] or [0]) + 1
    cambios = []
    pendientes = sorted((m for m in movimientos if not m.get("poliza_fija")),
                        key=lambda m: (str(m["fecha_pago"]), str(m.get("created_at") or "")))
    for m in pendientes:
        if m.get("poliza") != siguiente:
            cambios.append((m["id"], m.get("poliza"), siguiente))
        siguiente += 1
    return cambios


# ── Reglas ────────────────────────────────────────────

def _contiene(texto: str, palabra: str) -> bool:
    p = normalizar(palabra)
    return bool(p) and f" {p} " in f" {texto} "


def regla_aplica(regla: dict, ctx: dict) -> bool:
    """¿La regla aplica al renglón? Toda condición llena debe cumplirse."""
    if not regla.get("activa", True):
        return False
    for campo in ("proveedor_id", "caja_id", "insumo_id", "plaza"):
        if regla.get(campo) and regla[campo] != ctx.get(campo):
            return False
    if regla.get("con_cfdi") is not None and bool(regla["con_cfdi"]) != bool(ctx.get("con_cfdi")):
        return False
    texto = ctx.get("texto") or ""
    palabras = [p for p in regla.get("palabras") or [] if normalizar(p)]
    if palabras and not any(_contiene(texto, p) for p in palabras):
        return False
    if any(_contiene(texto, p) for p in regla.get("palabras_excluir") or []):
        return False
    return True


def resolver(reglas: list[dict], ctx: dict) -> dict:
    """Cuenta, categoría, plantilla y banderas para un renglón.

    La primera regla con cuenta (por prioridad) decide cuenta, categoría y
    plantilla. Las reglas sin cuenta solo modifican: fuerzan ND, desglosan IVA
    o ponen categoría o plantilla si la regla de la cuenta no las trae.
    """
    r = {"cuenta_id": None, "regla_id": None, "categoria": None, "plantilla_id": None,
         "fuerza_nd": False, "desglosa_iva": False}
    for regla in sorted(reglas, key=lambda x: (x.get("prioridad") or 0, x.get("nombre") or "")):
        if not regla_aplica(regla, ctx):
            continue
        if regla.get("cuenta_id") and not r["cuenta_id"]:
            r.update(cuenta_id=regla["cuenta_id"], regla_id=regla.get("id"),
                     categoria=regla.get("categoria_texto") or r["categoria"],
                     plantilla_id=regla.get("plantilla_id") or r["plantilla_id"])
        elif not regla.get("cuenta_id"):
            r["categoria"] = r["categoria"] or regla.get("categoria_texto")
            r["plantilla_id"] = r["plantilla_id"] or regla.get("plantilla_id")
        r["fuerza_nd"] = r["fuerza_nd"] or bool(regla.get("fuerza_nd"))
        r["desglosa_iva"] = r["desglosa_iva"] or bool(regla.get("desglosa_iva"))
    return r


# ── Generación ────────────────────────────────────────

def _avisos_contrato(lista: list[str]) -> list[str]:
    return [f"El contrato {c} no tiene 5 dígitos" for c in lista
            if c.isdigit() and len(c) != 5]


def unir_conceptos(conceptos: list[str]) -> str:
    """«TELA TAFETAN NEGRO, TAFETAN CRUDO»: la palabra que comparten todos va una vez."""
    if len(conceptos) < 2:
        return ", ".join(conceptos)
    primeras = {c.split(" ", 1)[0] for c in conceptos}
    if len(primeras) == 1 and all(" " in c for c in conceptos):
        return ", ".join([conceptos[0]] + [c.split(" ", 1)[1] for c in conceptos[1:]])
    return ", ".join(conceptos)


def _cantidad(valor) -> str:
    n = float(valor or 0)
    return f"{n:g}" if n else ""


class Motor:
    """Arma los renglones de un pago.

    `pago` trae lo que el motor necesita, ya leído de la base:
      fecha, plaza, poliza (texto «TCB12» o None), caja_id, comision, diferencia,
      aplicaciones: [{pago_id, orden_id, monto, naturaleza, orden_folio,
                      proveedor: {id, nombre}, partidas: [{concepto, cantidad,
                      importe, contrato, insumo_id}], factura: {total, iva,
                      metodo_pago, serie, folio} | None, iva_previo, pagado_previo,
                      total_orden}],
      lineas: [{concepto, cantidad, contrato, importe, con_iva, cuenta_id}],
      proveedor_texto.
    """

    def __init__(self, reglas: list[dict], cuentas: dict, plantillas: dict, parametros: dict = None):
        self.reglas = [r for r in reglas if r.get("activa", True)]
        self.cuentas = cuentas
        self.plantillas = plantillas
        self.parametros = parametros or {}
        self.cuenta_iva = next((c for c in cuentas.values() if c.get("tipo") == "iva" and c.get("activa", True)), None)
        # Solo estas plantillas juntan conceptos del mismo contrato (lavandería, una remisión de Depto 360).
        self.agrupan = {mayusculas(x) for x in (self.parametros.get("plantillas_agrupan", AGRUPAN) or "").split(",") if x.strip()}
        self.correcciones = corrector(self.parametros.get("correcciones"))

    def _plantilla(self, plantilla_id) -> str:
        p = self.plantillas.get(plantilla_id) if plantilla_id else None
        if p and p.get("activa", True):
            return p["plantilla"]
        general = next((x for x in self.plantillas.values() if x.get("codigo") == "GENERAL" and x.get("activa", True)), None)
        return general["plantilla"] if general else PLANTILLA_GENERAL

    def _cuenta(self, cuenta_id) -> Optional[dict]:
        c = self.cuentas.get(cuenta_id) if cuenta_id else None
        return c if c and c.get("activa", True) else None

    def _renglon(self, *, cargo, cuenta, ref, contrato, es_iva, regla_id, avisos, base) -> dict:
        avisos = list(avisos)
        if not cuenta:
            avisos.insert(0, "Falta la cuenta de IVA (H01) en el catálogo" if es_iva else "Falta elegir la cuenta")
        return {
            **base,
            "cargo": redondear(cargo),
            "cuenta_id": cuenta["id"] if cuenta else None,
            "cuenta_nombre_erp": cuenta["nombre_erp"] if cuenta else None,
            "ref": ref,
            "contrato": contrato,
            "id_evento": id_evento(contrato),
            "es_iva": es_iva,
            "regla_id": regla_id,
            "avisos": avisos,
            "estado": "listo" if cuenta else "pendiente",
        }

    def _agrupa(self, plantilla_id) -> bool:
        p = self.plantillas.get(plantilla_id) if plantilla_id else None
        return (p or {}).get("codigo", "GENERAL") in self.agrupan

    def _grupos(self, piezas: list[dict], ctx_base: dict) -> list[dict]:
        """Una partida por renglón (y contrato). Con una plantilla que agrupa
        (lavandería) los conceptos del mismo contrato y cuenta van juntos."""
        grupos: dict[tuple, dict] = {}
        for p in piezas:
            concepto = corregir(mayusculas(p["concepto"]), self.correcciones)
            r = resolver(self.reglas, {**ctx_base, "texto": normalizar(concepto), "insumo_id": p.get("insumo_id")})
            cuenta_id = p.get("cuenta_id") or r["cuenta_id"]
            llave = (p["contrato"], cuenta_id, r["categoria"], r["plantilla_id"],
                     None if self._agrupa(r["plantilla_id"]) else p.get("n"))
            g = grupos.setdefault(llave, {"contrato": p["contrato"], "cuenta_id": cuenta_id, "categoria": r["categoria"],
                                          "plantilla_id": r["plantilla_id"], "regla_id": None if p.get("cuenta_id") else r["regla_id"],
                                          "peso": 0.0, "conceptos": [], "items": [], "fuerza_nd": False, "desglosa_iva": False})
            g["peso"] += p["peso"]
            if concepto not in g["conceptos"]:
                g["conceptos"].append(concepto)
            g["items"].append(" ".join(x for x in (_cantidad(p.get("cantidad")), concepto) if x))
            g["fuerza_nd"] = g["fuerza_nd"] or r["fuerza_nd"]
            g["desglosa_iva"] = g["desglosa_iva"] or r["desglosa_iva"]
        return list(grupos.values())

    @staticmethod
    def _piezas(lineas: list[dict]) -> list[dict]:
        """Cada línea se parte entre sus contratos en partes iguales."""
        piezas = []
        for n, linea in enumerate(lineas):
            lista = contratos(linea.get("contrato")) or [None]
            for c in lista:
                piezas.append({**linea, "n": n, "contrato": c, "peso": float(linea.get("importe") or 0) / len(lista)})
        return piezas

    def _valores(self, grupo_o_grupos, verbo: str, proveedor: str, folio, doc) -> dict:
        grupos = grupo_o_grupos if isinstance(grupo_o_grupos, list) else [grupo_o_grupos]
        conceptos, items = [], []
        for g in grupos:
            conceptos += [c for c in g["conceptos"] if c not in conceptos]
            items += g["items"]
        return {"VERBO": verbo, "CONCEPTO": unir_conceptos(conceptos), "ITEMS": ", ".join(items),
                "PROVEEDOR": mayusculas(proveedor), "FOLIO": str(folio or ""), "DOC": mayusculas(doc)}

    def _bloque(self, pago: dict, grupos: list[dict], base_total: float, iva: float, nd: bool,
                verbo: str, proveedor: str, folio, doc, base_renglon: dict, avisos: list[str],
                naturaleza: Optional[str] = None) -> list[dict]:
        """Renglones de un conjunto de grupos que comparten un solo IVA."""
        renglones = []
        montos = repartir(base_total, [g["peso"] for g in grupos])
        for g, monto in zip(grupos, montos):
            if monto <= 0:
                continue
            cuenta = self._cuenta(g["cuenta_id"])
            cuerpo = con_naturaleza(aplicar_plantilla(self._plantilla(g["plantilla_id"]),
                                                      self._valores(g, verbo, proveedor, folio, doc)), naturaleza)
            lista = [g["contrato"]] if g["contrato"] else []
            ref = armar_ref(cdv=bool(cuenta and cuenta.get("cdv")), plaza=pago["plaza"], poliza=pago.get("poliza"),
                            nd=nd, categoria=g["categoria"], cuerpo=cuerpo, contratos_=lista, fecha=pago["fecha"])
            renglones.append(self._renglon(cargo=monto, cuenta=cuenta, ref=ref, contrato=g["contrato"], es_iva=False,
                                           regla_id=g["regla_id"], avisos=avisos + _avisos_contrato(lista), base=base_renglon))
        if iva > 0 and grupos:
            lista = []
            for g in grupos:
                if g["contrato"] and g["contrato"] not in lista:
                    lista.append(g["contrato"])
            cuerpo = con_naturaleza(aplicar_plantilla(self._plantilla(grupos[0]["plantilla_id"]),
                                                      self._valores(grupos, verbo, proveedor, folio, doc)), naturaleza)
            # El IVA nunca lleva [CDV], aunque su subtotal sí.
            ref = armar_ref(cdv=False, plaza=pago["plaza"], poliza=pago.get("poliza"), nd=False,
                            categoria=grupos[0]["categoria"], cuerpo=cuerpo, contratos_=lista, fecha=pago["fecha"])
            renglones.append(self._renglon(cargo=iva, cuenta=self.cuenta_iva, ref=ref,
                                           contrato="/".join(lista) or None, es_iva=True, regla_id=None,
                                           avisos=avisos, base=base_renglon))
            renglones[-1]["id_evento"] = id_evento(lista[0]) if lista else 0
        return renglones

    def _aplicacion(self, pago: dict, a: dict) -> list[dict]:
        factura = a.get("factura")
        proveedor = a.get("proveedor") or {}
        ctx = {"proveedor_id": proveedor.get("id"), "caja_id": pago.get("caja_id"), "plaza": pago.get("plaza"),
               "con_cfdi": bool(factura)}
        partidas = a.get("partidas") or [{"concepto": f"ORDEN {a.get('orden_folio')}", "importe": a["monto"]}]
        grupos = self._grupos(self._piezas([{**p, "importe": p.get("importe")} for p in partidas]), ctx)

        monto = redondear(a["monto"])
        fuerza_nd = any(g["fuerza_nd"] for g in grupos)
        desglosa = any(g["desglosa_iva"] for g in grupos)
        avisos, iva = [], 0.0
        if factura and not fuerza_nd:
            pagado_previo = float(a.get("pagado_previo") or 0)
            total_f = float(factura.get("total") or 0)
            liquida = pagado_previo + monto >= total_f - TOLERANCIA
            iva = iva_de_abono(monto, total_f, factura.get("iva"), a.get("iva_previo") or 0, liquida)
            parcial = not liquida or pagado_previo > TOLERANCIA
            if (factura.get("metodo_pago") or "").upper() == "PPD":
                avisos.append("Factura PPD: falta el complemento de pago de este abono")
            elif parcial:
                avisos.append("Factura PUE pagada en abonos: debió emitirse PPD")
        elif not factura and desglosa and not fuerza_nd:
            _, iva = desglosar(monto)
        nd = iva <= 0

        doc = ((factura.get("serie") or "") + (factura.get("folio") or "")) if factura else ""
        base_renglon = {"pago_id": a.get("pago_id"), "orden_id": a.get("orden_id")}
        return self._bloque(pago, grupos, redondear(monto - iva), iva, nd, "COMPRA", proveedor.get("nombre") or "",
                            a.get("orden_folio"), doc, base_renglon, avisos, a.get("naturaleza"))

    def _lineas(self, pago: dict) -> list[dict]:
        ctx = {"proveedor_id": None, "caja_id": pago.get("caja_id"), "plaza": pago.get("plaza")}
        renglones, con_iva = [], []
        for linea in pago.get("lineas") or []:
            grupos = self._grupos(self._piezas([linea]), {**ctx, "con_cfdi": bool(linea.get("con_iva"))})
            importe = redondear(linea["importe"])
            fuerza_nd = any(g["fuerza_nd"] for g in grupos)
            desglosa = bool(linea.get("con_iva")) or any(g["desglosa_iva"] for g in grupos)
            iva = desglosar(importe)[1] if desglosa and not fuerza_nd else 0.0
            filas = self._bloque(pago, grupos, redondear(importe - iva), 0.0, iva <= 0, "COMPRA",
                                 pago.get("proveedor_texto") or "", None, "", {"pago_id": None, "orden_id": None}, [])
            renglones += filas
            if iva > 0:
                con_iva.append((grupos, iva))
        if con_iva:
            # Un solo renglón de IVA para todas las líneas con factura.
            grupos = [g for gs, _ in con_iva for g in gs]
            iva = redondear(sum(_dec(i) for _, i in con_iva))
            renglones += self._bloque(pago, grupos, 0.0, iva, False, "COMPRA", pago.get("proveedor_texto") or "",
                                      None, "", {"pago_id": None, "orden_id": None}, [])
        return renglones

    def _extras(self, pago: dict, renglones: list[dict]) -> list[dict]:
        """Comisión y diferencia: al renglón principal (o a su cuenta, si se configuró)."""
        principales = [r for r in renglones if not r["es_iva"]]
        if not principales:
            return renglones
        ultimo = principales[-1]
        for campo, parametro in (("comision", "cuenta_comisiones"), ("diferencia", "cuenta_diferencias")):
            extra = redondear(pago.get(campo) or 0)
            if extra <= 0:
                continue
            cuenta = self._cuenta(self.parametros.get(parametro))
            if cuenta:
                renglones.append({**ultimo, "cargo": extra, "cuenta_id": cuenta["id"],
                                  "cuenta_nombre_erp": cuenta["nombre_erp"], "regla_id": None, "es_iva": False,
                                  "estado": "listo", "avisos": [a for a in ultimo["avisos"] if not a.startswith("Falta")]})
            else:
                ultimo["cargo"] = redondear(_dec(ultimo["cargo"]) + _dec(extra))
        return renglones

    def generar(self, pago: dict) -> list[dict]:
        renglones = []
        for a in pago.get("aplicaciones") or []:
            renglones += self._aplicacion(pago, a)
        renglones += self._lineas(pago)
        renglones = self._extras(pago, renglones)
        for n, r in enumerate(renglones, start=1):
            r["renglon"] = n
        return renglones


def cuadra(renglones: list[dict], monto) -> bool:
    return abs(sum(_dec(r["cargo"]) for r in renglones) - _dec(monto)) < Decimal("0.005")


# ── Archivo para el ERP ───────────────────────────────

ENCABEZADOS = ("Fecha", "CONCEPTO", "CARGO", "CLIENTE O GASTO EN", "REF", "ID_EVENTO", "PROPIETARIO")
ANCHOS = (11, 11, 11, 52, 90, 11, 16)


def excel(renglones: list[dict], fecha: date) -> bytes:
    """El archivo de importación: Fecha es el día de la descarga, CONCEPTO siempre
    EFECTIVO y PROPIETARIO el nombre de la caja."""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    libro = Workbook()
    hoja = libro.active
    hoja.title = "Registros"
    hoja.append(list(ENCABEZADOS))
    for r in renglones:
        hoja.append([fecha, "EFECTIVO", redondear(r["cargo"]), r.get("cuenta_nombre_erp") or "", r["ref"],
                     int(r.get("id_evento") or 0), r.get("propietario") or ""])
    for fila in hoja.iter_rows(min_row=2, max_col=1):
        fila[0].number_format = "dd-mmm-yy"
    for i, ancho in enumerate(ANCHOS, start=1):
        hoja.column_dimensions[get_column_letter(i)].width = ancho
    salida = BytesIO()
    libro.save(salida)
    return salida.getvalue()
