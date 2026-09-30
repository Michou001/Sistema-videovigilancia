"""Estado compartido entre varios procesos de la API, con Redis (opcional).

Sin REDIS_URL la API es UN proceso y todo vive en memoria: el canal de
alertas, el ultimo frame de cada camara, el limite de intentos de login y la
cache de la lista negra. Es lo mas simple y alcanza para un centro con pocas
camaras y pocos monitores.

Con muchos dashboards o muchas camaras conviene correr varios procesos
(API_WORKERS=4 en `python -m api`). Cada proceso tendria su propia memoria, y
eso rompe cuatro cosas, que aqui se resuelven con Redis:

  1. ALERTAS EN VIVO. La ingesta puede caer en el proceso A y el dashboard
     estar conectado al proceso B. Cada proceso publica en un canal de Redis
     y todos entregan a sus propios dashboards.
  2. VIDEO EN VIVO. El worker sube el frame a un proceso y el <img> del
     operador lo pide a otro. El ultimo frame de cada camara vive en Redis, y
     el aviso de "hay frame nuevo" va por un canal.
  3. LIMITE DE INTENTOS DE LOGIN. En memoria, un atacante tendria 5 intentos
     POR PROCESO. En Redis el conteo es uno solo.
  4. CACHE DE LA LISTA NEGRA. Dar de alta una placa en el proceso A debe
     invalidar la cache de todos, no solo la suya.

Cada proceso se identifica con host:pid. Los conteos por proceso (cuantos
dashboards miran una camara) llevan marca de tiempo y se ignoran si no se
refrescan: un proceso que muere no deja espectadores fantasma.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid
from typing import AsyncIterator, Optional

log = logging.getLogger(__name__)

PROCESO = f"{socket.gethostname()}:{os.getpid()}"
PREFIJO = "goss"
CANAL_WS = f"{PREFIJO}:ws"
CANAL_INVALIDAR = f"{PREFIJO}:invalidar"

# Un conteo por proceso vale mientras se refresque (cada pocos segundos).
VIGENCIA_CONTEO = 20.0
FRAME_TTL = 30          # s que vive el ultimo frame de una camara sin renovarse


def url_redis() -> Optional[str]:
    url = os.getenv("REDIS_URL", "").strip()
    return url or None


def cliente_async(url: str):
    import redis.asyncio as aioredis

    return aioredis.from_url(url, decode_responses=False)


def cliente_sync(url: str):
    import redis

    return redis.from_url(url, decode_responses=False)


def _conteo(valor: bytes | str) -> tuple[int, float]:
    try:
        texto = valor.decode() if isinstance(valor, bytes) else valor
        n, ts = texto.split("|", 1)
        return int(n), float(ts)
    except (ValueError, AttributeError):
        return 0, 0.0


def sumar_vigentes(conteos: dict, ahora: Optional[float] = None) -> int:
    ahora = ahora or time.time()
    total = 0
    for valor in conteos.values():
        n, ts = _conteo(valor)
        if ahora - ts <= VIGENCIA_CONTEO:
            total += n
    return total


# --------------------------------------------------------------------------
# 3. Limite de intentos de login
# --------------------------------------------------------------------------

class LimiteIntentosRedis:
    """Misma interfaz que api.security.LimiteIntentos, con el conteo en Redis.

    Cada intento fallido es un elemento de un conjunto ordenado por tiempo;
    los que salen de la ventana se borran antes de contar.
    """

    def __init__(self, cliente, maximo: int = 5, ventana: float = 300.0) -> None:
        self.r = cliente
        self.maximo = maximo
        self.ventana = ventana

    def _clave(self, clave: str) -> str:
        return f"{PREFIJO}:login:{clave}"

    def espera(self, clave: str) -> float:
        k = self._clave(clave)
        ahora = time.time()
        try:
            self.r.zremrangebyscore(k, 0, ahora - self.ventana)
            if self.r.zcard(k) < self.maximo:
                return 0.0
            primero = self.r.zrange(k, 0, 0, withscores=True)
        except Exception as e:  # noqa: BLE001
            # Sin Redis no se bloquea el login de todo el mundo: se deja pasar
            # y se registra. La contrasena sigue siendo necesaria.
            log.error("Limite de intentos sin Redis: %s", e)
            return 0.0
        if not primero:
            return 0.0
        return max(0.0, self.ventana - (ahora - float(primero[0][1])))

    def fallo(self, clave: str) -> None:
        k = self._clave(clave)
        try:
            self.r.zadd(k, {uuid.uuid4().hex: time.time()})
            self.r.expire(k, int(self.ventana) + 5)
        except Exception as e:  # noqa: BLE001
            log.error("No se pudo registrar el intento fallido en Redis: %s", e)

    def exito(self, clave: str) -> None:
        try:
            self.r.delete(self._clave(clave))
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# 2. Video en vivo
# --------------------------------------------------------------------------

class PreviewRedis:
    """Mismo papel que api.preview.BufferPreview, con el frame en Redis."""

    def __init__(self, cliente, proceso: str = PROCESO) -> None:
        self.r = cliente
        self.proceso = proceso
        self._locales: dict[str, int] = {}

    def _k(self, tipo: str, camera_id: str) -> str:
        return f"{PREFIJO}:preview:{tipo}:{camera_id}"

    async def _anunciar(self, camera_id: str) -> None:
        """Cuantos dashboards de ESTE proceso miran la camara."""
        clave = self._k("espectadores", camera_id)
        await self.r.hset(clave, self.proceso, f"{self._locales.get(camera_id, 0)}|{time.time()}")
        await self.r.expire(clave, 120)

    async def espectadores(self, camera_id: str) -> int:
        return sumar_vigentes(await self.r.hgetall(self._k("espectadores", camera_id)))

    async def apublicar(self, camera_id: str, jpeg: bytes) -> int:
        ahora = time.time()
        meta_k = self._k("meta", camera_id)
        previo = await self.r.hmget(meta_k, "recibido", "fps")
        fps = 0.0
        try:
            antes, fps_previo = float(previo[0] or 0), float(previo[1] or 0)
            dt = ahora - antes
            if antes and 0 < dt < 5.0:
                fps = 1.0 / dt if not fps_previo else fps_previo * 0.7 + (1.0 / dt) * 0.3
            else:
                fps = fps_previo
        except (TypeError, ValueError):
            pass
        seq = await self.r.incr(self._k("seq", camera_id))
        tuberia = self.r.pipeline()
        tuberia.set(self._k("frame", camera_id), jpeg, ex=FRAME_TTL)
        tuberia.hset(meta_k, mapping={"recibido": ahora, "fps": fps, "seq": seq})
        tuberia.expire(meta_k, FRAME_TTL)
        tuberia.publish(self._k("canal", camera_id), str(seq))
        await tuberia.execute()
        return await self.espectadores(camera_id)

    async def acamaras(self) -> list[dict]:
        ahora = time.time()
        salida = []
        async for clave in self.r.scan_iter(match=self._k("meta", "*")):
            clave = clave.decode() if isinstance(clave, bytes) else clave
            camera_id = clave.rsplit(":", 1)[-1]
            meta = await self.r.hgetall(clave)
            recibido = float(meta.get(b"recibido", 0) or 0)
            if ahora - recibido > FRAME_TTL:
                continue
            salida.append({
                "camera_id": camera_id,
                "fps": round(float(meta.get(b"fps", 0) or 0), 1),
                "espectadores": await self.espectadores(camera_id),
                "antiguedad": round(ahora - recibido, 1),
            })
        return sorted(salida, key=lambda c: c["camera_id"])

    async def flujo_mjpeg(self, camera_id: str, sin_frames: float = 15.0) -> AsyncIterator[bytes]:
        self._locales[camera_id] = self._locales.get(camera_id, 0) + 1
        await self._anunciar(camera_id)
        pubsub = self.r.pubsub()
        await pubsub.subscribe(self._k("canal", camera_id))
        ultimo_anuncio = time.monotonic()
        try:
            frame = await self.r.get(self._k("frame", camera_id))
            if frame:
                yield _parte(frame)
            espera_hasta = time.monotonic() + sin_frames
            while True:
                if time.monotonic() - ultimo_anuncio > VIGENCIA_CONTEO / 3:
                    await self._anunciar(camera_id)
                    ultimo_anuncio = time.monotonic()
                mensaje = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if mensaje is None:
                    if time.monotonic() > espera_hasta:
                        return
                    continue
                frame = await self.r.get(self._k("frame", camera_id))
                if not frame:
                    return
                espera_hasta = time.monotonic() + sin_frames
                yield _parte(frame)
        finally:
            self._locales[camera_id] = max(0, self._locales.get(camera_id, 1) - 1)
            try:
                await self._anunciar(camera_id)
                await pubsub.unsubscribe()
                await pubsub.aclose()
            except Exception:  # noqa: BLE001
                pass


def _parte(frame: bytes) -> bytes:
    return (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")


# --------------------------------------------------------------------------
# 1 y 4. Canal de alertas e invalidacion de caches
# --------------------------------------------------------------------------

class CanalRedis:
    """Publica mensajes para todos los procesos y reparte los que llegan."""

    def __init__(self, url: str, r=None, sync=None, proceso: str = PROCESO) -> None:
        self.url = url
        self.proceso = proceso
        self.r = r if r is not None else cliente_async(url)
        self._sync = sync if sync is not None else cliente_sync(url)
        self._tarea: Optional[asyncio.Task] = None

    def publicar_sync(self, canal: str, datos: str) -> None:
        """Para codigo sincrono (endpoints def): invalidar la lista negra."""
        try:
            self._sync.publish(canal, datos)
        except Exception as e:  # noqa: BLE001
            log.error("No se pudo publicar en Redis (%s): %s", canal, e)

    async def publicar(self, canal: str, datos: str) -> None:
        await self.r.publish(canal, datos)

    def escuchar(self, al_mensaje) -> None:
        """Arranca la tarea que recibe CANAL_WS y CANAL_INVALIDAR."""
        async def _bucle() -> None:
            while True:
                try:
                    pubsub = self.r.pubsub()
                    await pubsub.subscribe(CANAL_WS, CANAL_INVALIDAR)
                    log.info("Escuchando Redis (%s) como %s", self.url.split("@")[-1], self.proceso)
                    async for m in pubsub.listen():
                        if m.get("type") != "message":
                            continue
                        canal = m["channel"].decode() if isinstance(m["channel"], bytes) else m["channel"]
                        datos = m["data"].decode() if isinstance(m["data"], bytes) else m["data"]
                        try:
                            await al_mensaje(canal, datos)
                        except Exception as e:  # noqa: BLE001
                            log.debug("Mensaje de Redis no atendido: %s", e)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    # Redis reiniciado: se reintenta sin tumbar la API.
                    log.error("Conexion con Redis perdida (%s); reintento en 3 s", e)
                    await asyncio.sleep(3)

        self._tarea = asyncio.create_task(_bucle())

    async def cerrar(self) -> None:
        if self._tarea:
            self._tarea.cancel()
            try:
                await self._tarea
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.r.aclose()
        self._sync.close()

    async def anunciar_dashboards(self, n: int) -> None:
        clave = f"{PREFIJO}:dashboards"
        await self.r.hset(clave, self.proceso, f"{n}|{time.time()}")
        await self.r.expire(clave, 120)

    async def total_dashboards(self) -> int:
        return sumar_vigentes(await self.r.hgetall(f"{PREFIJO}:dashboards"))

    async def tomar_turno(self, nombre: str, segundos: int) -> bool:
        """Candado entre procesos: True si a este le toca (p.ej. la purga
        diaria, que no debe correr cuatro veces a la vez)."""
        return bool(await self.r.set(f"{PREFIJO}:turno:{nombre}", self.proceso, nx=True, ex=segundos))


def mensaje_ws(tipo: str, datos: dict, serializar) -> str:
    return json.dumps({"type": tipo, "data": datos}, default=serializar, ensure_ascii=False)
