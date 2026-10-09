"""Login del dashboard (con verificacion en dos pasos), cierre de sesion,
cambio de contrasena y alta de la app autenticadora."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlmodel import select

from api import doble_factor
from api.auditoria import registrar
from api.config import get_config
from api.deps import OperadorActual, OperadorLectura, SesionBD, _extraer_bearer, falta_2fa
from api.models import Operator
from api.security import (
    borrar_cookie_sesion,
    crear_desafio_2fa,
    crear_token,
    es_https,
    leer_desafio_2fa,
    hash_password,
    limite_login,
    poner_cookie_sesion,
    validar_password_nueva,
    verificar_password,
    verificar_password_o_señuelo,
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
    # Con verificacion en dos pasos, el login con contrasena NO da sesion:
    # devuelve requiere_2fa y un desafio para canjear con el codigo de la app.
    requiere_2fa: bool = False
    desafio: str = ""
    # Su rol exige la verificacion en dos pasos y aun no la activa: el
    # dashboard le pide darla de alta antes de dejarlo trabajar.
    debe_activar_2fa: bool = False
    totp_activo: bool = False


class SegundoPaso(BaseModel):
    desafio: str = Field(max_length=2000)
    codigo: str = Field(max_length=32, description="6 digitos de la app o un código de respaldo")


class ConPassword(BaseModel):
    password: str = Field(max_length=200)


class ConCodigo(BaseModel):
    codigo: str = Field(max_length=32)


class ConPasswordYCodigo(BaseModel):
    password: str = Field(max_length=200)
    codigo: str = Field(max_length=32)


class CambioPassword(BaseModel):
    actual: str = Field(max_length=200)
    nueva: str = Field(max_length=200)


def _datos_usuario(operador: Operator) -> dict:
    return {"username": operador.username, "display_name": operador.display_name,
            "role": operador.role, "totp_activo": bool(operador.totp_activo),
            "debe_activar_2fa": falta_2fa(operador)}


def _sesion(operador: Operator, response: Response, request: Request) -> Sesion:
    token = crear_token(operador.username, operador.role, operador.token_version or 0)
    poner_cookie_sesion(response, token, segura=es_https(request))
    return Sesion(token=token, **_datos_usuario(operador))


def _ip(request: Request) -> str:
    return request.client.host if request.client else "desconocida"


def _frenar_si_abusa(ip: str) -> None:
    espera = limite_login.espera(ip)
    if espera > 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Demasiados intentos fallidos. Intenta de nuevo en {math.ceil(espera / 60)} min.",
            headers={"Retry-After": str(math.ceil(espera))},
        )


def _verificar_codigo(operador: Operator, codigo: str, *, respaldo: bool) -> Optional[str]:
    """Comprueba un codigo de la app (o de respaldo si `respaldo`) contra el
    secreto del operador y, si vale, deja anotado que ya se uso. Devuelve
    "totp", "respaldo" o None."""
    secreto = doble_factor.descifrar(operador.totp_secreto or "", operador.username)
    if secreto:
        paso = doble_factor.verificar(secreto, codigo, operador.totp_ultimo_paso)
        if paso is not None:
            operador.totp_ultimo_paso = paso
            return "totp"
    if respaldo and operador.totp_activo:
        restantes = doble_factor.usar_respaldo(operador.totp_respaldo_json, codigo)
        if restantes is not None:
            operador.totp_respaldo_json = restantes
            return "respaldo"
    return None


@router.post("/login", response_model=Sesion)
def login(datos: Credenciales, session: SesionBD, request: Request, response: Response):
    ip = _ip(request)
    _frenar_si_abusa(ip)

    operador = session.exec(
        select(Operator).where(Operator.username == datos.username)
    ).first()

    # Se responde lo mismo si el usuario no existe o si la contrasena es
    # incorrecta: distinguirlos permite enumerar usuarios validos.
    hash_guardado = operador.password_hash if operador is not None and operador.active else None
    if not verificar_password_o_señuelo(datos.password, hash_guardado):
        limite_login.fallo(ip)
        log.warning("Login fallido para '%s' desde %s", datos.username, ip)
        registrar(session, "sesion.login_fallido", usuario=None,
                  objetivo=datos.username[:64], request=request, confirmar=True)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario o contraseña incorrectos")

    if operador.totp_activo:
        # La contrasena fue correcta, pero falta el segundo paso. El contador
        # de intentos fallidos NO se limpia todavia: se limpia al terminar.
        return Sesion(token="", requiere_2fa=True,
                      desafio=crear_desafio_2fa(operador.username, operador.token_version or 0),
                      **_datos_usuario(operador))

    limite_login.exito(ip)
    operador.last_login = datetime.now(timezone.utc)
    registrar(session, "sesion.login", usuario=operador.username, request=request)
    session.commit()
    session.refresh(operador)
    return _sesion(operador, response, request)


@router.post("/login/2fa", response_model=Sesion)
def login_segundo_paso(datos: SegundoPaso, session: SesionBD, request: Request,
                       response: Response):
    """Canjea el desafio del login y el codigo de la app (o uno de respaldo)
    por la sesion. Comparte el limite de intentos con el login."""
    ip = _ip(request)
    _frenar_si_abusa(ip)

    desafio = leer_desafio_2fa(datos.desafio)
    operador = None
    if desafio:
        operador = session.exec(
            select(Operator).where(Operator.username == desafio.get("sub"))
        ).first()
    if (operador is None or not operador.active or not operador.totp_activo
            or int(desafio.get("ver", 0)) != int(operador.token_version or 0)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "El inicio de sesión expiró. Vuelve a escribir tu contraseña.")

    metodo = _verificar_codigo(operador, datos.codigo, respaldo=True)
    if metodo is None:
        limite_login.fallo(ip)
        log.warning("Segundo paso fallido para '%s' desde %s", operador.username, ip)
        registrar(session, "sesion.2fa_fallido", usuario=None, objetivo=operador.username,
                  request=request, confirmar=True)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Código incorrecto o ya usado")

    limite_login.exito(ip)
    operador.last_login = datetime.now(timezone.utc)
    detalle = {"segundo_paso": metodo}
    if metodo == "respaldo":
        detalle["respaldos_restantes"] = doble_factor.respaldos_restantes(
            operador.totp_respaldo_json)
    registrar(session, "sesion.login", usuario=operador.username, detalle=detalle,
              request=request)
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


@router.get("/verificar", status_code=204)
def verificar(_: OperadorLectura) -> Response:
    """Para el proxy (Caddy forward_auth): 204 si la peticion trae una sesion
    valida (cookie o cabecera), 401 si no. Asi go2rtc, que no tiene
    contrasena propia, solo sirve video a quien entro al dashboard."""
    return Response(status_code=204)


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
    return Sesion(token="", **_datos_usuario(operador))


@router.post("/password", response_model=Sesion)
def cambiar_password(datos: CambioPassword, operador: OperadorActual, session: SesionBD,
                     request: Request, response: Response):
    """Cambio de la contrasena propia. Cierra todas las demas sesiones del
    usuario (sube token_version) y devuelve una sesion nueva para esta."""
    _comprobar_password(operador, datos.actual, request, "La contraseña actual no es correcta")
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


# --------------------------------------------------------------------------
# Verificacion en dos pasos: alta, baja y codigos de respaldo
# --------------------------------------------------------------------------

def _comprobar_password(operador: Operator, password: str, request: Request,
                        mensaje: str = "La contraseña no es correcta") -> None:
    """Contrasena de quien YA tiene sesion (cambiarla, alta o baja del 2FA).
    Cuenta para el mismo limite de intentos que el login: con una sesion
    abierta en un equipo descuidado no se puede adivinar la contrasena aqui."""
    ip = _ip(request)
    _frenar_si_abusa(ip)
    if not verificar_password(password, operador.password_hash):
        limite_login.fallo(ip)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, mensaje)


@router.get("/2fa")
def estado_2fa(operador: OperadorActual):
    return {
        "activo": bool(operador.totp_activo),
        "exigido": operador.role in get_config().exigir_2fa,
        "respaldos_restantes": doble_factor.respaldos_restantes(operador.totp_respaldo_json)
        if operador.totp_activo else 0,
    }


@router.post("/2fa/iniciar")
def iniciar_2fa(datos: ConPassword, operador: OperadorActual, session: SesionBD,
                request: Request):
    """Primer paso del alta: genera un secreto nuevo y devuelve el QR. No se
    activa hasta confirmar un codigo (POST /2fa/activar), asi un QR mal
    escaneado no deja a nadie fuera."""
    if operador.totp_activo:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "La verificación en dos pasos ya está activa.")
    _comprobar_password(operador, datos.password, request)
    secreto = doble_factor.generar_secreto()
    operador.totp_secreto = doble_factor.cifrar(secreto, operador.username)
    operador.totp_ultimo_paso = None
    session.commit()
    uri = doble_factor.uri_otpauth(operador.username, secreto)
    # Agrupado de 4 en 4 para teclearlo a mano si la camara del celular falla.
    legible = " ".join(secreto[i:i + 4] for i in range(0, len(secreto), 4))
    return {"secreto": legible, "uri": uri, "qr": doble_factor.qr_svg(uri)}


@router.post("/2fa/activar")
def activar_2fa(datos: ConCodigo, operador: OperadorActual, session: SesionBD,
                request: Request, response: Response):
    """Confirma el alta con un codigo de la app. Devuelve los codigos de
    respaldo (la UNICA vez que se ven) y una sesion nueva: las sesiones que se
    abrieron sin segundo paso se cierran."""
    if operador.totp_activo:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "La verificación en dos pasos ya está activa.")
    if not operador.totp_secreto:
        raise HTTPException(status.HTTP_409_CONFLICT, "Primero genera el código QR.")
    _frenar_si_abusa(_ip(request))
    if _verificar_codigo(operador, datos.codigo, respaldo=False) is None:
        limite_login.fallo(_ip(request))
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "El código no coincide. Revisa que la hora del celular sea automática.")
    codigos, hashes = doble_factor.generar_respaldo()
    operador.totp_activo = True
    operador.totp_respaldo_json = hashes
    operador.token_version = (operador.token_version or 0) + 1
    registrar(session, "sesion.2fa_activada", usuario=operador.username,
              objetivo=operador.username, request=request)
    session.commit()
    session.refresh(operador)
    log.info("El usuario %s activo la verificacion en dos pasos", operador.username)
    return {"codigos_respaldo": codigos, "sesion": _sesion(operador, response, request)}


@router.post("/2fa/desactivar", response_model=Sesion)
def desactivar_2fa(datos: ConPasswordYCodigo, operador: OperadorActual, session: SesionBD,
                   request: Request, response: Response):
    """Pide contrasena Y codigo: alguien con la sesion abierta en un equipo
    descuidado no puede quitarla."""
    if not operador.totp_activo:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "La verificación en dos pasos no está activa.")
    if operador.role in get_config().exigir_2fa:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Tu rol la exige: no se puede desactivar. Pide al "
                            "administrador que la restablezca si cambiaste de celular.")
    _comprobar_password(operador, datos.password, request)
    if _verificar_codigo(operador, datos.codigo, respaldo=True) is None:
        limite_login.fallo(_ip(request))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Código incorrecto o ya usado")
    operador.totp_activo = False
    operador.totp_secreto = None
    operador.totp_ultimo_paso = None
    operador.totp_respaldo_json = None
    operador.token_version = (operador.token_version or 0) + 1
    registrar(session, "sesion.2fa_desactivada", usuario=operador.username,
              objetivo=operador.username, request=request)
    session.commit()
    session.refresh(operador)
    return _sesion(operador, response, request)


@router.post("/2fa/respaldo")
def regenerar_respaldo(datos: ConPasswordYCodigo, operador: OperadorActual,
                       session: SesionBD, request: Request):
    """Codigos de respaldo nuevos; los anteriores dejan de servir."""
    if not operador.totp_activo:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "La verificación en dos pasos no está activa.")
    _comprobar_password(operador, datos.password, request)
    if _verificar_codigo(operador, datos.codigo, respaldo=False) is None:
        limite_login.fallo(_ip(request))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Código incorrecto o ya usado")
    codigos, hashes = doble_factor.generar_respaldo()
    operador.totp_respaldo_json = hashes
    registrar(session, "sesion.2fa_respaldo", usuario=operador.username,
              objetivo=operador.username, request=request)
    session.commit()
    return {"codigos_respaldo": codigos}
