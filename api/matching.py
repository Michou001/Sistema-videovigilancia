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
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from sqlmodel import Session, select

from api.config import get_config
from api.models import BlacklistFace, BlacklistPlate
from shared.events import DetectionEvent, EventType, MatchKind, MatchResult, Severity
from shared.plates import buscar_coincidencia

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Lista negra en memoria
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Registro:
    """Copia de solo lectura de una fila de lista negra, sin sesion de BD."""

    id: int
    valor: str          # placa o etiqueta de la persona
    severity: str
    reason: str
    expires_at: Optional[datetime]

    def vigente(self, ahora: datetime) -> bool:
        if self.expires_at is None:
            return True
        vence = self.expires_at
        if vence.tzinfo is None:
            vence = vence.replace(tzinfo=timezone.utc)
        return vence > ahora


class ListaNegraEnMemoria:
    """La lista negra se consultaba COMPLETA en cada evento: una consulta por
    placa leida y otra por rostro, y la comparacion facial en un bucle de
    Python registro por registro. Con la ingesta en rafaga eso era el costo
    dominante de /api/events.

    Aqui se carga una vez y se guarda ya lista para comparar: las placas como
    tuplas y los rostros como una matriz normalizada, de modo que comparar un
    rostro contra toda la lista es un solo producto matriz-vector.

    Se invalida al dar de alta o de baja (ver routers de lista negra) y, como
    red de seguridad para cambios hechos por fuera de la API, caduca sola a los
    `ttl` segundos.
    """

    def __init__(self, ttl: float = 30.0) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._placas: Optional[list[_Registro]] = None
        self._rostros: Optional[tuple[np.ndarray, list[_Registro]]] = None
        self._placas_en = 0.0
        self._rostros_en = 0.0
        self.al_invalidar = None
        """Con varios procesos de API (Redis), avisa a los demas que tambien
        invaliden: dar de alta una placa debe valer en todos al instante."""

    def invalidar(self, difundir: bool = True) -> None:
        with self._lock:
            self._placas = None
            self._rostros = None
        if difundir and self.al_invalidar is not None:
            try:
                self.al_invalidar()
            except Exception as e:  # noqa: BLE001
                log.error("No se pudo avisar la invalidacion a los demas procesos: %s", e)

    def _caducado(self, cargado_en: float) -> bool:
        return time.monotonic() - cargado_en > self.ttl

    def placas(self, session: Session) -> list[_Registro]:
        with self._lock:
            if self._placas is None or self._caducado(self._placas_en):
                filas = session.exec(select(BlacklistPlate).where(BlacklistPlate.active)).all()
                self._placas = [_Registro(r.id, r.plate, r.severity, r.reason, r.expires_at)
                                for r in filas]
                self._placas_en = time.monotonic()
            return self._placas

    def rostros(self, session: Session) -> tuple[np.ndarray, list[_Registro]]:
        with self._lock:
            if self._rostros is None or self._caducado(self._rostros_en):
                filas = session.exec(select(BlacklistFace).where(BlacklistFace.active)).all()
                vectores, meta = [], []
                for r in filas:
                    v = np.frombuffer(r.vector, dtype=np.float32, count=r.dim)
                    norma = float(np.linalg.norm(v))
                    if norma == 0:
                        continue
                    vectores.append(v / norma)
                    meta.append(_Registro(r.id, r.label, r.severity, r.reason, r.expires_at))
                dim = vectores[0].shape[0] if vectores else 512
                matriz = (np.vstack(vectores).astype(np.float32) if vectores
                          else np.zeros((0, dim), dtype=np.float32))
                self._rostros = (matriz, meta)
                self._rostros_en = time.monotonic()
            return self._rostros


lista_negra = ListaNegraEnMemoria()


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Rostros
# --------------------------------------------------------------------------

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

    matriz, registros = lista_negra.rostros(session)
    consulta = np.asarray(evento.embedding, dtype=np.float32)
    norma = float(np.linalg.norm(consulta))
    if not registros or norma == 0 or consulta.shape[0] != matriz.shape[1]:
        return MatchResult(event_id=evento.event_id, severity=Severity.INFO)

    # Similitud coseno contra toda la lista en una sola operacion. Con decenas
    # de miles de rostros sigue siendo cuestion de milisegundos; mas alla, el
    # reemplazo natural es FAISS o sqlite-vec sin tocar el resto.
    similitudes = matriz @ (consulta / norma)
    ahora = _ahora()
    for i in np.argsort(similitudes)[::-1]:
        mejor, mejor_score = registros[int(i)], float(similitudes[int(i)])
        if mejor.vigente(ahora):
            break
    else:
        return MatchResult(event_id=evento.event_id, severity=Severity.INFO)

    if mejor_score < cfg.face_match_threshold:
        # Se reporta el score aunque no alcance: al revisar un caso dudoso, el
        # operador necesita saber que tan lejos estuvo.
        return MatchResult(event_id=evento.event_id, severity=Severity.INFO,
                           score=round(mejor_score, 4))

    return MatchResult(
        event_id=evento.event_id,
        severity=Severity(mejor.severity),
        match_kind=MatchKind.BIOMETRIC,
        blacklist_id=mejor.id,
        matched_value=mejor.valor,
        score=round(mejor_score, 4),
        reason=f"Coincidencia biométrica con '{mejor.valor}' ({mejor_score:.0%}): {mejor.reason}",
    )


# --------------------------------------------------------------------------
# Placas
# --------------------------------------------------------------------------

def _cruzar_placa(evento: DetectionEvent, session: Session) -> MatchResult:
    cfg = get_config()
    ahora = _ahora()
    # Un registro vencido ya no alerta. Antes `expires_at` se guardaba pero
    # nadie lo consultaba: una placa dada de alta "por 30 dias" seguia
    # generando alertas criticas indefinidamente.
    registros = [r for r in lista_negra.placas(session) if r.vigente(ahora)]
    if not registros:
        return MatchResult(event_id=evento.event_id, severity=Severity.INFO)

    coincidencia = buscar_coincidencia(
        evento.value,
        [(r.valor, r.id) for r in registros],
        max_distancia=cfg.plate_fuzzy_max_dist,
    )
    if coincidencia is None:
        return MatchResult(event_id=evento.event_id, severity=Severity.INFO)

    registro = next((r for r in registros if r.id == coincidencia.id_lista), None)
    if registro is None:
        return MatchResult(event_id=evento.event_id, severity=Severity.INFO)

    if coincidencia.exacta:
        severidad = Severity(registro.severity)
        motivo = f"Placa {registro.valor} en lista negra: {registro.reason}"
    else:
        # Una coincidencia difusa es una LECTURA POSIBLE, no un hecho. Se
        # degrada a WARNING aunque el registro sea critico: la diferencia entre
        # "es esta placa" y "podria ser esta placa" tiene que llegarle al
        # operador, porque implica verificar antes de actuar.
        severidad = Severity.WARNING
        motivo = (
            f"Posible coincidencia con {registro.valor} "
            f"(se leyó '{evento.value}', {coincidencia.distancia} "
            f"{'carácter' if coincidencia.distancia == 1 else 'caracteres'} de diferencia): "
            f"{registro.reason}"
        )

    return MatchResult(
        event_id=evento.event_id,
        severity=severidad,
        match_kind=MatchKind.EXACT if coincidencia.exacta else MatchKind.FUZZY,
        blacklist_id=registro.id,
        matched_value=registro.valor,
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
    if evento.value == "persona_caida":
        return MatchResult(
            event_id=evento.event_id,
            severity=Severity.WARNING,
            match_kind=MatchKind.RULE,
            matched_value=evento.value,
            score=evento.confidence,
            reason="Posible persona caída: pasó de estar de pie a quedar tendida en "
                   "menos de 2 s. Verificar en video.",
        )

    velocidad = evento.meta.get("velocidad_alturas_por_s")
    umbral = evento.meta.get("umbral")
    detalle = ""
    if isinstance(velocidad, (int, float)):
        detalle = f" ({velocidad:.1f} alturas de cuerpo/s"
        if isinstance(umbral, (int, float)) and umbral > 0:
            detalle += f", {velocidad / umbral:.1f} veces el umbral"
        detalle += ")"
    return MatchResult(
        event_id=evento.event_id,
        severity=Severity.WARNING,
        match_kind=MatchKind.RULE,
        matched_value=evento.value,
        score=evento.confidence,
        reason=f"Movimiento súbito detectado{detalle}, {evento.observations} frames",
    )


# --------------------------------------------------------------------------
# La camara misma
# --------------------------------------------------------------------------

# (severidad, titulo, explicacion). Sabotaje y perdida de video son criticos:
# alguien puede estar tapando o desconectando la camara justo antes de actuar.
EVENTOS_CAMARA: dict[str, tuple[Severity, str, str]] = {
    "sin_senal": (Severity.WARNING, "Cámara sin señal",
                  "El worker dejó de reportar o la cámara no entrega imagen."),
    "senal_recuperada": (Severity.INFO, "Cámara recuperada", "La cámara volvió a entregar imagen."),
    "sabotaje": (Severity.CRITICAL, "SABOTAJE DE CÁMARA",
                 "La cámara detectó que la taparon, la movieron o la deslumbraron."),
    "perdida_video": (Severity.CRITICAL, "Pérdida de video",
                      "La cámara reporta pérdida de la señal de video."),
    "deteccion_linea": (Severity.WARNING, "Cruce de línea (analítica de la cámara)",
                        "La analítica propia de la cámara detectó un cruce de línea."),
    "intrusion_camara": (Severity.WARNING, "Intrusión (analítica de la cámara)",
                         "La analítica propia de la cámara detectó una intrusión en zona."),
    "movimiento_camara": (Severity.INFO, "Movimiento (analítica de la cámara)", ""),
}


def _cruzar_camara(evento: DetectionEvent) -> MatchResult:
    severidad, titulo, motivo = EVENTOS_CAMARA.get(
        evento.value, (Severity.INFO, f"Evento de cámara: {evento.value}", ""))
    # El detalle concreto (cuanto tiempo lleva sin imagen, que reporto la
    # camara) lo pone quien genera el evento.
    extra = (evento.meta or {}).get("detalle")
    motivo = " · ".join(str(x) for x in (motivo, extra) if x)
    return MatchResult(event_id=evento.event_id, severity=severidad, match_kind=MatchKind.RULE,
                       matched_value=evento.value, score=evento.confidence,
                       titulo=titulo, reason=motivo or None)


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
    if evento.type == EventType.CAMERA:
        return _cruzar_camara(evento)
    if evento.type == EventType.ZONE:
        try:
            from api.zonas import evaluar_zona
        except ImportError:
            return MatchResult(event_id=evento.event_id, severity=Severity.INFO)
        return evaluar_zona(evento, session)
    return MatchResult(event_id=evento.event_id, severity=Severity.INFO)
