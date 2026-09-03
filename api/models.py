"""Esquema de base de datos. Se congela en Fase 0 para que el borde y la web
puedan desarrollarse en paralelo sin pisarse.

SQLModel sobre SQLite para arrancar (igual que FamNet). El dia que haya varias
camaras escribiendo a la vez conviene migrar a PostgreSQL: SQLite serializa las
escrituras y con 3 detectores por camara eso se nota. El modelo esta escrito
para que la migracion sea solo cambiar la URL de conexion.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, Index, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Camaras
# --------------------------------------------------------------------------

class Camera(SQLModel, table=True):
    """Una camara registrada. La URL RTSP vive aqui, no en el codigo."""

    __tablename__ = "cameras"

    id: Optional[int] = Field(default=None, primary_key=True)
    camera_id: str = Field(index=True, unique=True, description="Slug, ej. 'cam-entrada'")
    name: str = Field(description="Nombre para el operador, ej. 'Acceso vehicular norte'")
    location: Optional[str] = None

    source_spec: Optional[str] = Field(
        default=None,
        description="Cadena SOURCE del worker. CONTIENE CREDENCIALES: nunca "
                    "devolver este campo en un endpoint sin filtrar.",
    )
    enabled: bool = Field(default=True)

    # Salud reportada por el worker (heartbeat). Permite pintar la camara en
    # rojo en el dashboard sin tener que mirar el video.
    last_heartbeat: Optional[datetime] = Field(default=None, index=True)
    status_json: Optional[str] = Field(default=None, description="SourceStatus serializado")

    created_at: datetime = Field(default_factory=_utcnow)


# --------------------------------------------------------------------------
# Eventos (lo que el borde vio)
# --------------------------------------------------------------------------

class Event(SQLModel, table=True):
    """Una deteccion consolidada. Espejo de shared.events.DetectionEvent.

    Esta tabla crece rapido: una camara con trafico genera miles de filas al
    dia. Por eso la severidad se guarda desnormalizada aqui (evita un JOIN con
    alerts en la consulta mas frecuente del dashboard) y hay indice por fecha
    para poder purgar por antiguedad.
    """

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_camera_ts", "camera_id", "ts"),
        Index("ix_events_type_value", "type", "value"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: str = Field(index=True, unique=True, description="UUID generado en el borde")
    dedupe_key: str = Field(index=True, description="Idempotencia ante reintentos del borde")

    camera_id: str = Field(index=True, foreign_key="cameras.camera_id")
    ts: datetime = Field(index=True)
    received_at: datetime = Field(default_factory=_utcnow)

    type: str = Field(index=True, description="plate | face | weapon")
    track_id: Optional[int] = None
    value: str = Field(index=True)
    confidence: float

    # Caja como columnas sueltas, no JSON: se filtra por tamano para descartar
    # detecciones diminutas, y en JSON eso no se puede indexar.
    bbox_x1: Optional[int] = None
    bbox_y1: Optional[int] = None
    bbox_x2: Optional[int] = None
    bbox_y2: Optional[int] = None

    observations: int = Field(default=1)
    snapshot_path: Optional[str] = None

    # Resultado del cruce contra lista negra, resuelto al momento de ingerir.
    severity: str = Field(default="info", index=True)
    match_kind: str = Field(default="none")
    matched_blacklist_id: Optional[int] = Field(default=None, index=True)
    match_score: Optional[float] = None

    meta_json: Optional[str] = None

    @property
    def meta(self) -> dict:
        return json.loads(self.meta_json) if self.meta_json else {}


class FaceEmbedding(SQLModel, table=True):
    """Embeddings de rostros DETECTADOS, separados de `events`.

    Van en su propia tabla por dos razones: pesan 2 KB cada uno y ensuciarian
    los SELECT del dashboard, y sobre todo porque son datos biometricos
    sensibles con su propia politica de retencion (se purgan antes que el
    resto del evento).
    """

    __tablename__ = "face_embeddings"

    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: str = Field(index=True, foreign_key="events.event_id")
    vector: bytes = Field(description="float32[512] via numpy.tobytes()")
    dim: int = Field(default=512)
    created_at: datetime = Field(default_factory=_utcnow, index=True)


# --------------------------------------------------------------------------
# Lista negra
# --------------------------------------------------------------------------

class BlacklistPlate(SQLModel, table=True):
    """Placa buscada.

    `plate_normalized` guarda la placa sin guiones ni espacios y con los
    caracteres ambiguos colapsados (O->0, I->1, S->5, B->8, Z->2). El OCR
    confunde justo esos, asi que comparar las formas normalizadas atrapa
    coincidencias que un match exacto perderia. La forma legible original se
    conserva en `plate` para mostrarla al operador.
    """

    __tablename__ = "blacklist_plates"

    id: Optional[int] = Field(default=None, primary_key=True)
    plate: str = Field(index=True, description="Como se escribe, ej. 'ABC-123'")
    plate_normalized: str = Field(index=True, description="Forma canonica para comparar")

    reason: str = Field(description="Motivo: robo, orden judicial, acceso denegado...")
    severity: str = Field(default="critical")
    notes: Optional[str] = None
    active: bool = Field(default=True, index=True)

    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: Optional[datetime] = Field(
        default=None, description="Vencimiento. Una placa no deberia estar buscada para siempre."
    )


class BlacklistFace(SQLModel, table=True):
    """Persona buscada, identificada por embedding facial.

    DATO BIOMETRICO SENSIBLE (LFPDPPP). Requiere base legal documentada para
    cada alta. El campo `legal_basis` no es decoracion: es lo que te permite
    justificar por que esa persona esta en la lista.
    """

    __tablename__ = "blacklist_faces"

    id: Optional[int] = Field(default=None, primary_key=True)
    label: str = Field(index=True, description="Nombre o identificador del registro")
    vector: bytes = Field(description="float32[512] de referencia (InsightFace)")
    dim: int = Field(default=512)

    reason: str
    legal_basis: Optional[str] = Field(default=None, description="Fundamento del alta")
    severity: str = Field(default="critical")
    photo_path: Optional[str] = None
    active: bool = Field(default=True, index=True)

    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: Optional[datetime] = None


# --------------------------------------------------------------------------
# Alertas
# --------------------------------------------------------------------------

class Alert(SQLModel, table=True):
    """Una coincidencia que amerita atencion humana.

    Separada de `events` a proposito: hay miles de eventos y decenas de
    alertas. El dashboard consulta esta tabla, que se mantiene pequena y
    rapida aunque `events` tenga millones de filas.
    """

    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_status_created", "status", "created_at"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: str = Field(index=True, foreign_key="events.event_id")
    camera_id: str = Field(index=True)

    type: str = Field(description="plate | face | weapon")
    severity: str = Field(index=True)
    title: str = Field(description="Resumen para el operador, ej. 'Placa ABC-123 en lista negra'")
    detail: Optional[str] = None

    match_kind: str = Field(default="none")
    match_score: Optional[float] = None
    snapshot_path: Optional[str] = None

    # Ciclo de vida: toda alerta debe terminar reconocida o descartada. Sin
    # esto no puedes medir cuantos falsos positivos genera el sistema, que es
    # la metrica que decide si sirve o no.
    status: str = Field(default="new", index=True, description="new | acknowledged | dismissed")
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[datetime] = None
    dismissed_reason: Optional[str] = Field(
        default=None, description="'falso positivo' alimenta el reentrenamiento"
    )

    created_at: datetime = Field(default_factory=_utcnow, index=True)


class Operator(SQLModel, table=True):
    """Usuario del dashboard. Reutiliza el enfoque de FamNet (bcrypt + JWT),
    sin el 3FA facial: aqui la camara es el sensor, no el metodo de login."""

    __tablename__ = "operators"

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    display_name: str
    password_hash: str
    role: str = Field(default="viewer", description="viewer | operator | admin")
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_utcnow)
