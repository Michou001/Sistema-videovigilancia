"""Arranque de la API con la configuracion del .env, con o sin HTTPS.

    python -m api

Lee del .env (o del entorno):

    API_HOST=0.0.0.0            interfaz donde escucha
    API_PORT=8000
    SSL_CERTFILE=data/tls/servidor.crt   con estas dos, sirve por HTTPS
    SSL_KEYFILE=data/tls/servidor.key    (genera un par con tools/generar_certificado.py)
    FORWARDED_ALLOW_IPS=127.0.0.1        proxys de confianza (Caddy, nginx) cuyas
                                         cabeceras X-Forwarded-* se respetan

Por que HTTPS aunque sea una red interna: por esa conexion pasan contrasenas,
fotos de personas y video en vivo. En una LAN compartida (una escuela, un
edificio de oficinas) cualquiera en la misma red puede capturar HTTP en claro.

Es UN solo proceso a proposito (sin --workers): el buffer del video en vivo,
el canal de alertas y el limite de intentos de login viven en memoria. Para
varios procesos hace falta REDIS_URL (ver api/hub.py y api/preview.py).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))


def _ruta(valor: str) -> str:
    ruta = Path(valor)
    return str(ruta if ruta.is_absolute() else RAIZ / ruta)


def main() -> int:
    import uvicorn

    from api.config import get_config

    get_config()  # carga el .env en el entorno
    host = os.getenv("API_HOST", "0.0.0.0")
    puerto = int(os.getenv("API_PORT", "8000"))
    cert = os.getenv("SSL_CERTFILE", "").strip()
    llave = os.getenv("SSL_KEYFILE", "").strip()
    opciones: dict = {}
    if cert or llave:
        if not (cert and llave):
            print("[x] SSL_CERTFILE y SSL_KEYFILE van juntos.", file=sys.stderr)
            return 1
        cert, llave = _ruta(cert), _ruta(llave)
        for f in (cert, llave):
            if not Path(f).is_file():
                print(f"[x] No existe {f}. Generalo con: python tools/generar_certificado.py",
                      file=sys.stderr)
                return 1
        opciones.update(ssl_certfile=cert, ssl_keyfile=llave)

    esquema = "https" if opciones else "http"
    print(f"  Dashboard en {esquema}://{'localhost' if host in ('0.0.0.0', '::') else host}:{puerto}")
    uvicorn.run(
        "api.main:app",
        host=host,
        port=puerto,
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        **opciones,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
