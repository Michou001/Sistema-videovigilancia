"""Dependencias de FastAPI: sesion de BD y autenticacion."""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Depends, Header, HTTPException, status
from sqlmodel import Session, select

from api.database import get_session
from api.models import Operator
from api.security import decodificar_token, token_ingesta_valido

SesionBD = Annotated[Session, Depends(get_session)]


def _extraer_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    partes = authorization.split(None, 1)
    if len(partes) == 2 and partes[0].lower() == "bearer":
        return partes[1].strip()
    return authorization.strip()


def operador_actual(
    session: SesionBD,
    authorization: Annotated[Optional[str], Header()] = None,
) -> Operator:
    """Valida el JWT del dashboard y devuelve el operador."""
    token = _extraer_bearer(authorization)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Falta el token de sesion")

    datos = decodificar_token(token)
    if not datos:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token invalido o expirado")

    operador = session.exec(
        select(Operator).where(Operator.username == datos.get("sub"))
    ).first()
    if operador is None or not operador.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario inexistente o desactivado")
    return operador


OperadorActual = Annotated[Operator, Depends(operador_actual)]


def requerir_admin(operador: OperadorActual) -> Operator:
    """Modificar la lista negra es una accion con consecuencias: puede hacer que
    el sistema senale a una persona o a un vehiculo. Se restringe a admin."""
    if operador.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Se requiere rol de administrador")
    return operador


Admin = Annotated[Operator, Depends(requerir_admin)]


def verificar_worker(
    x_api_token: Annotated[Optional[str], Header()] = None,
    authorization: Annotated[Optional[str], Header()] = None,
) -> None:
    """Autentica al worker de borde por token compartido.

    Es distinto del JWT del dashboard a proposito: el worker es una maquina que
    corre indefinidamente y no tiene forma de renovar una sesion caducada.
    """
    presentado = x_api_token or _extraer_bearer(authorization) or ""
    if not token_ingesta_valido(presentado):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token de ingesta invalido")
