"""Latido externo: aviso por Telegram cuando se apaga el sistema completo.

El vigilante de camaras caidas (camaras_caidas.py) avisa si un worker deja de
latir, pero vive dentro de la API: si se apaga el equipo, se va la luz o se
cae la red, ya no queda nadie que avise. El silencio no dispara nada.

Aqui la API le dice cada LATIDO_EXTERNO_S segundos a un servicio FUERA del
equipo que sigue viva. Cuando los latidos dejan de llegar, ese servicio avisa
por Telegram (o correo); cuando vuelven, avisa que se recupero. Es un
"interruptor de hombre muerto": funciona justamente porque no depende de que
el equipo caido haga nada.

Probado con healthchecks.io (plan gratuito, integracion con Telegram incluida;
tambien se puede instalar uno propio, es de codigo abierto):

    LATIDO_EXTERNO_URL=https://hc-ping.com/<uuid-del-chequeo>
    LATIDO_EXTERNO_S=60

Solo se hace la peticion: no viaja ningun dato de camaras, eventos ni
personas. La URL lleva un identificador secreto, asi que no se escribe en
los logs (solo el dominio).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

MINIMO_S = 10.0


@dataclass(frozen=True)
class ConfigLatido:
    url: str
    cada_s: float

    @property
    def dominio(self) -> str:
        return urlparse(self.url).hostname or "?"


def configuracion() -> Optional[ConfigLatido]:
    """None si no esta configurado o la URL no es http(s)."""
    url = os.getenv("LATIDO_EXTERNO_URL", "").strip()
    if not url:
        return None
    if urlparse(url).scheme not in {"http", "https"} or not urlparse(url).hostname:
        log.error("LATIDO_EXTERNO_URL no es una URL http(s) valida: latido externo desactivado")
        return None
    try:
        cada_s = float(os.getenv("LATIDO_EXTERNO_S", "60"))
    except ValueError:
        cada_s = 60.0
    return ConfigLatido(url=url, cada_s=max(MINIMO_S, cada_s))


async def latir(cfg: ConfigLatido, transporte=None) -> None:
    """Manda el latido para siempre (hasta que cancelen la tarea).

    Un fallo de red no detiene el ciclo: si la API sigue viva pero sin
    internet, el servicio externo avisara igual (no le llegan latidos), que
    es lo correcto, porque tampoco saldrian las alertas por Telegram.
    """
    import httpx

    from api.notificaciones import _OcultarSecretos

    log.info("Latido externo cada %.0f s hacia %s", cfg.cada_s, cfg.dominio)
    # httpx escribe en su log la URL de cada peticion: se cambia por el dominio.
    oculta = f"{urlparse(cfg.url).scheme}://{cfg.dominio}/***"
    filtro = _OcultarSecretos(lambda texto: texto.replace(cfg.url, oculta))
    logging.getLogger("httpx").addFilter(filtro)
    try:
        await _ciclo(cfg, httpx.AsyncClient(timeout=10.0, transport=transporte))
    finally:
        logging.getLogger("httpx").removeFilter(filtro)


async def _ciclo(cfg: ConfigLatido, cliente) -> None:
    fallos = 0
    async with cliente:
        while True:
            try:
                respuesta = await cliente.get(cfg.url)
                respuesta.raise_for_status()
                if fallos:
                    log.info("Latido externo restablecido tras %d intento(s) fallido(s)", fallos)
                fallos = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - nunca debe tumbar la API
                fallos += 1
                # Sin llenar el log: el primero, el decimo y luego uno por hora.
                if fallos in (1, 10) or fallos % max(1, round(3600 / cfg.cada_s)) == 0:
                    log.warning("No se pudo enviar el latido externo a %s (%d seguidos): %s",
                                cfg.dominio, fallos, type(e).__name__)
            await asyncio.sleep(cfg.cada_s)
