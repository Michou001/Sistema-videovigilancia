"""Login del dashboard."""

from __future__ import annotations

import logging
import math

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import select

from api.deps import OperadorActual, SesionBD
from api.models import Operator
from api.security import crear_token, limite_login, verificar_password

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Credenciales(BaseModel):
    username: str
    password: str


class Sesion(BaseModel):
    token: str
    username: str
    display_name: str
    role: str


@router.post("/login", response_model=Sesion)
def login(datos: Credenciales, session: SesionBD, request: Request):
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
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario o contraseña incorrectos")

    limite_login.exito(ip)
    return Sesion(
        token=crear_token(operador.username, operador.role),
        username=operador.username,
        display_name=operador.display_name,
        role=operador.role,
    )


@router.get("/me", response_model=Sesion)
def yo(operador: OperadorActual):
    """Permite al frontend verificar que su token sigue vivo al recargar."""
    return Sesion(token="", username=operador.username,
                  display_name=operador.display_name, role=operador.role)
