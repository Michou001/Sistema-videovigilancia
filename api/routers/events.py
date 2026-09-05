"""Ingesta de eventos del worker de borde.

Es el endpoint mas caliente del sistema: por aqui pasa todo lo que detectan las
camaras. Tres cosas importan mas que en cualquier otro sitio del proyecto:

  1. IDEMPOTENCIA. El worder reintenta cuando la red falla. El mismo evento
     puede llegar dos veces y no debe generar dos alertas.
  2. NO PERDER NADA. Si un evento del lote viene mal formado, se rechaza ese y
     los demas siguen. Un lote no se pierde entero por una manzana podrida.
  3. RAPIDEZ. El worker espera esta respuesta; si tarda, deja de capturar.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np
from fastapi import APIRouter, Depends
from sqlmodel import select

from api.deps import SesionBD, verificar_worker
from api.hub import hub
from api.matching import evaluar
from api.models import Alert, Camera, Event, FaceEmbedding
from shared.events import (
    DetectionEvent,
    EventBatch,
    EventType,
    IngestResponse,
    MatchKind,
    MatchResult,
    Severity,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["eventos"],
                   dependencies=[Depends(verificar_worker)])


TITULOS = {
    EventType.PLATE: "Placa {valor} en lista negra",
    EventType.FACE: "Persona identificada: {valor}",
    EventType.WEAPON: "ARMA DETECTADA: {valor}",
    EventType.ANOMALY: "Movimiento subito detectado",
}


def _titulo(evento: DetectionEvent, resultado: MatchResult) -> str:
    """Titulo de la alerta tal como lo vera el operador.

    Una coincidencia DIFUSA nunca debe titularse como si fuera un hecho. Decir
    "Placa ABC-123 en lista negra" cuando en realidad se leyo "ABD-123" lleva a
    actuar contra el vehiculo equivocado: el operador ve el titulo y reacciona,
    no siempre lee el detalle. El titulo tiene que cargar la incertidumbre.
    """
    if resultado.match_kind == MatchKind.FUZZY:
        return f"Posible placa {resultado.matched_value} (se leyo {evento.value})"
    etiqueta = resultado.matched_value or evento.value
    return TITULOS.get(evento.type, "Deteccion {valor}").format(valor=etiqueta)


def _guardar(evento: DetectionEvent, resultado: MatchResult, session) -> tuple[Event, Alert | None]:
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
        snapshot_path=evento.snapshot_path,
        severity=resultado.severity.value,
        match_kind=resultado.match_kind.value,
        matched_blacklist_id=resultado.blacklist_id,
        match_score=resultado.score,
        meta_json=None,
    )
    session.add(fila)

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

    alerta = None
    if resultado.severity != Severity.INFO:
        alerta = Alert(
            event_id=evento.event_id,
            camera_id=evento.camera_id,
            type=evento.type.value,
            severity=resultado.severity.value,
            title=_titulo(evento, resultado),
            detail=resultado.reason,
            match_kind=resultado.match_kind.value,
            match_score=resultado.score,
            snapshot_path=evento.snapshot_path,
        )
        session.add(alerta)

    return fila, alerta


@router.post("", response_model=IngestResponse)
async def ingerir(lote: EventBatch, session: SesionBD) -> IngestResponse:
    aceptados = 0
    duplicados = 0
    resultados: list[MatchResult] = []
    # La alerta se guarda como OBJETO, no como diccionario ya armado: su `id` lo
    # asigna la base de datos en el commit, que ocurre despues. Armando el
    # diccionario aqui, el `id` viajaba en None y el dashboard pintaba la alerta
    # sin los botones de "Atendida" y "Falso positivo" -- el operador tenia que
    # recargar la pagina para poder resolver una alerta que acababa de ver
    # entrar. Justo la que mas prisa corre.
    a_difundir: list[tuple[dict, Alert | None]] = []

    for evento in lote.events:
        # Idempotencia: el borde reintenta ante fallos de red.
        ya_existe = session.exec(
            select(Event).where(Event.event_id == evento.event_id)
        ).first()
        if ya_existe:
            duplicados += 1
            continue

        resultado = evaluar(evento, session)
        fila, alerta = _guardar(evento, resultado, session)
        resultados.append(resultado)
        aceptados += 1

        a_difundir.append((
            {
                "event_id": evento.event_id,
                "camera_id": evento.camera_id,
                "ts": evento.ts,
                "type": evento.type.value,
                "value": evento.value,
                "confidence": evento.confidence,
                "severity": resultado.severity.value,
                "snapshot_path": evento.snapshot_path,
                "observations": evento.observations,
            },
            alerta,
        ))

    # Heartbeat de la camara: saber que sigue viva sin consultar el video.
    camara = session.exec(
        select(Camera).where(Camera.camera_id == lote.camera_id)
    ).first()
    if camara is None:
        camara = Camera(camera_id=lote.camera_id, name=lote.camera_id)
        session.add(camara)
    camara.last_heartbeat = datetime.now(timezone.utc)

    session.commit()

    # La difusion va DESPUES del commit: si se difunde antes y el commit falla,
    # el dashboard muestra una alerta que no existe en la base de datos. Y solo
    # aqui la alerta ya tiene `id`, que es lo que el dashboard necesita para
    # poder resolverla.
    for datos_evento, alerta in a_difundir:
        await hub.difundir("event", datos_evento)
        if alerta:
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
                "ts": datos_evento["ts"],
            })
            log.warning("ALERTA %s: %s", alerta.severity.upper(), alerta.title)

    return IngestResponse(accepted=aceptados, duplicates=duplicados, matches=resultados)


@router.post("/heartbeat")
async def heartbeat(camera_id: str, status: dict, session: SesionBD) -> dict:
    """El worker reporta salud aunque no haya detecciones. Sin esto, una camara
    apagada es indistinguible de una camara sin trafico."""
    import json

    camara = session.exec(select(Camera).where(Camera.camera_id == camera_id)).first()
    if camara is None:
        camara = Camera(camera_id=camera_id, name=camera_id)
        session.add(camara)
    camara.last_heartbeat = datetime.now(timezone.utc)
    camara.status_json = json.dumps(status)
    session.commit()
    await hub.difundir("camera_status", {"camera_id": camera_id, "status": status})
    return {"ok": True}
