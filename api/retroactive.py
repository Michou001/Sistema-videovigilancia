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

OJO CON LA SESION: una tarea en segundo plano de FastAPI se ejecuta DESPUES de
que la respuesta ya se armo, y para entonces la sesion de base de datos de la
peticion original (inyectada por Depends) ya se cerro. Por eso estas funciones
abren su PROPIA sesion nueva en vez de recibir una prestada -- usar la sesion
de la peticion aqui fallaria de forma intermitente y dificil de reproducir.
El registro de lista negra que reciben si puede venir de la sesion original:
ya se hizo `session.refresh()` sobre el antes de desprenderse, asi que sus
campos escalares (placa, severidad, id...) siguen siendo validos aunque la
sesion que lo cargo ya no exista.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
from sqlmodel import Session, col, select

from api.config import BASE_DIR, get_config
from api.database import engine
from api.hub import hub
from api.matching import similitud_coseno
from api.models import Alert, BlacklistFace, BlacklistPlate, Event
from api.retention import Politica
from shared.plates import buscar_coincidencia

log = logging.getLogger(__name__)


def _desde(dias: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=dias)


async def _difundir_alerta_retroactiva(alerta: Alert, evento: Event) -> None:
    """Mismo formato que la alerta en vivo (ver api/routers/events.py), para
    que el dashboard no necesite un caso especial: si el operador tiene la
    pagina abierta, la ve aparecer igual que cualquier otra."""
    await hub.difundir("alert", {
        "id": alerta.id,
        "title": alerta.title,
        "detail": alerta.detail,
        "severity": alerta.severity,
        "type": alerta.type,
        "camera_id": alerta.camera_id,
        "event_id": alerta.event_id,
        "snapshot_path": alerta.snapshot_path,
        "match_kind": alerta.match_kind,
        "match_score": alerta.match_score,
        "status": alerta.status,
        "ts": evento.ts.isoformat(),
    })


# --------------------------------------------------------------------------
# Placas
# --------------------------------------------------------------------------

async def reescanear_placa(registro: BlacklistPlate) -> int:
    """Busca la placa recien agregada en las lecturas ya registradas."""
    cfg = get_config()
    dias = Politica().eventos_info  # el texto de la placa vive tanto como el evento

    with Session(engine) as session:
        eventos = session.exec(
            select(Event).where(
                Event.type == "plate",
                col(Event.ts) >= _desde(dias),
                col(Event.matched_blacklist_id).is_(None),
            )
        ).all()

        encontrados = 0
        for evento in eventos:
            coincidencia = buscar_coincidencia(
                evento.value, [(registro.plate, registro.id)],
                max_distancia=cfg.plate_fuzzy_max_dist,
            )
            if coincidencia is None:
                continue

            evento.severity = registro.severity if coincidencia.exacta else "warning"
            evento.match_kind = coincidencia.tipo
            evento.matched_blacklist_id = registro.id
            evento.match_score = coincidencia.score
            session.add(evento)

            alerta = Alert(
                event_id=evento.event_id,
                camera_id=evento.camera_id,
                type=evento.type,
                severity=evento.severity,
                title=f"Coincidencia retroactiva: placa {registro.plate}",
                detail=(f"Esta placa ya habia sido leida el {evento.ts:%Y-%m-%d %H:%M} "
                        f"(track {evento.track_id}), antes de agregarse a la lista negra. "
                        f"{registro.reason}"),
                match_kind=coincidencia.tipo,
                match_score=coincidencia.score,
                snapshot_path=evento.snapshot_path,
            )
            session.add(alerta)
            session.commit()
            session.refresh(alerta)
            await _difundir_alerta_retroactiva(alerta, evento)
            encontrados += 1

    if encontrados:
        log.warning("Re-escaneo retroactivo: placa %s ya habia pasado %d vez/veces",
                    registro.plate, encontrados)
    return encontrados


# --------------------------------------------------------------------------
# Rostros
# --------------------------------------------------------------------------

async def reescanear_rostro(registro: BlacklistFace) -> int:
    """Vuelve a calcular el embedding de las FOTOS de rostros ya guardadas (no
    hay vector que comparar: nunca se guardo uno para quien no coincidia) y
    las compara contra el vector de referencia recien agregado.

    El embedding recalculado vive solo en esta funcion: se usa para la
    comparacion y se descarta, igual que en el cruce en tiempo real. Nunca se
    escribe en la base de datos si no hay coincidencia.
    """
    cfg = get_config()
    dias = Politica().fotos_eventos  # sin foto no hay nada que re-calcular
    referencia = np.frombuffer(registro.vector, dtype=np.float32, count=registro.dim)

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
            return 0

        from edge.config import load_config
        from edge.detectors.faces import FaceEmbedder

        embedder = FaceEmbedder.compartido(load_config())

        encontrados = 0
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

            evento.severity = registro.severity
            evento.match_kind = "biometric_retroactivo"
            evento.matched_blacklist_id = registro.id
            evento.match_score = round(score, 4)
            session.add(evento)

            alerta = Alert(
                event_id=evento.event_id,
                camera_id=evento.camera_id,
                type=evento.type,
                severity=evento.severity,
                title=f"Coincidencia retroactiva: {registro.label}",
                detail=(f"Esta persona ya habia sido vista el {evento.ts:%Y-%m-%d %H:%M}, "
                        f"antes de agregarse a la lista negra ({score:.0%} de similitud). "
                        f"{registro.reason}"),
                match_kind="biometric",
                match_score=round(score, 4),
                snapshot_path=evento.snapshot_path,
            )
            session.add(alerta)
            session.commit()
            session.refresh(alerta)
            await _difundir_alerta_retroactiva(alerta, evento)
            encontrados += 1

    if encontrados:
        log.warning("Re-escaneo retroactivo: '%s' ya habia sido visto %d vez/veces",
                    registro.label, encontrados)
    return encontrados
