"""Snapshot en alta resolucion bajo demanda, para evidencia nitida.

EL PROBLEMA: la deteccion en tiempo real corre sobre el sub-stream de la
camara (Streaming/Channels/102): 1280x720, la resolucion pensada para bajo
consumo de CPU y red al procesar 8 veces por segundo. Para EVIDENCIA -- un
rostro, una placa -- esa resolucion se queda corta: un rostro de 90 px de
ancho en un frame de 720p se ve borroso en cuanto el operador hace zoom.
Medido contra la camara real: el canal principal (101) entrega 3200x1800,
6.4x mas pixeles, en la misma escena.

LA SOLUCION NO ES subir todo el pipeline a esa resolucion: decodificar e
inferir sobre un frame 6x mas grande, 8 veces por segundo, arruinaria los fps
para ganar nitidez que nadie necesita en el 99% de los frames donde no pasa
nada. La solucion es pedir una foto del canal principal por HTTP SOLO cuando
un detector ya decidio que vale la pena (un rostro nuevo, un vehiculo nuevo,
un movimiento que empieza a acumular evidencia) -- Hikvision expone esto como
una sola foto fija bajo demanda, no un segundo stream de video:

    GET http://<host>/ISAPI/Streaming/channels/101/picture

Eso es una peticion HTTP puntual (~300-500 KB, unos cientos de ms), no una
segunda conexion RTSP corriendo en paralelo. El costo solo existe cuando hay
algo que merece verse bien.

NO BLOQUEAR LA CAPTURA: la peticion se resuelve en un hilo aparte (mismo
principio que edge/preview.py). Un detector la pide en cuanto detecta algo
prometedor y recoge el resultado mas tarde, cuando arma el evento final -- si
para entonces no ha llegado, sigue con el frame normal. Es una mejora
oportunista, nunca un requisito para emitir un evento.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional
from urllib.parse import urlparse

import cv2
import numpy as np

log = logging.getLogger(__name__)


class SnapshotHD:
    """Pide fotos del canal principal de una Hikvision, sin frenar al detector.

    Se construye a partir de la misma URL de SOURCE que ya usa el worker: si
    trae usuario y contrasena (una URL rtsp://), se reutilizan para el
    endpoint HTTP -- no hace falta configurar credenciales por separado. Con
    una webcam o un archivo de video no hay canal principal que pedir:
    `disponible` es False y todo el resto de este modulo se vuelve un no-op.
    """

    def __init__(self, source: str, canal: str = "101", timeout: float = 3.0) -> None:
        self._url: Optional[str] = None
        self._auth = None
        self._timeout = timeout
        self._fallos_seguidos = 0
        self._pedidos = 0

        if source.startswith(("rtsp://", "rtsps://")):
            partes = urlparse(source)
            if partes.hostname and partes.username:
                self._url = f"http://{partes.hostname}/ISAPI/Streaming/channels/{canal}/picture"
                self._usuario = partes.username
                self._clave = partes.password or ""

        # Un solo hilo: no hace falta paralelismo para esto, y con un pool mas
        # grande dos peticiones simultaneas (dos tracks nuevos en el mismo
        # frame) competirian por el mismo enlace HTTP a la camara sin ganar
        # nada.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="snapshot-hd")

    @property
    def disponible(self) -> bool:
        return self._url is not None

    def pedir(self) -> Optional["Future[Optional[np.ndarray]]"]:
        """Encola una captura y devuelve el Future de inmediato. None si esta
        fuente no tiene canal principal que pedir (webcam, archivo)."""
        if not self.disponible:
            return None
        self._pedidos += 1
        return self._pool.submit(self._capturar)

    def _capturar(self) -> Optional[np.ndarray]:
        import httpx

        try:
            r = httpx.get(
                self._url,
                auth=httpx.DigestAuth(self._usuario, self._clave),
                timeout=self._timeout,
            )
            r.raise_for_status()
            datos = np.frombuffer(r.content, dtype=np.uint8)
            frame = cv2.imdecode(datos, cv2.IMREAD_COLOR)
            self._fallos_seguidos = 0
            return frame
        except Exception as e:  # noqa: BLE001 - esto nunca debe tumbar al detector
            self._fallos_seguidos += 1
            if self._fallos_seguidos in (1, 10) or self._fallos_seguidos % 50 == 0:
                log.warning("Snapshot HD fallo (%d seguidos): %s", self._fallos_seguidos, e)
            return None

    @staticmethod
    def resultado_listo(future: Optional["Future[Optional[np.ndarray]]"]) -> Optional[np.ndarray]:
        """Recoge el resultado si ya esta listo, sin esperar ni un milisegundo.

        Se llama al armar el evento final, que puede pasar bastante despues
        del `pedir()`. Si el hilo todavia no termina (camara lenta, red
        cargada) o el future quedo cancelado, se devuelve None y quien llama
        sigue con su frame de respaldo -- nunca hay que esperar aqui: llegar
        tarde a emitir un evento es peor que emitirlo con la foto de siempre.
        """
        if future is None or not future.done():
            return None
        try:
            return future.result()
        except Exception:  # noqa: BLE001
            return None

    def cerrar(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    @property
    def stats(self) -> dict:
        return {"disponible": self.disponible, "pedidos": self._pedidos,
                "fallos_seguidos": self._fallos_seguidos}


def escalar_bbox(
    bbox: tuple[float, float, float, float],
    forma_origen: tuple[int, int],
    forma_destino: tuple[int, int],
    margen: float = 0.0,
) -> tuple[int, int, int, int]:
    """Traduce una caja de las coordenadas del frame de deteccion (bajo) a las
    del frame HD (alto), con margen opcional (fraccion del lado, por ejemplo
    0.5 = 50% extra por lado) para tolerar que la posicion no sea exacta:
    el HD se pidio unos frames antes o despues del que genero la caja.
    """
    alto_o, ancho_o = forma_origen
    alto_d, ancho_d = forma_destino
    ex, ey = ancho_d / ancho_o, alto_d / alto_o

    x1, y1, x2, y2 = bbox
    x1, y1, x2, y2 = x1 * ex, y1 * ey, x2 * ex, y2 * ey

    mx, my = (x2 - x1) * margen, (y2 - y1) * margen
    x1, y1 = max(0, x1 - mx), max(0, y1 - my)
    x2, y2 = min(ancho_d, x2 + mx), min(alto_d, y2 + my)
    return int(x1), int(y1), int(x2), int(y2)
