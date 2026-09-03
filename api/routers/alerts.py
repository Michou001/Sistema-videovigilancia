"""Consulta y gestion de alertas."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import col, func, select

from api.deps import OperadorActual, SesionBD
from api.hub import hub
from api.models import Alert, Camera, Event, FechasEnUtc

router = APIRouter(prefix="/api", tags=["alertas"])


class AlertaLeida(FechasEnUtc, BaseModel):
    id: int
    event_id: str
    camera_id: str
    type: str
    severity: str
    title: str
    detail: Optional[str]
    match_kind: str
    match_score: Optional[float]
    snapshot_path: Optional[str]
    status: str
    acknowledged_by: Optional[str]
    created_at: datetime


class EventoLeido(FechasEnUtc, BaseModel):
    event_id: str
    camera_id: str
    ts: datetime
    type: str
    value: str
    confidence: float
    severity: str
    observations: int
    snapshot_path: Optional[str]


@router.get("/alerts", response_model=list[AlertaLeida])
def listar_alertas(
    session: SesionBD,
    _: OperadorActual,
    estado: Optional[str] = Query(None, description="new | acknowledged | dismissed"),
    limite: int = Query(50, le=500),
):
    consulta = select(Alert)
    if estado:
        consulta = consulta.where(Alert.status == estado)
    return session.exec(
        consulta.order_by(col(Alert.created_at).desc()).limit(limite)
    ).all()


class Resolucion(BaseModel):
    accion: str  # acknowledge | dismiss
    motivo: Optional[str] = None


@router.post("/alerts/{alerta_id}/resolver", response_model=AlertaLeida)
async def resolver(alerta_id: int, datos: Resolucion, session: SesionBD,
                   operador: OperadorActual):
    """Cerrar una alerta.

    `dismiss` con motivo 'falso positivo' es la senal mas valiosa del sistema:
    es lo que permite medir la tasa de error real y saber que reentrenar.
    """
    alerta = session.get(Alert, alerta_id)
    if alerta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa alerta")
    if datos.accion not in {"acknowledge", "dismiss"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "accion debe ser 'acknowledge' o 'dismiss'")

    alerta.status = "acknowledged" if datos.accion == "acknowledge" else "dismissed"
    alerta.acknowledged_by = operador.username
    alerta.acknowledged_at = datetime.now(timezone.utc)
    alerta.dismissed_reason = datos.motivo
    session.commit()
    session.refresh(alerta)

    await hub.difundir("alert_resolved", {"id": alerta.id, "status": alerta.status,
                                          "by": operador.username})
    return alerta


@router.get("/events", response_model=list[EventoLeido])
def listar_eventos(
    session: SesionBD,
    _: OperadorActual,
    tipo: Optional[str] = None,
    camera_id: Optional[str] = None,
    limite: int = Query(100, le=1000),
):
    consulta = select(Event)
    if tipo:
        consulta = consulta.where(Event.type == tipo)
    if camera_id:
        consulta = consulta.where(Event.camera_id == camera_id)
    return session.exec(consulta.order_by(col(Event.ts).desc()).limit(limite)).all()


@router.get("/stats")
def estadisticas(session: SesionBD, _: OperadorActual):
    total_eventos = session.exec(select(func.count()).select_from(Event)).one()
    alertas_nuevas = session.exec(
        select(func.count()).select_from(Alert).where(Alert.status == "new")
    ).one()
    criticas = session.exec(
        select(func.count()).select_from(Alert)
        .where(Alert.status == "new", Alert.severity == "critical")
    ).one()

    por_tipo = dict(session.exec(
        select(Event.type, func.count()).select_from(Event).group_by(Event.type)
    ).all())

    camaras = []
    for c in session.exec(select(Camera)).all():
        segundos = None
        if c.last_heartbeat:
            visto = c.last_heartbeat
            if visto.tzinfo is None:
                visto = visto.replace(tzinfo=timezone.utc)
            segundos = (datetime.now(timezone.utc) - visto).total_seconds()
        camaras.append({
            "camera_id": c.camera_id,
            "name": c.name,
            # Sin senal en 60 s se considera caida: a 8 fps eso son ~480 frames
            # perdidos, mas que suficiente para saber que algo pasa.
            "online": segundos is not None and segundos < 60,
            "segundos_sin_senal": round(segundos) if segundos is not None else None,
        })

    return {
        "total_eventos": total_eventos,
        "alertas_nuevas": alertas_nuevas,
        "alertas_criticas": criticas,
        "eventos_por_tipo": por_tipo,
        "camaras": camaras,
        "dashboards_conectados": hub.conectados,
    }
