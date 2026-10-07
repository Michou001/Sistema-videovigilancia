"""Recomendador de instalacion: que puede hacer una camara segun como se monte.

Reglas explicables, sin modelo que "opine": cada numero sale de geometria del
lente y de umbrales que el sistema ya mide, y cada advertencia dice por que.

    densidad = pixeles horizontales / ancho de escena a esa distancia  (px/m)
    placa    = densidad x 0.305 m x cos(angulo horizontal)             (px)

Umbrales y de donde salen:

  - Placa >= 36 px de ancho: medido con tools/calibrar_distancia.py contra el
    detector y OCR reales (ADR-001). Para instalar se pide el doble, porque en
    sitio hay movimiento, noche y suciedad que en la medicion no.
  - Rostro >= 50 px: edge/detectors/faces.py descarta los mas chicos (ArcFace
    se entreno con recortes de 112 px). Mismo margen del doble.
  - Personas: niveles DORI de la norma IEC 62676-4 en pixeles por metro
    (deteccion 25, observacion 62.5, reconocimiento 125, identificacion 250).
    Movimiento y zonas piden deteccion; caida y posturas piden observacion,
    porque el esqueleto necesita ver brazos y cadera.

Lo que no se puede saber sin ir al sitio (luz de noche, reflejo del IR en la
placa, vehiculos rapidos) no se adivina: sale como validacion pendiente.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

ANCHO_PLACA_M = 0.305     # NOM-001-SCT-2-2016
ANCHO_ROSTRO_M = 0.16
ALTURA_PERSONA_M = 1.70

PLACA_MIN_PX = 36.0
ROSTRO_MIN_PX = 50.0
MARGEN_INSTALACION = 2.0

# IEC 62676-4: pixeles por metro sobre el objetivo.
DORI = (
    ("identificacion", "Identificación", 250.0),
    ("reconocimiento", "Reconocimiento", 125.0),
    ("observacion", "Observación", 62.5),
    ("deteccion", "Detección", 25.0),
)
PPM = {clave: ppm for clave, _, ppm in DORI}

# Campo de vision horizontal de los lentes fijos de camaras bala Hikvision,
# los mismos que usa tools/calibrar_distancia.py. Otro lente: el instalador
# captura el HFOV de la hoja de datos.
LENTES_HFOV = {2.8: 106.0, 4.0: 84.0, 6.0: 54.0}

# Detectores del worker (variables ENABLE_* de su .env).
DETECTORES = ("ENABLE_PLATES", "ENABLE_FACES", "ENABLE_MOTION", "ENABLE_POSE",
              "ENABLE_ZONAS", "ENABLE_WEAPONS")


@dataclass(frozen=True)
class Funcion:
    clave: str
    nombre: str
    altura_objetivo_m: float        # a que altura esta lo que se quiere ver
    usos: tuple[str, ...]           # en orden de importancia para esta funcion
    detectores: dict = field(default_factory=dict)
    nota: str = ""


FUNCIONES: dict[str, Funcion] = {f.clave: f for f in (
    Funcion("lpr", "LPR / acceso vehicular", 0.5,
            ("placas",),
            {"ENABLE_PLATES": True},
            "Cámara dedicada al carril, apuntada a donde pasa la placa."),
    Funcion("peatonal", "Acceso peatonal", 1.6,
            ("pose", "movimiento", "rostros", "conteo"),
            {"ENABLE_MOTION": True, "ENABLE_POSE": True, "ENABLE_ZONAS": True},
            "Rostro opcional: exige aviso de privacidad y fundamento legal."),
    Funcion("pasillo", "Pasillo / corredor", 1.0,
            ("pose", "movimiento", "conteo"),
            {"ENABLE_MOTION": True, "ENABLE_POSE": True, "ENABLE_ZONAS": True}),
    Funcion("zona", "Zona restringida", 1.0,
            ("movimiento", "conteo", "pose"),
            {"ENABLE_MOTION": True, "ENABLE_ZONAS": True},
            "La intrusión se configura como zona con horario."),
    Funcion("patio", "Panorámica / patio", 1.0,
            ("movimiento", "pose", "conteo"),
            {"ENABLE_MOTION": True, "ENABLE_POSE": True, "ENABLE_ZONAS": True},
            "Merodeo y conteo; la caída solo en la parte cercana de la escena."),
    Funcion("estacionamiento", "Estacionamiento", 1.0,
            ("movimiento", "conteo", "placas"),
            {"ENABLE_MOTION": True, "ENABLE_ZONAS": True},
            "Para placas, una cámara aparte en el carril de entrada."),
)}

NOMBRES_USO = {
    "placas": "Lectura de placas",
    "rostros": "Reconocimiento facial",
    "pose": "Caída y posturas",
    "movimiento": "Movimiento, intrusión y merodeo",
    "conteo": "Conteo y cruce de línea",
    "armas": "Armas",
}


class DatosInvalidos(ValueError):
    """Entrada fuera de rango; el mensaje se muestra tal cual al usuario."""


def hfov_de(lente_mm: float | None, hfov_grados: float | None) -> tuple[float, str]:
    """Campo de vision horizontal y de donde salio."""
    if hfov_grados:
        if not 10 <= hfov_grados <= 180:
            raise DatosInvalidos("El campo de visión debe estar entre 10° y 180°.")
        return float(hfov_grados), "instalador"
    if lente_mm is not None:
        for mm, grados in LENTES_HFOV.items():
            if abs(mm - lente_mm) < 0.05:
                return grados, "tabla de lentes"
        raise DatosInvalidos(
            f"No tengo el campo de visión del lente de {lente_mm:g} mm: captúralo "
            "de la hoja de datos de la cámara (HFOV, en grados).")
    raise DatosInvalidos("Indica el lente (2.8, 4 o 6 mm) o el campo de visión horizontal.")


def nivel_dori(ppm: float) -> tuple[str, str] | None:
    for clave, nombre, minimo in DORI:
        if ppm >= minimo:
            return clave, nombre
    return None


def alcance_m(ancho_px: int, hfov: float, ancho_objeto_m: float, px_necesarios: float,
              desnivel_m: float = 0.0, angulo_h: float = 0.0) -> float:
    """Distancia horizontal maxima a la que el objeto todavia mide
    `px_necesarios` de ancho. Misma geometria que calibrar_distancia.py, mas
    el desnivel: lo que cuenta es la distancia en linea recta al lente."""
    efectivo = ancho_objeto_m * math.cos(math.radians(angulo_h))
    recta = (ancho_px * efectivo) / (2.0 * px_necesarios * math.tan(math.radians(hfov / 2)))
    if recta <= abs(desnivel_m):
        return 0.0
    return math.sqrt(recta ** 2 - desnivel_m ** 2)


def _medidas(ancho_px: int, hfov: float, recta_m: float, angulo_h: float) -> dict:
    escena = 2.0 * recta_m * math.tan(math.radians(hfov / 2))
    ppm = ancho_px / escena
    cos_h = math.cos(math.radians(angulo_h))
    return {
        "ancho_escena_m": round(escena, 2),
        "px_por_metro": round(ppm, 1),
        "placa_px": round(ppm * ANCHO_PLACA_M * cos_h, 1),
        "rostro_px": round(ppm * ANCHO_ROSTRO_M * cos_h, 1),
        "persona_px": round(ppm * ALTURA_PERSONA_M, 1),
    }


def _por_umbral(valor: float, minimo: float) -> str:
    if valor >= minimo * MARGEN_INSTALACION:
        return "ok"
    return "limite" if valor >= minimo else "no"


def _evaluar_uso(uso: str, m: dict, vertical: float) -> dict:
    estado, detalle = "no", ""
    if uso == "placas":
        estado = _por_umbral(m["placa_px"], PLACA_MIN_PX)
        detalle = (f"La placa mide {m['placa_px']:.0f} px; se lee desde {PLACA_MIN_PX:.0f} px "
                   f"y para instalar se piden {PLACA_MIN_PX * MARGEN_INSTALACION:.0f}.")
        if estado == "ok" and vertical > 30:
            estado = "limite"
            detalle += f" El ángulo vertical de {vertical:.0f}° deforma la placa (ideal ≤ 30°)."
    elif uso == "rostros":
        estado = _por_umbral(m["rostro_px"], ROSTRO_MIN_PX)
        detalle = (f"El rostro mide {m['rostro_px']:.0f} px; el sistema descarta los menores "
                   f"de {ROSTRO_MIN_PX:.0f} px.")
        if estado == "ok" and vertical > 15:
            estado = "limite"
            detalle += f" Desde {vertical:.0f}° hacia abajo se ve más frente y cabello que cara (ideal ≤ 15°)."
    elif uso == "pose":
        ppm = m["px_por_metro"]
        estado = "ok" if ppm >= PPM["observacion"] else ("limite" if ppm >= PPM["deteccion"] else "no")
        detalle = (f"{ppm:.0f} px/m: el esqueleto pide nivel Observación "
                   f"({PPM['observacion']:g} px/m, IEC 62676-4).")
    elif uso in ("movimiento", "conteo"):
        ppm = m["px_por_metro"]
        estado = "ok" if ppm >= PPM["deteccion"] else "no"
        detalle = (f"Una persona mide {m['persona_px']:.0f} px de alto; seguirla pide nivel "
                   f"Detección ({PPM['deteccion']:g} px/m).")
    return {"uso": uso, "nombre": NOMBRES_USO[uso], "estado": estado, "detalle": detalle}


def recomendar(funcion: str, altura_m: float, distancia_m: float, ancho_px: int, *,
               lente_mm: float | None = None, hfov_grados: float | None = None,
               angulo_horizontal: float = 0.0, ancho_px_principal: int | None = None,
               altura_objetivo_m: float | None = None, codec: str | None = None) -> dict:
    """Usos recomendados, detectores sugeridos, advertencias y validaciones
    pendientes para una camara montada asi."""
    f = FUNCIONES.get(funcion)
    if f is None:
        raise DatosInvalidos("Función de cámara desconocida.")
    if not 0.3 <= altura_m <= 30:
        raise DatosInvalidos("La altura de montaje debe estar entre 0.3 y 30 m.")
    if not 0.5 <= distancia_m <= 200:
        raise DatosInvalidos("La distancia al objetivo debe estar entre 0.5 y 200 m.")
    if not 160 <= ancho_px <= 8192:
        raise DatosInvalidos("La resolución horizontal debe estar entre 160 y 8192 px.")
    if not 0 <= angulo_horizontal <= 80:
        raise DatosInvalidos("El ángulo horizontal debe estar entre 0° y 80°.")

    hfov, fuente_hfov = hfov_de(lente_mm, hfov_grados)
    objetivo = f.altura_objetivo_m if altura_objetivo_m is None else altura_objetivo_m
    desnivel = altura_m - objetivo
    recta = math.hypot(distancia_m, desnivel)
    vertical = math.degrees(math.atan2(desnivel, distancia_m))

    m = _medidas(ancho_px, hfov, recta, angulo_horizontal)
    dori = nivel_dori(m["px_por_metro"])
    usos = [_evaluar_uso(u, m, vertical) for u in f.usos]
    usos.append({"uso": "armas", "nombre": NOMBRES_USO["armas"], "estado": "no",
                 "detalle": "Desactivado: no hay un modelo validado que lo haga de forma confiable."})

    detectores = {d: bool(f.detectores.get(d, False)) for d in DETECTORES}
    advertencias: list[str] = []
    validaciones: list[str] = []

    alcances = {
        "placa_lectura_m": round(alcance_m(ancho_px, hfov, ANCHO_PLACA_M, PLACA_MIN_PX,
                                           altura_m - 0.5, angulo_horizontal), 1),
        "rostro_m": round(alcance_m(ancho_px, hfov, ANCHO_ROSTRO_M, ROSTRO_MIN_PX,
                                    altura_m - 1.6, angulo_horizontal), 1),
        "observacion_m": round(alcance_m(ancho_px, hfov, 1.0, PPM["observacion"]), 1),
        "deteccion_m": round(alcance_m(ancho_px, hfov, 1.0, PPM["deteccion"]), 1),
    }

    principal = None
    if ancho_px_principal and ancho_px_principal > ancho_px:
        principal = _medidas(ancho_px_principal, hfov, recta, angulo_horizontal)

    # --- Advertencias por funcion ------------------------------------------
    estado_placas = next((u["estado"] for u in usos if u["uso"] == "placas"), None)
    if funcion == "lpr":
        if estado_placas == "no" and principal and principal["placa_px"] >= PLACA_MIN_PX:
            advertencias.append(
                f"Con este stream la placa no alcanza; con el principal "
                f"({ancho_px_principal} px) mediría {principal['placa_px']:.0f} px. "
                "Usa el principal para placas o acerca el encuadre.")
        elif estado_placas != "ok":
            advertencias.append(
                "La placa queda chica: acerca la cámara al carril, usa un lente más cerrado "
                f"o más resolución. Con este montaje se lee hasta ~{alcances['placa_lectura_m']} m.")
        if vertical > 30:
            advertencias.append(f"Ángulo vertical de {vertical:.0f}°: baja la cámara o aléjala "
                                "del carril; arriba de 30° la placa se ve aplastada.")
        if angulo_horizontal > 45:
            advertencias.append("Más de 45° de lado: las pruebas del sistema llegan a 50°, "
                                "pero la placa pierde ancho (×cos del ángulo).")
        validaciones += [
            "Leer una placa real o impresa en el punto exacto donde se detienen los vehículos.",
            "Probar de noche: el infrarrojo puede saturar la placa reflejante.",
            "Probar con un vehículo en movimiento: si la placa sale barrida, subir la "
            "velocidad de obturación en la cámara.",
        ]
    if funcion == "peatonal":
        estado_rostro = next(u["estado"] for u in usos if u["uso"] == "rostros")
        if estado_rostro == "ok":
            validaciones.append(
                "Rostros: activar solo con aviso de privacidad visible y fundamento legal "
                "(datos biométricos, LFPDPPP).")
        if vertical > 15:
            advertencias.append(f"Para rostros, {vertical:.0f}° hacia abajo es mucho: "
                                "monta más bajo o más lejos de la puerta.")
    if funcion == "estacionamiento":
        advertencias.append("No se recomienda leer placas de todo el estacionamiento: el lente "
                            "manda más que el modelo. Para placas, cámara dedicada al carril.")
    if funcion in ("zona", "patio", "estacionamiento", "pasillo", "peatonal"):
        validaciones.append("Dibujar la zona o línea en Monitoreo (icono de zona) y probar "
                            "con una persona caminando a la distancia más lejana.")
    if funcion == "patio" and m["px_por_metro"] < PPM["observacion"]:
        advertencias.append(
            f"La caída y las posturas solo se ven bien hasta ~{alcances['observacion_m']} m; "
            "más lejos, solo movimiento y zonas.")

    # --- Generales ---------------------------------------------------------
    if dori is None:
        advertencias.append(f"Menos de {PPM['deteccion']:g} px/m: a esa distancia ni siquiera "
                            "se detecta a una persona con confianza.")
    if altura_m < 2.5:
        advertencias.append("A menos de 2.5 m queda al alcance de la mano: más fácil de tapar "
                            "o girar (GOSS avisa de sabotaje en Hikvision, pero mejor evitarlo).")
    if codec and "265" in codec:
        advertencias.append("El stream va en H.265: el video WebRTC en el navegador no está "
                            "garantizado. Cambia ese stream a H.264 o se usará MJPEG.")
    if fuente_hfov == "instalador":
        validaciones.append("Confirmar el campo de visión en la hoja de datos del lente.")
    validaciones.append("Revisar el encuadre con la vista previa antes de dar de alta.")

    return {
        "funcion": {"clave": f.clave, "nombre": f.nombre, "nota": f.nota},
        "entrada": {
            "altura_m": altura_m, "distancia_m": distancia_m, "ancho_px": ancho_px,
            "hfov_grados": hfov, "hfov_fuente": fuente_hfov, "lente_mm": lente_mm,
            "angulo_horizontal": angulo_horizontal, "altura_objetivo_m": objetivo,
        },
        "geometria": {"distancia_recta_m": round(recta, 2),
                      "angulo_vertical": round(vertical, 1)},
        "medidas": m,
        "medidas_principal": principal,
        "dori": {"clave": dori[0], "nombre": dori[1]} if dori else None,
        "alcances": alcances,
        "usos": usos,
        "detectores": detectores,
        "advertencias": advertencias,
        "validaciones": validaciones,
    }
