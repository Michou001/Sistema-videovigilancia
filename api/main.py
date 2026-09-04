"""Plataforma web del sistema de videovigilancia.

    uvicorn api.main:app --host 0.0.0.0 --port 8000

El dashboard queda en http://localhost:8000
La documentacion interactiva de la API en http://localhost:8000/docs
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from api.config import get_config  # noqa: E402
from api.database import init_db  # noqa: E402
from api.hub import hub  # noqa: E402
from api.routers import alerts, auth, blacklist, events, faces, preview  # noqa: E402
from api.security import decodificar_token  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("api")


class _SinRuidoDePreview(logging.Filter):
    """Quita del log de acceso las subidas de frames de la vista en vivo.

    Cada frame es un POST, asi que uvicorn escribia PREVIEW_FPS lineas por
    segundo -- 10 con la configuracion habitual, y todas identicas. La consola
    de la API dejaba de servir para lo que importa (alertas, errores, quien
    entra) porque el 80% era eso.

    Solo se silencian las que salieron BIEN: un 4xx/5xx en el preview sigue
    apareciendo, que es cuando el log hace falta.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        ruta, codigo = args[2], args[4]
        return not (isinstance(ruta, str) and ruta.startswith("/api/preview/")
                    and isinstance(codigo, int) and codigo < 400)


logging.getLogger("uvicorn.access").addFilter(_SinRuidoDePreview())

cfg = get_config()


async def _purga_periodica() -> None:
    """Aplica la politica de retencion cada 24 h mientras la API este arriba.

    Va dentro del proceso en vez de depender solo de un cron porque el borrado
    de datos personales no puede quedar sujeto a que alguien se acuerde de
    configurarlo. El cron de tools/purgar_datos.py sigue siendo util como red
    de seguridad si el servidor se reinicia a menudo.
    """
    import asyncio

    from sqlmodel import Session

    from api.database import engine
    from api.retention import Politica, formatear, purgar

    politica = Politica()
    log.info("Purga automatica cada 24 h -- politica: %s", politica.resumen())
    while True:
        try:
            # A dormir primero: al arrancar la API conviene atender peticiones,
            # no bloquearse purgando.
            await asyncio.sleep(24 * 3600)
            # La purga toca el disco y la BD; en un hilo aparte para no frenar
            # el bucle de eventos mientras corre.
            def _tarea():
                with Session(engine) as s:
                    return purgar(s, politica)

            cuenta = await asyncio.to_thread(_tarea)
            log.info("Purga automatica: %s", formatear(cuenta, simular=False))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - nunca debe tumbar la API
            log.error("Fallo la purga automatica: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    init_db()
    log.info("Token de ingesta del worker: %s...%s",
             cfg.ingest_token[:6], cfg.ingest_token[-4:])
    log.info("Dashboard en http://localhost:8000")

    tarea = asyncio.create_task(_purga_periodica())
    try:
        yield
    finally:
        tarea.cancel()


app = FastAPI(
    title="Sistema de Videovigilancia",
    description="Deteccion de placas, rostros y armas con cruce contra lista negra",
    version="0.3.0",
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(events.router)
app.include_router(blacklist.router)
app.include_router(faces.router)
app.include_router(alerts.router)
app.include_router(preview.router)


@app.websocket("/ws/alerts")
async def ws_alertas(websocket: WebSocket, token: str = "") -> None:
    """Canal de alertas en vivo.

    El token va por query string y no por cabecera porque la API WebSocket de
    los navegadores no permite mandar cabeceras personalizadas en el handshake.
    Se valida ANTES de aceptar la conexion.
    """
    if not decodificar_token(token):
        await websocket.close(code=4401, reason="Token invalido")
        return

    await hub.conectar(websocket)
    try:
        while True:
            # No se esperan mensajes del cliente; esto mantiene viva la
            # conexion y detecta la desconexion.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.debug("WebSocket cerrado: %s", e)
    finally:
        await hub.desconectar(websocket)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "dashboards": hub.conectados}


# Capturas de evidencia. Van bajo /media y no como estatico general para que
# quede claro que son datos personales y no recursos publicos de la web.
app.mount("/media", StaticFiles(directory=cfg.snapshot_dir), name="media")

if cfg.web_dir.exists():
    app.mount("/static", StaticFiles(directory=cfg.web_dir), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(cfg.web_dir / "index.html")
