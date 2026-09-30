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


def verificar_password(password: str, hash_guardado: str) -> bool:
    try:
        return bcrypt.checkpw(_bytes(password), hash_guardado.encode("ascii"))
    except Exception:  # noqa: BLE001 - un hash corrupto no debe tumbar el login
        return False


def crear_token(username: str, role: str) -> str:
    cfg = get_config()
    ahora = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "role": role,
        "iat": ahora,
        "exp": ahora + timedelta(hours=cfg.jwt_hours),
    }
    return jwt.encode(payload, cfg.jwt_secret, algorithm=ALGORITMO)


def decodificar_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, get_config().jwt_secret, algorithms=[ALGORITMO])
    except jwt.PyJWTError:
        return None


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


limite_login = LimiteIntentos()
