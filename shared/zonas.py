"""Zonas y reglas: geometria y horarios, compartidos por el borde y la API.

El borde decide QUE paso dentro de una zona (alguien entro, cruzo la linea en
tal sentido, lleva 90 s rondando). La API decide si eso es una alerta, con la
misma funcion de horario que uso el borde y la hora del EVENTO: un evento que
llega tarde desde el spool se juzga por cuando ocurrio, no por cuando llego.

COORDENADAS
  Los puntos se guardan NORMALIZADOS (0..1 sobre el ancho y el alto del
  cuadro), no en pixeles: la misma zona sirve aunque el worker procese a
  otra resolucion que la del editor del dashboard.

PUNTO DE REFERENCIA DE UN OBJETO
  El centro inferior de su caja: los pies de la persona, las llantas del
  vehiculo. Las zonas se dibujan sobre el piso (el estacionamiento, la puerta);
  con el centro de la caja, una persona alta "entraria" a una zona que todavia
  no pisa.

HORARIOS
  Lista de franjas {"dias": [0..6], "desde": "HH:MM", "hasta": "HH:MM"} con
  0 = lunes. Una franja que cruza medianoche (22:00 a 06:00) pertenece al dia
  en que EMPIEZA: "viernes 22:00-06:00" cubre hasta el sabado a las 6.
  Sin horario (None o lista vacia) la regla esta activa siempre.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

Punto = tuple[float, float]

TIPOS = ("intrusion", "linea", "merodeo", "conteo")
"""intrusion: alguien entra a un poligono (con horario, p.ej. de noche).
linea:     alguien cruza una linea, opcionalmente en un solo sentido.
merodeo:   alguien permanece dentro de un poligono mas de N segundos.
conteo:    cuenta lo que cruza una linea (no alerta; es estadistica)."""

TIPOS_LINEA = ("linea", "conteo")
CLASES = ("persona", "vehiculo")
DIRECCIONES = ("ambas", "entrada", "salida")
"""Sentido de cruce de una linea A->B. "entrada" es pasar del lado izquierdo al
lado derecho de quien camina de A hacia B (la flecha que dibuja el editor);
"salida", al reves."""

VALOR_EVENTO = {"intrusion": "intrusion", "linea": "cruce_linea",
                "merodeo": "merodeo", "conteo": "conteo"}

NOMBRES_DIAS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
_HORA = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# --------------------------------------------------------------------------
# Geometria
# --------------------------------------------------------------------------

def punto_en_poligono(p: Punto, poligono: Sequence[Punto]) -> bool:
    """Ray casting. Un punto exactamente sobre el borde puede caer de
    cualquier lado; para objetos en movimiento no importa."""
    x, y = p
    dentro = False
    n = len(poligono)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = poligono[i]
        xj, yj = poligono[j]
        if (yi > y) != (yj > y):
            x_cruce = xj + (y - yj) * (xi - xj) / (yi - yj)
            if x < x_cruce:
                dentro = not dentro
        j = i
    return dentro


def lado(p: Punto, a: Punto, b: Punto) -> float:
    """Producto cruz (B-A) x (P-A). En coordenadas de imagen (y hacia abajo),
    positivo = a la DERECHA de quien camina de A hacia B."""
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def distancia_a_recta(p: Punto, a: Punto, b: Punto) -> float:
    largo = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
    return abs(lado(p, a, b)) / largo if largo > 0 else 0.0


def segmentos_se_cruzan(p0: Punto, p1: Punto, a: Punto, b: Punto) -> bool:
    d1, d2 = lado(p0, a, b), lado(p1, a, b)
    d3, d4 = lado(a, p0, p1), lado(b, p0, p1)
    return (d1 * d2 < 0) and (d3 * d4 <= 0)


def sentido_de_cruce(lado_antes: float, lado_despues: float) -> Optional[str]:
    if lado_antes < 0 < lado_despues:
        return "entrada"
    if lado_antes > 0 > lado_despues:
        return "salida"
    return None


def punto_referencia(bbox: Sequence[float], ancho: float, alto: float) -> Punto:
    """Centro inferior de la caja, normalizado."""
    x1, _, x2, y2 = bbox
    return ((x1 + x2) / 2 / max(1.0, ancho), y2 / max(1.0, alto))


# --------------------------------------------------------------------------
# Horarios
# --------------------------------------------------------------------------

def zona_horaria():
    """La del sitio (ZONA_HORARIA, por defecto America/Mexico_City).

    Los horarios de una regla son hora LOCAL del lugar ("de 22 a 6"); la base
    guarda UTC. En Windows zoneinfo necesita el paquete tzdata; si no esta, se
    usa la hora local del equipo.
    """
    nombre = os.getenv("ZONA_HORARIA", "America/Mexico_City")
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(nombre)
    except Exception:  # noqa: BLE001
        return datetime.now().astimezone().tzinfo or timezone.utc


def _minutos(texto: str) -> int:
    m = _HORA.match(texto or "")
    if not m:
        raise ValueError(f"hora invalida: {texto!r} (formato HH:MM)")
    return int(m.group(1)) * 60 + int(m.group(2))


def validar_horario(horario: Optional[Iterable[dict]]) -> list[dict]:
    """Normaliza y valida. Lanza ValueError con un mensaje para el operador."""
    if not horario:
        return []
    limpio = []
    for franja in horario:
        if not isinstance(franja, dict):
            raise ValueError("cada franja debe tener dias, desde y hasta")
        dias = sorted({int(d) for d in franja.get("dias", [])})
        if not dias or any(d < 0 or d > 6 for d in dias):
            raise ValueError("los dias van de 0 (lunes) a 6 (domingo) y hace falta al menos uno")
        desde, hasta = str(franja.get("desde", "")), str(franja.get("hasta", ""))
        if _minutos(desde) == _minutos(hasta):
            raise ValueError("una franja no puede empezar y terminar a la misma hora")
        limpio.append({"dias": dias, "desde": desde, "hasta": hasta})
    if len(limpio) > 14:
        raise ValueError("maximo 14 franjas por regla")
    return limpio


def en_horario(horario: Optional[Iterable[dict]], cuando: datetime, tz=None) -> bool:
    """Si la regla esta activa en el instante `cuando` (con zona)."""
    if not horario:
        return True
    if cuando.tzinfo is None:
        cuando = cuando.replace(tzinfo=timezone.utc)
    local = cuando.astimezone(tz or zona_horaria())
    minuto = local.hour * 60 + local.minute
    hoy = local.weekday()
    ayer = (hoy - 1) % 7
    for franja in horario:
        desde, hasta = _minutos(franja["desde"]), _minutos(franja["hasta"])
        dias = franja["dias"]
        if desde < hasta:
            if hoy in dias and desde <= minuto < hasta:
                return True
        else:  # cruza medianoche
            if hoy in dias and minuto >= desde:
                return True
            if ayer in dias and minuto < hasta:
                return True
    return False


def describir_horario(horario: Optional[Iterable[dict]]) -> str:
    """"lun-vie 22:00-06:00; sáb, dom todo el día"."""
    if not horario:
        return "siempre"
    partes = []
    for f in horario:
        dias = f["dias"]
        if dias == list(range(7)):
            txt_dias = "todos los días"
        elif len(dias) > 2 and dias == list(range(dias[0], dias[-1] + 1)):
            txt_dias = f"{NOMBRES_DIAS[dias[0]]}-{NOMBRES_DIAS[dias[-1]]}"
        else:
            txt_dias = ", ".join(NOMBRES_DIAS[d] for d in dias)
        partes.append(f"{txt_dias} {f['desde']}-{f['hasta']}")
    return "; ".join(partes)


# --------------------------------------------------------------------------
# Validacion de una zona completa (la usa la API al guardar)
# --------------------------------------------------------------------------

def validar_puntos(tipo: str, puntos: Sequence[Sequence[float]]) -> list[list[float]]:
    if tipo not in TIPOS:
        raise ValueError(f"tipo debe ser uno de: {', '.join(TIPOS)}")
    limpio = []
    for p in puntos:
        if len(p) != 2:
            raise ValueError("cada punto es [x, y]")
        x, y = float(p[0]), float(p[1])
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("las coordenadas van normalizadas entre 0 y 1")
        limpio.append([round(x, 5), round(y, 5)])
    if tipo in TIPOS_LINEA:
        if len(limpio) != 2:
            raise ValueError("una línea lleva exactamente 2 puntos")
        (ax, ay), (bx, by) = limpio
        if ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5 < 0.01:
            raise ValueError("la línea es demasiado corta")
    else:
        if not 3 <= len(limpio) <= 24:
            raise ValueError("un polígono lleva de 3 a 24 puntos")
        if abs(area(limpio)) < 1e-4:
            raise ValueError("el polígono es demasiado pequeño o sus puntos están alineados")
    return limpio


def area(poligono: Sequence[Sequence[float]]) -> float:
    s = 0.0
    n = len(poligono)
    for i in range(n):
        x1, y1 = poligono[i]
        x2, y2 = poligono[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2


def ahora_local(tz=None) -> datetime:
    return datetime.now(tz or zona_horaria())


def proximo_cambio(horario: Optional[Iterable[dict]], desde: datetime, tz=None,
                   paso_min: int = 5, max_horas: int = 24 * 8) -> Optional[datetime]:
    """Cuando cambia de activa a inactiva (o al reves). Para el dashboard:
    "armada hasta las 06:00"."""
    if not horario:
        return None
    estado = en_horario(horario, desde, tz)
    t = desde.replace(second=0, microsecond=0)
    for _ in range(max_horas * 60 // paso_min):
        t += timedelta(minutes=paso_min)
        if en_horario(horario, t, tz) != estado:
            return t
    return None

