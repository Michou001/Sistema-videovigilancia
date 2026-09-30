"""Vigilante de camaras caidas.

Una camara que deja de entregar imagen no produce eventos, y el silencio no
dispara nada: el dashboard la pinta en rojo, pero si nadie lo esta mirando la
camara puede pasar la noche apagada. Este vigilante revisa cada 30 s el
latido de cada camara y, cuando una lleva mas de NOTIFY_CAMARA_CAIDA_S
segundos sin imagen, registra un evento "sin_senal" (con su alerta, que llega
al dashboard y a las notificaciones). Al volver, registra "senal_recuperada" y
avisa cuanto tiempo estuvo fuera.

Una camara esta "sin senal" si:
  - el worker dejo de latir (worker caido, red cortada, equipo apagado), o
  - el worker late pero reporta que la fuente no entrega frames (camara
    desconectada, credenciales cambiadas, cable cortado).

El estado vive en la base de datos (Camera.caida_desde) y la transicion se
toma con un UPDATE condicional: aunque la API corra en varios procesos, o se
reinicie, el aviso sale UNA sola vez.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import update
from sqlmodel import Session, col, select

from api.database import engine
from api.hub import hub
from api.models import Camera
from shared.events import DetectionEvent, EventType
from shared.fechas import a_utc, ahora_utc

log = logging.getLogger(__name__)

INTERVALO_S = 30.0


def segundos_sin_imagen(camara: Camera, ahora: datetime) -> Optional[float]:
    """Cuanto hace que la camara no entrega imagen, o None si nunca ha latido
    (una camara dada de alta a mano que aun no arranca no es una caida)."""
    if camara.last_heartbeat is None:
        return None
    edad = max(0.0, (ahora - a_utc(camara.last_heartbeat)).total_seconds())
    try:
        salud = json.loads(camara.status_json) if camara.status_json else {}
    except ValueError:
        salud = {}
    if not isinstance(salud, dict):
        salud = {}
    sin_frame = salud.get("seconds_since_frame")
    sin_frame = float(sin_frame) if isinstance(sin_frame, (int, float)) else None
    if salud.get("connected") is False:
        # El worker vive pero la fuente no: si no sabe desde cuando, cuenta
        # como caida ya mismo.
        return edad + (sin_frame if sin_frame is not None else float("inf"))
    return edad + (sin_frame or 0.0)


def _duracion(segundos: float) -> str:
    segundos = int(segundos)
    if segundos < 90:
        return f"{segundos} s"
    minutos = segundos // 60
    if minutos < 90:
        return f"{minutos} min"
    horas, minutos = divmod(minutos, 60)
    if horas < 48:
        return f"{horas} h {minutos:02d} min"
    return f"{horas // 24} días"


@dataclass
class Transicion:
    camera_id: str
    caida: bool                 # True = se cayo, False = se recupero
    segundos: float             # sin imagen (caida) o tiempo fuera (recuperada)
    nombre: str = ""
    ubicacion: str = ""


def revisar(umbral_s: float, ahora: Optional[datetime] = None) -> list[Transicion]:
    """Marca en la base de datos las camaras que se cayeron o se recuperaron
    y devuelve SOLO las transiciones que tomo este proceso."""
    ahora = ahora or ahora_utc()
    transiciones: list[Transicion] = []
    with Session(engine) as s:
        camaras = s.exec(select(Camera).where(col(Camera.enabled).is_(True))).all()
        for c in camaras:
            sin_imagen = segundos_sin_imagen(c, ahora)
            if sin_imagen is None:
                continue
            if sin_imagen > umbral_s and c.caida_desde is None:
                desde = ahora - timedelta(seconds=sin_imagen if math.isfinite(sin_imagen) else 0)
                tomado = s.exec(
                    update(Camera).where(col(Camera.id) == c.id, col(Camera.caida_desde).is_(None))
                    .values(caida_desde=desde)
                ).rowcount
                if tomado:
                    transiciones.append(Transicion(c.camera_id, True, sin_imagen, c.name or c.camera_id,
                                                   c.location or ""))
            elif sin_imagen <= umbral_s and c.caida_desde is not None:
                fuera = (ahora - a_utc(c.caida_desde)).total_seconds()
                tomado = s.exec(
                    update(Camera).where(col(Camera.id) == c.id, col(Camera.caida_desde).is_not(None))
                    .values(caida_desde=None)
                ).rowcount
                if tomado:
                    transiciones.append(Transicion(c.camera_id, False, fuera, c.name or c.camera_id,
                                                   c.location or ""))
        s.commit()
    return transiciones


def _evento(t: Transicion) -> DetectionEvent:
    valor = "sin_senal" if t.caida else "senal_recuperada"
    if not t.caida:
        detalle = f"Estuvo sin señal {_duracion(t.segundos)}"
    elif math.isfinite(t.segundos):
        detalle = f"Sin imagen desde hace {_duracion(t.segundos)}"
    else:
        detalle = "El worker está activo pero la cámara no entrega imagen"
    return DetectionEvent(event_id=str(uuid.uuid4()), camera_id=t.camera_id, type=EventType.CAMERA,
                          value=valor, confidence=1.0, meta={"detalle": detalle, "origen": "api"})


async def procesar(umbral_s: float, ahora: Optional[datetime] = None) -> list[Transicion]:
    """Una pasada completa: detectar, registrar, difundir y avisar."""
    from fastapi.concurrency import run_in_threadpool

    from api import notificaciones
    from api.routers.events import registrar_evento_sistema

    transiciones = await run_in_threadpool(revisar, umbral_s, ahora)
    for t in transiciones:
        evento = _evento(t)
        datos_evento, datos_alerta = await run_in_threadpool(registrar_evento_sistema, evento)
        await hub.difundir("event", datos_evento)
        if datos_alerta is not None:
            await hub.difundir("alert", datos_alerta)
        await hub.difundir("camera_status", {"camera_id": t.camera_id, "caida": t.caida})

        if t.caida:
            log.warning("Cámara %s sin señal (%s)", t.camera_id, evento.meta["detalle"])
        else:
            log.info("Cámara %s recuperada (%s)", t.camera_id, evento.meta["detalle"])
            n = notificaciones.obtener()
            if n is not None:
                # La caida se notifico como alerta; la recuperacion no es
                # alerta (severidad info) pero quien recibio el aviso de caida
                # necesita saber que ya no tiene que ir a revisar.
                n.aviso(notificaciones.Mensaje(
                    titulo="Cámara recuperada", detalle=evento.meta["detalle"], severidad="info",
                    camara=t.nombre, ubicacion=t.ubicacion, tipo="camara"))
    return transiciones


async def vigilar(umbral_s: float, intervalo_s: float = INTERVALO_S) -> None:
    """Bucle de fondo de la API. La primera revision espera al menos un
    umbral completo: tras un reinicio de la API los workers necesitan unos
    segundos para volver a latir, y sin esa gracia TODAS las camaras
    parecerian caidas."""
    log.info("Vigilante de cámaras: aviso tras %d s sin imagen", umbral_s)
    await asyncio.sleep(max(umbral_s, intervalo_s))
    while True:
        try:
            await procesar(umbral_s)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - nunca debe tumbar la API
            log.error("Fallo el vigilante de cámaras: %s", e)
        await asyncio.sleep(intervalo_s)
