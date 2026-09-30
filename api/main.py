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
from api.deps import operador_de_websocket  # noqa: E402
from api.routers import (  # noqa: E402
    alerts,
    auditoria,
    auth,
    blacklist,
    camera_setup,
    events,
    faces,
    media,
    notificaciones,
    placas,
    preview,
    usuarios,
)
from api.redis_compartido import url_redis  # noqa: E402
from api.seguridad_http import CabecerasSeguridad  # noqa: E402

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
            # Con varios procesos (Redis) purga UNO solo por dia.
            if _canal is not None and not await _canal.tomar_turno("purga", 23 * 3600):
                continue

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

    from api import notificaciones
    from api.camaras_caidas import vigilar

    notificador = notificaciones.Notificador(datos_camara=notificaciones.datos_camara_desde_bd,
                                             foto_de=notificaciones.foto_desde_disco)
    notificaciones.notificador = notificador
    await notificador.iniciar()
    hub.al_alertar = notificador.alerta

    tareas = [asyncio.create_task(_purga_periodica())]
    if url_redis():
        tareas.append(asyncio.create_task(_conectar_redis(url_redis())))
    if notificador.cfg.camara_caida_s > 0:
        tareas.append(asyncio.create_task(vigilar(notificador.cfg.camara_caida_s)))
    try:
        yield
    finally:
        for t in tareas:
            t.cancel()
        hub.al_alertar = None
        await notificador.detener()
        notificaciones.notificador = None
        if _canal is not None:
            await _canal.cerrar()


_canal = None


async def _conectar_redis(url: str) -> None:
    """Varios procesos de API: alertas, video en vivo, login y cache de lista
    negra compartidos por Redis (ver api/redis_compartido.py)."""
    import asyncio

    from api.matching import lista_negra
    from api.preview import usar_preview
    from api.redis_compartido import (
        CANAL_INVALIDAR,
        CANAL_WS,
        PROCESO,
        CanalRedis,
        PreviewRedis,
    )

    global _canal
    _canal = CanalRedis(url)
    hub.usar_redis(_canal)
    usar_preview(PreviewRedis(_canal.r))
    lista_negra.al_invalidar = lambda: _canal.publicar_sync(CANAL_INVALIDAR, "lista_negra")

    async def _al_mensaje(canal: str, datos: str) -> None:
        if canal == CANAL_WS:
            await hub.entregar(datos)
        elif canal == CANAL_INVALIDAR and datos == "lista_negra":
            lista_negra.invalidar(difundir=False)

    _canal.escuchar(_al_mensaje)
    log.info("Modo multi-proceso con Redis activo (proceso %s)", PROCESO)
    while True:
        await hub.refrescar_total()
        await asyncio.sleep(5)


app = FastAPI(
    title="GOSS IP - Sistema de Videovigilancia",
    description="Deteccion de placas, rostros y movimiento con cruce contra lista negra",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(CabecerasSeguridad)

app.include_router(auth.router)
app.include_router(usuarios.router)
app.include_router(auditoria.router)
app.include_router(events.router)
app.include_router(blacklist.router)
app.include_router(faces.router)
app.include_router(alerts.router)
app.include_router(preview.router)
app.include_router(camera_setup.router)
app.include_router(media.router)
app.include_router(placas.router)
app.include_router(notificaciones.router)


@app.websocket("/ws/alerts")
async def ws_alertas(websocket: WebSocket) -> None:
    """Canal de alertas en vivo.

    La API WebSocket de los navegadores no permite cabeceras en el handshake,
    asi que la sesion viaja en la cookie HttpOnly del login (o, para clientes
    viejos, en ?token=). Se valida ANTES de aceptar la conexion.
    """
    from fastapi.concurrency import run_in_threadpool

    if await run_in_threadpool(operador_de_websocket, websocket) is None:
        await websocket.close(code=4401, reason="Sesion invalida")
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
    return {"status": "ok", "dashboards": hub.conectados_total,
            "multiproceso": hub.distribuido}


# Las capturas de evidencia ya NO se montan como estaticos publicos: las sirve
# api/routers/media.py, y solo a quien tiene sesion.

class _EstaticosRevalidados(StaticFiles):
    """Archivos del dashboard con `Cache-Control: no-cache`.

    El navegador los guarda, pero pregunta en cada carga si cambiaron (ETag,
    respuesta 304 de unos bytes). Sin esto, tras actualizar el sistema un
    navegador podia seguir usando el JavaScript viejo durante horas contra una
    API nueva.
    """

    def file_response(self, *args, **kwargs):
        respuesta = super().file_response(*args, **kwargs)
        respuesta.headers["Cache-Control"] = "no-cache"
        return respuesta


if cfg.web_dir.exists():
    app.mount("/static", _EstaticosRevalidados(directory=cfg.web_dir), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(cfg.web_dir / "index.html", headers={"Cache-Control": "no-cache"})
