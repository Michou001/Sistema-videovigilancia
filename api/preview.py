"""Vista en vivo de las camaras en el dashboard.

Por que el video pasa por la API y no va directo del navegador a la camara:

  - Una camara IP habla RTSP/H.264 y ningun navegador reproduce RTSP. Servirlo
    como HLS o WebRTC exige un servidor de medios aparte (mediamtx, Janus) con
    su propia operacion y su propio puerto abierto.
  - El operador necesita ver LAS CAJAS DIBUJADAS, no el video crudo. Esas cajas
    solo existen en el worker, que es quien corrio los modelos. Un flujo
    directo desde la camara mostraria video sin una sola anotacion.
  - La camara vive en una red privada a la que el navegador del operador no
    llega. El worker si.

Asi que el worker, que ya tiene el frame anotado en memoria para su ventana de
depuracion, lo manda a la API como JPEG y la API lo reparte a los dashboards
como MJPEG (multipart/x-mixed-replace), que cualquier navegador pinta con un
<img> y sin una linea de JavaScript de video.

LOS FRAMES NUNCA TOCAN EL DISCO. Es video de personas en tiempo real: la unica
copia vive en memoria y la sobreescribe el frame siguiente. Lo que se conserva
como evidencia son las capturas de los eventos, que ya pasan por
data/snapshots con su politica de retencion.

Limitacion conocida: el buffer es de proceso. Con varios workers de uvicorn
(--workers N) cada uno tendria el suyo y el dashboard veria video solo cuando
le tocara el proceso que recibe los frames. Para varios procesos hace falta
mover esto a Redis. Hoy el despliegue es de un proceso (ver docs/despliegue.md).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator, Optional

log = logging.getLogger(__name__)

# Un JPEG de 1920x1080 con calidad alta ronda los 400 KB. 2 MB deja margen de
# sobra y a la vez impide que un worker mal configurado agote la memoria.
MAX_BYTES_FRAME = 2 * 1024 * 1024

# Si una camara deja de mandar frames, el flujo MJPEG se cierra en vez de
# quedarse colgado para siempre. El dashboard lo nota al refrescar la lista de
# camaras y pinta el hueco de "sin senal".
SEGUNDOS_SIN_FRAMES = 15.0

# Una camara sin frames por mas de esto se considera apagada y desaparece de la
# lista. Es mas corto que el minuto del heartbeat porque el preview se corta en
# cuanto nadie mira: aqui solo interesa "esta mandando video AHORA".
SEGUNDOS_PARA_OLVIDAR = 30.0


class _Canal:
    """El ultimo frame de una camara, y quien lo esta esperando.

    Guarda UN solo frame a proposito. Un buffer de varios frames solo añade
    latencia: al operador no le sirve ver lo que paso hace dos segundos, le
    sirve ver lo que pasa ahora. Si un dashboard va lento, se pierde frames
    intermedios y eso es exactamente lo correcto.
    """

    def __init__(self, camera_id: str) -> None:
        self.camera_id = camera_id
        self.frame: Optional[bytes] = None
        self.seq = 0
        self.recibido = 0.0
        self.fps = 0.0
        self.espectadores = 0
        # Un future por espectador en vez de un asyncio.Event compartido: el
        # Event tiene una carrera de set()/clear() en la que un espectador que
        # todavia no fue planificado se pierde el aviso y espera un frame de
        # mas. Con futures cada espectador tiene el suyo y no hay carrera.
        self._esperando: list[asyncio.Future] = []

    def publicar(self, jpeg: bytes) -> None:
        ahora = time.monotonic()
        if self.recibido:
            dt = ahora - self.recibido
            # dt grande = la camara volvio despues de una pausa; no contamina
            # la media con un fps absurdamente bajo.
            if 0 < dt < 5.0:
                instantaneo = 1.0 / dt
                self.fps = (instantaneo if not self.fps
                            else self.fps * 0.7 + instantaneo * 0.3)

        self.frame = jpeg
        self.seq += 1
        self.recibido = ahora

        for fut in self._esperando:
            if not fut.done():
                fut.set_result(self.seq)
        self._esperando.clear()

    async def esperar_nuevo(self, desde_seq: int, timeout: float) -> Optional[int]:
        """Espera un frame mas reciente que `desde_seq`. None si expira."""
        if self.frame is not None and self.seq > desde_seq:
            return self.seq

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._esperando.append(fut)
        try:
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return None
        finally:
            if fut in self._esperando:
                self._esperando.remove(fut)

    @property
    def antiguedad(self) -> float:
        return time.monotonic() - self.recibido if self.recibido else 1e9

    @property
    def viva(self) -> bool:
        return self.frame is not None and self.antiguedad < SEGUNDOS_PARA_OLVIDAR


class BufferPreview:
    """Reparte los frames de cada camara a los dashboards conectados."""

    def __init__(self) -> None:
        self._canales: dict[str, _Canal] = {}

    def _canal(self, camera_id: str) -> _Canal:
        canal = self._canales.get(camera_id)
        if canal is None:
            canal = _Canal(camera_id)
            self._canales[camera_id] = canal
            log.info("Preview: canal abierto para '%s'", camera_id)
        return canal

    def publicar(self, camera_id: str, jpeg: bytes) -> int:
        """Guarda el frame y devuelve cuantos dashboards lo estan mirando.

        El worker usa ese numero para dejar de enviar cuando nadie mira: sin
        eso estaria codificando y subiendo JPEG las 24 horas para nadie,
        gastando CPU que le hace falta a los modelos.
        """
        canal = self._canal(camera_id)
        canal.publicar(jpeg)
        return canal.espectadores

    def espectadores(self, camera_id: str) -> int:
        canal = self._canales.get(camera_id)
        return canal.espectadores if canal else 0

    def camaras(self) -> list[dict]:
        """Camaras que estan mandando video ahora mismo."""
        self._olvidar_muertas()
        return [
            {
                "camera_id": c.camera_id,
                "fps": round(c.fps, 1),
                "espectadores": c.espectadores,
                "antiguedad": round(c.antiguedad, 1),
            }
            for c in self._canales.values()
            if c.viva
        ]

    def _olvidar_muertas(self) -> None:
        """Suelta el frame de las camaras apagadas.

        No es solo higiene de memoria: es no quedarse con la ultima imagen de
        una persona indefinidamente porque el worker se cayo en mal momento.
        """
        for cid, canal in list(self._canales.items()):
            if canal.frame is not None and not canal.viva:
                canal.frame = None
                canal.fps = 0.0
                log.info("Preview: '%s' dejo de mandar video", cid)
            # El canal en si se conserva: pesa nada y la camara suele volver.

    async def flujo_mjpeg(self, camera_id: str) -> AsyncIterator[bytes]:
        """Genera el cuerpo multipart que el navegador pinta en un <img>."""
        canal = self._canal(camera_id)
        canal.espectadores += 1
        log.debug("Preview '%s': +1 espectador (%d)", camera_id, canal.espectadores)

        try:
            seq = 0
            # El primer frame se manda de inmediato si ya lo hay: sin esto el
            # <img> queda en blanco hasta el siguiente, y con preview a 6 fps
            # eso es un parpadeo visible al cambiar de apartado.
            while True:
                nuevo = await canal.esperar_nuevo(seq, SEGUNDOS_SIN_FRAMES)
                if nuevo is None:
                    log.debug("Preview '%s': sin frames, cerrando flujo", camera_id)
                    return

                frame = canal.frame
                if frame is None:
                    return
                seq = nuevo

                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                    + frame + b"\r\n"
                )
        finally:
            canal.espectadores = max(0, canal.espectadores - 1)
            log.debug("Preview '%s': -1 espectador (%d)", camera_id, canal.espectadores)


buffer_preview = BufferPreview()
