"""Pruebas del video por WebRTC (go2rtc): la configuracion que recibe el
dashboard, la verificacion de sesion que usa el proxy, el canal de cajas
(worker -> API -> navegador por SSE) y el publicador del worker.

    python tests/test_webrtc.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

_TMP = Path(tempfile.mkdtemp())
from bd_prueba import borrar as _borrar_bd  # noqa: E402
from bd_prueba import url_temporal  # noqa: E402

os.environ["DATABASE_URL"] = url_temporal(_TMP, "webrtc")
os.environ["API_TOKEN"] = "token-de-prueba-del-worker"
os.environ["GO2RTC_URL"] = "http://go2rtc.local:1984/"

import warnings  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

import httpx  # noqa: E402
import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from api.database import engine, init_db  # noqa: E402
from api.main import app  # noqa: E402
from api.models import Operator  # noqa: E402
from api.preview import BufferPreview, clave_pistas  # noqa: E402
from api.security import hash_password, limite_login  # noqa: E402
from edge.detectors.base import Detector, Pista, caja  # noqa: E402
from edge.preview import PreviewPublisher  # noqa: E402

WORKER = {"X-API-Token": "token-de-prueba-del-worker"}


def _cliente(nombre="admin", rol="admin"):
    init_db()
    with Session(engine) as s:
        if s.exec(select(Operator).where(Operator.username == nombre)).first() is None:
            s.add(Operator(username=nombre, display_name=nombre, role=rol,
                           password_hash=hash_password("clave-de-prueba-1")))
            s.commit()
    limite_login.exito("testclient")
    c = TestClient(app)
    token = c.post("/api/auth/login", json={"username": nombre, "password": "clave-de-prueba-1"}).json()["token"]
    return c, {"Authorization": f"Bearer {token}"}


def test_configuracion_y_csp():
    c, h = _cliente()
    r = c.get("/api/preview/config", headers=h)
    assert r.json() == {"modo": "webrtc", "go2rtc": "http://go2rtc.local:1984"}
    assert "connect-src 'self'" in r.headers["content-security-policy"]
    assert "http://go2rtc.local:1984" in r.headers["content-security-policy"], \
        "el navegador tiene que poder negociar con go2rtc"
    assert TestClient(app).get("/api/preview/config").status_code == 401


def test_verificar_sesion_para_el_proxy():
    c, h = _cliente()
    assert c.get("/api/auth/verificar", headers=h).status_code == 204
    # La cookie del login basta (el navegador la manda sola a /go2rtc).
    assert c.get("/api/auth/verificar").status_code == 204
    anonimo = TestClient(app)
    assert anonimo.get("/api/auth/verificar").status_code == 401
    assert anonimo.get("/api/auth/verificar", headers={"Authorization": "Bearer x.y.z"}).status_code == 401


def test_cajas_del_worker_al_navegador():
    c, h = _cliente()
    datos = {"ancho": 640, "alto": 360, "ts": 1.5, "objetos": [{"b": [1, 2, 3, 4], "t": "ABC-123-A", "c": "#0f0",
                                                               "k": "placa"}], "extra": "no pasa"}
    assert c.post("/api/preview/cam-w/pistas", json=datos).status_code == 401
    r = c.post("/api/preview/cam-w/pistas", json=datos, headers=WORKER)
    assert r.status_code == 200 and r.json()["espectadores"] == 0
    for malo in [b"no es json", b'{"sin": "objetos"}', b"[1,2]"]:
        assert c.post("/api/preview/cam-w/pistas", content=malo, headers=WORKER).status_code == 400
    assert c.post("/api/preview/cam-w/pistas", content=b"x" * 70000, headers=WORKER).status_code == 413
    camaras = [x["camera_id"] for x in c.get("/api/preview/camaras", headers=h).json()]
    assert not any("~" in x for x in camaras), "el canal de cajas no aparece como camara"


def test_sse_entrega_y_cuenta_espectadores():
    """Con un buffer propio, sin servidor: el flujo SSE entrega lo publicado
    y el que mira cuenta como espectador hasta que cierra."""
    b = BufferPreview()
    clave = clave_pistas("cam-s")

    async def caso():
        b.publicar(clave, b'{"objetos":[]}')
        flujo = b.flujo_crudo(clave)
        primero = await flujo.__anext__()
        assert b.publicar(clave, b'{"objetos":[1]}') == 1
        segundo = await flujo.__anext__()
        await flujo.aclose()
        assert b.publicar(clave, b'{"objetos":[]}') == 0
        return primero, segundo

    primero, segundo = asyncio.run(caso())
    assert primero == b'{"objetos":[]}' and segundo == b'{"objetos":[1]}'
    assert b.camaras() == [], "solo claves de cajas: ninguna camara con video"


def test_sse_por_http():
    c, h = _cliente()
    anonimo = TestClient(app)
    assert anonimo.get("/api/preview/cam-h/pistas.sse").status_code == 401

    def publicar():
        for i in range(30):
            c.post("/api/preview/cam-h/pistas", headers=WORKER,
                   json={"ancho": 10, "alto": 10, "objetos": [{"b": [i, 0, 1, 1]}]})
            time.sleep(0.05)

    hilo = threading.Thread(target=publicar, daemon=True)
    hilo.start()
    lineas = []
    with c.stream("GET", "/api/preview/cam-h/pistas.sse", headers=h) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        for linea in r.iter_lines():
            lineas.append(linea)
            if sum(x.startswith("data: ") for x in lineas) >= 2:
                break
    assert lineas[0] == "retry: 3000"
    evento = json.loads(next(x for x in lineas if x.startswith("data: "))[6:])
    assert set(evento) == {"ancho", "alto", "ts", "objetos"}
    hilo.join(timeout=5)


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------

def test_cajas_de_los_detectores():
    class Det(Detector):
        name = "x"

        def procesar(self, frame):
            return []

        def pistas(self):
            return [Pista(3, "persona", (1.4, 2, 30, 60)), Pista(4, "vehiculo", (5, 5, 50, 50), etiqueta="truck")]

    cajas = Det().cajas()
    assert cajas[0] == {"b": [1, 2, 30, 60], "t": "persona #3", "c": "#9aa4b1", "k": "persona"}
    assert cajas[1]["t"] == "camión #4" and cajas[1]["k"] == "vehiculo"
    assert caja((0, 0, 1, 1), "", "#fff", "esqueleto", p=[None])["p"] == [None]


def test_publicador_manda_cajas_solo_si_miran():
    recibidos = []
    espectadores = {"n": 0}

    def servidor(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pistas"):
            recibidos.append((request.headers["content-type"], json.loads(request.content)))
            return httpx.Response(200, json={"espectadores": espectadores["n"]})
        return httpx.Response(200, json={"espectadores": 0})

    p = PreviewPublisher("http://api.local", "t", "cam-p", fps=20)
    p._cliente = httpx.Client(transport=httpx.MockTransport(servidor), headers={"Content-Type": "image/jpeg"})
    try:
        assert p.quiere_pistas(), "el primer envio sirve para preguntar si alguien mira"
        p.publicar_pistas({"ancho": 1, "alto": 1, "objetos": []})
        _esperar(lambda: len(recibidos) == 1)
        assert recibidos[0][0] == "application/json"
        assert not p.quiere_pistas(), "nadie mira: solo un sondeo cada 2 s"
        espectadores["n"] = 1
        p._ultimo_pistas = 0
        p.publicar_pistas({"ancho": 1, "alto": 1, "objetos": []})
        _esperar(lambda: len(recibidos) == 2)
        _esperar(lambda: p.stats["espectadores_webrtc"] == 1)
        time.sleep(0.06)
        assert p.quiere_pistas(), "con alguien mirando, al ritmo del preview"
    finally:
        p.cerrar()


def _esperar(condicion, segundos=3.0):
    fin = time.time() + segundos
    while time.time() < fin:
        if condicion():
            return
        time.sleep(0.02)
    raise AssertionError("no se cumplio a tiempo")


def test_worker_arma_las_cajas_de_todos_los_detectores():
    from edge.worker import Camara

    class Det:
        name = "d"

        def __init__(self, cajas):
            self._c = cajas

        def cajas(self):
            if self._c is None:
                raise RuntimeError("falla")
            return self._c

    cam = Camara.__new__(Camara)
    cam.id = "cam-x"
    cam.detectores = [Det([{"b": [0, 0, 1, 1]}]), Det(None), Det([{"b": [2, 2, 3, 3]}])]
    datos = cam.cajas_actuales(SimpleNamespace(ts=5.0, frame=np.zeros((360, 640, 3), np.uint8)))
    assert datos == {"ancho": 640, "alto": 360, "ts": 5.0, "objetos": [{"b": [0, 0, 1, 1]}, {"b": [2, 2, 3, 3]}]}, \
        "un detector que falla no deja sin cajas a los demas"


def test_configuracion_de_go2rtc():
    import yaml

    from tools import go2rtc_config as G

    configs = [SimpleNamespace(camera_id="cam-entrada",
                               source="rtsp://admin:Cl'av#e@192.168.1.64:554/Streaming/Channels/102"),
               SimpleNamespace(camera_id="cam-webcam", source="webcam:0")]
    streams = G.streams_desde(configs)
    assert list(streams) == ["cam-entrada"], "una webcam no pasa por go2rtc"
    texto = G.generar(streams, "192.168.1.10", principal=True)
    datos = yaml.safe_load(texto)
    assert datos["streams"]["cam-entrada"] == "rtsp://admin:Cl'av#e@192.168.1.64:554/Streaming/Channels/101", \
        "comillas y '#' de la contrasena sobreviven al YAML; canal principal"
    assert datos["webrtc"]["candidates"] == ["192.168.1.10:8555"]
    assert G.canal_principal("rtsp://x/Streaming/Channels/1602?transport=tcp") == \
        "rtsp://x/Streaming/Channels/1601?transport=tcp"
    assert yaml.safe_load(G.generar({}, None))["streams"] == {}


# --------------------------------------------------------------------------

def main() -> int:
    pruebas = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    fallos = 0
    try:
        for nombre, fn in pruebas:
            try:
                fn()
                print(f"  [OK]    {nombre}")
            except AssertionError as e:
                fallos += 1
                print(f"  [FALLA] {nombre}: {e}")
            except Exception as e:  # noqa: BLE001
                fallos += 1
                import traceback

                traceback.print_exc()
                print(f"  [ERROR] {nombre}: {type(e).__name__}: {e}")
    finally:
        engine.dispose()
        _borrar_bd(os.environ["DATABASE_URL"])
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
