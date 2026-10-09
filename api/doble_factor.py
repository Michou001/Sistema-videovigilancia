"""Verificacion en dos pasos (TOTP, RFC 6238): Google Authenticator,
Microsoft Authenticator, Authy, 1Password y cualquier app compatible.

Por que TOTP y no SMS ni correo: el codigo se calcula en el celular a partir
de un secreto compartido y la hora, sin red. Funciona aunque el centro de
monitoreo no tenga salida a internet, y no depende de un proveedor externo.

El algoritmo esta escrito aqui (son 15 lineas sobre hmac de la biblioteca
estandar) en vez de traer una dependencia mas: menos paquetes de terceros es
menos superficie de cadena de suministro. Se comprueba contra los vectores
de prueba del propio RFC 6238 (tests/test_doble_factor.py).

Que se guarda en la base de datos:
  - el secreto, CIFRADO con AES-GCM. La llave vive fuera de la base
    (TOTP_KEY en el entorno o data/.totp_key), asi que una copia robada de la
    base sola no basta para generar codigos.
  - el ultimo paso de tiempo aceptado: un codigo ya usado no se acepta dos
    veces (alguien que lo vea por encima del hombro no puede reutilizarlo).
  - los codigos de respaldo, solo como hash SHA-256: se muestran una vez.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from typing import Optional
from urllib.parse import quote

from api.config import BASE_DIR

PERIODO = 30          # segundos por codigo
DIGITOS = 6
# Pasos de tolerancia hacia atras y hacia adelante: cubre ~30 s de reloj
# desfasado en el celular o en el servidor sin abrir mucho la ventana.
TOLERANCIA = 1
EMISOR = "GOSS IP"
CODIGOS_RESPALDO = 10


# --------------------------------------------------------------------------
# TOTP
# --------------------------------------------------------------------------

def generar_secreto() -> str:
    """160 bits aleatorios en base32 (lo que esperan las apps)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _llave_secreto(secreto: str) -> bytes:
    relleno = "=" * (-len(secreto) % 8)
    return base64.b32decode(secreto.upper() + relleno)


def codigo(secreto: str, paso: int, digitos: int = DIGITOS, algoritmo: str = "sha1") -> str:
    """HOTP (RFC 4226) del contador `paso`."""
    mac = hmac.new(_llave_secreto(secreto), struct.pack(">Q", paso), algoritmo).digest()
    corte = mac[-1] & 0x0F
    valor = struct.unpack(">I", mac[corte:corte + 4])[0] & 0x7FFFFFFF
    return str(valor % (10 ** digitos)).zfill(digitos)


def paso_actual(ahora: Optional[float] = None) -> int:
    return int((time.time() if ahora is None else ahora) // PERIODO)


def verificar(secreto: str, presentado: str, ultimo_paso: Optional[int] = None,
              ahora: Optional[float] = None) -> Optional[int]:
    """El paso de tiempo que corresponde al codigo, o None si no vale.

    Rechaza un codigo de un paso igual o anterior al ultimo aceptado: cada
    codigo sirve una sola vez.
    """
    limpio = "".join(c for c in presentado or "" if c.isdigit())
    if len(limpio) != DIGITOS:
        return None
    base = paso_actual(ahora)
    for desfase in range(-TOLERANCIA, TOLERANCIA + 1):
        paso = base + desfase
        if ultimo_paso is not None and paso <= ultimo_paso:
            continue
        if hmac.compare_digest(codigo(secreto, paso), limpio):
            return paso
    return None


def uri_otpauth(usuario: str, secreto: str, emisor: str = EMISOR) -> str:
    """Lo que codifica el QR. Las apps leen emisor y cuenta de aqui."""
    etiqueta = quote(f"{emisor}:{usuario}")
    return (f"otpauth://totp/{etiqueta}?secret={secreto}&issuer={quote(emisor)}"
            f"&algorithm=SHA1&digits={DIGITOS}&period={PERIODO}")


def qr_svg(texto: str) -> str:
    """QR como data URI SVG, listo para un <img src="...">. Se genera en el
    servidor para no mandar el secreto a ningun servicio externo de QR."""
    import segno

    return segno.make(texto, error="m").svg_data_uri(scale=5, border=2, dark="#061430")


# --------------------------------------------------------------------------
# Cifrado del secreto en la base de datos
# --------------------------------------------------------------------------

_llave_cache: Optional[bytes] = None


def _llave_cifrado() -> bytes:
    """32 bytes para AES-256-GCM: de TOTP_KEY o de data/.totp_key.

    Mismo criterio que JWT_SECRET y API_TOKEN (ver api/config.py): si no se
    define, se genera una vez y se guarda en un archivo que .gitignore cubre.
    En Docker conviene definir TOTP_KEY: si se pierde la llave, cada usuario
    tiene que volver a dar de alta su app.
    """
    global _llave_cache
    if _llave_cache is not None:
        return _llave_cache
    texto = os.getenv("TOTP_KEY", "").strip()
    if not texto:
        archivo = BASE_DIR / "data" / ".totp_key"
        if archivo.exists():
            texto = archivo.read_text(encoding="utf-8").strip()
        else:
            texto = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
            archivo.parent.mkdir(parents=True, exist_ok=True)
            archivo.write_text(texto, encoding="utf-8")
    try:
        llave = base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))
    except ValueError:
        llave = b""
    if len(llave) != 32:
        # Una frase cualquiera tambien sirve: se deriva a 32 bytes.
        llave = hashlib.sha256(texto.encode("utf-8")).digest()
    _llave_cache = llave
    return llave


def cifrar(secreto: str, usuario: str) -> str:
    """El usuario va como dato asociado: un secreto cifrado copiado a la fila
    de otro usuario no se descifra."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    datos = AESGCM(_llave_cifrado()).encrypt(nonce, secreto.encode("ascii"), usuario.encode("utf-8"))
    return "v1:" + base64.urlsafe_b64encode(nonce + datos).decode("ascii")


def descifrar(guardado: str, usuario: str) -> Optional[str]:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not guardado or not guardado.startswith("v1:"):
        return None
    try:
        crudo = base64.urlsafe_b64decode(guardado[3:])
        claro = AESGCM(_llave_cifrado()).decrypt(crudo[:12], crudo[12:], usuario.encode("utf-8"))
    except (InvalidTag, ValueError):
        return None
    return claro.decode("ascii")


# --------------------------------------------------------------------------
# Codigos de respaldo (celular perdido)
# --------------------------------------------------------------------------

_ALFABETO = "abcdefghjkmnpqrstuvwxyz23456789"   # sin 0/o, 1/l/i: se dictan sin confusion


def _hash_respaldo(codigo_respaldo: str) -> str:
    limpio = "".join(c for c in codigo_respaldo.lower() if c.isalnum())
    return hashlib.sha256(limpio.encode("ascii", "ignore")).hexdigest()


def generar_respaldo() -> tuple[list[str], str]:
    """Codigos para mostrar UNA vez y el JSON con sus hashes para guardar.

    10 caracteres de un alfabeto de 31 son ~50 bits por codigo: con el limite
    de intentos del login no se adivinan, y por eso basta SHA-256 (bcrypt
    haria lento revisar diez hashes en cada intento).
    """
    codigos = []
    for _ in range(CODIGOS_RESPALDO):
        crudo = "".join(secrets.choice(_ALFABETO) for _ in range(10))
        codigos.append(f"{crudo[:5]}-{crudo[5:]}")
    return codigos, json.dumps([_hash_respaldo(c) for c in codigos])


def usar_respaldo(guardado_json: Optional[str], presentado: str) -> Optional[str]:
    """Si el codigo es valido, el JSON sin ese codigo (ya no sirve); si no, None."""
    try:
        hashes = json.loads(guardado_json or "[]")
    except ValueError:
        return None
    buscado = _hash_respaldo(presentado or "")
    for i, h in enumerate(hashes):
        if hmac.compare_digest(h, buscado):
            return json.dumps(hashes[:i] + hashes[i + 1:])
    return None


def respaldos_restantes(guardado_json: Optional[str]) -> int:
    try:
        return len(json.loads(guardado_json or "[]"))
    except ValueError:
        return 0
