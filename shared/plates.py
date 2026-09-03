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
# Heredados del detector original (legacy/detectarTexto.py), reescritos para
# aceptar cualquier separador (guion, espacio o nada) en un solo patron.

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
    """
    if not lecturas:
        return None

    validas = [(t, c) for t, c in lecturas if es_placa_valida(t)[0]]
    candidatas = validas or lecturas

    # Consenso: agrupar por forma normalizada
    grupos: dict[str, list[tuple[str, float]]] = {}
    for texto, conf in candidatas:
        grupos.setdefault(normalizar(texto), []).append((texto, conf))

    # Gana el grupo con mas apariciones; a igualdad, el de mayor confianza media
    mejor_grupo = max(
        grupos.values(),
        key=lambda g: (len(g), sum(c for _, c in g) / len(g)),
    )
    return max(mejor_grupo, key=lambda x: x[1])
