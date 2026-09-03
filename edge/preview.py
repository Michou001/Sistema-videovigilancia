"""Publica el frame anotado hacia la API para la vista en vivo del dashboard.

Dos reglas gobiernan este modulo, y las dos son sobre no estorbar:

1. NUNCA BLOQUEAR LA CAPTURA. Codificar un JPEG son ~5 ms y subirlo por red
   puede ser cualquier cosa. Hacerlo en el bucle de deteccion le robaria fps a
   los modelos y, con una fuente en vivo, frames perdidos. Por eso todo pasa
   en un hilo aparte con un buzon de UN solo frame: si el hilo va atrasado, el
   frame viejo se tira y se queda el nuevo. Preferir el frame reciente es
   justo lo que quiere una vista en vivo.

2. NO GASTAR SI NADIE MIRA. La API responde cuantos dashboards estan viendo
   esta camara. Con cero, el worker baja a un frame cada dos segundos: lo
   justo para enterarse de que alguien abrio el dashboard. Un sistema que
   corre meses sin que nadie mire no debe pagar por ello.

El worker pregunta primero con `quiere_frame()` y solo entonces dibuja las
cajas. Asi el anotado tampoco se paga cuando no hay nadie: la decision de si
un frame hace falta vive aqui, no repartida por el bucle de captura.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Con nadie mirando, cada cuanto se manda un frame solo para preguntar. Dos
# segundos es lo que tarda el recuadro en aparecer cuando el operador abre el
# dashboard: se percibe como una pequena espera, no como algo roto.
SONDEO_SIN_ESPECTADORES = 2.0


class PreviewPublisher:
    """Sube frames anotados a la API, sin frenar al worker."""

    def __init__(self, base_url: str, token: str, camera_id: str, *,
                 fps: float = 6.0, ancho: int = 640, calidad: int = 70,
                 timeout: float = 4.0) -> None:
        import httpx

        self.url = f"{base_url.rstrip('/')}/api/preview/{camera_id}"
        self.camera_id = camera_id
        self.intervalo = 1.0 / fps if fps > 0 else 0.0
        self.ancho = ancho
        self.calidad = int(max(20, min(95, calidad)))

        self._cliente = httpx.Client(
            timeout=timeout,
            headers={"X-API-Token": token, "Content-Type": "image/jpeg"},
        )

        self._buzon: Optional[np.ndarray] = None
        self._lock = threading.Lock()
        self._hay_frame = threading.Event()
        self._parar = threading.Event()

        self._espectadores = 0
        self._ultimo_tomado = 0.0
        self._enviados = 0
        self._descartados = 0
        self._fallos = 0

        # daemon: si el worker muere de forma abrupta, este hilo no debe
        # mantener el proceso vivo esperando una respuesta HTTP.
        self._hilo = threading.Thread(target=self._bucle, name="preview",
                                      daemon=True)
        self._hilo.start()

    # ----------------------------------------------------------------------

    def quiere_frame(self) -> bool:
        """Si hace falta un frame AHORA. Barato: el worker lo llama por frame.

        Preguntar antes de anotar es el punto: dibujar las cajas de tres
        detectores sobre una copia de 1080p no es gratis, y a 8 fps se paga
        todo el dia. Con esto solo se paga cuando alguien esta mirando.
        """
        if self._parar.is_set():
            return False
        ahora = time.monotonic()
        if self._espectadores == 0:
            return (ahora - self._ultimo_tomado) >= SONDEO_SIN_ESPECTADORES
        # Limite de fps del preview, independiente del de inferencia: el worker
        # puede detectar a 8 fps y mandar 6 al dashboard.
        return not self.intervalo or (ahora - self._ultimo_tomado) >= self.intervalo

    def publicar(self, frame: np.ndarray) -> None:
        """Ofrece un frame anotado. Vuelve de inmediato.

        Lo llama el bucle de captura, asi que aqui no se codifica, no se
        redimensiona y no se toca la red: solo se deja la referencia en el
        buzon. El coste es una copia de puntero.
        """
        if self._parar.is_set():
            return

        self._ultimo_tomado = time.monotonic()
        with self._lock:
            if self._buzon is not None:
                # El hilo no alcanzo a subir el anterior. Se tira: en vivo, el
                # frame mas nuevo siempre gana.
                self._descartados += 1
            self._buzon = frame
        self._hay_frame.set()

    def cerrar(self) -> None:
        self._parar.set()
        self._hay_frame.set()
        self._hilo.join(timeout=3.0)
        self._cliente.close()

    @property
    def stats(self) -> dict:
        return {
            "enviados": self._enviados,
            "descartados": self._descartados,
            "espectadores": self._espectadores,
            "fallos": self._fallos,
        }

    @property
    def espectadores(self) -> int:
        return self._espectadores

    # ----------------------------------------------------------------------

    def _bucle(self) -> None:
        """Codifica y sube. Todo el ritmo lo decide `quiere_frame()`."""
        while not self._parar.is_set():
            self._hay_frame.wait(timeout=SONDEO_SIN_ESPECTADORES)
            if self._parar.is_set():
                break
            self._hay_frame.clear()

            with self._lock:
                frame = self._buzon
                self._buzon = None
            if frame is None:
                continue

            jpeg = self._codificar(frame)
            if jpeg is not None:
                self._subir(jpeg)

    def _codificar(self, frame: np.ndarray) -> Optional[bytes]:
        try:
            alto, ancho = frame.shape[:2]
            if self.ancho and ancho > self.ancho:
                escala = self.ancho / ancho
                # INTER_AREA es el correcto al reducir: promedia los pixeles en
                # vez de muestrear, y con eso las cajas y el texto de las
                # anotaciones siguen legibles a la mitad de tamano.
                frame = cv2.resize(frame, (self.ancho, int(alto * escala)),
                                   interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", frame,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), self.calidad])
            return buf.tobytes() if ok else None
        except Exception as e:  # noqa: BLE001 - el preview nunca tumba al worker
            log.debug("No se pudo codificar el frame de preview: %s", e)
            return None

    def _subir(self, jpeg: bytes) -> None:
        try:
            r = self._cliente.post(self.url, content=jpeg)
            if r.status_code == 401:
                log.error("La API rechazo el token en el preview (401). "
                          "Se desactiva la vista en vivo de esta camara.")
                self._parar.set()
                return
            r.raise_for_status()
            self._espectadores = int(r.json().get("espectadores", 0))
            self._enviados += 1
            self._fallos = 0
        except Exception as e:  # noqa: BLE001
            self._fallos += 1
            # El preview es prescindible: si la API no esta, los eventos ya se
            # guardan en el spool y eso es lo que importa. Aqui solo se avisa,
            # y con cuentagotas para no llenar el log.
            if self._fallos in (1, 20) or self._fallos % 100 == 0:
                log.warning("Preview: fallo el envio (%d seguidos): %s",
                            self._fallos, e)
            # Se asume que nadie mira: asi el worker baja a sondeo y deja de
            # martillar una API caida a 6 peticiones por segundo.
            self._espectadores = 0


def crear_publicador(cfg) -> Optional[PreviewPublisher]:
    """Crea el publicador si la configuracion lo permite. None si no."""
    if not cfg.preview_enabled:
        return None
    if not (cfg.api_url and cfg.api_token):
        log.info("Sin API_URL/API_TOKEN: no hay vista en vivo en el dashboard.")
        return None

    log.info("Vista en vivo activa (%.0f fps, %d px, calidad %d)",
             cfg.preview_fps, cfg.preview_width, cfg.preview_quality)
    return PreviewPublisher(
        cfg.api_url, cfg.api_token, cfg.camera_id,
        fps=cfg.preview_fps, ancho=cfg.preview_width,
        calidad=cfg.preview_quality,
    )
