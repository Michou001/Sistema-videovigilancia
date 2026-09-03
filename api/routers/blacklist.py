"""Gestion de la lista negra de placas.

Dar de alta una placa aqui hace que el sistema la senale cada vez que la vea.
Es una accion con consecuencias sobre personas reales, asi que: requiere rol
admin, registra quien la dio de alta, y cada entrada exige un motivo escrito.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlmodel import col, select

from api.deps import Admin, OperadorActual, SesionBD
from api.models import BlacklistPlate
from shared.plates import es_placa_valida, formatear, normalizar

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/blacklist/plates", tags=["lista negra"])


class AltaPlaca(BaseModel):
    plate: str = Field(min_length=4, max_length=15)
    reason: str = Field(min_length=3, max_length=300)
    severity: str = Field(default="critical")
    notes: Optional[str] = None
    expires_at: Optional[datetime] = None

    @field_validator("severity")
    @classmethod
    def _validar_severidad(cls, v: str) -> str:
        if v not in {"critical", "warning"}:
            raise ValueError("severity debe ser 'critical' o 'warning'")
        return v


class PlacaLeida(BaseModel):
    id: int
    plate: str
    plate_normalized: str
    reason: str
    severity: str
    notes: Optional[str]
    active: bool
    created_by: Optional[str]
    created_at: datetime
    expires_at: Optional[datetime]


@router.get("", response_model=list[PlacaLeida])
def listar(session: SesionBD, _: OperadorActual, incluir_inactivas: bool = False):
    consulta = select(BlacklistPlate)
    if not incluir_inactivas:
        consulta = consulta.where(BlacklistPlate.active)
    return session.exec(consulta.order_by(col(BlacklistPlate.created_at).desc())).all()


@router.post("", response_model=PlacaLeida, status_code=status.HTTP_201_CREATED)
def agregar(datos: AltaPlaca, session: SesionBD, admin: Admin):
    valida, limpio, _ = es_placa_valida(datos.plate)
    if not valida:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"'{datos.plate}' no tiene formato de placa mexicana valida. "
            "Se valida al dar de alta para que una placa mal escrita no quede "
            "en la lista sin coincidir nunca con nada.",
        )

    normalizada = normalizar(limpio)

    # Se compara la forma NORMALIZADA: "ABC-123" y "ABC 123" son la misma placa
    # y no deben duplicarse en la lista.
    existente = session.exec(
        select(BlacklistPlate).where(BlacklistPlate.plate_normalized == normalizada)
    ).first()
    if existente is not None:
        if existente.active:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"La placa {existente.plate} ya esta en la lista negra")
        # Reactivar la existente es mejor que crear un duplicado: conserva el
        # historial de eventos que ya apuntaban a este registro.
        existente.active = True
        existente.reason = datos.reason
        existente.severity = datos.severity
        existente.notes = datos.notes
        existente.expires_at = datos.expires_at
        existente.created_by = admin.username
        existente.created_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(existente)
        log.info("Placa %s reactivada en lista negra por %s", existente.plate, admin.username)
        return existente

    registro = BlacklistPlate(
        plate=formatear(limpio),
        plate_normalized=normalizada,
        reason=datos.reason,
        severity=datos.severity,
        notes=datos.notes,
        expires_at=datos.expires_at,
        created_by=admin.username,
    )
    session.add(registro)
    session.commit()
    session.refresh(registro)
    log.info("Placa %s agregada a lista negra por %s", registro.plate, admin.username)
    return registro


@router.delete("/{registro_id}", status_code=status.HTTP_204_NO_CONTENT)
def desactivar(registro_id: int, session: SesionBD, admin: Admin):
    """Baja logica, no borrado.

    Los eventos historicos apuntan a este registro; borrarlo de verdad dejaria
    alertas pasadas sin explicacion de por que se dispararon.
    """
    registro = session.get(BlacklistPlate, registro_id)
    if registro is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese registro")
    registro.active = False
    session.commit()
    log.info("Placa %s desactivada por %s", registro.plate, admin.username)
