"""
app/utils/precios.py
--------------------
Histórico de precios de insumos: normalización de textos, parecido entre
descripciones, unidades del SAT, precio de referencia y avisos.

Funciones puras (sin acceso a la base) para poder probarlas sin Supabase.

Todos los precios son unitarios, sin IVA y en pesos: así vienen los
conceptos del CFDI (ValorUnitario) y así se capturan las partidas.
"""

import re
import unicodedata
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from statistics import median
from typing import Optional

# Aviso cuando el precio se aleja de la referencia en este porcentaje.
UMBRAL_ALTO = 0.15
UMBRAL_BAJO = -0.15
# La factura contra lo cotizado en la orden: basta una diferencia menor.
UMBRAL_FACTURA = 0.05
# Sin estas compras comparables no hay base para avisar.
MINIMO_COMPRAS = 3
# Ventana de la referencia; si en 6 meses hay pocas compras se amplía a 12.
VENTANAS_MESES = (6, 12)
# Parecido mínimo para sugerir un insumo o para reconocer una descripción.
PARECIDO_SUGERENCIA = 0.55
PARECIDO_RECONOCER = 0.80

PALABRAS_VACIAS = {"DE", "DEL", "LA", "LAS", "EL", "LOS", "CON", "PARA", "Y", "EN", "A", "AL", "X", "POR", "SIN"}

# Claves de unidad del SAT (c_ClaveUnidad) a las unidades de ProCompra.
CLAVES_UNIDAD = {
    "H87": "PZA", "EA": "PZA", "C62": "PZA", "XUN": "PZA", "PZA": "PZA", "PR": "PAR", "SET": "JUEGO",
    "KGM": "KILO", "GRM": "GRAMO", "TNE": "TONELADA",
    "LTR": "LITRO", "MLT": "ML", "GLL": "GALON", "A76": "GALON",
    "MTR": "MTR", "CMT": "CM", "MTK": "M2", "MTQ": "M3", "LM": "MTR",
    "XBX": "CAJA", "XPK": "PAQUETE", "XPA": "PAQUETE", "XRO": "ROLLO", "XRL": "ROLLO",
    "XBJ": "CUBETA", "XCN": "BOTE", "XTN": "BOTE", "XBO": "BOTELLA", "XBG": "BOLSA", "XSA": "SACO",
    "XKI": "KIT", "KT": "KIT", "LO": "LOTE", "XLT": "LOTE",
    "E48": "SERVICIO", "ACT": "SERVICIO", "E51": "SERVICIO", "HUR": "HORA", "DAY": "DIA",
}

# Claves que muchos emisores usan para todo: si el texto de la unidad dice
# otra cosa más precisa, manda el texto.
CLAVES_GENERICAS = {"H87", "EA", "C62", "XUN", "ZZ", "ACT", "E48"}

TEXTOS_UNIDAD = {
    "PZA": ("PZA", "PZAS", "PZ", "PIEZA", "PIEZAS", "PIE", "UNIDAD", "UNIDADES", "UN", "U", "NO APLICA"),
    "MTR": ("MTR", "MTRS", "MT", "MTS", "M", "METRO", "METROS", "METRO LINEAL", "ML LINEAL", "MTL"),
    "KILO": ("KILO", "KILOS", "KG", "KGS", "KILOGRAMO", "KILOGRAMOS"),
    "LITRO": ("LITRO", "LITROS", "LT", "LTS", "L", "LTR"),
    "GALON": ("GALON", "GALONES", "GAL", "GL"),
    "CAJA": ("CAJA", "CAJAS", "CJ", "CJA"),
    "ROLLO": ("ROLLO", "ROLLOS", "RLL", "RLO"),
    "CUBETA": ("CUBETA", "CUBETAS", "CUB"),
    "BOTE": ("BOTE", "BOTES", "LATA", "LATAS"),
    "PAQUETE": ("PAQUETE", "PAQUETES", "PAQ", "PQT", "PACK"),
    "SERVICIO": ("SERVICIO", "SERVICIOS", "SERV", "SRV", "ACTIVIDAD"),
    "CORTE": ("CORTE", "CORTES"),
    "M2": ("M2", "MTS2", "MT2", "METRO CUADRADO", "METROS CUADRADOS"),
    "JUEGO": ("JUEGO", "JUEGOS", "JGO"),
    "PAR": ("PAR", "PARES"),
}
_POR_TEXTO = {t: u for u, textos in TEXTOS_UNIDAD.items() for t in textos}


# ── Textos ────────────────────────────────────────────

def normalizar(texto: Optional[str]) -> str:
    """Forma canónica de una descripción para agrupar y buscar.

    Mayúsculas, sin acentos ni signos; los decimales se conservan (1,50 →
    1.50) porque suelen ser medidas que distinguen un insumo de otro.
    """
    t = unicodedata.normalize("NFKD", texto or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).upper()
    t = re.sub(r"(?<=\d),(?=\d)", ".", t)
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)
    t = re.sub(r"[^A-Z0-9.]+", " ", t)
    return " ".join(t.split())[:200]


def _tokens(texto: str) -> list[str]:
    return [p for p in normalizar(texto).split() if p not in PALABRAS_VACIAS]


def _raiz(palabra: str) -> str:
    """Sin plural ni género: NEGRO, NEGRA y NEGROS se comparan igual."""
    return re.sub(r"(OS|AS|ES|O|A|S)$", "", palabra) if len(palabra) > 3 else palabra


def _abreviatura(corta: str, larga: str) -> bool:
    """NGO ↔ NEGRA, BCO ↔ BLANCO, VINIL ↔ VINILICA: misma inicial y letras en orden."""
    if len(corta) < 3 or corta[0] != larga[0] or re.search(r"\d", corta + larga):
        return False  # las medidas (1.50, 19) se comparan exactas
    if larga.startswith(corta):
        return True
    corta, larga = re.sub(r"[OA]$", "", corta), _raiz(larga)
    if re.search(r"[AEIOU]", corta[1:]):
        return False  # una abreviatura es casi pura consonante: TELA no abrevia TERGAL
    resto = iter(larga)
    return all(letra in resto for letra in corta)


def _coincide(token: str, otros: list[str]) -> bool:
    for o in otros:
        if token == o or (_raiz(token) == _raiz(o) and not token.isdigit()):
            return True
        corta, larga = sorted((token, o), key=len)
        if len(corta) < len(larga) and _abreviatura(corta, larga):
            return True
    return False


def similitud(a: str, b: str) -> float:
    """Parecido entre dos descripciones, de 0 a 1."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    comunes = sum(1 for t in ta if _coincide(t, tb)) + sum(1 for t in tb if _coincide(t, ta))
    por_palabras = comunes / (len(ta) + len(tb))
    por_letras = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    puntaje = 0.8 * por_palabras + 0.2 * por_letras
    # Medidas distintas (1.50 contra 2.80) casi siempre son otro insumo.
    medidas_a = {t for t in ta if re.search(r"\d", t)}
    medidas_b = {t for t in tb if re.search(r"\d", t)}
    if medidas_a and medidas_b and medidas_a != medidas_b:
        puntaje *= 0.85
    return round(puntaje, 3)


def mas_parecidos(texto: str, candidatos: list[dict], campo: str = "texto",
                  minimo: float = PARECIDO_SUGERENCIA, limite: int = 5) -> list[dict]:
    """Los candidatos más parecidos a `texto`, con su puntaje en `parecido`."""
    puntuados = []
    for c in candidatos:
        p = similitud(texto, c.get(campo) or "")
        if p >= minimo:
            puntuados.append({**c, "parecido": p})
    puntuados.sort(key=lambda c: -c["parecido"])
    return puntuados[:limite]


class IndiceTextos:
    """Búsqueda de parecidos sin comparar contra todo el catálogo.

    Solo se comparan los candidatos que comparten al menos una palabra de 3
    letras o más con el texto buscado; con miles de textos, comparar todos
    contra todos tardaría segundos en cada captura.
    """

    def __init__(self, candidatos: list[dict], campo: str = "texto"):
        self.candidatos = candidatos
        self.campo = campo
        self.por_palabra: dict[str, set] = {}
        for n, c in enumerate(candidatos):
            for t in set(_tokens(c.get(campo) or "")):
                if len(t) >= 3:
                    self.por_palabra.setdefault(t, set()).add(n)

    def buscar(self, texto: str, minimo: float = PARECIDO_SUGERENCIA, limite: int = 5) -> list[dict]:
        indices = set()
        for t in set(_tokens(texto)):
            indices |= self.por_palabra.get(t, set())
        return mas_parecidos(texto, [self.candidatos[n] for n in indices], self.campo, minimo, limite)


# ── Código del proveedor ──────────────────────────────
# El CFDI trae el código interno del proveedor (NoIdentificacion). Cuando un
# proveedor cambia cómo describe un artículo («EMPLAYE CN4» → «PELICULA
# ESTIRABLE 18 CN4») el código suele quedarse igual. No se clasifica solo por
# código porque algunos proveedores lo reutilizan para cosas distintas: solo se
# sugiere y alguien confirma.

CODIGOS_GENERICOS = {"N/A", "S/N", "SN", "NA", "SIN", "NINGUNO", "GENERICO", "VARIOS", "NO APLICA", "NOAPLICA"}


def codigo_util(codigo: Optional[str]) -> bool:
    """¿El código distingue un artículo? 000, 1, S/N y similares no."""
    c = " ".join((codigo or "").upper().split())
    if len(c) < 3 or c in CODIGOS_GENERICOS or not re.search(r"[A-Z0-9]", c):
        return False
    if re.fullmatch(r"(.)\1*", c) or (c.isdigit() and int(c) < 1000):
        return False
    return True


def llave_codigo(fila: dict) -> Optional[tuple]:
    """(RFC del emisor, código del proveedor) de un renglón, si el código sirve."""
    codigo = fila.get("no_identificacion")
    rfc = (fila.get("rfc_emisor") or "").upper().strip()
    if not rfc or not codigo_util(codigo):
        return None
    return (rfc, codigo.strip())


def insumo_por_codigo(llaves: set, insumos_por_llave: dict) -> Optional[dict]:
    """El insumo al que apuntan los códigos de un texto, si es uno solo.

    Un código que el proveedor ya usó para dos insumos no dice nada.
    """
    candidatos: dict[str, str] = {}
    for llave in sorted(llaves):
        insumos = insumos_por_llave.get(llave) or set()
        if len(insumos) == 1:
            candidatos.setdefault(next(iter(insumos)), llave[1])
    if len(candidatos) != 1:
        return None
    insumo_id, codigo = next(iter(candidatos.items()))
    return {"insumo_id": insumo_id, "codigo": codigo}


def agrupar_mismo_codigo(filas: list[dict], llaves: set, nombres: dict = None) -> list[dict]:
    """Textos sin clasificar facturados con alguno de esos códigos, del más reciente al más viejo."""
    nombres = nombres or {}
    grupos: dict[str, dict] = {}
    for f in sorted(filas, key=lambda f: _fecha(f.get("fecha")) or date.min):
        llave = llave_codigo(f)
        if llave not in llaves:
            continue
        g = grupos.setdefault(f["texto_norm"], {
            "texto_norm": f["texto_norm"], "codigo": llave[1], "compras": 0,
            "proveedor": nombres.get(llave[0]) or f.get("nombre_emisor") or llave[0],
        })
        g["compras"] += 1
        g["ultima"] = str(_fecha(f.get("fecha")))
        g["ultimo_precio"] = round(float(f.get("precio_unitario") or 0), 4)
        g["unidad"] = f.get("unidad")
    return sorted(grupos.values(), key=lambda g: g["ultima"], reverse=True)


def nombre_sugerido(descripcion: str) -> str:
    """Nombre legible para un insumo nuevo a partir de la descripción."""
    limpio = " ".join((descripcion or "").split())[:120]
    if limpio.isupper() or limpio.islower():
        limpio = limpio.capitalize()
    return limpio


# ── Unidades ──────────────────────────────────────────

def unidad_estandar(unidad: Optional[str], clave: Optional[str] = None) -> Optional[str]:
    """Unidad de ProCompra a partir del texto de la unidad y su clave SAT."""
    por_texto = _POR_TEXTO.get(normalizar(unidad)) if unidad else None
    clave = (clave or "").strip().upper()
    por_clave = CLAVES_UNIDAD.get(clave)
    if por_clave and clave not in CLAVES_GENERICAS:
        return por_clave
    if por_texto:
        return por_texto
    if por_clave:
        return por_clave
    texto = normalizar(unidad)
    return texto[:20] or (clave or None)


# ── Estadística ───────────────────────────────────────

def _fecha(valor) -> Optional[date]:
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(valor)[:10])
        except ValueError:
            return None


def _meses_atras(hoy: date, meses: int) -> date:
    return hoy - timedelta(days=round(meses * 30.44))


def _mediana(valores: list[float]) -> Optional[float]:
    return round(median(valores), 4) if valores else None


def unidad_principal(puntos: list[dict]) -> Optional[str]:
    """La unidad en que más se ha comprado (gana la compra más reciente al empatar)."""
    conteo: dict[str, list] = {}
    for p in puntos:
        u = p.get("unidad")
        if not u:
            continue
        actual = conteo.setdefault(u, [0, date.min])
        actual[0] += 1
        actual[1] = max(actual[1], _fecha(p.get("fecha")) or date.min)
    if not conteo:
        return None
    return max(conteo.items(), key=lambda kv: (kv[1][0], kv[1][1]))[0]


def estadisticas(puntos: list[dict], hoy: date = None, unidad: str = None) -> dict:
    """Precio de referencia y resumen de un insumo.

    `puntos` son compras facturadas: {fecha, precio, cantidad, proveedor, unidad}.
    Solo cuentan las de la unidad del insumo (o la más usada): un metro y un
    rollo no se comparan. La referencia es la mediana de los últimos 6 meses
    (12 si en 6 hay menos de 3 compras): una compra rara no la mueve.
    """
    hoy = hoy or date.today()
    unidad = unidad or unidad_principal(puntos)
    validos = [p for p in puntos if _fecha(p.get("fecha")) and float(p.get("precio") or 0) > 0]
    comparables = [p for p in validos if not unidad or not p.get("unidad") or p["unidad"] == unidad]
    comparables.sort(key=lambda p: _fecha(p["fecha"]))

    referencia, ventana, en_ventana = None, None, []
    for meses in VENTANAS_MESES:
        desde = _meses_atras(hoy, meses)
        en_ventana = [p for p in comparables if _fecha(p["fecha"]) >= desde]
        if len(en_ventana) >= MINIMO_COMPRAS:
            referencia = _mediana([float(p["precio"]) for p in en_ventana])
            ventana = meses
            break

    ultimo_anio = [p for p in comparables if _fecha(p["fecha"]) >= _meses_atras(hoy, 12)]
    precios_anio = [float(p["precio"]) for p in ultimo_anio]
    mas_barata = min(en_ventana if referencia else ultimo_anio, key=lambda p: float(p["precio"]), default=None)

    reciente = [float(p["precio"]) for p in comparables if _fecha(p["fecha"]) >= _meses_atras(hoy, 3)]
    hace_un_anio = [float(p["precio"]) for p in comparables
                    if _meses_atras(hoy, 15) <= _fecha(p["fecha"]) < _meses_atras(hoy, 9)]
    cambio_anual = None
    if reciente and hace_un_anio:
        cambio_anual = round(_mediana(reciente) / _mediana(hace_un_anio) - 1, 4)

    return {
        "unidad":         unidad,
        "referencia":     referencia,
        "ventana_meses":  ventana,
        "compras":        len(comparables),
        "compras_ventana": len(en_ventana) if referencia else 0,
        "otras_unidades": len(validos) - len(comparables),
        "ultima":         _resumen_punto(comparables[-1]) if comparables else None,
        "minimo_12m":     round(min(precios_anio), 4) if precios_anio else None,
        "maximo_12m":     round(max(precios_anio), 4) if precios_anio else None,
        "mas_barata":     _resumen_punto(mas_barata) if mas_barata else None,
        "cambio_anual":   cambio_anual,
        "por_proveedor":  por_proveedor(comparables, hoy),
        "por_mes":        por_mes(comparables),
    }


def _resumen_punto(p: dict) -> dict:
    return {"fecha": str(_fecha(p["fecha"])), "precio": round(float(p["precio"]), 4),
            "proveedor": p.get("proveedor"), "cantidad": p.get("cantidad")}


def por_proveedor(puntos: list[dict], hoy: date = None) -> list[dict]:
    hoy = hoy or date.today()
    grupos: dict[str, list] = {}
    for p in puntos:
        grupos.setdefault(p.get("proveedor") or "Sin proveedor", []).append(p)
    filas = []
    for nombre, lista in grupos.items():
        lista.sort(key=lambda p: _fecha(p["fecha"]))
        precios = [float(p["precio"]) for p in lista]
        filas.append({
            "proveedor": nombre,
            "compras":   len(lista),
            "mediana":   _mediana(precios),
            "minimo":    round(min(precios), 4),
            "ultimo":    round(precios[-1], 4),
            "ultima_fecha": str(_fecha(lista[-1]["fecha"])),
            "cantidad":  round(sum(float(p.get("cantidad") or 0) for p in lista), 4),
        })
    filas.sort(key=lambda f: (f["mediana"], -f["compras"]))
    return filas


def por_mes(puntos: list[dict]) -> list[dict]:
    """Mediana por mes, para la línea de la gráfica."""
    meses: dict[str, list] = {}
    for p in puntos:
        f = _fecha(p["fecha"])
        meses.setdefault(f"{f.year:04d}-{f.month:02d}", []).append(float(p["precio"]))
    return [{"mes": m, "mediana": _mediana(v), "compras": len(v)} for m, v in sorted(meses.items())]


def evaluar(precio, unidad: Optional[str], stats: Optional[dict]) -> dict:
    """Compara un precio con la referencia del insumo.

    nivel: alto | bajo | normal | sin_historial | unidad_distinta | sin_precio
    """
    precio = float(precio or 0)
    if not stats or stats.get("referencia") is None:
        return {"nivel": "sin_historial", "diferencia": None}
    if precio <= 0:
        return {"nivel": "sin_precio", "diferencia": None}
    u = unidad_estandar(unidad) if unidad else None
    if u and stats.get("unidad") and u != stats["unidad"]:
        return {"nivel": "unidad_distinta", "diferencia": None}
    diferencia = round(precio / stats["referencia"] - 1, 4)
    if diferencia >= UMBRAL_ALTO:
        nivel = "alto"
    elif diferencia <= UMBRAL_BAJO:
        nivel = "bajo"
    else:
        nivel = "normal"
    return {"nivel": nivel, "diferencia": diferencia}


def porcentaje(diferencia: Optional[float]) -> str:
    if diferencia is None:
        return ""
    n = round(diferencia * 100)
    return f"+{n}%" if n > 0 else f"{n}%".replace("-", "−")


def rfc_empresa_probable(xmls: list[dict]) -> Optional[str]:
    """El RFC que aparece en más facturas (como emisor o receptor) es el de la empresa."""
    conteo: dict[str, int] = {}
    for x in xmls:
        for rfc in {x.get("rfc_emisor"), x.get("rfc_receptor")}:
            if rfc:
                conteo[rfc] = conteo.get(rfc, 0) + 1
    if not conteo:
        return None
    return max(conteo.items(), key=lambda kv: kv[1])[0]
