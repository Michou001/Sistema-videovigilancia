"""Zonas y reglas del lado de la API: decidir si un evento de zona es alerta.

El borde reporta "persona dentro de 'Estacionamiento'" o "vehiculo cruzo
'Salida' en sentido entrada". Aqui se busca la regla, se revisa que siga
activa y que el evento haya ocurrido dentro de su horario (a la hora del
EVENTO, no de su llegada), y se le pone la severidad que el administrador
eligio. El conteo nunca es alerta: es estadistica.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

from sqlmodel import Session, select

from api.models import Zone
from shared.events import DetectionEvent, MatchKind, MatchResult, Severity
from shared.zonas import describir_horario, en_horario

log = logging.getLogger(__name__)

NOMBRE_CLASE = {"persona": "Persona", "vehiculo": "Vehículo"}
NOMBRE_DIRECCION = {"entrada": "sentido de entrada", "salida": "sentido de salida"}


def _info(evento: DetectionEvent, motivo: str) -> MatchResult:
    return MatchResult(event_id=evento.event_id, severity=Severity.INFO, match_kind=MatchKind.NONE,
                       reason=motivo)


def _zona_del_evento(evento: DetectionEvent, session: Session) -> Optional[Zone]:
    zona_id = (evento.meta or {}).get("zona_id")
    try:
        zona_id = int(zona_id)
    except (TypeError, ValueError):
        return None
    zona = session.get(Zone, zona_id)
    if zona is None or zona.camera_id != evento.camera_id:
        return None
    return zona


def evaluar_zona(evento: DetectionEvent, session: Session) -> MatchResult:
    zona = _zona_del_evento(evento, session)
    if zona is None:
        return _info(evento, "La regla ya no existe (se borró o es de otra cámara)")
    if evento.value == "conteo" or zona.tipo == "conteo":
        return _info(evento, f"Conteo en {zona.nombre}")
    if not zona.activa:
        return _info(evento, f"La regla '{zona.nombre}' está desactivada")
    if not en_horario(zona.horario, evento.ts):
        return _info(evento, f"Fuera del horario de '{zona.nombre}' ({describir_horario(zona.horario)})")

    meta = evento.meta or {}
    quien = NOMBRE_CLASE.get(meta.get("clase"), "Objeto")
    horario = describir_horario(zona.horario)
    cuando = "" if horario == "siempre" else f" · horario de la regla: {horario}"
    if evento.value == "cruce_linea":
        sentido = NOMBRE_DIRECCION.get(meta.get("direccion"), "")
        titulo = f"Cruce de línea: {zona.nombre}"
        motivo = f"{quien} cruzó la línea{f' en {sentido}' if sentido else ''}{cuando}"
    elif evento.value == "merodeo":
        segundos = meta.get("segundos")
        titulo = f"Merodeo en {zona.nombre}"
        motivo = (f"{quien} lleva {int(segundos)} s en la zona (umbral {zona.segundos} s){cuando}"
                  if isinstance(segundos, (int, float)) else f"{quien} permanece en la zona{cuando}")
    else:
        titulo = f"Intrusión en {zona.nombre}"
        motivo = f"{quien} dentro de la zona{cuando}"

    severidad = Severity.CRITICAL if zona.severidad == "critical" else Severity.WARNING
    return MatchResult(event_id=evento.event_id, severity=severidad, match_kind=MatchKind.RULE,
                       matched_value=zona.nombre, score=evento.confidence, titulo=titulo,
                       reason=motivo)


# --------------------------------------------------------------------------
# Lo que recibe el borde
# --------------------------------------------------------------------------

def para_borde(zona: Zone) -> dict:
    return {"id": zona.id, "nombre": zona.nombre, "tipo": zona.tipo, "puntos": zona.puntos,
            "clases": zona.lista_clases, "direccion": zona.direccion, "segundos": zona.segundos,
            "horario": zona.horario}


def zonas_de_camara(session: Session, camera_id: str) -> tuple[str, list[dict]]:
    """Zonas activas de una camara y una version (hash) para que el worker
    solo recargue cuando algo cambio."""
    zonas = session.exec(
        select(Zone).where(Zone.camera_id == camera_id, Zone.activa == True)  # noqa: E712
        .order_by(Zone.id)
    ).all()
    datos = [para_borde(z) for z in zonas]
    version = hashlib.sha1(json.dumps(datos, sort_keys=True).encode()).hexdigest()[:16]
    return version, datos
