"""Re-escaneo retroactivo: cuando alguien entra a la lista negra, revisa el
pasado ademas de vigilar el futuro.

El resto del sistema solo mira hacia ADELANTE: si agregas una placa hoy, el
worker la va a detectar la proxima vez que la camara la vea, pero nada revisa
si esa placa YA habia pasado antes del alta. Este modulo llena ese hueco.

DIFERENCIA IMPORTANTE entre placas y rostros, por la misma razon documentada
en api/routers/events.py (MINIMIZACION DE DATOS BIOMETRICOS):

  PLACAS   El texto de la lectura ya esta guardado en events.value en texto
           plano -- no es dato sensible. Comparar es una consulta y una
           distancia de edicion, nada que recalcular.

  ROSTROS  El embedding de un rostro que NO coincidio nunca se guardo, a
           proposito: guardarlo por si acaso convertiria el sistema en una
           base de datos biometrica de todo el que paso por la camara. Solo
           queda la FOTO (7 dias por defecto). Para comparar hay que volver a
           calcular el embedding de esa foto EN EL MOMENTO del re-escaneo,
           compararlo, y soltarlo otra vez sin guardarlo -- igual que en
           tiempo real. Es mas trabajo, pero es la unica forma de dar esta
           funcion sin renunciar a la minimizacion de datos.

Se corre en segundo plano (FastAPI BackgroundTasks) porque revisar dias de
eventos puede tardar mas de lo que un POST deberia hacer esperar al operador.
Y dentro de la tarea, el trabajo pesado (consultas, recalcular embeddings en
GPU) va al pool de hilos: una tarea de fondo `async` corre en el mismo bucle
de eventos que el video en vivo, y sin esto lo congelaba mientras duraba.

OJO CON LA SESION: una tarea en segundo plano se ejecuta DESPUES de que la
respuesta ya se armo, y para entonces la sesion de la peticion original ya se
cerro. Por eso estas funciones abren su PROPIA sesion y reciben del registro
solo valores planos (id, placa, severidad...), no el objeto de la sesion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session, col, select

from api.alertas import descripcion_vehiculo, mensaje
from api.config import BASE_DIR, get_config
from api.database import engine
from api.hub import hub
from api.matching import similitud_coseno
from api.models import Alert, BlacklistFace, BlacklistPlate, Event
from api.retention import Politica
from shared.plates import buscar_coincidencia

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Referencia:
    id: int
    valor: str          # placa o etiqueta de la persona
    severity: str
    reason: str


def _desde(dias: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=dias)


def _en_utc(ts: datetime) -> datetime:
    # SQLite devuelve las fechas sin zona; se guardaron en UTC.
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _hora_local(ts: datetime) -> str:
    """Fecha para el texto de la alerta, en la hora del servidor: el operador
    lee "ya habia pasado el 3 de sept. a las 14:10", no una hora UTC."""
    return f"{_en_utc(ts).astimezone():%Y-%m-%d %H:%M}"


def _mensaje_alerta(alerta: Alert, evento: Event) -> dict:
    """Mismo formato que la alerta en vivo (ver api/alertas.py), para que el
    dashboard no necesite un caso especial: si el operador tiene la pagina
    abierta, la ve aparecer igual que cualquier otra."""
    return mensaje(alerta, _en_utc(evento.ts).isoformat())


async def _difundir(mensajes: list[dict]) -> None:
    for m in mensajes:
        await hub.difundir("alert", m)


# --------------------------------------------------------------------------
# Placas
# --------------------------------------------------------------------------

def _reescanear_placa(ref: _Referencia) -> list[dict]:
    cfg = get_config()
    dias = Politica().eventos_info  # el texto de la placa vive tanto como el evento
    mensajes: list[dict] = []

    with Session(engine) as session:
        eventos = session.exec(
            select(Event).where(
                Event.type == "plate",
                col(Event.ts) >= _desde(dias),
                col(Event.matched_blacklist_id).is_(None),
            )
        ).all()

        nuevas: list[tuple[Alert, Event]] = []
        for evento in eventos:
            coincidencia = buscar_coincidencia(
                evento.value, [(ref.valor, ref.id)],
                max_distancia=cfg.plate_fuzzy_max_dist,
            )
            if coincidencia is None:
                continue

            evento.severity = ref.severity if coincidencia.exacta else "warning"
            evento.match_kind = coincidencia.tipo
            evento.matched_blacklist_id = ref.id
            evento.match_score = coincidencia.score
            session.add(evento)

            alerta = Alert(
                event_id=evento.event_id,
                camera_id=evento.camera_id,
                type=evento.type,
                severity=evento.severity,
                title=f"Coincidencia retroactiva: placa {ref.valor}",
                detail=" · ".join(p for p in [
                    f"Esta placa ya había sido leída el {_hora_local(evento.ts)} "
                    f"(track {evento.track_id}), antes de agregarse a la lista negra. {ref.reason}",
                    descripcion_vehiculo(evento.meta),
                ] if p),
                match_kind=coincidencia.tipo,
                match_score=coincidencia.score,
                snapshot_path=evento.snapshot_path,
            )
            session.add(alerta)
            nuevas.append((alerta, evento))

        if nuevas:
            session.commit()
            for alerta, evento in nuevas:
                session.refresh(alerta)
                session.refresh(evento)
                mensajes.append(_mensaje_alerta(alerta, evento))
    return mensajes


async def reescanear_placa(registro: BlacklistPlate) -> int:
    """Busca la placa recien agregada en las lecturas ya registradas."""
    ref = _Referencia(registro.id, registro.plate, registro.severity, registro.reason)
    try:
        mensajes = await run_in_threadpool(_reescanear_placa, ref)
    except Exception as e:  # noqa: BLE001 - una tarea de fondo no tiene a quien avisar
        log.error("Fallo el re-escaneo retroactivo de la placa %s: %s", ref.valor, e)
        return 0
    await _difundir(mensajes)
    if mensajes:
        log.warning("Re-escaneo retroactivo: placa %s ya habia pasado %d vez/veces",
                    ref.valor, len(mensajes))
    return len(mensajes)


# --------------------------------------------------------------------------
# Rostros
# --------------------------------------------------------------------------

def _reescanear_rostro(ref: _Referencia, referencia: np.ndarray) -> list[dict]:
    cfg = get_config()
    dias = Politica().fotos_eventos  # sin foto no hay nada que re-calcular
    mensajes: list[dict] = []

    with Session(engine) as session:
        eventos = session.exec(
            select(Event).where(
                Event.type == "face",
                col(Event.ts) >= _desde(dias),
                col(Event.matched_blacklist_id).is_(None),
                col(Event.snapshot_path).is_not(None),
            )
        ).all()
        if not eventos:
            return mensajes

        from edge.config import load_config
        from edge.detectors.faces import FaceEmbedder

        embedder = FaceEmbedder.compartido(load_config())

        for evento in eventos:
            ruta = BASE_DIR / evento.snapshot_path
            if not ruta.exists():
                continue  # la purga automatica pudo borrarla entre la consulta y aqui

            imagen = cv2.imread(str(ruta))
            if imagen is None:
                continue

            vector = embedder.embedding_de_foto(imagen)
            if vector is None:
                continue  # la foto guardada no tenia un rostro nitido para re-detectar

            score = similitud_coseno(referencia, vector)
            del vector  # explicito: no sale de esta funcion, no se guarda en ningun lado
            if score < cfg.face_match_threshold:
                continue

            evento.severity = ref.severity
            evento.match_kind = "biometric_retroactivo"
            evento.matched_blacklist_id = ref.id
            evento.match_score = round(score, 4)
            session.add(evento)

            alerta = Alert(
                event_id=evento.event_id,
                camera_id=evento.camera_id,
                type=evento.type,
                severity=evento.severity,
                title=f"Coincidencia retroactiva: {ref.valor}",
                detail=(f"Esta persona ya había sido vista el {_hora_local(evento.ts)}, "
                        f"antes de agregarse a la lista negra ({score:.0%} de similitud). "
                        f"{ref.reason}"),
                match_kind="biometric",
                match_score=round(score, 4),
                snapshot_path=evento.snapshot_path,
            )
            session.add(alerta)
            session.commit()
            session.refresh(alerta)
            session.refresh(evento)
            mensajes.append(_mensaje_alerta(alerta, evento))
    return mensajes


async def reescanear_rostro(registro: BlacklistFace) -> int:
    """Vuelve a calcular el embedding de las FOTOS de rostros ya guardadas (no
    hay vector que comparar: nunca se guardo uno para quien no coincidia) y
    las compara contra el vector de referencia recien agregado.

    El embedding recalculado vive solo dentro de la busqueda: se usa para la
    comparacion y se descarta, igual que en el cruce en tiempo real. Nunca se
    escribe en la base de datos si no hay coincidencia.
    """
    ref = _Referencia(registro.id, registro.label, registro.severity, registro.reason)
    referencia = np.frombuffer(registro.vector, dtype=np.float32, count=registro.dim).copy()
    try:
        mensajes = await run_in_threadpool(_reescanear_rostro, ref, referencia)
    except Exception as e:  # noqa: BLE001
        log.error("Fallo el re-escaneo retroactivo de '%s': %s", ref.valor, e)
        return 0
    await _difundir(mensajes)
    if mensajes:
        log.warning("Re-escaneo retroactivo: '%s' ya habia sido visto %d vez/veces",
                    ref.valor, len(mensajes))
    return len(mensajes)
