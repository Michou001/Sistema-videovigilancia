"""Gestion de la lista negra de placas.

Dar de alta una placa aqui hace que el sistema la senale cada vez que la vea.
Es una accion con consecuencias sobre personas reales, asi que: requiere rol
admin, registra quien la dio de alta, y cada entrada exige un motivo escrito.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlmodel import col, select

from api.auditoria import registrar
from api.deps import Admin, OperadorActual, SesionBD
from api.matching import lista_negra
from api.models import BlacklistPlate, FechasEnUtc
from api.retroactive import reescanear_placa
from shared.fechas import a_utc
from shared.plates import (
    analizar_placa,
    corregir_placa,
    es_placa_valida,
    formatear,
    limpiar,
    normalizar,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/blacklist/plates", tags=["lista negra"])


class AltaPlaca(BaseModel):
    plate: str = Field(min_length=2, max_length=15)
    reason: str = Field(min_length=3, max_length=300)
    severity: str = Field(default="critical")
    notes: Optional[str] = Field(default=None, max_length=500)
    expires_at: Optional[datetime] = None
    extranjera: bool = Field(default=False,
                             description="Placa no mexicana: no se valida contra los formatos de la NOM")

    @field_validator("severity")
    @classmethod
    def _validar_severidad(cls, v: str) -> str:
        if v not in {"critical", "warning"}:
            raise ValueError("severity debe ser 'critical' o 'warning'")
        return v

    @field_validator("expires_at")
    @classmethod
    def _vence_en_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        return a_utc(v)


class PlacaLeida(FechasEnUtc, BaseModel):
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
    extranjera: bool = False
    tipo: Optional[str] = None
    entidad: Optional[str] = None


def _leida(registro: BlacklistPlate) -> PlacaLeida:
    """El registro con su tipo y entidad, para que el administrador vea que
    placa dio de alta ("Automovil particular de Jalisco")."""
    info = analizar_placa(registro.plate, "United States" if registro.extranjera else None)
    return PlacaLeida(**registro.model_dump(),
                      tipo=info.tipo if info else None,
                      entidad=info.entidad if info else None)


@router.get("", response_model=list[PlacaLeida])
def listar(session: SesionBD, _: OperadorActual, incluir_inactivas: bool = False):
    consulta = select(BlacklistPlate)
    if not incluir_inactivas:
        consulta = consulta.where(BlacklistPlate.active)
    filas = session.exec(consulta.order_by(col(BlacklistPlate.created_at).desc())).all()
    return [_leida(r) for r in filas]


@router.post("", response_model=PlacaLeida, status_code=status.HTTP_201_CREATED)
def agregar(datos: AltaPlaca, session: SesionBD, admin: Admin, tareas: BackgroundTasks,
            request: Request):
    limpio = limpiar(datos.plate)
    if datos.extranjera:
        valida = 2 <= len(limpio) <= 10
    else:
        valida, limpio, _ = es_placa_valida(datos.plate)
    if not valida:
        # Se valida al dar de alta para que una placa mal escrita no quede en
        # la lista sin coincidir nunca con nada. Si hay una correccion
        # plausible, se sugiere: casi siempre es una O en vez de D o de 0.
        corregida = None if datos.extranjera else corregir_placa(datos.plate)
        sugerencia = f" ¿Quisiste decir {formatear(corregida[0])}?" if corregida else ""
        prohibidas = (" Las placas vigentes no usan las letras I, Ñ, O ni Q."
                      if any(c in limpio for c in "IOQ") else "")
        raise HTTPException(
            422,
            f"'{datos.plate}' no tiene formato de placa mexicana.{sugerencia}{prohibidas} "
            "Si es de otro país, marca «placa extranjera».",
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
                                f"La placa {existente.plate} ya está en la lista negra")
        # Reactivar la existente es mejor que crear un duplicado: conserva el
        # historial de eventos que ya apuntaban a este registro.
        existente.active = True
        existente.reason = datos.reason
        existente.severity = datos.severity
        existente.notes = datos.notes
        existente.expires_at = datos.expires_at
        existente.extranjera = datos.extranjera
        existente.created_by = admin.username
        existente.created_at = datetime.now(timezone.utc)
        registrar(session, "lista_negra.reactivacion_placa", usuario=admin.username,
                  objetivo=existente.plate, detalle={"motivo": datos.reason,
                                                     "vence": datos.expires_at},
                  request=request)
        session.commit()
        session.refresh(existente)
        lista_negra.invalidar()
        log.info("Placa %s reactivada en lista negra por %s", existente.plate, admin.username)
        tareas.add_task(reescanear_placa, existente)
        return _leida(existente)

    registro = BlacklistPlate(
        plate=limpio if datos.extranjera else formatear(limpio),
        extranjera=datos.extranjera,
        plate_normalized=normalizada,
        reason=datos.reason,
        severity=datos.severity,
        notes=datos.notes,
        expires_at=datos.expires_at,
        created_by=admin.username,
    )
    session.add(registro)
    registrar(session, "lista_negra.alta_placa", usuario=admin.username,
              objetivo=registro.plate, detalle={"motivo": datos.reason, "vence": datos.expires_at,
                                                "severidad": datos.severity,
                                                "extranjera": datos.extranjera},
              request=request)
    session.commit()
    session.refresh(registro)
    lista_negra.invalidar()
    log.info("Placa %s agregada a lista negra por %s", registro.plate, admin.username)
    # En segundo plano: si esta placa ya habia pasado antes de hoy, que el
    # operador se entere sin tener que acordarse de revisarlo el mismo.
    tareas.add_task(reescanear_placa, registro)
    return _leida(registro)


@router.delete("/{registro_id}", status_code=status.HTTP_204_NO_CONTENT)
def desactivar(registro_id: int, session: SesionBD, admin: Admin, request: Request):
    """Baja logica, no borrado.

    Los eventos historicos apuntan a este registro; borrarlo de verdad dejaria
    alertas pasadas sin explicacion de por que se dispararon.
    """
    registro = session.get(BlacklistPlate, registro_id)
    if registro is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese registro")
    registro.active = False
    registrar(session, "lista_negra.baja_placa", usuario=admin.username,
              objetivo=registro.plate, request=request)
    session.commit()
    lista_negra.invalidar()
    log.info("Placa %s desactivada por %s", registro.plate, admin.username)
