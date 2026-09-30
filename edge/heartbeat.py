"""Latido del worker hacia la API.

Sin esto la API solo sabia que una camara estaba viva cuando llegaba un
evento: una camara funcionando perfectamente en un pasillo vacio aparecia como
caida en el dashboard al minuto. El latido va cada pocos segundos con la salud
de la fuente (fps reales, reconexiones, frames perdidos) y un resumen de cada
detector, asi el operador distingue "no pasa nada" de "no se ve nada".
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)


class Heartbeat:
    def __init__(self, base_url: str, token: str, camera_id: str,
                 estado: Callable[[], dict], intervalo: float = 15.0) -> None:
        import httpx

        self.url = f"{base_url.rstrip('/')}/api/events/heartbeat"
        self.camera_id = camera_id
        self.estado = estado
        self.intervalo = intervalo
        self._cliente = httpx.Client(timeout=5.0, headers={"X-API-Token": token})
        self._parar = threading.Event()
        self._fallos = 0
        self._hilo = threading.Thread(target=self._bucle, name="heartbeat", daemon=True)
        self._hilo.start()

    def _bucle(self) -> None:
        # El primero sale de inmediato: la camara aparece en el dashboard en
        # cuanto arranca el worker, no a los 15 s.
        while not self._parar.is_set():
            self._latir()
            self._parar.wait(self.intervalo)

    def _latir(self) -> None:
        try:
            datos = self.estado()
        except Exception as e:  # noqa: BLE001 - el latido nunca tumba al worker
            datos = {"error": f"{type(e).__name__}: {e}"}
        try:
            r = self._cliente.post(self.url, params={"camera_id": self.camera_id}, json=datos)
            r.raise_for_status()
            self._fallos = 0
        except Exception as e:  # noqa: BLE001
            self._fallos += 1
            if self._fallos in (1, 20) or self._fallos % 100 == 0:
                log.debug("Heartbeat sin respuesta (%d seguidos): %s", self._fallos, e)

    def cerrar(self) -> None:
        self._parar.set()
        self._hilo.join(timeout=6.0)
        self._cliente.close()
