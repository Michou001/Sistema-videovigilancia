"""Placas: correccion de lecturas por el operador, analisis de un texto de
placa y exportacion del dataset para reentrenar el OCR."""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import select

from api.alertas import mensaje as mensaje_alerta
from api.alertas import nueva_alerta
from api.auditoria import registrar
from api.dataset import exportar
from api.deps import Admin, Operador, OperadorActual, SesionBD
from api.hub import hub
from api.matching import evaluar
from api.models import Alert, Event
from shared.events import DetectionEvent, EventType, Severity
from shared.fechas import a_utc
from shared.plates import analizar_placa, corregir_placa, formatear, limpiar

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["placas"])


class InfoPlacaLeida(BaseModel):
    valida: bool
    legible: Optional[str] = None
    tipo: Optional[str] = None
    norma: Optional[str] = None
    entidad: Optional[str] = None
    pais: Optional[str] = None
    sugerencia: Optional[str] = None


def _analizar(texto: str, extranjera: bool) -> InfoPlacaLeida:
    info = analizar_placa(texto, "United States" if extranjera else None)
    if info is not None:
        return InfoPlacaLeida(valida=True, legible=info.legible, tipo=info.tipo,
                              norma=info.norma or None, entidad=info.entidad,
                              pais=None if extranjera else info.pais)
    corregida = corregir_placa(texto)
    return InfoPlacaLeida(valida=False, sugerencia=formatear(corregida[0]) if corregida else None)


@router.get("/placas/analizar", response_model=InfoPlacaLeida)
def analizar(_: OperadorActual, texto: str = Query(min_length=1, max_length=20),
             extranjera: bool = False):
    """Tipo, norma y entidad de una placa escrita a mano. Lo usa el dashboard
    para validar mientras el operador escribe."""
    return _analizar(texto, extranjera)


# --------------------------------------------------------------------------
# Correccion de una lectura
# --------------------------------------------------------------------------

class Correccion(BaseModel):
    valor: str = Field(min_length=2, max_length=20)
    extranjera: bool = False


class RevisionPlaca(BaseModel):
    resultado: Literal["confirmada", "no_es_placa", "pendiente"]


@router.post("/events/{event_id}/revision-placa")
def revisar_placa(event_id: str, datos: RevisionPlaca, operador: Operador,
                  request: Request, session: SesionBD) -> dict:
    evento = session.exec(select(Event).where(Event.event_id == event_id)).first()
    if evento is None:
        raise HTTPException(404, "No existe ese evento")
    if evento.type != EventType.PLATE.value:
        raise HTTPException(422, "Solo se revisan lecturas de placa")
    meta = evento.meta
    anterior = meta.get("revision_placa", "pendiente")
    meta.update(revision_placa=datos.resultado, revisado_por=operador.username,
                revisado_en=datetime.now(timezone.utc).isoformat())
    evento.meta_json = json.dumps(meta, ensure_ascii=False)
    registrar(session, "eventos.revision_placa", usuario=operador.username, objetivo=event_id,
              detalle={"antes": anterior, "despues": datos.resultado}, request=request)
    session.commit()
    return {"event_id": event_id, "revision_placa": datos.resultado}


def mensaje_invalida(texto: str, info: InfoPlacaLeida) -> str:
    base = f"'{texto}' no tiene formato de placa mexicana."
    if info.sugerencia:
        base += f" ¿Quisiste decir {info.sugerencia}?"
    return base + (" Recuerda: las placas vigentes no usan las letras I, Ñ, O ni Q."
                   if any(c in limpiar(texto) for c in "IOQ") else "")


@router.post("/events/{event_id}/correccion")
async def corregir(event_id: str, datos: Correccion, operador: Operador, request: Request,
                   session: SesionBD) -> dict:
    """El operador corrige la lectura de una placa mirando la foto.

    Tres efectos:
      1. El registro queda con el valor correcto (la busqueda lo encuentra).
      2. Se vuelve a cruzar contra la lista negra: un "ABC-I23-A" mal leido
         que en realidad era una placa buscada genera su alerta en ese momento.
      3. La lectura corregida es verdad de campo para reentrenar el OCR
         (se conserva mas tiempo y entra al dataset exportable).
    """
    evento = session.exec(select(Event).where(Event.event_id == event_id)).first()
    if evento is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese evento")
    if evento.type != EventType.PLATE.value:
        raise HTTPException(422, "Solo se corrigen lecturas de placa")

    info = _analizar(datos.valor, datos.extranjera)
    if not info.valida:
        raise HTTPException(422, mensaje_invalida(datos.valor, info))

    anterior = evento.value
    if limpiar(anterior) == limpiar(info.legible):
        raise HTTPException(422, "La lectura ya es esa")

    meta = evento.meta
    meta["revision_placa"] = "confirmada"
    meta.setdefault("lectura_original", anterior)
    meta.update({"corregido_por": operador.username,
                 "corregido_en": datetime.now(timezone.utc).isoformat()})
    for clave in ("tipo_placa", "entidad", "norma", "pais"):
        meta.pop(clave, None)
    meta.update({k: v for k, v in {"tipo_placa": info.tipo, "entidad": info.entidad,
                                   "norma": info.norma,
                                   "pais": "Extranjera" if datos.extranjera else "México"}.items() if v})
    evento.value = info.legible
    evento.corregido = True
    evento.meta_json = json.dumps(meta, ensure_ascii=False, default=str)

    # Nuevo cruce contra la lista negra con el valor correcto.
    deteccion = DetectionEvent(event_id=evento.event_id, camera_id=evento.camera_id,
                               ts=a_utc(evento.ts), type=EventType.PLATE, value=evento.value,
                               confidence=1.0, meta=meta, track_id=evento.track_id)
    resultado = evaluar(deteccion, session)
    alerta = session.exec(select(Alert).where(Alert.event_id == evento.event_id)).first()
    nueva = None
    if resultado.severity != Severity.INFO:
        evento.severity = resultado.severity.value
        evento.match_kind = resultado.match_kind.value
        evento.matched_blacklist_id = resultado.blacklist_id
        evento.match_score = resultado.score
        if alerta is None:
            nueva = nueva_alerta(deteccion, resultado, evento.snapshot_path)
            if nueva is not None:
                nueva.detail = f"{nueva.detail} · Lectura corregida por {operador.username} (se leyó {anterior})"
                session.add(nueva)
    elif alerta is None:
        # Sin coincidencia y sin alerta previa: el evento queda como info.
        evento.severity = Severity.INFO.value
        evento.match_kind = "none"
        evento.matched_blacklist_id = None
        evento.match_score = None
    # Si ya habia una alerta (p.ej. por la lectura anterior), NO se toca: la
    # alerta es historia de lo que el operador vio y atendio; la correccion
    # queda en el evento y en la bitacora.

    registrar(session, "eventos.correccion", usuario=operador.username, objetivo=evento.value,
              detalle={"evento": event_id, "antes": anterior, "despues": evento.value,
                       "alerta_nueva": nueva is not None}, request=request)
    session.commit()

    respuesta = {"event_id": event_id, "value": evento.value, "severity": evento.severity,
                 "tipo_placa": info.tipo, "entidad": info.entidad, "alerta": None}
    if nueva is not None:
        session.refresh(nueva)
        mensaje = mensaje_alerta(nueva, a_utc(evento.ts).isoformat())
        respuesta["alerta"] = mensaje
        await hub.difundir("alert", mensaje)
        log.warning("ALERTA por correccion de %s: %s", operador.username, nueva.title)
    return respuesta


# --------------------------------------------------------------------------
# Dataset para reentrenar el OCR
# --------------------------------------------------------------------------

@router.get("/dataset/placas.zip")
def dataset_placas(
    admin: Admin,
    request: Request,
    session: SesionBD,
    automaticas: bool = Query(False, description="Incluir lecturas automaticas muy seguras"),
    min_conf: float = Query(0.9, ge=0.5, le=1.0),
    desde: Optional[datetime] = None,
):
    """ZIP con recortes de placas y su texto (formato fast-plate-ocr)."""
    temporal = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
    resumen = exportar(session, temporal, incluir_automaticas=automaticas,
                       min_conf=min_conf, desde=desde)
    registrar(session, "reportes.dataset", usuario=admin.username,
              detalle={"corregidas": resumen.corregidas, "automaticas": resumen.automaticas},
              request=request, confirmar=True)
    temporal.seek(0)

    def _leer():
        try:
            while bloque := temporal.read(1024 * 1024):
                yield bloque
        finally:
            temporal.close()

    nombre = f"dataset-placas-{datetime.now():%Y%m%d-%H%M}.zip"
    return StreamingResponse(_leer(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{nombre}"',
        "X-Placas-Corregidas": str(resumen.corregidas),
        "X-Placas-Automaticas": str(resumen.automaticas),
    })
