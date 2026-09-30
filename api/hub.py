"""Difusion de alertas en tiempo real por WebSocket."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Callable, Optional

from fastapi import WebSocket

log = logging.getLogger(__name__)

# Un dashboard que no acepta un mensaje en este tiempo se da por caido. Sin
# limite, un navegador colgado (portatil suspendida con la pestana abierta)
# frenaba la difusion a todos los demas.
TIMEOUT_ENVIO = 3.0


def _serializar(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


class Hub:
    """Mantiene las conexiones abiertas del dashboard y les reparte eventos.

    Difundir NUNCA debe hacer fallar la ingesta: si un navegador se desconecta
    a media escritura, eso no puede propagarse hasta devolverle un error 500 al
    worker de borde. Por eso cada envio va con su propio try y los clientes
    muertos se retiran en silencio. Los envios van en paralelo: el mensaje
    tarda lo que el cliente mas lento, no la suma de todos.

    Con REDIS_URL (varios procesos de API, ver api/redis_compartido.py), el
    mensaje se publica en Redis y CADA proceso lo entrega a sus dashboards:
    la alerta llega aunque el dashboard este conectado a otro proceso.
    """

    def __init__(self) -> None:
        self._clientes: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._canal = None
        self._total_global = 0
        # Enganche de las notificaciones externas (api/notificaciones.py).
        # Se llama en el proceso que CREA la alerta, no en los que solo la
        # reciben por Redis: con cuatro procesos el aviso sale una vez.
        self.al_alertar: Optional[Callable[[dict], None]] = None

    # -- Redis (opcional) ------------------------------------------------

    def usar_redis(self, canal) -> None:
        self._canal = canal

    @property
    def distribuido(self) -> bool:
        return self._canal is not None

    async def refrescar_total(self) -> None:
        if self._canal is None:
            return
        try:
            await self._canal.anunciar_dashboards(len(self._clientes))
            self._total_global = await self._canal.total_dashboards()
        except Exception as e:  # noqa: BLE001
            log.debug("No se pudo refrescar el conteo global de dashboards: %s", e)

    # -- Conexiones -------------------------------------------------------

    async def conectar(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clientes.add(ws)
        log.info("Dashboard conectado (%d activos)", len(self._clientes))

    async def desconectar(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clientes.discard(ws)
        log.info("Dashboard desconectado (%d activos)", len(self._clientes))

    async def _enviar(self, ws: WebSocket, mensaje: str) -> bool:
        try:
            await asyncio.wait_for(ws.send_text(mensaje), TIMEOUT_ENVIO)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def difundir(self, tipo: str, datos: dict, notificar: bool = True) -> None:
        if tipo == "alert" and notificar and self.al_alertar is not None:
            try:
                self.al_alertar(datos)
            except Exception as e:  # noqa: BLE001 - notificar nunca frena la difusion
                log.error("No se pudo encolar la notificacion: %s", e)
        mensaje = json.dumps({"type": tipo, "data": datos}, default=_serializar,
                             ensure_ascii=False)
        if self._canal is not None:
            from api.redis_compartido import CANAL_WS

            try:
                await self._canal.publicar(CANAL_WS, mensaje)
                return
            except Exception as e:  # noqa: BLE001
                # Sin Redis, al menos los dashboards de este proceso se enteran.
                log.error("No se pudo publicar en Redis (%s); se entrega solo localmente", e)
        await self.entregar(mensaje)

    async def entregar(self, mensaje: str) -> None:
        """Envia un mensaje ya serializado a los dashboards de ESTE proceso."""
        async with self._lock:
            clientes = list(self._clientes)
        if not clientes:
            return

        resultados = await asyncio.gather(*(self._enviar(ws, mensaje) for ws in clientes))
        caidos = [ws for ws, ok in zip(clientes, resultados) if not ok]
        if caidos:
            async with self._lock:
                for ws in caidos:
                    self._clientes.discard(ws)

    @property
    def conectados(self) -> int:
        """Dashboards conectados a este proceso."""
        return len(self._clientes)

    @property
    def conectados_total(self) -> int:
        """Dashboards conectados a todos los procesos (con Redis) o a este."""
        if self._canal is None:
            return len(self._clientes)
        return max(self._total_global, len(self._clientes))


hub = Hub()
_canal_global: Optional[Any] = None
