"""Cruce de detecciones contra la lista negra.

Aqui vive la decision que el borde deliberadamente no toma: si una deteccion
amerita alerta y con que gravedad. Esta separacion importa porque la lista
negra cambia constantemente (se dan de alta y baja placas) y no queremos
redesplegar los workers cada vez.

Cada tipo de deteccion tiene su propia estrategia, y son genuinamente
distintas:

  PLACAS    Texto con errores de OCR predecibles -> normalizacion + distancia
            de edicion. Ver shared/plates.py.
  ROSTROS   Vector de 512 dimensiones -> similitud coseno con umbral.
  ARMAS     No hay lista negra: cualquier deteccion confirmada es alerta.
  MOVIMIENTO No hay lista negra tampoco, pero a diferencia de un arma
            confirmada, una velocidad anomala tiene explicaciones inocentes
            (alguien corriendo para alcanzar algo). Se degrada a WARNING: el
            operador debe mirarlo, no saltar una alarma automatica.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
from sqlmodel import Session, select

from api.config import get_config
from api.models import BlacklistFace, BlacklistPlate
from shared.events import DetectionEvent, EventType, MatchKind, MatchResult, Severity
from shared.plates import buscar_coincidencia

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Rostros
# --------------------------------------------------------------------------

def _a_vector(datos: bytes, dim: int = 512) -> np.ndarray:
    return np.frombuffer(datos, dtype=np.float32, count=dim)


def similitud_coseno(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _cruzar_rostro(evento: DetectionEvent, session: Session) -> MatchResult:
    cfg = get_config()
    if not evento.embedding:
        return MatchResult(event_id=evento.event_id, severity=Severity.INFO,
                           reason="rostro sin embedding")

    consulta = np.asarray(evento.embedding, dtype=np.float32)
    registros = session.exec(select(BlacklistFace).where(BlacklistFace.active)).all()

    # Comparacion lineal contra toda la lista. Con menos de ~5.000 rostros esto
    # tarda milisegundos y no justifica un indice vectorial. Si la lista crece
    # mucho mas, el reemplazo natural es FAISS o sqlite-vec sin tocar el resto.
    mejor: Optional[BlacklistFace] = None
    # Se arranca en -1.0 y no en 0.0: la similitud coseno va de -1 a 1, y con
    # 0.0 como piso los vectores de similitud negativa nunca se registran. Eso
    # hacia que el evento llegara sin score y el operador perdiera el dato de
    # "que tan lejos estuvo" al revisar un caso dudoso.
    mejor_score = -1.0
    for registro in registros:
        score = similitud_coseno(consulta, _a_vector(registro.vector, registro.dim))
        if score > mejor_score:
            mejor_score, mejor = score, registro

    if mejor is None or mejor_score < cfg.face_match_threshold:
        return MatchResult(event_id=evento.event_id, severity=Severity.INFO,
                           score=round(mejor_score, 4) if mejor else None)

    return MatchResult(
        event_id=evento.event_id,
        severity=Severity(mejor.severity),
        match_kind=MatchKind.BIOMETRIC,
        blacklist_id=mejor.id,
        matched_value=mejor.label,
        score=round(mejor_score, 4),
        reason=f"Coincidencia biometrica con '{mejor.label}' ({mejor_score:.0%}): {mejor.reason}",
    )


# --------------------------------------------------------------------------
# Placas
# --------------------------------------------------------------------------

def _cruzar_placa(evento: DetectionEvent, session: Session) -> MatchResult:
    cfg = get_config()
    registros = session.exec(select(BlacklistPlate).where(BlacklistPlate.active)).all()
    if not registros:
        return MatchResult(event_id=evento.event_id, severity=Severity.INFO)

    coincidencia = buscar_coincidencia(
        evento.value,
        [(r.plate, r.id) for r in registros],
        max_distancia=cfg.plate_fuzzy_max_dist,
    )
    if coincidencia is None:
        return MatchResult(event_id=evento.event_id, severity=Severity.INFO)

    registro = next((r for r in registros if r.id == coincidencia.id_lista), None)
    if registro is None:
        return MatchResult(event_id=evento.event_id, severity=Severity.INFO)

    if coincidencia.exacta:
        severidad = Severity(registro.severity)
        motivo = f"Placa {registro.plate} en lista negra: {registro.reason}"
    else:
        # Una coincidencia difusa es una LECTURA POSIBLE, no un hecho. Se
        # degrada a WARNING aunque el registro sea critico: la diferencia entre
        # "es esta placa" y "podria ser esta placa" tiene que llegarle al
        # operador, porque implica verificar antes de actuar.
        severidad = Severity.WARNING
        motivo = (
            f"Posible coincidencia con {registro.plate} "
            f"(se leyo '{evento.value}', {coincidencia.distancia} caracter de diferencia): "
            f"{registro.reason}"
        )

    return MatchResult(
        event_id=evento.event_id,
        severity=severidad,
        match_kind=MatchKind.EXACT if coincidencia.exacta else MatchKind.FUZZY,
        blacklist_id=registro.id,
        matched_value=registro.plate,
        score=round(coincidencia.score, 4),
        reason=motivo,
    )


# --------------------------------------------------------------------------
# Armas
# --------------------------------------------------------------------------

def _cruzar_arma(evento: DetectionEvent) -> MatchResult:
    """No hay lista negra de armas: la deteccion misma es la alerta.

    El borde ya aplico la confirmacion temporal (N de M frames), asi que si un
    evento de arma llego hasta aqui, ya paso ese filtro.
    """
    return MatchResult(
        event_id=evento.event_id,
        severity=Severity.CRITICAL,
        match_kind=MatchKind.RULE,
        matched_value=evento.value,
        score=evento.confidence,
        reason=f"Arma detectada: {evento.value} "
               f"(confianza {evento.confidence:.0%}, {evento.observations} frames)",
    )


# --------------------------------------------------------------------------
# Movimiento anomalo
# --------------------------------------------------------------------------

def _cruzar_movimiento(evento: DetectionEvent) -> MatchResult:
    """Sin lista negra: la lectura de velocidad ya viene confirmada del borde
    (N de M frames). Pero a diferencia de un arma, correr o forcejear tiene
    explicaciones inocentes -- se alerta como WARNING, no CRITICAL, para que
    el operador decida en vez de que salte una alarma automatica."""
    velocidad = evento.meta.get("velocidad_alturas_por_s")
    detalle = f" ({velocidad:.1f}x el umbral normal)" if velocidad is not None else ""
    return MatchResult(
        event_id=evento.event_id,
        severity=Severity.WARNING,
        match_kind=MatchKind.RULE,
        matched_value=evento.value,
        score=evento.confidence,
        reason=f"Movimiento subito detectado{detalle}, {evento.observations} frames",
    )


# --------------------------------------------------------------------------

def evaluar(evento: DetectionEvent, session: Session) -> MatchResult:
    """Punto de entrada: decide severidad y coincidencia de un evento."""
    if evento.type == EventType.PLATE:
        return _cruzar_placa(evento, session)
    if evento.type == EventType.FACE:
        return _cruzar_rostro(evento, session)
    if evento.type == EventType.WEAPON:
        return _cruzar_arma(evento)
    if evento.type == EventType.ANOMALY:
        return _cruzar_movimiento(evento)
    return MatchResult(event_id=evento.event_id, severity=Severity.INFO)
