"""Dependencias de FastAPI: sesion de BD, autenticacion y permisos."""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlmodel import Session, select

from api.config import get_config
from api.database import engine, get_session
from api.models import Operator
from api.security import COOKIE_SESION, decodificar_token, token_ingesta_valido

SesionBD = Annotated[Session, Depends(get_session)]

# Jerarquia de roles. Cada uno puede todo lo del anterior:
#   viewer    mira: video en vivo, detecciones, alertas, registro.
#   operator  ademas atiende: cierra alertas, corrige lecturas, exporta reportes.
#   admin     ademas administra: lista negra, camaras, zonas, usuarios, bitacora.
ROLES = ("viewer", "operator", "admin")
NIVEL = {rol: i for i, rol in enumerate(ROLES)}


def _extraer_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    partes = authorization.split(None, 1)
    if len(partes) == 2 and partes[0].lower() == "bearer":
        return partes[1].strip()
    return authorization.strip()


def operador_por_token(session: Session, token: Optional[str]) -> Optional[Operator]:
    """El operador dueno de un JWT vigente, o None.

    Ademas de la firma y la caducidad se comprueba contra la base de datos:
    que el usuario siga activo y que el token sea de su version de
    credenciales actual. Asi desactivar a alguien o cambiarle la contrasena
    le cierra la sesion en ese momento, no dentro de JWT_HOURS.
    """
    if not token:
        return None
    datos = decodificar_token(token)
    if not datos:
        return None
    operador = session.exec(
        select(Operator).where(Operator.username == datos.get("sub"))
    ).first()
    if operador is None or not operador.active:
        return None
    if int(datos.get("ver", 0)) != int(operador.token_version or 0):
        return None
    return operador


def falta_2fa(operador: Operator) -> bool:
    """Su rol exige verificacion en dos pasos (EXIGIR_2FA) y aun no la activo."""
    return operador.role in get_config().exigir_2fa and not operador.totp_activo


def _exigir_2fa(operador: Operator, ruta: str) -> None:
    """Sin la verificacion en dos pasos que su rol exige, solo puede usar
    /api/auth/ (darla de alta, ver su sesion, salir). Todo lo demas, 403."""
    if falta_2fa(operador) and not ruta.startswith("/api/auth/"):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Activa la verificación en dos pasos para continuar.",
                            headers={"X-Requiere-2FA": "1"})


def operador_actual(
    session: SesionBD,
    request: Request,
    authorization: Annotated[Optional[str], Header()] = None,
) -> Operator:
    """Valida el JWT del dashboard (cabecera Authorization) y devuelve el operador.

    Solo acepta la cabecera, nunca la cookie: es la dependencia de todos los
    endpoints que CAMBIAN algo, y una cabecera no la puede poner otro sitio web
    (proteccion contra CSRF).
    """
    token = _extraer_bearer(authorization)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Falta el token de sesión")
    operador = operador_por_token(session, token)
    if operador is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Sesión inválida o expirada. Vuelve a iniciar sesión.")
    _exigir_2fa(operador, request.url.path)
    return operador


OperadorActual = Annotated[Operator, Depends(operador_actual)]


def token_de_lectura(request: Request) -> Optional[str]:
    """Token para una peticion de SOLO LECTURA hecha por el navegador sin
    JavaScript de por medio (<img>, <video>, WebSocket): cabecera, cookie de
    sesion o, como ultimo recurso, ?token= (compatibilidad con clientes
    viejos)."""
    return (_extraer_bearer(request.headers.get("authorization"))
            or request.cookies.get(COOKIE_SESION)
            or request.query_params.get("token"))


def operador_lectura(request: Request) -> Operator:
    # Sesion de BD propia y breve, no la de la peticion: el video en vivo es
    # una respuesta que dura horas, y una sesion atada a ella retendria una
    # conexion del pool todo ese tiempo.
    with Session(engine) as session:
        operador = operador_por_token(session, token_de_lectura(request))
        if operador is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesión inválida o expirada")
        _exigir_2fa(operador, request.url.path)
        session.expunge(operador)
        return operador


OperadorLectura = Annotated[Operator, Depends(operador_lectura)]


def operador_de_websocket(websocket) -> Optional[Operator]:
    """Mismo criterio que `operador_lectura`, para el handshake del WebSocket
    (que no pasa por el sistema de dependencias de las rutas HTTP)."""
    token = (websocket.cookies.get(COOKIE_SESION)
             or websocket.query_params.get("token"))
    with Session(engine) as session:
        operador = operador_por_token(session, token)
        if operador is None or falta_2fa(operador):
            return None
        session.expunge(operador)
        return operador


def requerir_rol(minimo: str):
    """Dependencia que exige al menos el rol `minimo`."""
    requerido = NIVEL[minimo]

    def _verificar(operador: OperadorActual) -> Operator:
        if NIVEL.get(operador.role, -1) < requerido:
            nombres = {"operator": "operador", "admin": "administrador"}
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"Se requiere rol de {nombres.get(minimo, minimo)}")
        return operador

    return _verificar


# Atender alertas, corregir lecturas y exportar reportes: el operador del turno.
Operador = Annotated[Operator, Depends(requerir_rol("operator"))]

# Modificar la lista negra es una accion con consecuencias: puede hacer que el
# sistema senale a una persona o a un vehiculo. Igual con camaras y usuarios.
Admin = Annotated[Operator, Depends(requerir_rol("admin"))]


def requerir_admin(operador: OperadorActual) -> Operator:
    """Compatibilidad: algunas rutas la importan por nombre."""
    return requerir_rol("admin")(operador)


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
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token de ingesta inválido")
