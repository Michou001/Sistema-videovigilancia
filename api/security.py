"""Autenticacion: hash de contrasenas y JWT para el dashboard."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from passlib.context import CryptContext

from api.config import get_config

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITMO = "HS256"


def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verificar_password(password: str, hash_guardado: str) -> bool:
    try:
        return _pwd.verify(password, hash_guardado)
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
