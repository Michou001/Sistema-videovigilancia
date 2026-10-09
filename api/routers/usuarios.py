"""Usuarios del dashboard: alta, edicion, baja logica y restablecer contrasena.

Antes la unica forma de crear un usuario era correr tools/init_plataforma.py
en el servidor, y en la practica todo el turno entraba con la cuenta admin.
Con eso no se puede saber quien cerro una alerta ni quien dio de alta una
placa: la bitacora dice "admin" para todos.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlmodel import col, func, select

from api.auditoria import registrar
from api.deps import ROLES, Admin, SesionBD
from api.models import FechasEnUtc, Operator
from api.security import hash_password, validar_password_nueva

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["usuarios"])

_USUARIO_VALIDO = re.compile(r"[a-z0-9][a-z0-9._\-]{2,31}")


class UsuarioLeido(FechasEnUtc, BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    active: bool
    created_at: datetime
    last_login: Optional[datetime] = None
    totp_activo: bool = False


class AltaUsuario(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(min_length=2, max_length=80)
    role: str = "operator"
    password: str = Field(max_length=200)

    @field_validator("username")
    @classmethod
    def _usuario(cls, v: str) -> str:
        v = v.strip().lower()
        if not _USUARIO_VALIDO.fullmatch(v):
            raise ValueError("solo minúsculas, números, '.', '-' y '_' (3 a 32)")
        return v

    @field_validator("role")
    @classmethod
    def _rol(cls, v: str) -> str:
        if v not in ROLES:
            raise ValueError(f"rol debe ser uno de: {', '.join(ROLES)}")
        return v


class EdicionUsuario(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=2, max_length=80)
    role: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = Field(default=None, max_length=200,
                                    description="Restablece la contraseña")
    restablecer_2fa: bool = Field(
        default=False,
        description="Quita la verificación en dos pasos (celular perdido). Si su "
                    "rol la exige, tendrá que darla de alta otra vez al entrar.")

    @field_validator("role")
    @classmethod
    def _rol(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ROLES:
            raise ValueError(f"rol debe ser uno de: {', '.join(ROLES)}")
        return v


def _admins_activos(session) -> int:
    return session.exec(
        select(func.count()).select_from(Operator)
        .where(Operator.role == "admin", Operator.active)
    ).one()


@router.get("", response_model=list[UsuarioLeido])
def listar(session: SesionBD, _: Admin):
    return session.exec(select(Operator).order_by(col(Operator.username))).all()


@router.post("", response_model=UsuarioLeido, status_code=status.HTTP_201_CREATED)
def crear(datos: AltaUsuario, session: SesionBD, admin: Admin, request: Request):
    motivo = validar_password_nueva(datos.password)
    if motivo:
        raise HTTPException(422, motivo)
    if session.exec(select(Operator).where(Operator.username == datos.username)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, f"El usuario '{datos.username}' ya existe")

    usuario = Operator(username=datos.username, display_name=datos.display_name.strip(),
                       role=datos.role, password_hash=hash_password(datos.password))
    session.add(usuario)
    registrar(session, "usuarios.alta", usuario=admin.username, objetivo=datos.username,
              detalle={"rol": datos.role}, request=request)
    session.commit()
    session.refresh(usuario)
    log.info("Usuario %s (%s) creado por %s", usuario.username, usuario.role, admin.username)
    return usuario


@router.patch("/{usuario_id}", response_model=UsuarioLeido)
def editar(usuario_id: int, datos: EdicionUsuario, session: SesionBD, admin: Admin,
           request: Request):
    usuario = session.get(Operator, usuario_id)
    if usuario is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese usuario")

    deja_de_ser_admin = (usuario.role == "admin" and usuario.active and (
        (datos.role is not None and datos.role != "admin") or datos.active is False))
    if deja_de_ser_admin and _admins_activos(session) <= 1:
        # Sin ningun admin activo nadie puede volver a administrar el sistema
        # desde el dashboard: habria que entrar al servidor a arreglarlo.
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Debe quedar al menos un administrador activo")

    cambios: dict = {}
    invalida_sesiones = False
    if datos.display_name is not None and datos.display_name.strip() != usuario.display_name:
        usuario.display_name = datos.display_name.strip()
        cambios["nombre"] = usuario.display_name
    if datos.role is not None and datos.role != usuario.role:
        cambios["rol"] = f"{usuario.role} → {datos.role}"
        usuario.role = datos.role
        invalida_sesiones = True
    if datos.active is not None and datos.active != usuario.active:
        usuario.active = datos.active
        cambios["activo"] = datos.active
        invalida_sesiones = True
    if datos.password:
        motivo = validar_password_nueva(datos.password)
        if motivo:
            raise HTTPException(422, motivo)
        usuario.password_hash = hash_password(datos.password)
        cambios["contraseña"] = "restablecida"
        invalida_sesiones = True
    if datos.restablecer_2fa and (usuario.totp_activo or usuario.totp_secreto):
        usuario.totp_activo = False
        usuario.totp_secreto = None
        usuario.totp_ultimo_paso = None
        usuario.totp_respaldo_json = None
        cambios["verificación en dos pasos"] = "restablecida"
        invalida_sesiones = True

    if not cambios:
        return usuario
    if invalida_sesiones:
        # Un rol rebajado o una cuenta desactivada no deben seguir operando
        # con la sesion que ya tenian abierta.
        usuario.token_version = (usuario.token_version or 0) + 1
    registrar(session, "usuarios.edicion", usuario=admin.username, objetivo=usuario.username,
              detalle=cambios, request=request)
    session.commit()
    session.refresh(usuario)
    log.info("Usuario %s editado por %s: %s", usuario.username, admin.username, cambios)
    return usuario
