"""Consulta de eventos, gestion de alertas y estadisticas del dashboard."""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi import Path as PathParam
from pydantic import BaseModel, Field
from sqlmodel import col, func, select

from api.auditoria import registrar
from api.deps import Admin, Operador, OperadorActual, SesionBD
from api.exportacion import celda
from api.hub import hub
from api.models import Alert, Camera, Event, FechasEnUtc
from shared.events import PATRON_CAMARA
from shared.fechas import a_utc
from shared.plates import limpiar

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["alertas"])


def _limite_utc(fecha: datetime) -> datetime:
    """Limite de busqueda en UTC y CON zona.

    Una fecha con zona que llega del navegador se lleva a UTC antes de
    comparar; sin esto, "desde las 00:00" en Mexico filtraba seis horas
    corrido. La zona NO se quita: SQLModel 0.0.45+ rechaza comparar una
    columna de fecha contra un valor sin zona, y la busqueda respondia 500
    en cualquier instalacion nueva (ver shared/fechas.py).
    """
    return a_utc(fecha)


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
    acknowledged_at: Optional[datetime] = None
    dismissed_reason: Optional[str] = None
    notes: Optional[str] = None
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
    meta: dict = Field(default_factory=dict)


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
    motivo: Optional[str] = Field(default=None, max_length=200)
    nota: Optional[str] = Field(default=None, max_length=1000,
                                description="Que se hizo: se aviso a la patrulla, se verifico...")


@router.post("/alerts/{alerta_id}/resolver", response_model=AlertaLeida)
async def resolver(alerta_id: int, datos: Resolucion, session: SesionBD,
                   operador: Operador, request: Request):
    """Cerrar una alerta.

    `dismiss` con motivo 'falso positivo' es la senal mas valiosa del sistema:
    es lo que permite medir la tasa de error real y saber que reentrenar.
    """
    alerta = session.get(Alert, alerta_id)
    if alerta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa alerta")
    if datos.accion not in {"acknowledge", "dismiss"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "acción debe ser 'acknowledge' o 'dismiss'")

    if alerta.status != "new":
        # Dos operadores atendiendo la misma alerta a la vez: gana el primero y
        # el segundo se entera, en vez de sobrescribir la nota del otro.
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"La alerta ya fue cerrada por {alerta.acknowledged_by or 'otro operador'}")

    alerta.status = "acknowledged" if datos.accion == "acknowledge" else "dismissed"
    alerta.acknowledged_by = operador.username
    alerta.acknowledged_at = datetime.now(timezone.utc)
    alerta.dismissed_reason = datos.motivo
    alerta.notes = (datos.nota or "").strip() or None
    registrar(session, "alertas.atendida" if datos.accion == "acknowledge" else "alertas.descartada",
              usuario=operador.username, objetivo=f"ALR-{alerta.id:06d}",
              detalle={"motivo": datos.motivo, "nota": alerta.notes}, request=request)
    session.commit()
    session.refresh(alerta)

    await hub.difundir("alert_resolved", {"id": alerta.id, "status": alerta.status,
                                          "by": operador.username, "notes": alerta.notes})
    return alerta


def _consulta_eventos(tipo, camera_id, q, severidad, desde, hasta):
    consulta = select(Event)
    if tipo:
        consulta = consulta.where(Event.type == tipo)
    if camera_id:
        consulta = consulta.where(Event.camera_id == camera_id)
    if severidad:
        consulta = consulta.where(Event.severity == severidad)
    if desde:
        consulta = consulta.where(col(Event.ts) >= _limite_utc(desde))
    if hasta:
        consulta = consulta.where(col(Event.ts) <= _limite_utc(hasta))
    if q:
        buscado = limpiar(q)
        if buscado:
            sin_guiones = func.upper(func.replace(func.replace(Event.value, "-", ""), " ", ""))
            consulta = consulta.where(sin_guiones.contains(buscado))
    return consulta.order_by(col(Event.ts).desc())


@router.get("/events", response_model=list[EventoLeido])
def listar_eventos(
    session: SesionBD,
    _: OperadorActual,
    tipo: Optional[str] = Query(None, description="plate | face | weapon | anomaly"),
    camera_id: Optional[str] = None,
    q: Optional[str] = Query(None, max_length=20,
                             description="Texto a buscar en el valor, ej. una placa"),
    severidad: Optional[str] = Query(None, description="info | warning | critical"),
    desde: Optional[datetime] = None,
    hasta: Optional[datetime] = None,
    limite: int = Query(100, ge=1, le=1000),
):
    """Historico de eventos, del mas reciente al mas antiguo.

    `q` busca sin importar guiones ni espacios: "abc123", "ABC 123" y
    "ABC-123" encuentran lo mismo, porque asi es como el operador la va a
    escribir y asi es como el OCR la pudo haber leido.
    """
    consulta = _consulta_eventos(tipo, camera_id, q, severidad, desde, hasta)
    return session.exec(consulta.limit(limite)).all()


@router.get("/events/export.csv")
def exportar_eventos(
    session: SesionBD,
    operador: Operador,
    request: Request,
    tipo: Optional[str] = None,
    camera_id: Optional[str] = None,
    q: Optional[str] = Query(None, max_length=20),
    severidad: Optional[str] = None,
    desde: Optional[datetime] = None,
    hasta: Optional[datetime] = None,
):
    """Los mismos filtros de la busqueda, como CSV para un reporte.

    Es lo que se entrega cuando alguien pide "todas las veces que paso esta
    placa" (un parte, una denuncia). Las fechas salen en hora local del
    servidor, que es como las lee quien recibe el reporte. Hasta 10 000 filas.
    """
    filas = session.exec(_consulta_eventos(tipo, camera_id, q, severidad, desde, hasta)
                         .limit(10_000)).all()
    salida = io.StringIO()
    escritor = csv.writer(salida)
    escritor.writerow(["fecha_hora_local", "camara", "tipo", "valor", "color_vehiculo",
                       "confianza", "estado", "coincidencia", "frames", "evidencia"])
    for e in filas:
        ts = e.ts if e.ts.tzinfo else e.ts.replace(tzinfo=timezone.utc)
        escritor.writerow([f"{ts.astimezone():%Y-%m-%d %H:%M:%S}", celda(e.camera_id), e.type,
                           celda(e.value), celda(e.meta.get("color_vehiculo")),
                           f"{e.confidence:.2f}", e.severity, e.match_kind, e.observations,
                           Path(e.snapshot_path).name if e.snapshot_path else ""])
    log.info("Reporte CSV de %d eventos exportado por %s", len(filas), operador.username)
    registrar(session, "reportes.csv", usuario=operador.username,
              detalle={"filas": len(filas), "tipo": tipo, "camara": camera_id, "texto": q,
                       "severidad": severidad, "desde": desde, "hasta": hasta},
              request=request, confirmar=True)
    nombre = f"eventos-{datetime.now():%Y%m%d-%H%M}.csv"
    # Con BOM para que Excel abra bien los acentos.
    return Response("\ufeff" + salida.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


class CamaraEdicion(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    location: Optional[str] = Field(default=None, max_length=160)
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)


@router.put("/cameras/{camera_id}")
def editar_camara(camera_id: Annotated[str, PathParam(pattern=PATRON_CAMARA)], datos: CamaraEdicion, session: SesionBD, admin: Admin,
                  request: Request) -> dict:
    """Nombre y ubicacion que ve el operador ("Acceso norte - Av. Juarez").

    Un identificador como "cam-02" no le dice al monitorista a donde mandar
    la patrulla; la ubicacion si.
    """
    camara = session.exec(select(Camera).where(Camera.camera_id == camera_id)).first()
    if camara is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa cámara")
    camara.name = datos.name.strip()
    camara.location = (datos.location or "").strip() or None
    if (datos.lat is None) != (datos.lon is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Latitud y longitud van juntas (o ninguna de las dos)")
    camara.lat, camara.lon = datos.lat, datos.lon
    registrar(session, "camaras.edicion", usuario=admin.username, objetivo=camera_id,
              detalle={"nombre": camara.name, "ubicacion": camara.location,
                       "lat": camara.lat, "lon": camara.lon}, request=request)
    session.commit()
    log.info("Camara %s renombrada a '%s' por %s", camera_id, camara.name, admin.username)
    return {"camera_id": camara.camera_id, "name": camara.name, "location": camara.location,
            "lat": camara.lat, "lon": camara.lon}


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
        try:
            salud = json.loads(c.status_json) if c.status_json else {}
        except ValueError:
            salud = {}
        camaras.append({
            "camera_id": c.camera_id,
            "name": c.name,
            "location": c.location,
            "lat": c.lat,
            "lon": c.lon,
            # El worker late cada 15 s; sin senal en 60 s se considera caida.
            # Que la fuente reporte connected=False tambien cuenta: el worker
            # sigue vivo pero la camara no entrega imagen.
            "online": (segundos is not None and segundos < 60
                       and salud.get("connected", True) is not False),
            "segundos_sin_senal": round(segundos) if segundos is not None else None,
            "fps": salud.get("fps_procesados"),
            "reconexiones": salud.get("reconnects"),
        })

    return {
        "total_eventos": total_eventos,
        "alertas_nuevas": alertas_nuevas,
        "alertas_criticas": criticas,
        "eventos_por_tipo": por_tipo,
        "camaras": camaras,
        "dashboards_conectados": hub.conectados,
    }
