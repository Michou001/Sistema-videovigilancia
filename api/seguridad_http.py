"""Cabeceras de seguridad HTTP para todas las respuestas.

Middleware ASGI puro (no BaseHTTPMiddleware) a proposito: el video en vivo es
una respuesta que no termina nunca, y BaseHTTPMiddleware la envuelve de una
forma que retrasa o acumula los fragmentos. Aqui solo se agregan cabeceras al
inicio de la respuesta y el cuerpo pasa intacto.

Lo que se protege:
  - Content-Security-Policy: el navegador solo ejecuta scripts de este
    servidor y de los CDN declarados. Un XSS que lograra inyectar HTML no
    podria correr un <script> ni un onclick="".
  - X-Frame-Options / frame-ancestors: nadie puede meter el dashboard en un
    iframe de otro sitio para engañar al operador (clickjacking).
  - nosniff, Referrer-Policy, Permissions-Policy: endurecimiento estandar.
  - Strict-Transport-Security: solo si la peticion llego por HTTPS.
"""

from __future__ import annotations

from typing import Iterable

# Origenes externos de los que el dashboard carga codigo o recursos. Todos con
# version fija en el HTML. Si la red de las camaras no tiene internet, la
# pagina funciona igual (sin iconos ni mapa de calles).
CDN_SCRIPTS = ("https://cdn.jsdelivr.net", "https://cdnjs.cloudflare.com")
CDN_ESTILOS = ("https://cdnjs.cloudflare.com",)
MOSAICOS_MAPA = ("https://tile.openstreetmap.org", "https://*.tile.openstreetmap.org")


def politica_csp(host: str, extra_connect: Iterable[str] = ()) -> str:
    conexiones = ["'self'"]
    if host:
        conexiones += [f"ws://{host}", f"wss://{host}"]
    conexiones += list(extra_connect)
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self' {' '.join(CDN_SCRIPTS)}",
        # 'unsafe-inline' SOLO en estilos: el HTML usa atributos style="" para
        # detalles de maquetacion. Un estilo inyectado no ejecuta codigo.
        f"style-src 'self' 'unsafe-inline' {' '.join(CDN_ESTILOS)}",
        f"img-src 'self' data: blob: {' '.join(MOSAICOS_MAPA)}",
        "media-src 'self' blob:",
        f"connect-src {' '.join(conexiones)}",
        "font-src 'self' data:",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])


class CabecerasSeguridad:
    def __init__(self, app, extra_connect: Iterable[str] = ()) -> None:
        self.app = app
        self.extra_connect = tuple(extra_connect)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        host = ""
        for nombre, valor in scope.get("headers", []):
            if nombre == b"host":
                host = valor.decode("latin-1")
                break
        https = scope.get("scheme") == "https"

        async def _send(mensaje):
            if mensaje["type"] == "http.response.start":
                cabeceras = list(mensaje.get("headers", []))
                existentes = {k.lower() for k, _ in cabeceras}

                def poner(nombre: str, valor: str) -> None:
                    clave = nombre.lower().encode("latin-1")
                    if clave not in existentes:
                        cabeceras.append((clave, valor.encode("latin-1")))

                poner("X-Content-Type-Options", "nosniff")
                poner("X-Frame-Options", "DENY")
                poner("Referrer-Policy", "no-referrer")
                poner("Permissions-Policy",
                      "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
                poner("Cross-Origin-Opener-Policy", "same-origin")
                poner("Content-Security-Policy", politica_csp(host, self.extra_connect))
                if https:
                    poner("Strict-Transport-Security", "max-age=31536000")
                mensaje = {**mensaje, "headers": cabeceras}
            await send(mensaje)

        await self.app(scope, receive, _send)
