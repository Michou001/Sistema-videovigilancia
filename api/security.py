"""Autenticacion: hash de contrasenas y JWT para el dashboard."""

from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt

from api.config import get_config

ALGORITMO = "HS256"

# bcrypt solo usa los primeros 72 bytes de la contrasena. Las versiones
# recientes de la libreria lanzan error con una mas larga en vez de truncarla;
# se trunca aqui para que el comportamiento no dependa de la version instalada
# y para seguir validando los hashes que se crearon cuando se truncaba solo.
_MAX_BYTES = 72


def _bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_MAX_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_bytes(password), bcrypt.gensalt(rounds=12)).decode("ascii")


# Hash de una contrasena que nadie tiene. Se compara contra el cuando el
# usuario no existe, para que esa respuesta tarde lo mismo que una contrasena
# equivocada: si no, medir el tiempo de respuesta dice que usuarios existen.
_HASH_SEÑUELO = bcrypt.hashpw(secrets.token_bytes(16), bcrypt.gensalt(rounds=12)).decode("ascii")


def verificar_password_o_señuelo(password: str, hash_guardado: Optional[str]) -> bool:
    """verificar_password, gastando el mismo tiempo si no hay hash."""
    if not hash_guardado:
        verificar_password(password, _HASH_SEÑUELO)
        return False
    return verificar_password(password, hash_guardado)


def verificar_password(password: str, hash_guardado: str) -> bool:
    try:
        return bcrypt.checkpw(_bytes(password), hash_guardado.encode("ascii"))
    except Exception:  # noqa: BLE001 - un hash corrupto no debe tumbar el login
        return False


def crear_token(username: str, role: str, version: int = 0) -> str:
    cfg = get_config()
    ahora = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "role": role,
        # Version de credenciales del usuario (Operator.token_version). Al
        # cambiar contrasena o rol sube, y este token deja de valer aunque no
        # haya caducado.
        "ver": version,
        "iat": ahora,
        "exp": ahora + timedelta(hours=cfg.jwt_hours),
    }
    return jwt.encode(payload, cfg.jwt_secret, algorithm=ALGORITMO)


def decodificar_token(token: str) -> Optional[dict]:
    """Datos de un token de SESION vigente, o None.

    Un token con "tipo" (el desafio del segundo paso) no es una sesion: se
    rechaza aqui para que nadie entre con solo la contrasena.
    """
    try:
        datos = jwt.decode(token, get_config().jwt_secret, algorithms=[ALGORITMO])
    except jwt.PyJWTError:
        return None
    return None if datos.get("tipo") else datos


# El desafio del segundo paso: prueba de que la contrasena fue correcta, valida
# unos minutos y solo para /api/auth/login/2fa.
DESAFIO_2FA_MIN = 5


def crear_desafio_2fa(username: str, version: int = 0) -> str:
    ahora = datetime.now(timezone.utc)
    payload = {"sub": username, "tipo": "2fa", "ver": version, "iat": ahora,
               "exp": ahora + timedelta(minutes=DESAFIO_2FA_MIN)}
    return jwt.encode(payload, get_config().jwt_secret, algorithm=ALGORITMO)


def leer_desafio_2fa(token: str) -> Optional[dict]:
    try:
        datos = jwt.decode(token, get_config().jwt_secret, algorithms=[ALGORITMO])
    except jwt.PyJWTError:
        return None
    return datos if datos.get("tipo") == "2fa" else None


def token_ingesta_valido(presentado: str) -> bool:
    """Compara el token del worker con comparacion de tiempo constante.

    `==` sobre cadenas sale antes en el primer caracter distinto, y esa
    diferencia de tiempo es medible: permite adivinar el token caracter por
    caracter. compare_digest siempre tarda lo mismo.
    """
    return secrets.compare_digest(presentado or "", get_config().ingest_token)


class LimiteIntentos:
    """Frena la adivinacion de contrasenas por fuerza bruta.

    Tras `maximo` intentos fallidos desde la misma IP dentro de `ventana`
    segundos, esa IP espera hasta que el mas viejo salga de la ventana. Vive en
    memoria del proceso: suficiente para un despliegue de un solo proceso (ver
    docs/despliegue.md) y sin dependencias.
    """

    def __init__(self, maximo: int = 5, ventana: float = 300.0) -> None:
        self.maximo = maximo
        self.ventana = ventana
        self._fallos: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _limpiar(self, clave: str, ahora: float) -> deque[float]:
        cola = self._fallos[clave]
        while cola and ahora - cola[0] > self.ventana:
            cola.popleft()
        return cola

    def espera(self, clave: str) -> float:
        """Segundos que debe esperar esta clave antes de intentar; 0 si puede."""
        ahora = time.monotonic()
        with self._lock:
            cola = self._limpiar(clave, ahora)
            if len(cola) < self.maximo:
                return 0.0
            return max(0.0, self.ventana - (ahora - cola[0]))

    def fallo(self, clave: str) -> None:
        with self._lock:
            self._limpiar(clave, time.monotonic()).append(time.monotonic())

    def exito(self, clave: str) -> None:
        with self._lock:
            self._fallos.pop(clave, None)


def _crear_limite():
    """En memoria con un solo proceso de API; en Redis con varios, para que el
    atacante no tenga 5 intentos POR PROCESO (ver api/redis_compartido.py)."""
    get_config()  # carga el .env: REDIS_URL puede venir de ahi
    from api.redis_compartido import LimiteIntentosRedis, cliente_sync, url_redis

    url = url_redis()
    if url:
        return LimiteIntentosRedis(cliente_sync(url))
    return LimiteIntentos()


limite_login = _crear_limite()


# --------------------------------------------------------------------------
# Cookie de sesion
# --------------------------------------------------------------------------
#
# Un <img>, un <video> y el WebSocket del navegador no pueden mandar la
# cabecera Authorization. Antes la solucion era poner el token en la URL
# (?token=...), y la URL de una foto de evidencia o del video en vivo terminaba
# en el historial del navegador, en logs de proxys y copiada en reportes.
#
# La cookie resuelve eso sin exponer el token:
#   - HttpOnly: el JavaScript de la pagina no la puede leer (un XSS no la roba).
#   - SameSite=Strict: el navegador no la manda en peticiones que se originan
#     en otro sitio, asi que no sirve para CSRF.
#   - Solo se acepta en peticiones de LECTURA (evidencia, video, WebSocket).
#     Todo lo que cambia datos sigue exigiendo la cabecera Authorization, que
#     otro sitio no puede poner.

COOKIE_SESION = "goss_sesion"


def poner_cookie_sesion(response, token: str, segura: bool) -> None:
    response.set_cookie(
        COOKIE_SESION,
        token,
        max_age=get_config().jwt_hours * 3600,
        httponly=True,
        samesite="strict",
        secure=segura,
        path="/",
    )


def borrar_cookie_sesion(response) -> None:
    response.delete_cookie(COOKIE_SESION, path="/", samesite="strict", httponly=True)


def es_https(request) -> bool:
    """Si la peticion llego por HTTPS (directo o a traves de un proxy de
    confianza que uvicorn ya resolvio con --proxy-headers)."""
    return getattr(getattr(request, "url", None), "scheme", "") in {"https", "wss"}


def validar_password_nueva(password: str) -> str | None:
    """Motivo por el que una contrasena nueva no sirve, o None si sirve.

    Largo minimo y nada mas: las reglas de "una mayuscula y un simbolo" llevan
    a contrasenas previsibles (Password1!). Diez caracteres de una frase son
    mas dificiles de adivinar y mas faciles de recordar.
    """
    if len(password) < 10:
        return "La contraseña debe tener al menos 10 caracteres."
    if len(password.encode("utf-8")) > _MAX_BYTES:
        return f"La contraseña no puede pasar de {_MAX_BYTES} bytes."
    if password.strip() != password:
        return "La contraseña no puede empezar ni terminar con espacios."
    return None
