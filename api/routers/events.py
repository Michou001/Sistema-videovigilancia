"""Ingesta de eventos del worker de borde.

Es el endpoint mas caliente del sistema: por aqui pasa todo lo que detectan las
camaras. Tres cosas importan mas que en cualquier otro sitio del proyecto:

  1. IDEMPOTENCIA. El worker reintenta cuando la red falla. El mismo evento
     puede llegar dos veces y no debe generar dos alertas.
  2. NO PERDER NADA. Si un evento del lote viene mal formado, se rechaza ese y
     los demas siguen. Un lote no se pierde entero por una manzana podrida.
  3. NO FRENAR AL RESTO. La base de datos y el cruce contra la lista negra
     corren en el pool de hilos, no en el bucle de eventos: si corrieran ahi,
     cada lote congelaria el video en vivo y el WebSocket de todos los
     dashboards mientras dura el commit.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError
from sqlmodel import Session, col, select

from api.alertas import mensaje as mensaje_alerta
from api.alertas import nueva_alerta
from api.config import BASE_DIR, get_config
from api.database import engine
from api.deps import verificar_worker
from api.hub import hub
from api.matching import evaluar
from api.models import Alert, Camera, Event, FaceEmbedding
from shared.events import (
    PATRON_CAMARA,
    DetectionEvent,
    EventoRechazado,
    IngestResponse,
    MatchResult,
    Severity,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["eventos"],
                   dependencies=[Depends(verificar_worker)])

# Una captura HD de 3200x1800 ronda 1 MB; 6 MB deja margen sin permitir que un
# cliente mal configurado llene el disco.
MAX_BYTES_FOTO = 6 * 1024 * 1024
_ID_SEGURO = re.compile(r"[A-Za-z0-9\-]{8,64}")


def _ruta_captura(evento: DetectionEvent) -> Optional[str]:
    """Ruta de la captura DENTRO de la carpeta de evidencia de la API.

    Si el worker adjunto la foto (corre en otra maquina), se escribe aqui. Si
    no, se asume disco compartido y solo se conserva el nombre del archivo: la
    ruta que manda el worker nunca se usa tal cual, para que un valor raro no
    apunte fuera de data/snapshots.
    """
    cfg = get_config()
    carpeta = cfg.snapshot_dir

    if evento.snapshot_b64:
        try:
            datos = base64.b64decode(evento.snapshot_b64, validate=True)
        except (binascii.Error, ValueError):
            datos = b""
        if datos.startswith(b"\xff\xd8") and len(datos) <= MAX_BYTES_FOTO:
            if _ID_SEGURO.fullmatch(evento.event_id):
                nombre = f"{evento.event_id}.jpg"
            else:
                nombre = f"evento-{hashlib.sha1(evento.event_id.encode()).hexdigest()[:20]}.jpg"
            destino = carpeta / nombre
            if not destino.exists():
                destino.write_bytes(datos)
            return destino.relative_to(BASE_DIR).as_posix()
        log.warning("Captura adjunta invalida en el evento %s; se ignora", evento.event_id)

    if not evento.snapshot_path:
        return None
    nombre = Path(evento.snapshot_path).name
    return (carpeta / nombre).relative_to(BASE_DIR).as_posix() if nombre else None


def _guardar(evento: DetectionEvent, resultado: MatchResult,
             session: Session) -> tuple[Event, Alert | None]:
    captura = _ruta_captura(evento)
    fila = Event(
        event_id=evento.event_id,
        dedupe_key=evento.dedupe_key(),
        camera_id=evento.camera_id,
        ts=evento.ts,
        type=evento.type.value,
        track_id=evento.track_id,
        value=evento.value,
        confidence=evento.confidence,
        bbox_x1=evento.bbox.x1 if evento.bbox else None,
        bbox_y1=evento.bbox.y1 if evento.bbox else None,
        bbox_x2=evento.bbox.x2 if evento.bbox else None,
        bbox_y2=evento.bbox.y2 if evento.bbox else None,
        observations=evento.observations,
        snapshot_path=captura,
        severity=resultado.severity.value,
        match_kind=resultado.match_kind.value,
        matched_blacklist_id=resultado.blacklist_id,
        match_score=resultado.score,
        meta_json=json.dumps(evento.meta, ensure_ascii=False, default=str) if evento.meta else None,
    )
    session.add(fila)
    # El evento se escribe ANTES que su alerta y su embedding, que lo
    # referencian por llave foranea. Sin relaciones declaradas, SQLAlchemy no
    # garantiza ese orden dentro de un mismo flush: SQLite no revisa las llaves
    # foraneas y no se notaba, PostgreSQL si y rechazaba la ingesta.
    session.flush()

    # MINIMIZACION DE DATOS BIOMETRICOS
    #
    # El embedding facial solo se guarda si la persona COINCIDIO con la lista
    # negra. Si no coincidio, ya cumplio su unica funcion -- ser comparado -- y
    # conservarlo no aporta nada: no se puede consultar por rostro despues, y
    # nadie va a preguntar "quien mas paso por aqui".
    #
    # Guardarlo convertiria el sistema en una base de datos biometrica de todo
    # el que pase frente a la camara. Bajo la LFPDPPP eso es tratamiento de
    # datos sensibles sin finalidad que lo justifique, y ademas crea un activo
    # que hay que proteger. Se descarta en memoria y no toca el disco.
    if evento.embedding and resultado.severity != Severity.INFO:
        session.add(FaceEmbedding(
            event_id=evento.event_id,
            vector=np.asarray(evento.embedding, dtype=np.float32).tobytes(),
            dim=len(evento.embedding),
        ))

    alerta = nueva_alerta(evento, resultado, captura)
    if alerta is not None:
        session.add(alerta)

    return fila, alerta


# Lo que el dashboard muestra de un evento en vivo. El resto de `meta`
# (lecturas de OCR, velocidades) se queda en la base de datos.
_META_VISIBLE = ("color_vehiculo", "tipo_placa", "entidad", "pais", "zona", "direccion",
                 "lectura_original")


def _meta_para_dashboard(meta: Optional[dict]) -> dict:
    return {k: meta[k] for k in _META_VISIBLE if meta and meta.get(k) is not None}


def _datos_evento(evento: DetectionEvent, resultado: MatchResult, fila: Event) -> dict:
    """Lo que se difunde por WebSocket de un evento recien guardado."""
    return {
        "event_id": evento.event_id,
        "camera_id": evento.camera_id,
        "ts": evento.ts,
        "type": evento.type.value,
        "value": evento.value,
        "confidence": evento.confidence,
        "severity": resultado.severity.value,
        "snapshot_path": fila.snapshot_path,
        "observations": evento.observations,
        "meta": _meta_para_dashboard(evento.meta),
    }


def registrar_evento_sistema(evento: DetectionEvent) -> tuple[dict, Optional[dict]]:
    """Guarda un evento que genera la propia API (p.ej. "camara sin senal").

    Pasa por el mismo cruce y el mismo guardado que la ingesta, pero SIN
    contar como latido: un aviso de camara caida no puede revivir la camara.
    Devuelve (mensaje del evento, mensaje de la alerta o None) para difundir.
    """
    with Session(engine) as session:
        resultado = evaluar(evento, session)
        fila, alerta = _guardar(evento, resultado, session)
        datos = _datos_evento(evento, resultado, fila)
        session.commit()
        datos_alerta = None
        if alerta is not None:
            session.refresh(alerta)
            datos_alerta = mensaje_alerta(alerta, evento.ts)
    return datos, datos_alerta


def _latido(session: Session, camera_id: str, estado: Optional[dict] = None) -> None:
    camara = session.exec(select(Camera).where(Camera.camera_id == camera_id)).first()
    if camara is None:
        camara = Camera(camera_id=camera_id, name=camera_id)
        session.add(camara)
    camara.last_heartbeat = datetime.now(timezone.utc)
    if estado is not None:
        camara.status_json = json.dumps(estado, ensure_ascii=False, default=str)


class LoteEntrante(BaseModel):
    """El lote tal como llega. Los eventos se validan UNO POR UNO despues (ver
    `ingerir`): si el lote entero se validara de golpe, un solo evento mal
    formado hacia rechazar los veinte con 422 y el worker los apartaba todos."""

    camera_id: str = Field(pattern=PATRON_CAMARA)
    sent_at: Optional[datetime] = None
    events: list[dict[str, Any]] = Field(max_length=500)


def _validar_eventos(crudos: list[dict]) -> tuple[list[DetectionEvent], list[EventoRechazado]]:
    validos, rechazados = [], []
    for crudo in crudos:
        try:
            validos.append(DetectionEvent.model_validate(crudo))
        except ValidationError as e:
            errores = "; ".join(
                f"{'.'.join(str(x) for x in err.get('loc', ()))}: {err.get('msg')}"
                for err in e.errors()[:3]
            )
            evento_id = crudo.get("event_id") if isinstance(crudo, dict) else None
            rechazados.append(EventoRechazado(
                event_id=str(evento_id)[:64] if evento_id else None, motivo=errores[:300]))
    if rechazados:
        log.warning("Ingesta: %d evento(s) rechazados por formato: %s", len(rechazados),
                    rechazados[0].motivo)
    return validos, rechazados


def _procesar_lote(camera_id: str, eventos: list[DetectionEvent]
                   ) -> tuple[IngestResponse, list[tuple[dict, Optional[dict]]]]:
    """Todo el trabajo sincrono de la ingesta, en un hilo del pool."""
    aceptados = 0
    duplicados = 0
    resultados: list[MatchResult] = []
    guardados: list[tuple[dict, Alert | None]] = []

    with Session(engine) as session:
        # La camara tiene que existir ANTES que sus eventos (llave foranea).
        # Un lote tambien cuenta como senal de vida de la camara.
        for cid in sorted({camera_id, *(e.camera_id for e in eventos)}):
            _latido(session, cid)
        session.flush()

        # Idempotencia en UNA consulta para todo el lote, no una por evento.
        ids = [e.event_id for e in eventos]
        existentes = set(session.exec(
            select(Event.event_id).where(col(Event.event_id).in_(ids))
        ).all()) if ids else set()

        vistos: set[str] = set()
        for evento in eventos:
            if evento.event_id in existentes or evento.event_id in vistos:
                duplicados += 1
                continue
            vistos.add(evento.event_id)

            resultado = evaluar(evento, session)
            fila, alerta = _guardar(evento, resultado, session)
            resultados.append(resultado)
            aceptados += 1
            guardados.append((_datos_evento(evento, resultado, fila), alerta))

        session.commit()

        # El `id` de la alerta lo asigna la base de datos en el commit, y es lo
        # que el dashboard necesita para mostrar los botones de "Atendida" y
        # "Falso positivo". Por eso el mensaje se arma aqui, ya confirmado, y
        # no antes: difundir antes del commit mostraria alertas que quiza no
        # existan en la base de datos.
        a_difundir = []
        for datos_evento, alerta in guardados:
            datos_alerta = None
            if alerta is not None:
                session.refresh(alerta)
                datos_alerta = mensaje_alerta(alerta, datos_evento["ts"])
            a_difundir.append((datos_evento, datos_alerta))

    respuesta = IngestResponse(accepted=aceptados, duplicates=duplicados, matches=resultados)
    return respuesta, a_difundir


@router.post("", response_model=IngestResponse)
async def ingerir(lote: LoteEntrante) -> IngestResponse:
    eventos, rechazados = _validar_eventos(lote.events)
    respuesta, a_difundir = await run_in_threadpool(_procesar_lote, lote.camera_id, eventos)
    respuesta.rejected = rechazados

    for datos_evento, datos_alerta in a_difundir:
        await hub.difundir("event", datos_evento)
        if datos_alerta:
            await hub.difundir("alert", datos_alerta)
            log.warning("ALERTA %s: %s", datos_alerta["severity"].upper(), datos_alerta["title"])

    return respuesta


@router.post("/heartbeat")
async def heartbeat(camera_id: str = Query(pattern=PATRON_CAMARA),
                    status: dict = Body(default_factory=dict)) -> dict:
    """El worker reporta salud aunque no haya detecciones. Sin esto, una camara
    apagada es indistinguible de una camara sin trafico."""

    def _registrar() -> None:
        with Session(engine) as session:
            _latido(session, camera_id, status)
            session.commit()

    await run_in_threadpool(_registrar)
    await hub.difundir("camera_status", {"camera_id": camera_id, "status": status})
    return {"ok": True}


# Un clip de 20 s a 960 px ronda 1-5 MB. 80 MB deja margen para camaras de
# alta resolucion sin permitir que un cliente mal configurado llene el disco.
MAX_BYTES_CLIP = 80 * 1024 * 1024


def _tipo_de_video(datos: bytes) -> Optional[str]:
    """Extension segun el contenido real, no segun lo que diga la cabecera."""
    if len(datos) > 12 and datos[4:8] == b"ftyp":
        return "mp4"
    if datos[:4] == b"\x1a\x45\xdf\xa3":      # EBML: Matroska / WebM
        return "webm"
    return None


@router.post("/{event_id}/clip")
async def subir_clip(event_id: str, request: Request) -> dict:
    """El worker sube el clip de video de un evento que fue alerta.

    Se liga a TODAS las alertas de ese evento y se avisa a los dashboards,
    que muestran el boton "Ver clip" sin recargar.
    """
    if not _ID_SEGURO.fullmatch(event_id):
        raise HTTPException(422, "event_id invalido")
    try:
        declarado = int(request.headers.get("content-length") or 0)
    except ValueError:
        declarado = 0
    if declarado > MAX_BYTES_CLIP:
        raise HTTPException(413, "Clip demasiado grande")
    # Se lee por partes y se corta al pasar el limite: sin Content-Length
    # (envio por trozos) no se sabe el tamano hasta leerlo.
    partes, total = [], 0
    async for parte in request.stream():
        total += len(parte)
        if total > MAX_BYTES_CLIP:
            raise HTTPException(413, "Clip demasiado grande")
        partes.append(parte)
    datos = b"".join(partes)
    extension = _tipo_de_video(datos)
    if extension is None:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "El clip debe ser MP4 o WebM")

    def _guardar_clip() -> list[dict]:
        cfg = get_config()
        with Session(engine) as session:
            evento = session.exec(select(Event).where(Event.event_id == event_id)).first()
            if evento is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese evento")
            destino = cfg.clips_dir / f"{event_id}.{extension}"
            destino.write_bytes(datos)
            ruta = destino.relative_to(BASE_DIR).as_posix()
            alertas = session.exec(select(Alert).where(Alert.event_id == event_id)).all()
            for alerta in alertas:
                alerta.clip_path = ruta
            session.commit()
            ts = evento.ts.isoformat() if evento.ts else None
            mensajes = []
            for alerta in alertas:
                session.refresh(alerta)
                mensajes.append(mensaje_alerta(alerta, ts))
            return mensajes

    mensajes = await run_in_threadpool(_guardar_clip)
    for m in mensajes:
        await hub.difundir("alert_updated", m)
    return {"ok": True, "alertas": len(mensajes)}
