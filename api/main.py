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
from api.routers import alerts, auth, blacklist, events, faces  # noqa: E402
from api.security import decodificar_token  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("api")

cfg = get_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info("Token de ingesta del worker: %s...%s",
             cfg.ingest_token[:6], cfg.ingest_token[-4:])
    log.info("Dashboard en http://localhost:8000")
    yield


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
