"""Normalizacion y comparacion de placas, tolerante a errores de OCR.

Por que existe este modulo: si comparas la salida del OCR contra la lista negra
con `==`, pierdes la mitad de las coincidencias reales. El OCR lee "ABC-I23"
donde dice "ABC-123", y un match exacto lo deja pasar. En vigilancia eso es un
falso negativo, que es el error caro.

La estrategia tiene dos capas:

  1. NORMALIZAR: colapsar los caracteres que el OCR confunde a una sola forma
     canonica (O, D, Q -> 0). Dos placas que solo difieren en esos caracteres
     quedan identicas y el match exacto ya las atrapa.

  2. DISTANCIA: si aun asi no son iguales, medir distancia de edicion. Una sola
     sustitucion sobre una placa de 6-7 caracteres es casi siempre la misma
     placa mal leida.

Este modulo lo usan los dos lados: el borde para validar el OCR, y la API para
cruzar contra la lista negra. Por eso vive en shared/.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

# --------------------------------------------------------------------------
# Formatos de placa mexicana
# --------------------------------------------------------------------------
# Formatos de la NOM-001-SCT-2-2016 y de las placas estatales vigentes. Cada
# patron acepta cualquier separador (guion, espacio o nada).

_SEP = r"[\s\-]*"

PATRONES_PLACAS: list[tuple[str, str]] = [
    (rf"^[A-Z]{{3}}{_SEP}\d{{3}}$", "particular (3 letras + 3 digitos)"),
    (rf"^[A-Z]{{3}}{_SEP}\d{{2}}{_SEP}\d{{2}}$", "particular Guanajuato"),
    (rf"^[A-Z]{{3}}{_SEP}\d{{3}}{_SEP}[A-Z]$", "particular Edomex"),
    (rf"^[A-Z]{{2}}{_SEP}\d{{4}}$", "2 letras + 4 digitos"),
    (rf"^[A-Z]{_SEP}\d{{4}}$", "1 letra + 4 digitos"),
    (rf"^\d{{3}}{_SEP}[A-Z]{{3}}$", "3 digitos + 3 letras"),
    (rf"^(CD|CC){_SEP}\d{{3,4}}$", "cuerpo diplomatico/consular"),
    (rf"^(OF|OFICIAL){_SEP}\d+$", "oficial"),
    (rf"^[A-Z]{{3}}{_SEP}\d{{4}}$", "carga/publico"),
]

_PATRONES_COMPILADOS = [(re.compile(p), etiqueta) for p, etiqueta in PATRONES_PLACAS]

# Los mismos formatos expresados como secuencia de clases de caracter
# (L = letra, D = digito), sin separadores. Sirven para corregir la lectura
# por POSICION: si el formato dice que ahi va un digito y el OCR leyo una "O",
# es un cero. Los prefijos fijos (CD, CC, OF) no necesitan plantilla propia:
# caben en las de 2 letras + digitos.
PLANTILLAS: list[tuple[str, str]] = [
    ("LLLDDD", "particular (3 letras + 3 digitos)"),
    ("LLLDDDD", "particular Guanajuato / carga"),
    ("LLLDDDL", "particular Edomex"),
    ("LLDDDD", "2 letras + 4 digitos"),
    ("LDDDD", "1 letra + 4 digitos"),
    ("DDDLLL", "3 digitos + 3 letras"),
]

# Correcciones por posicion. Solo se aplican cuando la plantilla lo exige, asi
# que no hay ambiguedad: en una posicion de digito una "B" solo puede ser 8.
LETRA_A_DIGITO = {"O": "0", "D": "0", "Q": "0", "U": "0", "I": "1", "L": "1",
                  "J": "1", "Z": "2", "S": "5", "G": "6", "B": "8", "A": "4",
                  "T": "7"}
DIGITO_A_LETRA = {"0": "O", "1": "I", "2": "Z", "4": "A", "5": "S", "6": "G",
                  "7": "T", "8": "B"}

# Texto que aparece en las placas y no es parte del numero: nombre del estado,
# leyendas. Cualquier fragmento solo de letras con 5 o mas caracteres se
# descarta igual (ninguna placa mexicana tiene 5 letras seguidas); aqui van
# los cortos que pasarian por longitud.
_LEYENDAS = {"CDMX"}


# --------------------------------------------------------------------------
# Normalizacion
# --------------------------------------------------------------------------

# Caracteres que el OCR confunde entre si, mapeados a una forma canonica.
# Se elige el DIGITO como canonico porque en las placas mexicanas la parte
# numerica es mas larga que la alfabetica, asi que estadisticamente acierta mas.
CONFUSIONES: dict[str, str] = {
    "O": "0", "D": "0", "Q": "0",
    "I": "1", "L": "1", "T": "1",
    "Z": "2",
    "S": "5",
    "G": "6",
    "B": "8",
    "A": "4",
}


def limpiar(texto: str) -> str:
    """Mayusculas, sin acentos y sin nada que no sea alfanumerico."""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]", "", texto.upper())


def normalizar(texto: str) -> str:
    """Forma canonica para comparar. NO es para mostrar al operador.

        normalizar("ABC-123")  -> "48C123"
        normalizar("A8C-I23")  -> "48C123"   <- misma placa, OCR distinto
    """
    return "".join(CONFUSIONES.get(c, c) for c in limpiar(texto))


def formatear(texto: str) -> str:
    """Forma legible para mostrar, ej. 'ABC123' -> 'ABC-123'.
    Solo cosmetico: nunca compares usando esto."""
    limpio = limpiar(texto)
    for patron, _ in _PATRONES_COMPILADOS:
        if patron.match(limpio):
            m = re.match(r"^([A-Z]+)(\d+)([A-Z]*)$", limpio)
            if m:
                partes = [p for p in m.groups() if p]
                return "-".join(partes)
            break
    return limpio


def es_placa_valida(texto: str) -> tuple[bool, str, Optional[str]]:
    """Verifica si el texto tiene forma de placa mexicana.

    Devuelve (es_valida, texto_limpio, etiqueta_del_formato).

    Nota: el patron generico `[A-Za-z0-9]+-[A-Za-z0-9]+` del codigo original se
    elimino a proposito. Aceptaba practicamente cualquier cosa con un guion
    ("AB-1", "HOLA-MUNDO") y era la causa de la mayoria de las filas basura en
    placas_detectadas.csv.
    """
    limpio = limpiar(texto)
    if not (5 <= len(limpio) <= 8):
        return False, limpio, None
    for patron, etiqueta in _PATRONES_COMPILADOS:
        if patron.match(limpio):
            return True, limpio, etiqueta
    return False, limpio, None


def corregir_placa(texto: str) -> Optional[tuple[str, str, int]]:
    """Ajusta una lectura de OCR al formato de placa que mejor le queda.

    El OCR no sabe que en "ABC-123" las tres primeras posiciones son letras:
    lee "A8C-I23" y esa lectura no pasa ningun patron. Aqui se prueba cada
    plantilla del mismo largo y se corrige caracter por caracter segun lo que
    la plantilla espera en esa posicion.

    Devuelve (texto_corregido, formato, numero_de_correcciones) con la
    plantilla que menos correcciones necesito, o None si ninguna encaja.
    Una lectura que ya era valida sale con 0 correcciones.
    """
    limpio = limpiar(texto)
    valida, _, formato = es_placa_valida(limpio)
    if valida:
        return limpio, formato or "", 0

    mejor: Optional[tuple[str, str, int]] = None
    for plantilla, etiqueta in PLANTILLAS:
        if len(plantilla) != len(limpio):
            continue
        salida = []
        correcciones = 0
        for c, clase in zip(limpio, plantilla):
            if clase == "D" and not c.isdigit():
                c2 = LETRA_A_DIGITO.get(c)
            elif clase == "L" and not c.isalpha():
                c2 = DIGITO_A_LETRA.get(c)
            else:
                c2 = c
            if c2 is None:
                break
            correcciones += c2 != c
            salida.append(c2)
        else:
            if mejor is None or correcciones < mejor[2]:
                mejor = ("".join(salida), etiqueta, correcciones)

    # Un OCR razonable confunde uno o dos caracteres por placa. Mas que eso ya
    # no es una lectura corregida, es una lectura inventada.
    if mejor is not None and mejor[2] > max(1, len(limpio) // 3):
        return None
    return mejor


def _es_leyenda(fragmento: str) -> bool:
    if fragmento == "OFICIAL":
        return False
    return fragmento in _LEYENDAS or (len(fragmento) >= 5 and fragmento.isalpha())


def lecturas_de_ocr(resultados: Iterable, min_conf: float = 0.0,
                    penalizacion: float = 0.9) -> list[tuple[str, float]]:
    """Convierte la salida cruda de un OCR sobre UN recorte de placa en
    lecturas candidatas (texto, confianza).

    `resultados` es una lista de (caja, texto, conf), con la caja como 4
    puntos. Un OCR de texto general parte la placa en varios trozos con
    frecuencia ("ABC" y "123", o "ABC", "12", "34" en Guanajuato) y ademas lee
    el nombre del estado; tomar cada trozo por separado dejaria sin evento a
    esas placas. El OCR de placas del detector devuelve un solo trozo, que
    pasa por el mismo camino.

    Se generan tres tipos de candidato:
      - cada fragmento por separado,
      - los fragmentos de la linea principal unidos de izquierda a derecha
        (los caracteres de la placa son el texto mas alto del recorte; las
        leyendas son mas chicas),
      - pares consecutivos de esa linea.
    Cada candidato pasa por `corregir_placa`, y la confianza se multiplica por
    `penalizacion` por cada caracter corregido: una lectura que necesito
    ajustes vale un poco menos en el consenso que una que salio limpia.
    """
    fragmentos: list[tuple[float, float, float, str, float]] = []  # x, y, alto, texto, conf
    for caja, texto, conf in resultados:
        conf = float(conf)
        limpio = limpiar(texto)
        if not limpio or conf < min_conf or _es_leyenda(limpio):
            continue
        try:
            xs = [float(p[0]) for p in caja]
            ys = [float(p[1]) for p in caja]
            x, y, alto = min(xs), (min(ys) + max(ys)) / 2, max(ys) - min(ys)
        except (TypeError, ValueError, IndexError):
            x, y, alto = float(len(fragmentos)), 0.0, 1.0
        fragmentos.append((x, y, alto, limpio, conf))

    if not fragmentos:
        return []

    candidatos: list[tuple[str, float]] = [(f[3], f[4]) for f in fragmentos]

    alto_max = max(f[2] for f in fragmentos)
    linea = sorted((f for f in fragmentos if f[2] >= 0.6 * alto_max), key=lambda f: f[0])
    if len(linea) >= 2:
        unido = "".join(f[3] for f in linea)
        candidatos.append((unido, sum(f[4] for f in linea) / len(linea)))
        for a, b in zip(linea, linea[1:]):
            candidatos.append((a[3] + b[3], (a[4] + b[4]) / 2))

    lecturas: list[tuple[str, float]] = []
    vistas: set[str] = set()
    for texto, conf in candidatos:
        corregida = corregir_placa(texto)
        if corregida is None:
            continue
        placa, _, n = corregida
        if placa in vistas:
            continue
        vistas.add(placa)
        lecturas.append((placa, conf * (penalizacion ** n)))

    # Sin ninguna lectura con forma de placa se devuelven los fragmentos tal
    # cual: el consenso por track puede combinarlos con otros frames.
    return lecturas or [(f[3], f[4]) for f in fragmentos]


# --------------------------------------------------------------------------
# Distancia de edicion
# --------------------------------------------------------------------------

def distancia_levenshtein(a: str, b: str) -> int:
    """Numero minimo de inserciones/borrados/sustituciones para pasar de a a b.
    Implementacion iterativa en O(len(a)*len(b)) memoria O(len(b)); las placas
    tienen 6-8 caracteres, asi que sobra."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previa = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        actual = [i]
        for j, cb in enumerate(b, start=1):
            actual.append(min(
                previa[j] + 1,          # borrado
                actual[j - 1] + 1,      # insercion
                previa[j - 1] + (ca != cb),  # sustitucion
            ))
        previa = actual
    return previa[-1]


def similitud(a: str, b: str) -> float:
    """Similitud en [0, 1] entre dos placas ya normalizadas."""
    if not a and not b:
        return 1.0
    maximo = max(len(a), len(b))
    return 1.0 - (distancia_levenshtein(a, b) / maximo) if maximo else 0.0


# --------------------------------------------------------------------------
# Coincidencia contra lista negra
# --------------------------------------------------------------------------

@dataclass
class Coincidencia:
    """Resultado de cruzar una lectura contra la lista negra."""

    placa_lista: str      # el registro de la lista negra
    id_lista: Optional[int]
    exacta: bool          # True = las formas normalizadas son identicas
    score: float          # 1.0 en exacta; <1.0 en difusa
    distancia: int

    @property
    def tipo(self) -> str:
        return "exact" if self.exacta else "fuzzy"


def buscar_coincidencia(
    lectura: str,
    lista: Iterable[tuple[str, Optional[int]]],
    *,
    max_distancia: int = 1,
    min_similitud: float = 0.80,
) -> Optional[Coincidencia]:
    """Cruza una lectura de OCR contra la lista negra.

    `lista` es un iterable de (placa, id). La comparacion se hace sobre formas
    normalizadas, asi que atrapa confusiones de OCR sin marcar placas distintas.

    `max_distancia=1` es deliberadamente conservador: permite UN error de
    lectura. Subirlo a 2 empieza a producir falsos positivos entre placas
    legitimamente distintas, y una alerta falsa en vigilancia le cuesta tiempo
    real a una persona.
    """
    objetivo = normalizar(lectura)
    if not objetivo:
        return None

    mejor: Optional[Coincidencia] = None

    for placa, id_registro in lista:
        candidato = normalizar(placa)
        if not candidato:
            continue

        if candidato == objetivo:
            return Coincidencia(placa, id_registro, exacta=True, score=1.0, distancia=0)

        # Una diferencia grande de longitud no puede resolverse con 1 edicion.
        if abs(len(candidato) - len(objetivo)) > max_distancia:
            continue

        distancia = distancia_levenshtein(objetivo, candidato)
        if distancia > max_distancia:
            continue

        score = similitud(objetivo, candidato)
        if score < min_similitud:
            continue

        if mejor is None or score > mejor.score:
            mejor = Coincidencia(placa, id_registro, exacta=False, score=score, distancia=distancia)

    return mejor


def elegir_mejor_lectura(lecturas: list[tuple[str, float]]) -> Optional[tuple[str, float]]:
    """Dadas varias lecturas OCR del MISMO vehiculo (mismo track_id), elige una.

    No se queda con la de mayor confianza a secas: primero filtra las que tienen
    forma de placa valida. Una lectura basura con confianza 0.9 vale menos que
    una placa bien formada con 0.6.

    Si varias lecturas normalizan al mismo valor, gana esa por consenso: que
    tres frames distintos coincidan es mejor evidencia que un solo puntaje alto.
    El voto de cada grupo es la SUMA de confianzas, no el conteo: cuatro
    lecturas dudosas (0.2) no deben ganarle a tres claras (0.9).

    Ademas se intenta una fusion caracter por caracter (ver
    `fusionar_por_posicion`): con varias lecturas del mismo largo, cada
    posicion se decide por voto. Asi se recupera la placa aunque ningun frame
    la haya leido completa sin error.
    """
    if not lecturas:
        return None

    validas = [(t, c) for t, c in lecturas if es_placa_valida(t)[0]]
    candidatas = validas or lecturas

    # Consenso: agrupar por forma normalizada
    grupos: dict[str, list[tuple[str, float]]] = {}
    for texto, conf in candidatas:
        grupos.setdefault(normalizar(texto), []).append((texto, conf))

    mejor_grupo = max(
        grupos.values(),
        key=lambda g: (sum(c for _, c in g), len(g)),
    )
    elegida = max(mejor_grupo, key=lambda x: x[1])

    if len(validas) >= 3:
        fusion = fusionar_por_posicion(validas)
        if fusion is not None and normalizar(fusion[0]) != normalizar(elegida[0]):
            # La fusion solo sustituye a la eleccion por grupos si su voto es
            # mas fuerte que el del grupo ganador.
            voto_grupo = sum(c for _, c in mejor_grupo)
            if fusion[2] > voto_grupo:
                return fusion[0], fusion[1]
    return elegida


def fusionar_por_posicion(
    lecturas: list[tuple[str, float]],
) -> Optional[tuple[str, float, float]]:
    """Voto por caracter entre lecturas validas del mismo largo.

    Devuelve (placa, confianza, voto) o None si no hay una placa valida que
    salga del voto. `voto` es la suma de las confianzas que respaldan cada
    posicion, promediada por caracter: comparable con el voto de un grupo en
    `elegir_mejor_lectura`.
    """
    por_largo: dict[int, list[tuple[str, float]]] = {}
    for texto, conf in lecturas:
        limpio = limpiar(texto)
        por_largo.setdefault(len(limpio), []).append((limpio, conf))
    if not por_largo:
        return None

    grupo = max(por_largo.values(), key=lambda g: sum(c for _, c in g))
    if len(grupo) < 3:
        return None

    largo = len(grupo[0][0])
    total = sum(c for _, c in grupo)
    if total <= 0:
        return None

    salida, respaldo = [], 0.0
    for i in range(largo):
        votos: dict[str, float] = {}
        for texto, conf in grupo:
            votos[texto[i]] = votos.get(texto[i], 0.0) + conf
        caracter, peso = max(votos.items(), key=lambda kv: kv[1])
        salida.append(caracter)
        respaldo += peso

    placa = "".join(salida)
    if not es_placa_valida(placa)[0]:
        return None
    voto = respaldo / largo
    confianza = min(1.0, voto / len(grupo))
    return formatear(placa), round(confianza, 4), voto
