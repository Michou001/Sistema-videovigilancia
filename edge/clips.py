"""Clips de video de las alertas: unos segundos antes y despues del hecho.

Una foto dice QUE se vio; un clip dice QUE PASO: de donde vino el vehiculo,
que hizo la persona antes de caer, si el "forcejeo" era un juego. Para un
parte o una denuncia, el clip es la evidencia.

COMO FUNCIONA
  1. Cada frame analizado entra a un buffer circular en memoria, reducido y
     comprimido a JPEG (~60 KB por frame a 960 px). Con 8 fps y 40 s de
     buffer son ~20 MB por camara. Se hace en un hilo aparte: el bucle de
     deteccion solo deja la referencia al frame.
  2. El worker NO decide que es una alerta: eso lo decide la API al cruzar
     contra la lista negra. Cuando la API responde que un evento fue alerta
     (ver HttpSink.al_responder), se pide el clip de ese evento.
  3. Se espera a tener CLIP_POST_S segundos despues del evento, se arma el
     video con los frames de [evento - CLIP_PRE_S, evento + CLIP_POST_S] y se
     sube a la API, que lo liga a la alerta (boton "Ver clip" en el dashboard).

CODEC
  - `vp8` (WebM): lo escribe OpenCV, que ya es dependencia. Libre de
    regalias y se reproduce en Chrome, Edge y Firefox. Es el default.
  - `h264` (MP4): mas compatible (Safari, celulares). Requiere `pip install av`
    (PyAV trae FFmpeg con libx264, bajo licencia GPL: ver docs/licencias.md).
  - `auto`: h264 si PyAV esta instalado, si no vp8.

Si la API estuvo caida y el evento llego tarde (desde el spool), los frames ya
no estan en el buffer: ese evento se queda sin clip, y se registra.
"""

from __future__ import annotations

import logging
import queue
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Pedido:
    event_id: str
    ts: float              # instante del evento (epoch)
    listo_en: float        # cuando ya hay suficientes frames posteriores


def codec_disponible(preferido: str) -> str:
    preferido = (preferido or "auto").lower()
    if preferido in {"auto", "h264"}:
        try:
            import av  # noqa: F401

            return "h264"
        except ImportError:
            if preferido == "h264":
                log.warning("CLIP_CODEC=h264 requiere `pip install av`; se usa vp8")
    return "vp8"


def escribir_video(frames: list[tuple[float, bytes]], destino: Path, codec: str,
                   fps: float) -> Optional[Path]:
    """Arma el video con los JPEG del buffer. Devuelve la ruta final o None."""
    if not frames:
        return None
    primero = cv2.imdecode(np.frombuffer(frames[0][1], np.uint8), cv2.IMREAD_COLOR)
    if primero is None:
        return None
    alto, ancho = primero.shape[:2]
    ancho, alto = ancho - ancho % 2, alto - alto % 2   # los codecs piden medidas pares
    fps = max(1.0, min(30.0, fps))

    if codec == "h264":
        import av

        ruta = destino.with_suffix(".mp4")
        with av.open(str(ruta), "w", options={"movflags": "+faststart"}) as contenedor:
            flujo = contenedor.add_stream("libx264", rate=round(fps))
            flujo.width, flujo.height, flujo.pix_fmt = ancho, alto, "yuv420p"
            flujo.options = {"crf": "26", "preset": "veryfast"}
            for _, jpeg in frames:
                img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    continue
                img = cv2.resize(img, (ancho, alto)) if img.shape[:2] != (alto, ancho) else img
                for paquete in flujo.encode(av.VideoFrame.from_ndarray(img, format="bgr24")):
                    contenedor.mux(paquete)
            for paquete in flujo.encode():
                contenedor.mux(paquete)
        return ruta

    ruta = destino.with_suffix(".webm")
    escritor = cv2.VideoWriter(str(ruta), cv2.VideoWriter_fourcc(*"VP80"), fps, (ancho, alto))
    if not escritor.isOpened():
        log.error("OpenCV no pudo abrir el codificador VP8: sin clip")
        return None
    try:
        for _, jpeg in frames:
            img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            if img.shape[:2] != (alto, ancho):
                img = cv2.resize(img, (ancho, alto))
            escritor.write(img)
    finally:
        escritor.release()
    return ruta


class GrabadorClips:
    """Buffer circular de frames + armado y subida de clips bajo pedido."""

    def __init__(self, cfg, subir, *, reloj=time.time) -> None:
        self.cfg = cfg
        self.subir = subir          # funcion (event_id, ruta_video) -> bool
        self.reloj = reloj
        self.pre = float(cfg.clip_pre_s)
        self.post = float(cfg.clip_post_s)
        self.ancho = int(cfg.clip_ancho)
        self.calidad = int(cfg.clip_calidad)
        self.codec = codec_disponible(cfg.clip_codec)
        segundos_buffer = self.pre + self.post + 20.0
        self._buffer: deque[tuple[float, bytes]] = deque(
            maxlen=max(10, int(segundos_buffer * max(1.0, cfg.infer_fps) * 1.5)))
        self._entrada: queue.Queue = queue.Queue(maxsize=4)
        self._pedidos: list[Pedido] = []
        self._candado = threading.Lock()
        self._parar = threading.Event()
        self.grabados = 0
        self.perdidos = 0
        self._hilo_buffer = threading.Thread(target=self._bucle_buffer, name=f"clips-buffer-{cfg.camera_id}",
                                             daemon=True)
        self._hilo_armado = threading.Thread(target=self._bucle_armado, name=f"clips-{cfg.camera_id}",
                                             daemon=True)
        self._hilo_buffer.start()
        self._hilo_armado.start()
        log.info("[%s] Clips de alertas: %.0fs antes y %.0fs despues (%s)",
                 cfg.camera_id, self.pre, self.post, self.codec)

    # -- interfaz de complemento del worker --------------------------------

    def al_frame(self, frame) -> None:
        """Lo llama el bucle de deteccion. Solo deja la referencia: si el hilo
        de compresion va atrasado, se pierde ese frame del clip y no un frame
        de deteccion."""
        try:
            self._entrada.put_nowait((frame.ts, frame.frame))
        except queue.Full:
            pass

    def eventos(self) -> list:
        return []

    def estado(self) -> dict:
        with self._candado:
            pendientes = len(self._pedidos)
        return {"clips": {"grabados": self.grabados, "pendientes": pendientes,
                          "sin_frames": self.perdidos, "codec": self.codec}}

    def cerrar(self) -> None:
        self._parar.set()
        self._hilo_buffer.join(timeout=3)
        self._hilo_armado.join(timeout=10)

    # -- pedidos -----------------------------------------------------------

    def solicitar(self, event_id: str, ts: float) -> None:
        with self._candado:
            if any(p.event_id == event_id for p in self._pedidos):
                return
            self._pedidos.append(Pedido(event_id, ts, ts + self.post))

    def al_responder(self, lote: dict, respuesta: dict) -> None:
        """Enganche de HttpSink: pide clip de los eventos que fueron alerta."""
        severos = {m.get("event_id") for m in respuesta.get("matches", [])
                   if m.get("severity") in {"warning", "critical"}}
        if not severos:
            return
        for evento in lote.get("events", []):
            if evento.get("event_id") in severos:
                self.solicitar(evento["event_id"], _epoch(evento.get("ts")) or self.reloj())

    # -- hilos -------------------------------------------------------------

    def _bucle_buffer(self) -> None:
        while not self._parar.is_set():
            try:
                ts, img = self._entrada.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                alto, ancho = img.shape[:2]
                if self.ancho and ancho > self.ancho:
                    img = cv2.resize(img, (self.ancho, int(alto * self.ancho / ancho)),
                                     interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), self.calidad])
                if ok:
                    with self._candado:
                        self._buffer.append((ts, buf.tobytes()))
            except Exception as e:  # noqa: BLE001 - el clip nunca tumba la deteccion
                log.debug("No se pudo comprimir un frame para clips: %s", e)

    def _bucle_armado(self) -> None:
        while not self._parar.wait(0.5):
            self.procesar_pendientes()
        self.procesar_pendientes(forzar=True)

    def procesar_pendientes(self, forzar: bool = False) -> int:
        ahora = self.reloj()
        with self._candado:
            listos = [p for p in self._pedidos if forzar or ahora >= p.listo_en]
            self._pedidos = [p for p in self._pedidos if p not in listos]
            copia = list(self._buffer)
        hechos = 0
        for pedido in listos:
            frames = [(ts, j) for ts, j in copia if pedido.ts - self.pre <= ts <= pedido.ts + self.post]
            if len(frames) < 3:
                self.perdidos += 1
                log.info("[%s] Sin frames para el clip de %s (el evento llego tarde)",
                         self.cfg.camera_id, pedido.event_id)
                continue
            duracion = max(1e-3, frames[-1][0] - frames[0][0])
            fps = (len(frames) - 1) / duracion
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    ruta = escribir_video(frames, Path(tmp) / pedido.event_id, self.codec, fps)
                except Exception as e:  # noqa: BLE001
                    log.error("[%s] No se pudo armar el clip de %s: %s", self.cfg.camera_id,
                              pedido.event_id, e)
                    ruta = None
                if ruta is not None and self.subir(pedido.event_id, ruta):
                    self.grabados += 1
                    hechos += 1
        return hechos


def _epoch(ts) -> Optional[float]:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def subir_a_api(cfg):
    """Funcion que sube un clip a POST /api/events/{id}/clip."""
    import httpx

    url = cfg.api_url.rstrip("/")
    cliente = httpx.Client(timeout=60.0, headers={"X-API-Token": cfg.api_token})

    def _subir(event_id: str, ruta: Path) -> bool:
        tipo = "video/mp4" if ruta.suffix == ".mp4" else "video/webm"
        try:
            r = cliente.post(f"{url}/api/events/{event_id}/clip", content=ruta.read_bytes(),
                             headers={"Content-Type": tipo})
            if r.status_code >= 400:
                log.warning("La API rechazo el clip de %s: %s %s", event_id, r.status_code, r.text[:200])
                return False
            log.info("Clip de %s subido (%d KB)", event_id, ruta.stat().st_size // 1024)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo subir el clip de %s: %s", event_id, e)
            return False

    return _subir


def crear_grabador(cfg, sink) -> Optional[GrabadorClips]:
    """El grabador de la camara, enganchado a las respuestas de la API."""
    if not getattr(cfg, "clip_enabled", False) or not (cfg.api_url and cfg.api_token):
        return None
    http = getattr(sink, "http", None)
    if http is None:
        return None
    grabador = GrabadorClips(cfg, subir_a_api(cfg))
    anterior = http.al_responder

    def _encadenado(lote, respuesta):
        grabador.al_responder(lote, respuesta)
        if anterior is not None:
            anterior(lote, respuesta)

    http.al_responder = _encadenado
    return grabador
