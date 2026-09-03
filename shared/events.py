"""Contrato de evento: la frontera entre el worker de borde y la plataforma web.

Este modulo es la UNICA fuente de verdad del formato de evento. El worker de
borde (edge/) construye estos objetos y la API (api/) los recibe y valida con
las mismas clases. Si cambias algo aqui, cambia en los dos lados a la vez.

Regla de diseno: el borde NO decide si algo es una alerta. El borde solo
reporta "vi esto, con esta confianza". El cruce contra la lista negra y la
severidad final los decide la API, que es quien tiene la base de datos.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------
# Enumeraciones
# --------------------------------------------------------------------------

class EventType(str, Enum):
    """Que tipo de deteccion produjo el evento."""

    PLATE = "plate"    # placa vehicular + texto OCR
    FACE = "face"      # rostro + embedding biometrico
    WEAPON = "weapon"  # arma de fuego o arma blanca


class Severity(str, Enum):
    """Severidad asignada por la API despues de cruzar la lista negra."""

    INFO = "info"          # deteccion normal, sin coincidencia
    WARNING = "warning"    # coincidencia difusa o confianza baja: revisar
    CRITICAL = "critical"  # match exacto en lista negra, o arma confirmada


class MatchKind(str, Enum):
    """Como se llego a la coincidencia con la lista negra."""

    NONE = "none"          # no hubo coincidencia
    EXACT = "exact"        # placa identica al registro
    FUZZY = "fuzzy"        # placa con correccion de errores de OCR (O/0, I/1...)
    BIOMETRIC = "biometric"  # similitud coseno de embedding sobre el umbral
    RULE = "rule"          # regla fija: toda arma confirmada alerta


# --------------------------------------------------------------------------
# Estructuras auxiliares
# --------------------------------------------------------------------------

class BBox(BaseModel):
    """Caja delimitadora en pixeles, sobre el frame ORIGINAL (no el redimensionado)."""

    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    def clamp(self, frame_width: int, frame_height: int) -> "BBox":
        """Recorta la caja a los limites del frame. YOLO a veces devuelve
        coordenadas ligeramente fuera del borde y el crop truena."""
        return BBox(
            x1=max(0, min(self.x1, frame_width)),
            y1=max(0, min(self.y1, frame_height)),
            x2=max(0, min(self.x2, frame_width)),
            y2=max(0, min(self.y2, frame_height)),
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# El evento
# --------------------------------------------------------------------------

class DetectionEvent(BaseModel):
    """Una deteccion consolidada, lista para enviarse a la API.

    IMPORTANTE: un evento NO es un frame. Es una observacion agregada de un
    objeto seguido (`track_id`) durante varios frames. El worker acumula
    lecturas del mismo track y emite un solo evento con la mejor de ellas.
    Esto es lo que evita el problema de escribir 30 filas por segundo del
    mismo coche (el motivo del COOLDOWN_SEGUNDOS=15 del script original).
    """

    model_config = ConfigDict(use_enum_values=False)

    # --- Identidad ---------------------------------------------------------
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    camera_id: str = Field(description="Identificador logico de la camara, ej. 'cam-entrada'")
    ts: datetime = Field(default_factory=_utcnow, description="Instante de la MEJOR observacion, en UTC")

    # --- Que se detecto ----------------------------------------------------
    type: EventType
    track_id: Optional[int] = Field(
        default=None,
        description="ID del tracker (ByteTrack). Estable mientras el objeto siga visible. "
                    "None si el detector corrio sin tracking.",
    )
    value: str = Field(
        description="Contenido segun el tipo: texto de la placa ('ABC-123'), "
                    "etiqueta del arma ('knife'/'pistol'), o 'face' para rostros "
                    "(la identidad vive en el embedding, no aqui).",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Confianza de la MEJOR observacion")
    bbox: Optional[BBox] = None

    # --- Evidencia de respaldo --------------------------------------------
    snapshot_path: Optional[str] = Field(
        default=None,
        description="Ruta relativa del recorte guardado en disco por el borde.",
    )
    snapshot_b64: Optional[str] = Field(
        default=None,
        description="JPEG en base64 del recorte. Se usa cuando el borde y la API "
                    "no comparten disco. Puede pesar: no lo llenes para eventos INFO.",
    )
    embedding: Optional[list[float]] = Field(
        default=None,
        description="Vector facial de 512-d (InsightFace). Solo para type=FACE. "
                    "Es DATO BIOMETRICO SENSIBLE: ver nota de privacidad abajo.",
    )

    # --- Calidad de la observacion ----------------------------------------
    observations: int = Field(
        default=1, ge=1,
        description="Cuantos frames sostuvieron esta deteccion. Para armas es el "
                    "criterio de confirmacion temporal (ej. 4 de 6).",
    )
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None

    # --- Contexto libre ----------------------------------------------------
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description="Extras por detector: color de vehiculo, atributos de "
                    "vestimenta, lecturas OCR alternativas, etc.",
    )

    def dedupe_key(self) -> str:
        """Clave de idempotencia. Si el borde reintenta por un fallo de red, la
        API debe reconocer que es el mismo evento y no duplicarlo."""
        return f"{self.camera_id}:{self.type.value}:{self.track_id}:{self.value}"


class EventBatch(BaseModel):
    """Envio agrupado. El borde acumula y manda en lotes para no abrir una
    conexion HTTP por deteccion."""

    camera_id: str
    sent_at: datetime = Field(default_factory=_utcnow)
    events: list[DetectionEvent]


# --------------------------------------------------------------------------
# Respuesta de la API
# --------------------------------------------------------------------------

class MatchResult(BaseModel):
    """Lo que la API responde por cada evento: si hubo coincidencia y que tan grave."""

    event_id: str
    severity: Severity
    match_kind: MatchKind = MatchKind.NONE
    blacklist_id: Optional[int] = None
    matched_value: Optional[str] = Field(
        default=None, description="El registro de la lista negra con el que coincidio"
    )
    score: Optional[float] = Field(
        default=None,
        description="Similitud coseno (rostros) o distancia de edicion normalizada (placas)",
    )
    reason: Optional[str] = Field(default=None, description="Explicacion legible para el operador")


class IngestResponse(BaseModel):
    accepted: int
    duplicates: int = 0
    matches: list[MatchResult] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Nota de privacidad (no borrar)
# --------------------------------------------------------------------------
# El campo `embedding` de un evento FACE es dato personal biometrico y, bajo la
# LFPDPPP (Mexico), es DATO SENSIBLE. Implica: aviso de privacidad visible en el
# punto de captura, consentimiento expreso, finalidad acotada, y politica de
# retencion y borrado. No lo registres en logs ni lo expongas en endpoints
# publicos. Ver la Fase 6 del plan.
