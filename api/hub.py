"""Difusion de alertas en tiempo real por WebSocket."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

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
    """

    def __init__(self) -> None:
        self._clientes: set[WebSocket] = set()
        self._lock = asyncio.Lock()

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

    async def difundir(self, tipo: str, datos: dict) -> None:
        mensaje = json.dumps({"type": tipo, "data": datos}, default=_serializar,
                             ensure_ascii=False)
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
        return len(self._clientes)


hub = Hub()
