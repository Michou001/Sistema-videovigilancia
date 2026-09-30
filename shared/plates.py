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
#
# Fuentes:
#   - NOM-001-SCT-2-2016 (DOF 24/06/2016), tabla de conformacion de caracteres
#     por tipo de vehiculo y servicio, y su regla general: "en los caracteres
#     alfabeticos no deberan utilizarse las letras I, N, O, Q".
#   - Formatos que las entidades emiten en la practica y los de la norma
#     anterior (NOM-001-SCT-2-2000) que siguen en circulacion.
#
# Una mascara se escribe con L (letra), D (digito), '-' (separador, solo para
# mostrar) y texto entre comillas simples para caracteres fijos: 'AM'-DDD-LL
# es "AM" seguido de tres digitos y dos letras (ambulancias).
#
# `nom2016`: el formato es de la norma vigente, y por lo tanto NUNCA lleva I,
# N, O ni Q. Esa regla es la que mas ayuda al OCR: una "O" en una posicion de
# letra de una placa nueva no puede ser una O -- es una D mal leida (o un 0).
#
# `frecuente`: el formato se usa como destino de la correccion por posicion.
# Los formatos raros (auto antiguo, ambulancia...) se aceptan si la lectura ya
# encaja tal cual, pero no se "corrige" una lectura dudosa hacia ellos: con
# 30 plantillas, casi cualquier ruido encajaria en alguna con 2 cambios.
#
# `peso`: desempate entre formatos que encajan igual de bien. Mas alto = mas
# comun en la calle.

LETRAS_PROHIBIDAS_NOM = frozenset("IÑOQ")


@dataclass(frozen=True)
class FormatoPlaca:
    mascara: str
    tipo: str
    nom2016: bool = False
    entidad: Optional[str] = None
    frecuente: bool = True
    peso: int = 0

    @property
    def clases(self) -> tuple[str, ...]:
        """Una entrada por caracter: 'L', 'D' o '=X' para un caracter fijo."""
        return _clases_de(self.mascara)

    @property
    def grupos(self) -> tuple[int, ...]:
        return tuple(len(_clases_de(g)) for g in _partir(self.mascara))

    @property
    def norma(self) -> str:
        return "NOM-001-SCT-2-2016" if self.nom2016 else "formato anterior"


def _partir(mascara: str) -> list[str]:
    grupos, actual, literal = [], "", False
    for c in mascara:
        if c == "'":
            literal = not literal
            actual += c
        elif c == "-" and not literal:
            grupos.append(actual)
            actual = ""
        else:
            actual += c
    grupos.append(actual)
    return grupos


def _clases_de(mascara: str) -> tuple[str, ...]:
    salida, literal = [], False
    for c in mascara:
        if c == "'":
            literal = not literal
        elif literal:
            salida.append("=" + c)
        elif c in "LD":
            salida.append(c)
    return tuple(salida)


FORMATOS: tuple[FormatoPlaca, ...] = (
    # --- Particulares ------------------------------------------------------
    FormatoPlaca("LLL-DDD-L", "Automóvil particular", nom2016=True, peso=100),
    FormatoPlaca("LDD-LLL", "Automóvil particular", nom2016=True, entidad="Ciudad de México", peso=90),
    FormatoPlaca("LLL-DD-DD", "Automóvil particular (formato anterior)", peso=80),
    FormatoPlaca("LLL-DDD", "Automóvil (formato anterior)", peso=70),
    FormatoPlaca("DDD-LLL", "Automóvil (formato anterior)", peso=65),
    FormatoPlaca("LL-DDDD", "Vehículo (formato anterior)", peso=40),
    FormatoPlaca("L-DDDD", "Vehículo (formato anterior)", peso=30),
    # --- Carga ---------------------------------------------------------------
    FormatoPlaca("L-DDD-LL", "Camión particular", nom2016=True, peso=60),
    FormatoPlaca("LL-DDDD-L", "Camión particular", peso=50),
    FormatoPlaca("LL-DD-DDD", "Camión particular", peso=45),
    FormatoPlaca("DD-LL-DL", "Autotransporte federal", nom2016=True, peso=55),
    FormatoPlaca("'D'-DD-DDD", "Convertidor (dolly) federal", nom2016=True, frecuente=False),
    # --- Pasaje ----------------------------------------------------------------
    FormatoPlaca("DD-LL-D", "Autobús particular", nom2016=True, frecuente=False),
    FormatoPlaca("DD-LLL-DD", "Autobús", frecuente=False),
    FormatoPlaca("D-LLL-DD", "Autobús", frecuente=False),
    FormatoPlaca("L-DDD-LLL", "Taxi / servicio público", nom2016=True, peso=58),
    FormatoPlaca("DD-DD-LLL", "Taxi / servicio público", peso=40),
    FormatoPlaca("L-DDDD-L", "Taxi / servicio público", nom2016=True, entidad="Ciudad de México", peso=45),
    FormatoPlaca("L-DDDDD", "Taxi / servicio público", entidad="Ciudad de México", peso=35),
    FormatoPlaca("DDD-L-DDD", "Transporte público", nom2016=True, entidad="Ciudad de México", peso=35),
    # --- Remolques ---------------------------------------------------------
    FormatoPlaca("L-DL-DD", "Remolque", nom2016=True, frecuente=False),
    FormatoPlaca("DLL-DDD-L", "Remolque", frecuente=False),
    FormatoPlaca("D-LL-DDDD", "Remolque", frecuente=False),
    # --- Motocicletas --------------------------------------------------------
    FormatoPlaca("LDD-LL", "Motocicleta", nom2016=True, peso=50),
    FormatoPlaca("LDDD-L", "Motocicleta", peso=45),
    FormatoPlaca("LLL-DD", "Motocicleta", peso=40),
    FormatoPlaca("DDDD-L", "Motocicleta", entidad="Ciudad de México", peso=40),
    # --- Especiales ----------------------------------------------------------
    FormatoPlaca("D-LL-DDL", "Demostración (agencia)", nom2016=True, frecuente=False),
    FormatoPlaca("D-LL-DDD", "Demostración (agencia)", frecuente=False),
    FormatoPlaca("LL-D-L", "Auto antiguo", nom2016=True, frecuente=False),
    FormatoPlaca("LL-DD", "Auto antiguo", frecuente=False),
    FormatoPlaca("DLL-DD", "Auto antiguo", frecuente=False),
    FormatoPlaca("DDD-L", "Personas con discapacidad", nom2016=True, frecuente=False),
    FormatoPlaca("DDD-LL", "Personas con discapacidad", frecuente=False),
    FormatoPlaca("DD-LLL", "Personas con discapacidad", frecuente=False),
    FormatoPlaca("DDL-DDD", "Vehículo ecológico", nom2016=True, frecuente=False),
    FormatoPlaca("LL-DDDL-D", "Policía", nom2016=True, frecuente=False),
    FormatoPlaca("DD-'PC'-DDD", "Protección civil", nom2016=True, frecuente=False),
    FormatoPlaca("'AM'-DDD-LL", "Ambulancia", nom2016=True, frecuente=False),
    FormatoPlaca("'BM'-DDD-LL", "Bomberos", nom2016=True, frecuente=False),
    FormatoPlaca("'CD'-DDDD", "Cuerpo diplomático", frecuente=False),
    FormatoPlaca("'CD'-DDD", "Cuerpo diplomático", frecuente=False),
    FormatoPlaca("'CC'-DDDD", "Cuerpo consular", frecuente=False),
    FormatoPlaca("'CC'-DDD", "Cuerpo consular", frecuente=False),
)

LARGO_MIN = min(len(f.clases) for f in FORMATOS)
LARGO_MAX = max(len(f.clases) for f in FORMATOS)

# Series de la NOM-001-SCT-2-2016 para AUTOMOVILES PARTICULARES (formato
# LLL-DDD-L): las tres primeras letras dicen la entidad que la emitio. Los
# rangos brincan las letras prohibidas. Fuente: tabla de series por entidad
# (Apendice C de la norma, reproducida en "Vehicle registration plates of
# Mexico", Wikipedia); coincide con otras transcripciones de la norma en las
# entidades que estas incluyen. La Ciudad de México no aparece porque usa su
# propio formato (LDD-LLL).
SERIES_AUTOMOVIL_2016: tuple[tuple[str, str, str], ...] = (
    ("AAA", "AFZ", "Aguascalientes"),
    ("AGA", "CYZ", "Baja California"),
    ("CZA", "DEZ", "Baja California Sur"),
    ("DFA", "DKZ", "Campeche"),
    ("DLA", "DSZ", "Chiapas"),
    ("DTA", "ETZ", "Chihuahua"),
    ("EUA", "FPZ", "Coahuila"),
    ("FRA", "FWZ", "Colima"),
    ("FXA", "GFZ", "Durango"),
    ("GGA", "GYZ", "Guanajuato"),
    ("GZA", "HFZ", "Guerrero"),
    ("HGA", "HRZ", "Hidalgo"),
    ("HSA", "LFZ", "Jalisco"),
    ("LGA", "PEZ", "Estado de México"),
    ("PFA", "PUZ", "Michoacán"),
    ("PVA", "RDZ", "Morelos"),
    ("REA", "RJZ", "Nayarit"),
    ("RKA", "TGZ", "Nuevo León"),
    ("THA", "TMZ", "Oaxaca"),
    ("TNA", "UJZ", "Puebla"),
    ("UKA", "UPZ", "Querétaro"),
    ("URA", "UVZ", "Quintana Roo"),
    ("UWA", "VEZ", "San Luis Potosí"),
    ("VFA", "VSZ", "Sinaloa"),
    ("VTA", "WKZ", "Sonora"),
    ("WLA", "WWZ", "Tabasco"),
    ("WXA", "XSZ", "Tamaulipas"),
    ("XTA", "XXZ", "Tlaxcala"),
    ("XYA", "YVZ", "Veracruz"),
    ("YWA", "ZCZ", "Yucatán"),
    ("ZDA", "ZHZ", "Zacatecas"),
)


def entidad_por_serie(placa: str) -> Optional[str]:
    """Entidad que emitio una placa particular de la norma vigente, segun su
    serie. None si no es de ese formato o la serie no esta asignada."""
    limpio = limpiar(placa)
    if len(limpio) != 7 or not re.fullmatch(r"[A-Z]{3}\d{3}[A-Z]", limpio):
        return None
    serie = limpio[:3]
    for desde, hasta, entidad in SERIES_AUTOMOVIL_2016:
        if desde <= serie <= hasta:
            return entidad
    return None


# Correcciones por posicion. Solo se aplican cuando la plantilla lo exige, asi
# que no hay ambiguedad: en una posicion de digito una "B" solo puede ser 8.
LETRA_A_DIGITO = {"O": "0", "D": "0", "Q": "0", "U": "0", "I": "1", "L": "1",
                  "J": "1", "Z": "2", "S": "5", "G": "6", "B": "8", "A": "4",
                  "T": "7"}
DIGITO_A_LETRA = {"0": "O", "1": "I", "2": "Z", "4": "A", "5": "S", "6": "G",
                  "7": "T", "8": "B"}
# En un formato de la norma vigente no existen O, I ni Q: el cero en una
# posicion de letra es una D, y una O o una Q leidas ahi tambien. Para el "1"
# no hay una letra permitida que se le parezca lo suficiente como para
# adivinarla: se deja sin corregir.
DIGITO_A_LETRA_NOM = {**{k: v for k, v in DIGITO_A_LETRA.items() if v not in LETRAS_PROHIBIDAS_NOM},
                      "0": "D"}
LETRA_PROHIBIDA_A_LETRA = {"O": "D", "Q": "D"}

# Texto que aparece en las placas y no es parte del numero: nombre del estado,
# leyendas. Cualquier fragmento solo de letras con 5 o mas caracteres se
# descarta igual (ninguna placa mexicana tiene 5 letras seguidas); aqui van
# los cortos que pasarian por longitud.
_LEYENDAS = {"CDMX"}

# Letra que la NOM imprime en chico para distinguir la placa delantera (F) de
# la trasera (T). Si el OCR la alcanza a leer, sobra un caracter al inicio o
# al final.
_MARCAS_POSICION = "FT"

# Nombres de pais del OCR (en ingles) como los lee el operador.
PAISES = {
    "Mexico": "México", "United States": "Estados Unidos", "Canada": "Canadá",
    "Brazil": "Brasil", "Argentina": "Argentina", "Spain": "España",
    "Germany": "Alemania", "France": "Francia", "Italy": "Italia",
    "United Kingdom": "Reino Unido", "Netherlands": "Países Bajos",
}


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


# --------------------------------------------------------------------------
# Formato
# --------------------------------------------------------------------------

def _encaja(limpio: str, formato: FormatoPlaca) -> Optional[tuple[str, int]]:
    """Ajusta `limpio` a la mascara del formato.

    Devuelve (texto_ajustado, numero_de_correcciones) o None si no hay forma.
    Con 0 correcciones la lectura ya era valida para este formato.
    """
    clases = formato.clases
    if len(clases) != len(limpio):
        return None
    salida = []
    correcciones = 0
    for c, clase in zip(limpio, clases):
        if clase == "D":
            nuevo = c if c.isdigit() else LETRA_A_DIGITO.get(c)
        elif clase == "L":
            if c.isalpha():
                nuevo = c
                if formato.nom2016 and c in LETRAS_PROHIBIDAS_NOM:
                    nuevo = LETRA_PROHIBIDA_A_LETRA.get(c)
            else:
                mapa = DIGITO_A_LETRA_NOM if formato.nom2016 else DIGITO_A_LETRA
                nuevo = mapa.get(c)
        else:  # caracter fijo
            fijo = clase[1]
            if c == fijo:
                nuevo = c
            elif LETRA_A_DIGITO.get(c) == fijo or DIGITO_A_LETRA.get(c) == fijo:
                nuevo = fijo
            else:
                nuevo = None
        if nuevo is None:
            return None
        correcciones += nuevo != c
        salida.append(nuevo)
    return "".join(salida), correcciones


def _formatos_exactos(limpio: str) -> list[FormatoPlaca]:
    return [f for f in FORMATOS if (r := _encaja(limpio, f)) is not None and r[1] == 0]


def _especificidad(formato: FormatoPlaca) -> int:
    """Cuantos caracteres fijos tiene: "CD-1234" es diplomatica antes que
    "dos letras y cuatro digitos" generica."""
    return sum(1 for c in formato.clases if c.startswith("="))


def formato_de(texto: str) -> Optional[FormatoPlaca]:
    """El formato mas probable de una placa ya valida (sin corregir nada)."""
    exactos = _formatos_exactos(limpiar(texto))
    return max(exactos, key=lambda f: (_especificidad(f), f.peso)) if exactos else None


def es_formato_frecuente(texto: str) -> bool:
    """Si la placa (ya valida) es de un formato comun en la calle."""
    formato = formato_de(texto)
    return formato is not None and formato.frecuente


def formatear(texto: str) -> str:
    """Forma legible para mostrar, ej. 'ABC123D' -> 'ABC-123-D'.
    Solo cosmetico: nunca compares usando esto."""
    limpio = limpiar(texto)
    formato = formato_de(limpio)
    if formato is None:
        return limpio
    partes, i = [], 0
    for n in formato.grupos:
        partes.append(limpio[i:i + n])
        i += n
    return "-".join(p for p in partes if p)


def es_placa_valida(texto: str) -> tuple[bool, str, Optional[str]]:
    """Verifica si el texto tiene forma de placa mexicana.

    Devuelve (es_valida, texto_limpio, tipo_de_placa).

    Nota: el patron generico `[A-Za-z0-9]+-[A-Za-z0-9]+` del codigo original se
    elimino a proposito. Aceptaba practicamente cualquier cosa con un guion
    ("AB-1", "HOLA-MUNDO") y era la causa de la mayoria de las filas basura en
    placas_detectadas.csv.
    """
    limpio = limpiar(texto)
    if not (LARGO_MIN <= len(limpio) <= LARGO_MAX):
        return False, limpio, None
    formato = formato_de(limpio)
    if formato is None:
        return False, limpio, None
    return True, limpio, formato.tipo


def corregir_placa(texto: str) -> Optional[tuple[str, str, int]]:
    """Ajusta una lectura de OCR al formato de placa que mejor le queda.

    El OCR no sabe que en "ABC-123-D" las tres primeras posiciones son letras:
    lee "A8C-I23-D" y esa lectura no pasa ningun patron. Aqui se prueba cada
    formato del mismo largo y se corrige caracter por caracter segun lo que
    el formato espera en esa posicion.

    Tambien se prueba quitar la letra chica F/T (placa delantera/trasera) si
    el OCR la leyo como un caracter mas al inicio o al final.

    Devuelve (texto_corregido, tipo_de_placa, numero_de_correcciones) con el
    formato que menos correcciones necesito, o None si ninguno encaja. Una
    lectura que ya era valida sale con 0 correcciones.
    """
    limpio = limpiar(texto)
    if not limpio:
        return None
    valida, _, tipo = es_placa_valida(limpio)
    if valida and es_formato_frecuente(limpio):
        return limpio, tipo or "", 0
    # Si la lectura solo encaja TAL CUAL en un formato raro, se compara con
    # la mejor correccion hacia uno comun: "0BC-123-A" es un remolque valido
    # (1AB-234-C), pero es mucho mas probable un particular DBC-123-A con la
    # D mal leida. Hay muchos mas autos que remolques en la calle.
    exacta_rara = (limpio, tipo or "", 0) if valida else None

    candidatos: list[tuple[int, int, str, str]] = []  # (correcciones, -peso, texto, tipo)
    variantes = [(limpio, 0)]
    if len(limpio) > LARGO_MIN:
        if limpio[0] in _MARCAS_POSICION:
            variantes.append((limpio[1:], 1))
        if limpio[-1] in _MARCAS_POSICION:
            variantes.append((limpio[:-1], 1))

    for variante, costo in variantes:
        for formato in FORMATOS:
            if costo and not formato.nom2016:
                continue   # la marca F/T solo existe en placas de la norma vigente
            r = _encaja(variante, formato)
            if r is None:
                continue
            ajustado, n = r
            if n and not formato.frecuente:
                continue
            candidatos.append((n + costo, -formato.peso, ajustado, formato.tipo))

    if exacta_rara is not None:
        comunes = [c for c in candidatos if c[0] == 1 and _es_tipo_frecuente(c[3])]
        return min(comunes)[2:] + (1,) if comunes else exacta_rara
    if not candidatos:
        return None
    n, _, ajustado, tipo = min(candidatos)
    # Un OCR razonable confunde uno o dos caracteres por placa. Mas que eso ya
    # no es una lectura corregida, es una lectura inventada.
    if n > max(1, len(limpio) // 3):
        return None
    return ajustado, tipo, n


def _es_tipo_frecuente(tipo: str) -> bool:
    return any(f.tipo == tipo and f.frecuente for f in FORMATOS)


@dataclass(frozen=True)
class InfoPlaca:
    """Lo que se sabe de una placa leida, para mostrar al operador."""

    texto: str                 # limpio, sin separadores
    legible: str               # con guiones, como se escribe
    tipo: str                  # "Automóvil particular", "Motocicleta"...
    norma: str                 # "NOM-001-SCT-2-2016" | "formato anterior" | ""
    entidad: Optional[str]     # por serie o por formato; None si no se sabe
    pais: str                  # "México", "Estados Unidos"...
    extranjera: bool = False

    def como_meta(self) -> dict:
        meta = {"tipo_placa": self.tipo, "pais": self.pais}
        if self.norma:
            meta["norma"] = self.norma
        if self.entidad:
            meta["entidad"] = self.entidad
        return meta


def nombre_pais(region: Optional[str]) -> Optional[str]:
    if not region or region == "Unknown":
        return None
    return PAISES.get(region, region)


def analizar_placa(texto: str, pais: Optional[str] = None) -> Optional[InfoPlaca]:
    """Tipo, norma y entidad de una placa. None si no tiene forma de placa.

    `pais` es el que reconocio el OCR ("Mexico", "United States"...). Una
    placa extranjera NO se ajusta a los formatos mexicanos: forzar "ABC1234"
    de Texas a "ABC-12-34" inventaria una placa mexicana que no existe.
    """
    limpio = limpiar(texto)
    nombre = nombre_pais(pais)
    if nombre and nombre != "México":
        if not (2 <= len(limpio) <= 10):
            return None
        return InfoPlaca(limpio, limpio, "Placa extranjera", "", None, nombre, extranjera=True)

    formato = formato_de(limpio)
    if formato is None:
        return None
    entidad = formato.entidad
    if formato.mascara == "LLL-DDD-L":
        entidad = entidad_por_serie(limpio)
    return InfoPlaca(limpio, formatear(limpio), formato.tipo, formato.norma, entidad, "México")


def pais_por_votos(votos: Iterable[tuple[Optional[str], float]],
                   minimo: float = 0.6) -> Optional[str]:
    """Pais de un vehiculo a partir de las lecturas de varios frames.

    Cada lectura del OCR trae el pais que reconoce y su probabilidad. Se suma
    el peso por pais y se devuelve el ganador solo si concentra al menos
    `minimo` del total: una placa de Texas vista de lado puede salir "Mexico"
    en un frame y "United States" en cinco. Con votos repartidos se devuelve
    None, y el vehiculo se trata como mexicano (el caso comun).
    """
    pesos: dict[str, float] = {}
    for region, peso in votos:
        if region and region != "Unknown" and peso > 0:
            pesos[region] = pesos.get(region, 0.0) + float(peso)
    if not pesos:
        return None
    total = sum(pesos.values())
    region, peso = max(pesos.items(), key=lambda kv: kv[1])
    return region if peso / total >= minimo else None


def describir(texto: str, pais: Optional[str] = None) -> str:
    """Una linea para la alerta: 'Automóvil particular de Jalisco'."""
    info = analizar_placa(texto, pais)
    if info is None:
        return ""
    if info.extranjera:
        return f"Placa de {info.pais}"
    return f"{info.tipo} de {info.entidad}" if info.entidad else info.tipo


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
    # Entre las validas, las de formatos comunes primero: una lectura que solo
    # encaja en un formato raro (auto antiguo, demostracion) suele ser una
    # placa comun mal leida.
    frecuentes = [(t, c) for t, c in validas if es_formato_frecuente(t)]
    candidatas = frecuentes or validas or lecturas

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
