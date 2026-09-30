"""Pruebas de los clips de video de las alertas: el grabador del borde (buffer,
pedido solo para alertas, armado del video), la subida a la API, la entrega
al dashboard y la retencion.

    python tests/test_clips.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

_TMP = Path(tempfile.mkdtemp())
from bd_prueba import borrar as _borrar_bd  # noqa: E402
from bd_prueba import url_temporal  # noqa: E402

os.environ["DATABASE_URL"] = url_temporal(_TMP, "clips")
os.environ["API_TOKEN"] = "token-de-prueba-del-worker"

import warnings  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from api.config import BASE_DIR, get_config  # noqa: E402
from api.database import engine, init_db  # noqa: E402
from api.main import app  # noqa: E402
from api.models import Alert, Operator  # noqa: E402
from api.retention import Politica, purgar  # noqa: E402
from api.security import hash_password, limite_login  # noqa: E402
from edge.clips import GrabadorClips, codec_disponible, crear_grabador, escribir_video  # noqa: E402

WORKER = {"X-API-Token": "token-de-prueba-del-worker"}
_archivos: list[Path] = []


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

def _jpeg(i: int, ancho=320, alto=180) -> bytes:
    img = np.full((alto, ancho, 3), (i * 9) % 255, np.uint8)
    cv2.putText(img, str(i), (20, 100), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
    return cv2.imencode(".jpg", img)[1].tobytes()


def _cfg(**extra) -> SimpleNamespace:
    base = dict(clip_pre_s=2.0, clip_post_s=2.0, clip_ancho=320, clip_calidad=70, clip_codec="vp8",
                infer_fps=10.0, camera_id="cam-clips", clip_enabled=True,
                api_url="http://api.local", api_token="t")
    base.update(extra)
    return SimpleNamespace(**base)


class _Reloj:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _alimentar(g: GrabadorClips, t0: float, segundos: float, fps: float = 10.0) -> None:
    """Mete frames con marca de tiempo al grabador, esperando a que el hilo
    de compresion los tome (la cola es corta a proposito)."""
    n = int(segundos * fps)
    for i in range(n):
        img = np.full((360, 640, 3), (i * 7) % 255, np.uint8)
        while g._entrada.full():
            time.sleep(0.002)
        g.al_frame(SimpleNamespace(ts=t0 + i / fps, frame=img))
    fin = time.time() + 5
    while (not g._entrada.empty() or len(g._buffer) < n) and time.time() < fin:
        time.sleep(0.01)


def _esperar(condicion, segundos=8.0) -> bool:
    fin = time.time() + segundos
    while time.time() < fin:
        if condicion():
            return True
        time.sleep(0.05)
    return False


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def _webm(n=12) -> bytes:
    ruta = escribir_video([(i / 8, _jpeg(i)) for i in range(n)], _TMP / f"v-{uuid.uuid4().hex}", "vp8", 8)
    return ruta.read_bytes()


def _usuario(nombre: str, rol: str) -> None:
    with Session(engine) as s:
        if s.exec(select(Operator).where(Operator.username == nombre)).first() is None:
            s.add(Operator(username=nombre, display_name=nombre, role=rol,
                           password_hash=hash_password("clave-de-prueba-1")))
            s.commit()


def _cliente(nombre: str = "admin", rol: str = "admin") -> tuple[TestClient, dict]:
    init_db()
    _usuario(nombre, rol)
    limite_login.exito("testclient")
    c = TestClient(app)
    token = c.post("/api/auth/login", json={"username": nombre, "password": "clave-de-prueba-1"}).json()["token"]
    return c, {"Authorization": f"Bearer {token}"}


def _alerta_de_sabotaje(c: TestClient) -> str:
    """Un evento de camara 'sabotaje' siempre es alerta critica."""
    evento_id = str(uuid.uuid4())
    r = c.post("/api/events", headers=WORKER, json={"camera_id": "cam-clips", "events": [
        {"event_id": evento_id, "camera_id": "cam-clips", "type": "camera", "value": "sabotaje",
         "confidence": 1.0}]})
    assert r.status_code == 200, r.text
    assert r.json()["matches"][0]["severity"] == "critical"
    return evento_id


# --------------------------------------------------------------------------
# Borde: armado del video
# --------------------------------------------------------------------------

def test_escribir_video_vp8_se_puede_reproducir():
    frames = [(i / 8, _jpeg(i)) for i in range(16)]
    ruta = escribir_video(frames, _TMP / "prueba", "vp8", fps=8)
    assert ruta is not None and ruta.suffix == ".webm"
    assert ruta.read_bytes()[:4] == b"\x1a\x45\xdf\xa3", "debe ser un contenedor WebM (EBML)"
    cap = cv2.VideoCapture(str(ruta))
    leidos = 0
    while cap.read()[0]:
        leidos += 1
    cap.release()
    assert leidos == 16, leidos


def test_escribir_video_h264_si_pyav_esta_instalado():
    if codec_disponible("auto") != "h264":
        print("        (PyAV no instalado: se omite H.264)")
        return
    frames = [(i / 8, _jpeg(i, 321, 181)) for i in range(16)]  # medidas impares a proposito
    ruta = escribir_video(frames, _TMP / "prueba264", "h264", fps=8)
    assert ruta is not None and ruta.suffix == ".mp4"
    assert ruta.read_bytes()[4:8] == b"ftyp"
    cap = cv2.VideoCapture(str(ruta))
    ok, img = cap.read()
    cap.release()
    assert ok and img.shape[:2] == (180, 320), "medidas pares para el codec"


def test_sin_frames_no_hay_video():
    assert escribir_video([], _TMP / "vacio", "vp8", fps=8) is None


# --------------------------------------------------------------------------
# Borde: grabador
# --------------------------------------------------------------------------

def test_clip_solo_para_eventos_que_fueron_alerta():
    t0 = 1_700_000_000.0
    reloj = _Reloj(t0)
    subidos: dict[str, bytes] = {}
    g = GrabadorClips(_cfg(), lambda eid, ruta: subidos.setdefault(eid, ruta.read_bytes()) is not None,
                      reloj=reloj)
    try:
        _alimentar(g, t0, 8.0)
        g.al_responder(
            {"events": [{"event_id": "evento-alerta", "ts": _iso(t0 + 4)},
                        {"event_id": "evento-normal", "ts": _iso(t0 + 4)}]},
            {"matches": [{"event_id": "evento-alerta", "severity": "critical"},
                         {"event_id": "evento-normal", "severity": "info"}]})
        # Antes de tener los segundos posteriores no se arma nada.
        reloj.t = t0 + 5
        time.sleep(0.7)
        assert not subidos, "todavia faltan frames posteriores"
        reloj.t = t0 + 6.1
        assert _esperar(lambda: "evento-alerta" in subidos), "el clip debio subirse"
        assert "evento-normal" not in subidos, "un evento info no lleva clip"
        video = subidos["evento-alerta"]
        assert video[:4] == b"\x1a\x45\xdf\xa3"
        ruta = _TMP / "subido.webm"
        ruta.write_bytes(video)
        cap = cv2.VideoCapture(str(ruta))
        n = 0
        while cap.read()[0]:
            n += 1
        cap.release()
        # [t0+2, t0+6] a 10 fps -> ~41 frames
        assert 35 <= n <= 42, n
        assert g.estado()["clips"]["grabados"] == 1
    finally:
        g.cerrar()


def test_evento_que_llego_tarde_se_queda_sin_clip():
    t0 = 1_700_000_000.0
    reloj = _Reloj(t0 + 100)
    subidos = []
    g = GrabadorClips(_cfg(), lambda eid, ruta: subidos.append(eid) or True, reloj=reloj)
    try:
        _alimentar(g, t0 + 90, 5.0)
        g.solicitar("evento-viejo", t0 + 10)   # hace 90 s: ya no esta en el buffer
        assert _esperar(lambda: g.perdidos == 1)
        assert subidos == []
    finally:
        g.cerrar()


def test_pedido_repetido_no_duplica_el_clip():
    t0 = 1_700_000_000.0
    g = GrabadorClips(_cfg(), lambda eid, ruta: True, reloj=_Reloj(t0))
    try:
        g.solicitar("e1", t0)
        g.solicitar("e1", t0)
        assert g.estado()["clips"]["pendientes"] == 1
    finally:
        g.cerrar()


def test_crear_grabador_se_encadena_a_la_respuesta_de_la_api():
    llamadas = []
    http = SimpleNamespace(al_responder=lambda lote, resp: llamadas.append("anterior"))
    sink = SimpleNamespace(http=http)
    g = crear_grabador(_cfg(), sink)
    assert g is not None
    try:
        http.al_responder({"events": [{"event_id": "e9", "ts": _iso(time.time())}]},
                          {"matches": [{"event_id": "e9", "severity": "warning"}]})
        assert llamadas == ["anterior"], "el enganche previo debe seguir llamandose"
        assert g.estado()["clips"]["pendientes"] == 1
    finally:
        g.cerrar()
    assert crear_grabador(_cfg(clip_enabled=False), sink) is None
    assert crear_grabador(_cfg(api_url=""), sink) is None
    assert crear_grabador(_cfg(), SimpleNamespace()) is None, "sin envio HTTP no hay a quien subir"


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def test_subir_clip_lo_liga_a_la_alerta_y_avisa_al_dashboard():
    c, h = _cliente()
    evento_id = _alerta_de_sabotaje(c)
    video = _webm()
    with c.websocket_connect("/ws/alerts") as ws:
        r = c.post(f"/api/events/{evento_id}/clip", content=video,
                   headers={**WORKER, "Content-Type": "video/webm"})
        assert r.status_code == 200, r.text
        assert r.json()["alertas"] == 1
        mensaje = json.loads(ws.receive_text())
    assert mensaje["type"] == "alert_updated"
    assert mensaje["data"]["clip_path"].endswith(f"{evento_id}.webm")

    destino = get_config().clips_dir / f"{evento_id}.webm"
    _archivos.append(destino)
    assert destino.read_bytes() == video

    alertas = c.get("/api/alerts", headers=h).json()
    alerta = next(a for a in alertas if a["event_id"] == evento_id)
    assert alerta["clip_path"] == destino.relative_to(BASE_DIR).as_posix()

    # Se sirve solo con sesion, con su tipo y con soporte de rangos (el
    # reproductor del navegador pide el video por partes para adelantar).
    assert TestClient(app).get(f"/media/{destino.name}").status_code == 401
    r = c.get(f"/media/{destino.name}", headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "video/webm"
    r = c.get(f"/media/{destino.name}", headers={**h, "Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100


def test_subir_clip_valida_contenido_evento_y_token():
    c, _ = _cliente()
    evento_id = _alerta_de_sabotaje(c)
    r = c.post(f"/api/events/{evento_id}/clip", content=_jpeg(1), headers=WORKER)
    assert r.status_code == 415, "una foto no es un clip, diga lo que diga la cabecera"
    r = c.post(f"/api/events/{uuid.uuid4()}/clip", content=_webm(), headers=WORKER)
    assert r.status_code == 404
    r = c.post(f"/api/events/{evento_id}/clip", content=_webm())
    assert r.status_code == 401, r.status_code
    r = c.post("/api/events/..%2F..%2Fetc/clip", content=_webm(), headers=WORKER)
    assert r.status_code in (404, 422)

    from api.routers import events

    anterior = events.MAX_BYTES_CLIP
    events.MAX_BYTES_CLIP = 1000
    try:
        r = c.post(f"/api/events/{evento_id}/clip", content=_webm(40), headers=WORKER)
        assert r.status_code == 413
    finally:
        events.MAX_BYTES_CLIP = anterior
    assert not (get_config().clips_dir / f"{evento_id}.webm").exists()


def test_retencion_de_clips():
    c, _ = _cliente()
    viejo, nuevo = _alerta_de_sabotaje(c), _alerta_de_sabotaje(c)
    for evento_id in (viejo, nuevo):
        assert c.post(f"/api/events/{evento_id}/clip", content=_webm(), headers=WORKER).status_code == 200
        _archivos.append(get_config().clips_dir / f"{evento_id}.webm")

    with Session(engine) as s:
        a = s.exec(select(Alert).where(Alert.event_id == viejo)).one()
        a.created_at = datetime.now(timezone.utc) - timedelta(days=100)
        s.add(a)
        s.commit()

    # Un clip suelto (su alerta ya no existe) de hace una hora.
    huerfano = get_config().clips_dir / f"{uuid.uuid4()}.webm"
    huerfano.write_bytes(_webm())
    _archivos.append(huerfano)
    hace_una_hora = time.time() - 3600
    os.utime(huerfano, (hace_una_hora, hace_una_hora))
    # Y uno recien subido, que puede estar a medio ligar: se respeta.
    reciente = get_config().clips_dir / f"{uuid.uuid4()}.webm"
    reciente.write_bytes(_webm())
    _archivos.append(reciente)

    os.environ["RETENCION_CLIPS_DIAS"] = "90"
    try:
        with Session(engine) as s:
            cuenta = purgar(s, Politica())
    finally:
        os.environ.pop("RETENCION_CLIPS_DIAS", None)
    assert cuenta["clips"] == 1, cuenta
    assert not (get_config().clips_dir / f"{viejo}.webm").exists()
    assert (get_config().clips_dir / f"{nuevo}.webm").exists()
    assert not huerfano.exists(), "clip sin alerta que lo referencie"
    assert reciente.exists()
    with Session(engine) as s:
        a = s.exec(select(Alert).where(Alert.event_id == viejo)).one()
        assert a.clip_path is None, "la alerta se queda, el video no"


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
                print(f"  [ERROR] {nombre}: {type(e).__name__}: {e}")
    finally:
        for f in _archivos:
            f.unlink(missing_ok=True)
        engine.dispose()
        _borrar_bd(os.environ["DATABASE_URL"])
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
