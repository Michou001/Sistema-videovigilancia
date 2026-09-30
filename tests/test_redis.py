"""Pruebas del modo multi-proceso de la API (estado compartido en Redis).

    PRUEBAS_REDIS=redis://localhost:6379/15 python tests/test_redis.py

Simulan DOS procesos de API con dos juegos de objetos que solo se comunican
por Redis: una alerta publicada en el "proceso A" llega a un dashboard del
"proceso B", el video subido a A se ve desde B, el limite de login es uno
solo y dar de alta una placa invalida la cache de ambos.

Sin PRUEBAS_REDIS se usa fakeredis (si esta instalado); sin ninguno de los
dos, se omiten.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from api.redis_compartido import (  # noqa: E402
    CANAL_INVALIDAR,
    CANAL_WS,
    CanalRedis,
    LimiteIntentosRedis,
    PreviewRedis,
    sumar_vigentes,
)

URL = os.getenv("PRUEBAS_REDIS", "").strip()
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"


def _clientes():
    """(async, sync) contra el mismo servidor, real o falso."""
    if URL:
        import redis
        import redis.asyncio as aioredis

        return aioredis.from_url(URL), redis.from_url(URL)
    import fakeredis

    servidor = fakeredis.FakeServer()
    return fakeredis.FakeAsyncRedis(server=servidor), fakeredis.FakeRedis(server=servidor)


def _hay_redis() -> bool:
    if URL:
        return True
    try:
        import fakeredis  # noqa: F401
        return True
    except ImportError:
        return False


class _WsFalso:
    def __init__(self) -> None:
        self.recibidos: list[str] = []

    async def accept(self) -> None:
        pass

    async def send_text(self, texto: str) -> None:
        self.recibidos.append(texto)


async def _limpiar(r) -> None:
    async for clave in r.scan_iter(match="goss:*"):
        await r.delete(clave)


# --------------------------------------------------------------------------

def test_limite_de_login_es_uno_solo_para_todos_los_procesos():
    async def _prueba():
        r, s = _clientes()
        await _limpiar(r)
        a, b = LimiteIntentosRedis(s, maximo=3), LimiteIntentosRedis(s, maximo=3)
        for _ in range(2):
            a.fallo("10.0.0.9")
        b.fallo("10.0.0.9")
        assert b.espera("10.0.0.9") > 0 and a.espera("10.0.0.9") > 0
        assert a.espera("10.0.0.10") == 0, "otra IP no se bloquea"
        a.exito("10.0.0.9")
        assert b.espera("10.0.0.9") == 0
        await r.aclose()

    asyncio.run(_prueba())


def test_video_subido_a_un_proceso_se_ve_desde_otro():
    async def _prueba():
        r, _ = _clientes()
        await _limpiar(r)
        proceso_a, proceso_b = PreviewRedis(r, "host:1"), PreviewRedis(r, "host:2")
        recibidos: list[bytes] = []

        async def _mirar():
            async for parte in proceso_b.flujo_mjpeg("cam-r", sin_frames=3.0):
                recibidos.append(parte)
                if len(recibidos) >= 2:
                    return

        tarea = asyncio.create_task(_mirar())
        await asyncio.sleep(0.3)
        assert await proceso_a.apublicar("cam-r", JPEG) == 1, "A debe ver al espectador de B"
        await asyncio.sleep(0.2)
        await proceso_a.apublicar("cam-r", JPEG)
        await asyncio.wait_for(tarea, 5)
        assert len(recibidos) == 2 and all(JPEG in p for p in recibidos)
        await asyncio.sleep(0.1)
        assert await proceso_a.espectadores("cam-r") == 0, "al cerrar el flujo se descuenta"
        camaras = await proceso_b.acamaras()
        assert [c["camera_id"] for c in camaras] == ["cam-r"]
        await r.aclose()

    asyncio.run(_prueba())


def test_alerta_publicada_en_a_llega_al_dashboard_de_b():
    from api.hub import Hub

    async def _prueba():
        r, s = _clientes()
        await _limpiar(r)
        canal_a = CanalRedis("redis://prueba", r=r, sync=s, proceso="host:1")
        canal_b = CanalRedis("redis://prueba", r=r, sync=s, proceso="host:2")
        hub_a, hub_b = Hub(), Hub()
        hub_a.usar_redis(canal_a)
        hub_b.usar_redis(canal_b)

        async def _entregar_b(canal: str, datos: str) -> None:
            if canal == CANAL_WS:
                await hub_b.entregar(datos)

        canal_b.escuchar(_entregar_b)
        ws = _WsFalso()
        await hub_b.conectar(ws)
        await asyncio.sleep(0.3)
        await hub_a.difundir("alert", {"title": "Placa PZW-123-A en lista negra"})
        for _ in range(30):
            if ws.recibidos:
                break
            await asyncio.sleep(0.1)
        assert ws.recibidos and "PZW-123-A" in ws.recibidos[0]
        assert hub_a.conectados == 0 and hub_b.conectados == 1

        await hub_b.refrescar_total()
        await hub_a.refrescar_total()
        assert hub_a.conectados_total == 1, "el total cuenta los dashboards de todos los procesos"
        canal_b._tarea.cancel()
        await r.aclose()

    asyncio.run(_prueba())


def test_alta_de_placa_invalida_la_cache_de_todos():
    from api.matching import ListaNegraEnMemoria

    async def _prueba():
        r, s = _clientes()
        await _limpiar(r)
        canal = CanalRedis("redis://prueba", r=r, sync=s)
        cache_a, cache_b = ListaNegraEnMemoria(), ListaNegraEnMemoria()
        cache_b._placas = ["cargada"]
        cache_b._placas_en = time.monotonic()

        async def _al_mensaje(c: str, datos: str) -> None:
            if c == CANAL_INVALIDAR and datos == "lista_negra":
                cache_b.invalidar(difundir=False)

        canal.escuchar(_al_mensaje)
        await asyncio.sleep(0.3)
        cache_a.al_invalidar = lambda: canal.publicar_sync(CANAL_INVALIDAR, "lista_negra")
        await asyncio.to_thread(cache_a.invalidar)
        for _ in range(30):
            if cache_b._placas is None:
                break
            await asyncio.sleep(0.1)
        assert cache_b._placas is None
        canal._tarea.cancel()
        await r.aclose()

    asyncio.run(_prueba())


def test_conteos_de_procesos_muertos_no_cuentan():
    ahora = time.time()
    conteos = {b"vivo:1": f"2|{ahora}".encode(), b"muerto:2": f"5|{ahora - 300}".encode(),
               b"roto:3": b"x"}
    assert sumar_vigentes(conteos, ahora) == 2


def test_purga_la_hace_un_solo_proceso():
    async def _prueba():
        r, s = _clientes()
        await _limpiar(r)
        a = CanalRedis("redis://prueba", r=r, sync=s, proceso="host:1")
        b = CanalRedis("redis://prueba", r=r, sync=s, proceso="host:2")
        assert await a.tomar_turno("purga", 60)
        assert not await b.tomar_turno("purga", 60)
        await r.aclose()

    asyncio.run(_prueba())


# --------------------------------------------------------------------------

def main() -> int:
    pruebas = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    if not _hay_redis():
        print("  Sin PRUEBAS_REDIS ni fakeredis: se omiten las pruebas de Redis")
        print(f"\n{len(pruebas)}/{len(pruebas)} pruebas pasan (omitidas)")
        return 0
    print(f"  Redis: {URL or 'fakeredis'}")
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
