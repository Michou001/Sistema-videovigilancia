"""Consulta de eventos, gestion de alertas y estadisticas del dashboard."""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi import Path as PathParam
from pydantic import BaseModel, Field, computed_field
from sqlmodel import col, func, select

from api.auditoria import registrar
from api.deps import Admin, Operador, OperadorActual, SesionBD
from api.exportacion import celda
from api.ficha_evidencia import DESTINOS, RESULTADOS, armar_ficha, leer_canalizaciones
from api.hub import hub
from api.models import Alert, AuditLog, Camera, Event, FechasEnUtc
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
    clip_path: Optional[str] = None
    status: str
    acknowledged_by: Optional[str]
    acknowledged_at: Optional[datetime] = None
    dismissed_reason: Optional[str] = None
    resultado: Optional[str] = None
    notes: Optional[str] = None
    canalizaciones_json: Optional[str] = Field(default=None, exclude=True)
    created_at: datetime

    @computed_field
    @property
    def canalizaciones(self) -> list[dict]:
        return leer_canalizaciones(self.canalizaciones_json)


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


Resultado = Literal["confirmado", "falso_aviso", "indeterminado", "duplicado", "ensayo"]


class Resolucion(BaseModel):
    accion: str  # acknowledge | dismiss
    motivo: Optional[str] = Field(default=None, max_length=200)
    nota: Optional[str] = Field(default=None, max_length=1000,
                                description="Que se hizo: se aviso a la patrulla, se verifico...")
    resultado: Optional[Resultado] = Field(
        default=None, description="Que se encontro al revisar. Al descartar, falso_aviso por defecto.")


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
        raise HTTPException(422,
                            "acción debe ser 'acknowledge' o 'dismiss'")

    if alerta.status != "new":
        # Dos operadores atendiendo la misma alerta a la vez: gana el primero y
        # el segundo se entera, en vez de sobrescribir la nota del otro.
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"La alerta ya fue cerrada por {alerta.acknowledged_by or 'otro operador'}")

    resultado = datos.resultado
    if datos.accion == "dismiss":
        # Descartar es decir "no era": un descarte "confirmado" no tiene sentido.
        resultado = resultado or "falso_aviso"
        if resultado == "confirmado":
            raise HTTPException(422, "Una alerta confirmada se atiende, no se descarta")
    elif resultado == "falso_aviso":
        raise HTTPException(422, "Un falso aviso se descarta con 'Falso positivo'")

    alerta.status = "acknowledged" if datos.accion == "acknowledge" else "dismissed"
    alerta.acknowledged_by = operador.username
    alerta.acknowledged_at = datetime.now(timezone.utc)
    alerta.dismissed_reason = datos.motivo
    alerta.resultado = resultado
    alerta.notes = (datos.nota or "").strip() or None
    registrar(session, "alertas.atendida" if datos.accion == "acknowledge" else "alertas.descartada",
              usuario=operador.username, objetivo=f"ALR-{alerta.id:06d}",
              detalle={"motivo": datos.motivo, "resultado": resultado, "nota": alerta.notes},
              request=request)
    session.commit()
    session.refresh(alerta)

    await hub.difundir("alert_resolved", {"id": alerta.id, "status": alerta.status,
                                          "by": operador.username, "notes": alerta.notes})
    return alerta


def _percentil(valores: list[float], p: float) -> Optional[float]:
    """Percentil por rango mas cercano; None sin datos."""
    if not valores:
        return None
    orden = sorted(valores)
    k = max(0, min(len(orden) - 1, -(-len(orden) * p // 100) - 1))
    return round(orden[int(k)], 1)


@router.get("/alerts/metricas")
def metricas_alertas(
    session: SesionBD,
    _: OperadorActual,
    desde: Optional[datetime] = None,
    hasta: Optional[datetime] = None,
    incluir_ensayos: bool = Query(False, description="Contar tambien las marcadas como ensayo"),
):
    """Lo que se puede afirmar con las alertas revisadas, con sus denominadores.

    La precision es confirmadas / (confirmadas + falsos avisos): sin verdad de
    referencia no se puede saber cuantos hechos NO detecto el sistema, asi que
    no se reporta sensibilidad. El tiempo es de la alerta a su cierre en el
    dashboard (revision humana), no la llegada de apoyo. Los ensayos se
    excluyen por defecto para no mezclar la demo con la operacion.
    """
    consulta = select(Alert)
    if desde:
        consulta = consulta.where(col(Alert.created_at) >= _limite_utc(desde))
    if hasta:
        consulta = consulta.where(col(Alert.created_at) <= _limite_utc(hasta))
    alertas = session.exec(consulta).all()

    por_resultado = {k: 0 for k in RESULTADOS}
    sin_revisar = sin_resultado = 0
    tiempos: list[float] = []
    por_tipo: dict[str, dict[str, int]] = {}
    for a in alertas:
        if a.resultado == "ensayo" and not incluir_ensayos:
            por_resultado["ensayo"] += 1
            continue
        if a.status == "new":
            sin_revisar += 1
            continue
        if a.resultado in por_resultado:
            por_resultado[a.resultado] += 1
        else:
            sin_resultado += 1
        fila = por_tipo.setdefault(a.type, {"confirmado": 0, "falso_aviso": 0})
        if a.resultado in fila:
            fila[a.resultado] += 1
        if a.acknowledged_at and a.created_at:
            ini = a.created_at if a.created_at.tzinfo else a.created_at.replace(tzinfo=timezone.utc)
            fin = a.acknowledged_at if a.acknowledged_at.tzinfo else a.acknowledged_at.replace(tzinfo=timezone.utc)
            tiempos.append(max(0.0, (fin - ini).total_seconds()))

    conf, falsos = por_resultado["confirmado"], por_resultado["falso_aviso"]
    return {
        "total_alertas": len(alertas),
        "ensayos_excluidos": 0 if incluir_ensayos else por_resultado["ensayo"],
        "sin_revisar": sin_revisar,
        "revisadas_sin_resultado": sin_resultado,
        "por_resultado": por_resultado,
        "precision": round(conf / (conf + falsos), 3) if conf + falsos else None,
        "denominador_precision": conf + falsos,
        "por_tipo": por_tipo,
        "segundos_hasta_revision": {
            "n": len(tiempos), "mediana": _percentil(tiempos, 50), "p95": _percentil(tiempos, 95),
        },
    }


class Canalizacion(BaseModel):
    destino: Literal["proteccion_universitaria", "911_c5", "c4_municipal", "fiscalia", "otro"]
    referencia: Optional[str] = Field(default=None, max_length=80,
                                      description="Folio del 911, numero de reporte o de denuncia")
    nota: Optional[str] = Field(default=None, max_length=500)


MAX_CANALIZACIONES = 10


@router.post("/alerts/{alerta_id}/canalizar", response_model=AlertaLeida)
async def canalizar(alerta_id: int, datos: Canalizacion, session: SesionBD,
                    operador: Operador, request: Request):
    """Dejar constancia de a quien se paso el caso.

    GOSS IP no llama a la policia por su cuenta: el monitorista decide y
    canaliza (Proteccion Universitaria, 911/C5, C4 municipal, Fiscalia). Aqui
    queda quien lo hizo, cuando y con que folio externo, para poder seguir el
    caso. Se puede canalizar a mas de una instancia y aun con la alerta
    cerrada: el reporte al 911 a veces se hace despues de atenderla.
    """
    alerta = session.get(Alert, alerta_id)
    if alerta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa alerta")
    previas = leer_canalizaciones(alerta.canalizaciones_json)
    if len(previas) >= MAX_CANALIZACIONES:
        raise HTTPException(422, f"Máximo {MAX_CANALIZACIONES} canalizaciones por alerta")
    nueva = {
        "destino": datos.destino,
        "referencia": (datos.referencia or "").strip() or None,
        "nota": (datos.nota or "").strip() or None,
        "por": operador.username,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    alerta.canalizaciones_json = json.dumps(previas + [nueva], ensure_ascii=False)
    registrar(session, "alertas.canalizada", usuario=operador.username,
              objetivo=f"ALR-{alerta.id:06d}",
              detalle={"destino": DESTINOS[datos.destino], "referencia": nueva["referencia"],
                       "nota": nueva["nota"]}, request=request)
    session.commit()
    session.refresh(alerta)
    log.info("Alerta ALR-%06d canalizada a %s por %s", alerta.id, datos.destino, operador.username)

    await hub.difundir("alert_canalizada", {"id": alerta.id,
                                            "canalizaciones": leer_canalizaciones(alerta.canalizaciones_json)})
    return alerta


@router.get("/alerts/{alerta_id}/ficha.zip")
def ficha_evidencia(alerta_id: int, session: SesionBD, operador: Operador, request: Request):
    """Paquete para entregar a una autoridad: ficha imprimible, foto, clip y
    la huella SHA-256 de cada archivo.

    La huella permite demostrar despues que lo entregado no se altero: queda
    tambien en la bitacora, con quien descargo la ficha y cuando.
    """
    alerta = session.get(Alert, alerta_id)
    if alerta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa alerta")
    folio = f"ALR-{alerta.id:06d}"
    evento = session.exec(select(Event).where(Event.event_id == alerta.event_id)).first()
    camara = session.exec(select(Camera).where(Camera.camera_id == alerta.camera_id)).first()
    bitacora = session.exec(
        select(AuditLog).where(AuditLog.objetivo == folio).order_by(col(AuditLog.ts))
    ).all()
    contenido, huellas = armar_ficha(alerta, evento, camara, bitacora, generado_por=operador.username)

    registrar(session, "alertas.ficha_evidencia", usuario=operador.username, objetivo=folio,
              detalle={"sha256": huellas}, request=request, confirmar=True)
    log.info("Ficha de evidencia %s descargada por %s", folio, operador.username)
    return Response(contenido, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{folio}-evidencia.zip"'})


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
    return consulta.order_by(col(Event.ts).desc(), col(Event.id).desc())


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
    con_foto: bool = Query(False, description="Solo eventos con captura (galeria)"),
    desde_n: int = Query(0, ge=0, le=100_000, description="Saltar los primeros N (paginar)"),
):
    """Historico de eventos, del mas reciente al mas antiguo.

    `q` busca sin importar guiones ni espacios: "abc123", "ABC 123" y
    "ABC-123" encuentran lo mismo, porque asi es como el operador la va a
    escribir y asi es como el OCR la pudo haber leido.
    """
    consulta = _consulta_eventos(tipo, camera_id, q, severidad, desde, hasta)
    if con_foto:
        consulta = consulta.where(col(Event.snapshot_path).is_not(None))
    return session.exec(consulta.offset(desde_n).limit(limite)).all()


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
    funcion: Optional[Literal['lpr', 'peatonal', 'pasillo', 'zona', 'patio', 'estacionamiento']] = None
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
    if 'funcion' in datos.model_fields_set:
        camara.funcion = datos.funcion
    camara.location = (datos.location or "").strip() or None
    if (datos.lat is None) != (datos.lon is None):
        raise HTTPException(422,
                            "Latitud y longitud van juntas (o ninguna de las dos)")
    camara.lat, camara.lon = datos.lat, datos.lon
    registrar(session, "camaras.edicion", usuario=admin.username, objetivo=camera_id,
              detalle={"nombre": camara.name, "ubicacion": camara.location,
                       "lat": camara.lat, "lon": camara.lon, "funcion": camara.funcion}, request=request)
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
            "funcion": c.funcion,
            "enabled": c.enabled,
            "connected": salud.get("connected"),
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
        "dashboards_conectados": hub.conectados_total,
    }
