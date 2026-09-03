"""Abstraccion de fuente de video: webcam, archivo o RTSP, con la misma interfaz.

Motivo: la camara Hikvision todavia no esta conectada. Todo el resto del
sistema se desarrolla contra `FrameSource`, y el dia que llegue la camara solo
cambia la variable SOURCE en el .env. Cero cambios de codigo.

    SOURCE=webcam:0
    SOURCE=file:videos/prueba.mp4
    SOURCE=rtsp://admin:pass@192.168.1.64:554/Streaming/Channels/102

Las dos trampas de RTSP que este modulo resuelve:

1. EL BUFFER SE LLENA. OpenCV acumula frames en una cola interna. Si tu
   inferencia tarda 120 ms y la camara entrega a 25 fps, cada segundo se
   acumulan ~17 frames. A los 30 segundos estas analizando video de hace medio
   minuto y no lo notas. La solucion es un hilo lector que vacia la cola sin
   parar y conserva SOLO el ultimo frame; el consumidor siempre ve el presente.

2. UDP PIERDE PAQUETES. Por defecto FFmpeg negocia RTSP sobre UDP, que en Wi-Fi
   produce frames rasgados o verdes. Se fuerza TCP por variable de entorno.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# Debe fijarse ANTES de construir cualquier VideoCapture: OpenCV la lee al
# momento de abrir el stream, no al importar.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000",  # TCP + timeout de 5 s en microsegundos
)

import cv2  # noqa: E402  (import despues de fijar la env var, a proposito)
import numpy as np  # noqa: E402

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Tipos
# --------------------------------------------------------------------------

@dataclass
class FrameInfo:
    """Un frame con su metadato de captura."""

    frame: np.ndarray
    index: int          # numero de frame desde que arranco la fuente
    ts: float           # time.time() del instante de captura
    dropped: int = 0    # frames descartados desde la lectura anterior (solo en vivo)

    @property
    def age(self) -> float:
        """Segundos transcurridos desde la captura. Si esto crece, vas atrasado."""
        return time.time() - self.ts

    @property
    def shape(self) -> tuple[int, int]:
        h, w = self.frame.shape[:2]
        return w, h


@dataclass
class SourceStatus:
    """Salud de la fuente. La API la expone en el dashboard para saber si una
    camara se cayo sin tener que mirar el video."""

    connected: bool = False
    frames_grabbed: int = 0
    frames_dropped: int = 0
    """Frames que el hilo lector descarto porque el consumidor iba atrasado.
    Distinto de frames_skipped: esto SI es senal de problema. Si crece, la
    inferencia no alcanza y hay que bajar imgsz o el numero de detectores."""
    frames_skipped: int = 0
    """Frames omitidos a proposito por el limitador de fps. Esperado y sano:
    es la diferencia entre los fps de la camara y los de inferencia."""
    reconnects: int = 0
    last_error: Optional[str] = None
    last_frame_at: Optional[float] = None
    measured_fps: float = 0.0

    def as_dict(self) -> dict:
        return {
            "connected": self.connected,
            "frames_grabbed": self.frames_grabbed,
            "frames_dropped": self.frames_dropped,
            "frames_skipped": self.frames_skipped,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "seconds_since_frame": (
                round(time.time() - self.last_frame_at, 2) if self.last_frame_at else None
            ),
            "measured_fps": round(self.measured_fps, 2),
        }


# --------------------------------------------------------------------------
# Interfaz
# --------------------------------------------------------------------------

class FrameSource(ABC):
    """Interfaz comun. Usar como context manager:

        with open_source(cfg.source) as src:
            for f in src.frames():
                ...
    """

    name: str = "source"
    is_live: bool = True

    @abstractmethod
    def read(self, timeout: float = 5.0) -> Optional[FrameInfo]:
        """Devuelve el siguiente frame, o None si la fuente termino o expiro."""

    @abstractmethod
    def release(self) -> None:
        ...

    @property
    @abstractmethod
    def status(self) -> SourceStatus:
        ...

    def frames(self, max_fps: Optional[float] = None):
        """Generador de frames con limitador de tasa opcional.

        `max_fps` es la tasa de INFERENCIA deseada. En una fuente en vivo los
        frames sobrantes simplemente se descartan (siempre te quedas con el mas
        reciente). En un archivo se respetan todos para no perder informacion.
        """
        min_interval = (1.0 / max_fps) if (max_fps and self.is_live) else 0.0
        next_at = 0.0
        while True:
            frame = self.read()
            if frame is None:
                return
            now = time.monotonic()
            if now < next_at:
                # Omitido por el limitador de fps: esperado, no es un fallo.
                self.status.frames_skipped += 1
                continue

            # El siguiente objetivo se ACUMULA sobre el anterior, no se calcula
            # desde `now`. La diferencia importa: solo se puede entregar un
            # frame cuando llega uno de la camara, asi que `now` casi siempre
            # cae despues del objetivo. Recalcular desde ahi arrastra ese
            # sobrante frame tras frame y el periodo real se redondea hacia
            # arriba al siguiente multiplo del periodo de la camara. Medido con
            # camara a 20 fps y objetivo 8: daba 5.5 fps en vez de 8.
            if next_at == 0.0:
                next_at = now
            next_at += min_interval
            if next_at < now:
                # Nos quedamos atras (inferencia mas lenta que el objetivo).
                # Se reancla al presente en vez de acumular una deuda que
                # provocaria una rafaga de frames al recuperarse.
                next_at = now + min_interval
            yield frame

    def __enter__(self) -> "FrameSource":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


# --------------------------------------------------------------------------
# Fuente en vivo (webcam / RTSP) con hilo lector y reconexion
# --------------------------------------------------------------------------

class LiveSource(FrameSource):
    """Camara USB o stream de red.

    Un hilo en segundo plano lee sin parar y guarda unicamente el ultimo frame.
    `read()` entrega ese ultimo frame; si el consumidor es lento, los frames
    intermedios se pierden a proposito (eso es lo correcto en vigilancia:
    quieres el presente, no la cola acumulada).
    """

    is_live = True

    def __init__(
        self,
        target: int | str,
        *,
        name: str = "live",
        reconnect: bool = True,
        max_backoff: float = 30.0,
        open_timeout: float = 15.0,
    ) -> None:
        self.target = target
        self.name = name
        self.reconnect = reconnect
        self.max_backoff = max_backoff
        self.open_timeout = open_timeout

        self._cap: Optional[cv2.VideoCapture] = None
        self._latest: Optional[FrameInfo] = None
        self._last_delivered = -1
        self._counter = 0
        self._status = SourceStatus()
        self._fps_window: list[float] = []

        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"reader-{name}", daemon=True)
        self._thread.start()

    # -- ciclo del hilo lector ---------------------------------------------

    def _open(self) -> bool:
        backend = cv2.CAP_FFMPEG if isinstance(self.target, str) else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.target, backend)
        if not cap.isOpened():
            cap.release()
            return False

        # Cola interna minima: sin esto la latencia crece sin limite.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._cap = cap
        with self._cond:
            self._status.connected = True
            self._status.last_error = None
        log.info("[%s] conectado a %s", self.name, _redact(self.target))
        return True

    def _loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if self._cap is None:
                if not self._open():
                    with self._cond:
                        self._status.connected = False
                        self._status.last_error = "no se pudo abrir la fuente"
                    if not self.reconnect:
                        with self._cond:
                            self._cond.notify_all()
                        return
                    log.warning("[%s] sin conexion, reintento en %.0fs", self.name, backoff)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, self.max_backoff)
                    continue
                backoff = 1.0
                with self._cond:
                    if self._status.frames_grabbed:  # no contar la conexion inicial
                        self._status.reconnects += 1

            ok, frame = self._cap.read()
            if not ok or frame is None:
                log.warning("[%s] se perdio el stream, reconectando", self.name)
                self._cap.release()
                self._cap = None
                with self._cond:
                    self._status.connected = False
                    self._status.last_error = "lectura fallida"
                continue

            now = time.time()
            with self._cond:
                # Si el consumidor no alcanzo a tomar el frame anterior, se pierde.
                if self._latest is not None and self._latest.index > self._last_delivered:
                    self._status.frames_dropped += 1
                self._counter += 1
                self._status.frames_grabbed += 1
                self._status.last_frame_at = now
                self._latest = FrameInfo(
                    frame=frame,
                    index=self._counter,
                    ts=now,
                    dropped=self._status.frames_dropped,
                )
                self._measure_fps(now)
                self._cond.notify_all()

    def _measure_fps(self, now: float) -> None:
        """FPS real medido sobre los ultimos 30 frames (no el que declara la camara,
        que suele mentir)."""
        self._fps_window.append(now)
        if len(self._fps_window) > 30:
            self._fps_window.pop(0)
        if len(self._fps_window) >= 2:
            span = self._fps_window[-1] - self._fps_window[0]
            if span > 0:
                self._status.measured_fps = (len(self._fps_window) - 1) / span

    # -- interfaz publica ---------------------------------------------------

    def read(self, timeout: float = 5.0) -> Optional[FrameInfo]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                if self._latest is not None and self._latest.index > self._last_delivered:
                    self._last_delivered = self._latest.index
                    return self._latest
                if self._stop.is_set() or not self._thread.is_alive():
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning("[%s] sin frames por %.0fs", self.name, timeout)
                    return None
                self._cond.wait(remaining)

    def release(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        self._thread.join(timeout=3.0)
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        log.info("[%s] cerrada (%d frames, %d descartados, %d reconexiones)",
                 self.name, self._status.frames_grabbed,
                 self._status.frames_dropped, self._status.reconnects)

    @property
    def status(self) -> SourceStatus:
        with self._cond:
            return self._status

    def wait_until_ready(self, timeout: float = 15.0) -> bool:
        """Bloquea hasta que llegue el primer frame. Util al arrancar para fallar
        rapido con un mensaje claro en vez de morir a mitad del pipeline."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._latest is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
        return True


# --------------------------------------------------------------------------
# Fuente de archivo (desarrollo y pruebas reproducibles)
# --------------------------------------------------------------------------

class FileSource(FrameSource):
    """Video en disco. A diferencia de la fuente en vivo, NO descarta frames:
    en pruebas quieres determinismo, que la misma grabacion de siempre el mismo
    resultado."""

    is_live = False

    def __init__(self, path: str | Path, *, loop: bool = False, realtime: bool = False) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"No existe el video: {self.path}")
        self.name = self.path.name
        self.loop = loop
        self.realtime = realtime

        self._cap = cv2.VideoCapture(str(self.path))
        if not self._cap.isOpened():
            raise RuntimeError(f"OpenCV no pudo abrir {self.path} (codec no soportado?)")

        self._counter = 0
        self._status = SourceStatus(connected=True)
        self._native_fps = self._cap.get(cv2.CAP_PROP_FPS) or 25.0
        self._t0 = time.monotonic()

    def read(self, timeout: float = 5.0) -> Optional[FrameInfo]:
        ok, frame = self._cap.read()
        if not ok or frame is None:
            if self.loop:
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                self._t0 = time.monotonic()
                ok, frame = self._cap.read()
            if not ok or frame is None:
                self._status.connected = False
                return None

        self._counter += 1
        self._status.frames_grabbed = self._counter
        self._status.last_frame_at = time.time()
        self._status.measured_fps = self._native_fps

        if self.realtime:
            # Reproduce a la velocidad original, para simular una camara real.
            target = self._t0 + (self._counter / self._native_fps)
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)

        return FrameInfo(frame=frame, index=self._counter, ts=time.time())

    def release(self) -> None:
        self._cap.release()

    @property
    def status(self) -> SourceStatus:
        return self._status

    @property
    def total_frames(self) -> int:
        return int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT))


# --------------------------------------------------------------------------
# Fabrica
# --------------------------------------------------------------------------

def open_source(spec: str, **kwargs) -> FrameSource:
    """Construye la fuente adecuada a partir de la cadena SOURCE.

        webcam:0 | 0                -> LiveSource(indice)
        rtsp://... | http://...     -> LiveSource(url, con reconexion)
        file:ruta.mp4 | ruta.mp4    -> FileSource
    """
    spec = spec.strip()

    if spec.startswith(("rtsp://", "rtsps://", "http://", "https://")):
        host = urlparse(spec).hostname or "red"
        return LiveSource(spec, name=host, reconnect=True, **kwargs)

    if spec.startswith("webcam:") or spec.isdigit():
        index = int(spec.split(":", 1)[1]) if ":" in spec else int(spec)
        # La webcam no se reconecta sola: si se desconecta fisicamente, es un
        # error que el operador debe ver, no algo que reintentar en silencio.
        return LiveSource(index, name=f"webcam{index}", reconnect=False, **kwargs)

    if spec.startswith("file:"):
        spec = spec[5:]
    return FileSource(spec, **kwargs)


def _redact(target: int | str) -> str:
    """Oculta la contrasena antes de mandar una URL RTSP a los logs.
    Los logs terminan en pantallas y tickets; la credencial de la camara no."""
    if not isinstance(target, str) or "@" not in target:
        return str(target)
    scheme, _, rest = target.partition("://")
    creds, _, hostpart = rest.rpartition("@")
    if not creds:
        return target
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{hostpart}"
