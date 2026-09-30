"""Login del dashboard, cierre de sesion y cambio de contrasena."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlmodel import select

from api.auditoria import registrar
from api.deps import OperadorActual, SesionBD, _extraer_bearer
from api.models import Operator
from api.security import (
    borrar_cookie_sesion,
    crear_token,
    es_https,
    hash_password,
    limite_login,
    poner_cookie_sesion,
    validar_password_nueva,
    verificar_password,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Credenciales(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=200)


class Sesion(BaseModel):
    token: str
    username: str
    display_name: str
    role: str


class CambioPassword(BaseModel):
    actual: str = Field(max_length=200)
    nueva: str = Field(max_length=200)


def _sesion(operador: Operator, response: Response, request: Request) -> Sesion:
    token = crear_token(operador.username, operador.role, operador.token_version or 0)
    poner_cookie_sesion(response, token, segura=es_https(request))
    return Sesion(token=token, username=operador.username,
                  display_name=operador.display_name, role=operador.role)


@router.post("/login", response_model=Sesion)
def login(datos: Credenciales, session: SesionBD, request: Request, response: Response):
    ip = request.client.host if request.client else "desconocida"
    espera = limite_login.espera(ip)
    if espera > 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Demasiados intentos fallidos. Intenta de nuevo en {math.ceil(espera / 60)} min.",
            headers={"Retry-After": str(math.ceil(espera))},
        )

    operador = session.exec(
        select(Operator).where(Operator.username == datos.username)
    ).first()

    # Se responde lo mismo si el usuario no existe o si la contrasena es
    # incorrecta: distinguirlos permite enumerar usuarios validos.
    if operador is None or not operador.active or \
            not verificar_password(datos.password, operador.password_hash):
        limite_login.fallo(ip)
        log.warning("Login fallido para '%s' desde %s", datos.username, ip)
        registrar(session, "sesion.login_fallido", usuario=None,
                  objetivo=datos.username[:64], request=request, confirmar=True)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario o contraseña incorrectos")

    limite_login.exito(ip)
    operador.last_login = datetime.now(timezone.utc)
    registrar(session, "sesion.login", usuario=operador.username, request=request)
    session.commit()
    session.refresh(operador)
    return _sesion(operador, response, request)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(session: SesionBD, request: Request, response: Response,
           authorization: Annotated[Optional[str], Header()] = None):
    """Borra la cookie de sesion. El JWT sigue siendo valido hasta caducar si
    alguien lo copio; para invalidarlo de verdad, cambia la contrasena."""
    from api.deps import operador_por_token

    operador = operador_por_token(session, _extraer_bearer(authorization))
    if operador is not None:
        registrar(session, "sesion.logout", usuario=operador.username,
                  request=request, confirmar=True)
    borrar_cookie_sesion(response)


@router.get("/me", response_model=Sesion)
def yo(operador: OperadorActual, request: Request, response: Response,
       authorization: Annotated[Optional[str], Header()] = None):
    """Permite al frontend verificar que su token sigue vivo al recargar.

    Tambien repone la cookie de sesion con el mismo token: una sesion abierta
    antes de que existiera la cookie (o con la cookie borrada) recupera la
    evidencia y el video en vivo sin tener que volver a entrar.
    """
    token = _extraer_bearer(authorization) or ""
    poner_cookie_sesion(response, token, segura=es_https(request))
    return Sesion(token="", username=operador.username,
                  display_name=operador.display_name, role=operador.role)


@router.post("/password", response_model=Sesion)
def cambiar_password(datos: CambioPassword, operador: OperadorActual, session: SesionBD,
                     request: Request, response: Response):
    """Cambio de la contrasena propia. Cierra todas las demas sesiones del
    usuario (sube token_version) y devuelve una sesion nueva para esta."""
    if not verificar_password(datos.actual, operador.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "La contraseña actual no es correcta")
    motivo = validar_password_nueva(datos.nueva)
    if motivo:
        raise HTTPException(422, motivo)
    if datos.nueva == datos.actual:
        raise HTTPException(422,
                            "La contraseña nueva debe ser distinta de la actual")

    operador.password_hash = hash_password(datos.nueva)
    operador.token_version = (operador.token_version or 0) + 1
    registrar(session, "sesion.cambio_password", usuario=operador.username,
              objetivo=operador.username, request=request)
    session.commit()
    session.refresh(operador)
    log.info("El usuario %s cambio su contrasena", operador.username)
    return _sesion(operador, response, request)
