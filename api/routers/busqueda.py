"""Busqueda de eventos por descripcion en lenguaje natural (ver api/semantica.py)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlmodel import col, select

from api import semantica
from api.auditoria import registrar
from api.deps import OperadorActual, SesionBD
from api.models import Event, SemanticEmbedding
from api.routers.alerts import EventoLeido
from shared.events import PATRON_CAMARA
from shared.fechas import a_utc

router = APIRouter(prefix="/api/busqueda", tags=["busqueda"])


class EventoEncontrado(EventoLeido):
    similitud: float


class RespuestaBusqueda(BaseModel):
    consulta: str
    resultados: list[EventoEncontrado]


@router.get("/estado")
def estado(_: OperadorActual) -> dict:
    idx = semantica.indice
    if idx is None:
        return {"estado": "desactivado"}
    return {**idx.resumen(), "pendientes": idx.pendientes() if idx.listo else None}


@router.get("", response_model=RespuestaBusqueda)
async def buscar(
    request: Request,
    session: SesionBD,
    operador: OperadorActual,
    q: Annotated[str, Query(min_length=2, max_length=200, description="Ej. 'camioneta blanca'")],
    tipo: Optional[str] = None,
    camera_id: Annotated[Optional[str], Query(pattern=PATRON_CAMARA)] = None,
    desde: Optional[datetime] = None,
    hasta: Optional[datetime] = None,
    limite: Annotated[int, Query(ge=1, le=200)] = 30,
):
    idx = semantica.indice
    if idx is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "La búsqueda por descripción no está activada (SEMANTIC_SEARCH=true en la API)")
    if not idx.listo:
        detalle = ("El modelo de búsqueda se está cargando; intenta en un momento" if idx.estado == "cargando"
                   else f"El modelo de búsqueda no está disponible: {idx.error or idx.estado}")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detalle)

    frase = " ".join(q.split())
    candidatos = None
    if tipo or camera_id or desde or hasta:
        consulta = select(Event.event_id).join(SemanticEmbedding, SemanticEmbedding.event_id == Event.event_id)
        if tipo:
            consulta = consulta.where(Event.type == tipo)
        if camera_id:
            consulta = consulta.where(Event.camera_id == camera_id)
        if desde:
            consulta = consulta.where(col(Event.ts) >= a_utc(desde))
        if hasta:
            consulta = consulta.where(col(Event.ts) <= a_utc(hasta))
        candidatos = set(session.exec(consulta).all())

    try:
        encontrados = await run_in_threadpool(idx.buscar, frase, candidatos, limite)
    except RuntimeError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "El modelo de búsqueda no está listo") from None

    por_id = {e.event_id: e for e in session.exec(
        select(Event).where(col(Event.event_id).in_([r.event_id for r in encontrados]))).all()} \
        if encontrados else {}
    resultados = []
    for r in encontrados:
        ev = por_id.get(r.event_id)
        if ev is None:
            continue
        resultados.append(EventoEncontrado(
            event_id=ev.event_id, camera_id=ev.camera_id, ts=ev.ts, type=ev.type, value=ev.value,
            confidence=ev.confidence, severity=ev.severity, observations=ev.observations,
            snapshot_path=ev.snapshot_path, meta=ev.meta, similitud=r.similitud))

    # Buscar personas por como se ven es sensible: queda quien y que busco.
    registrar(session, "busqueda.semantica", usuario=operador.username, objetivo=frase[:200],
              detalle={"tipo": tipo, "camara": camera_id, "resultados": len(resultados)},
              request=request, confirmar=True)
    return RespuestaBusqueda(consulta=frase, resultados=resultados)
