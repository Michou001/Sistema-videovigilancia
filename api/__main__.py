"""Arranque de la API con la configuracion del .env, con o sin HTTPS.

    python -m api

Lee del .env (o del entorno):

    API_HOST=127.0.0.1          interfaz donde escucha. Por defecto SOLO este
                                equipo; 0.0.0.0 la abre a toda la red (ver abajo)
    API_PORT=8000
    SSL_CERTFILE=data/tls/servidor.crt   con estas dos, sirve por HTTPS
    SSL_KEYFILE=data/tls/servidor.key    (genera un par con tools/generar_certificado.py)
    FORWARDED_ALLOW_IPS=127.0.0.1        proxys de confianza (Caddy, nginx) cuyas
                                         cabeceras X-Forwarded-* se respetan

Por que 127.0.0.1 por defecto: una laptop conectada al Wi-Fi de la escuela (o
a una red con IP publica) quedaria con el dashboard visible para cualquiera en
esa red. Abrirlo es una decision explicita (API_HOST=0.0.0.0), y si se abre
sin HTTPS el arranque lo advierte en grande.

Por que HTTPS aunque sea una red interna: por esa conexion pasan contrasenas,
fotos de personas y video en vivo. En una LAN compartida (una escuela, un
edificio de oficinas) cualquiera en la misma red puede capturar HTTP en claro.

Por defecto es UN solo proceso: el buffer del video en vivo, el canal de
alertas y el limite de intentos de login viven en memoria. Para varios
procesos (muchos dashboards o muchas camaras):

    REDIS_URL=redis://localhost:6379/0
    API_WORKERS=4

(ver api/redis_compartido.py).
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


def _es_local(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127.")


def _advertir_red_abierta(host: str, con_tls: bool) -> None:
    """Escuchar en toda la red por HTTP en claro deja pasar contrasenas,
    codigos de verificacion, fotos y video sin cifrar por la red."""
    if _es_local(host) or con_tls:
        return
    # Detras de Caddy/nginx el HTTPS lo pone el proxy (Docker): ahi esta bien.
    if os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1").strip() not in {"", "127.0.0.1"}:
        return
    linea = "!" * 74
    print(f"""
{linea}
  ATENCION: el dashboard escucha en {host} (toda la red) SIN HTTPS.
  Cualquiera conectado a esta red puede abrirlo, y las contrasenas y el
  video viajan sin cifrar. Para uso en un solo equipo deja API_HOST vacio
  (127.0.0.1). Para abrirlo a la red genera un certificado:
      python tools/generar_certificado.py
  y define SSL_CERTFILE / SSL_KEYFILE en el .env (docs/seguridad-red.md).
{linea}
""", file=sys.stderr)


def main() -> int:
    import uvicorn

    from api.config import get_config

    get_config()  # carga el .env en el entorno
    host = os.getenv("API_HOST", "").strip() or "127.0.0.1"
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

    procesos = int(os.getenv("API_WORKERS", "1") or 1)
    if procesos > 1:
        if not os.getenv("REDIS_URL", "").strip():
            # Sin Redis cada proceso tendria su propio canal de alertas, su
            # propio video en vivo y su propio limite de intentos de login.
            print("[x] API_WORKERS > 1 requiere REDIS_URL (ver api/redis_compartido.py).",
                  file=sys.stderr)
            return 1
        opciones["workers"] = procesos

    _advertir_red_abierta(host, "ssl_certfile" in opciones)
    esquema = "https" if "ssl_certfile" in opciones else "http"
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
