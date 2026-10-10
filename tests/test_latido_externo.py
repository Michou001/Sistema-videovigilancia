"""Pruebas del latido externo (aviso cuando se apaga el sistema completo).

    python tests/test_latido_externo.py

No salen a internet: las peticiones van a un transporte simulado de httpx.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import httpx  # noqa: E402

from api import latido_externo  # noqa: E402
from api.latido_externo import ConfigLatido, configuracion, latir  # noqa: E402

URL = "https://hc-ping.com/0f1e2d3c-secreto"


def _con_env(**valores):
    """Aplica variables de entorno y devuelve una funcion que las restaura."""
    previos = {k: os.environ.get(k) for k in valores}
    for k, v in valores.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    def restaurar():
        for k, v in previos.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return restaurar


def _correr(cfg: ConfigLatido, responder, latidos: int) -> list[httpx.Request]:
    """Corre el latido hasta recibir `latidos` peticiones y lo cancela."""
    recibidas: list[httpx.Request] = []
    listo = asyncio.Event()

    def manejar(peticion: httpx.Request) -> httpx.Response:
        recibidas.append(peticion)
        if len(recibidas) >= latidos:
            listo.set()
        return responder(len(recibidas))

    async def principal():
        tarea = asyncio.create_task(latir(cfg, transporte=httpx.MockTransport(manejar)))
        try:
            await asyncio.wait_for(listo.wait(), timeout=5)
        finally:
            tarea.cancel()
            try:
                await tarea
            except asyncio.CancelledError:
                pass

    asyncio.run(principal())
    return recibidas


def test_sin_url_no_se_activa():
    restaurar = _con_env(LATIDO_EXTERNO_URL=None)
    try:
        assert configuracion() is None
    finally:
        restaurar()


def test_url_invalida_no_se_activa():
    restaurar = _con_env(LATIDO_EXTERNO_URL="file:///etc/passwd")
    try:
        assert configuracion() is None
    finally:
        restaurar()


def test_intervalo_tiene_minimo():
    restaurar = _con_env(LATIDO_EXTERNO_URL=URL, LATIDO_EXTERNO_S="1")
    try:
        cfg = configuracion()
        assert cfg is not None and cfg.cada_s == latido_externo.MINIMO_S
        assert cfg.dominio == "hc-ping.com"
    finally:
        restaurar()


def test_late_periodicamente_sin_datos():
    cfg = ConfigLatido(url=URL, cada_s=0.01)
    peticiones = _correr(cfg, lambda n: httpx.Response(200), latidos=3)
    assert len(peticiones) >= 3
    for p in peticiones:
        assert p.method == "GET" and str(p.url) == URL
        assert not p.content, "el latido no debe llevar datos"


def test_un_fallo_no_detiene_el_latido_ni_expone_la_url():
    cfg = ConfigLatido(url=URL, cada_s=0.01)
    registros: list[str] = []

    class Captura(logging.Handler):
        def emit(self, registro):
            registros.append(registro.getMessage())

    manejador = Captura()
    registro = logging.getLogger("api.latido_externo")
    nivel = registro.level
    registro.setLevel(logging.INFO)
    registro.addHandler(manejador)
    try:
        # Falla el 1o y el 2o; despues responde bien: el ciclo debe seguir.
        peticiones = _correr(cfg, lambda n: httpx.Response(503 if n <= 2 else 200), latidos=4)
    finally:
        registro.removeHandler(manejador)
        registro.setLevel(nivel)
    assert len(peticiones) >= 4
    assert any("restablecido" in r for r in registros)
    assert not any("secreto" in r for r in registros), "la URL secreta no va al log"


def test_el_log_de_httpx_no_muestra_la_url_secreta():
    cfg = ConfigLatido(url=URL, cada_s=0.01)
    registros: list[str] = []

    class Captura(logging.Handler):
        def emit(self, registro):
            registros.append(registro.getMessage())

    manejador = Captura()
    registro = logging.getLogger("httpx")
    nivel = registro.level
    registro.setLevel(logging.INFO)
    registro.addHandler(manejador)
    try:
        _correr(cfg, lambda n: httpx.Response(200), latidos=2)
    finally:
        registro.removeHandler(manejador)
        registro.setLevel(nivel)
    assert registros, "httpx deberia registrar las peticiones"
    assert not any("secreto" in r for r in registros), registros
    assert all("hc-ping.com/***" in r for r in registros), registros


def main() -> int:
    pruebas = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    fallos = 0
    for nombre, fn in pruebas:
        try:
            fn()
            print(f"  [OK]    {nombre}")
        except AssertionError as e:
            fallos += 1
            print(f"  [FALLA] {nombre}: {e}")
        except Exception as e:  # noqa: BLE001
            fallos += 1
            print(f"  [ERROR] {nombre}: {type(e).__name__}: {e}")
    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
