"""Pruebas del detector de pose (caida, manos arriba, posible agresion), de las
pistas de personas y vehiculos del detector de movimiento, y de los eventos
ISAPI de la camara Hikvision. Sin GPU ni modelos: los modelos se simulan.

    python tests/test_pose_isapi.py
"""

from __future__ import annotations

import http.server
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import warnings  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

import numpy as np  # noqa: E402

from api.matching import _cruzar_movimiento  # noqa: E402
from edge.detectors import pose as P  # noqa: E402
from edge.isapi import EventosCamara, canal_de_fuente, crear_eventos_camara, parsear_alerta  # noqa: E402
from shared.events import DetectionEvent, EventType  # noqa: E402

_TMP = Path(tempfile.mkdtemp())


# --------------------------------------------------------------------------
# Esqueletos sinteticos
# --------------------------------------------------------------------------

def _esqueleto(cx=500.0, pies=900.0, alto=400.0, angulo=0.0, manos="abajo", munecas_dx=0.0):
    """17 puntos COCO de una persona. `angulo` inclina el torso (0 = de pie,
    90 = tendida); la cadera queda a media altura de la caja."""
    import math

    kp = np.zeros((17, 2), np.float32)
    largo = alto * 0.3
    cadera = (cx, pies - alto * 0.5)
    rad = math.radians(angulo)
    hombros = (cadera[0] + math.sin(rad) * largo, cadera[1] - math.cos(rad) * largo)
    cabeza = (hombros[0] + math.sin(rad) * largo * 0.4, hombros[1] - math.cos(rad) * largo * 0.4)
    kp[P.NARIZ] = cabeza
    kp[P.OJO_I] = kp[P.OJO_D] = kp[P.OREJA_I] = kp[P.OREJA_D] = cabeza
    kp[P.HOMBRO_I] = (hombros[0] - 20, hombros[1])
    kp[P.HOMBRO_D] = (hombros[0] + 20, hombros[1])
    kp[P.CADERA_I] = (cadera[0] - 15, cadera[1])
    kp[P.CADERA_D] = (cadera[0] + 15, cadera[1])
    if manos == "arriba":
        kp[P.MUNECA_I] = (hombros[0] - 30 + munecas_dx, cabeza[1] - largo * 0.5)
        kp[P.MUNECA_D] = (hombros[0] + 30 + munecas_dx, cabeza[1] - largo * 0.5)
    else:
        kp[P.MUNECA_I] = (cadera[0] - 40 + munecas_dx, cadera[1])
        kp[P.MUNECA_D] = (cadera[0] + 40 + munecas_dx, cadera[1])
    kp[P.CODO_I], kp[P.CODO_D] = kp[P.HOMBRO_I], kp[P.HOMBRO_D]
    conf = np.full(17, 0.9, np.float32)
    return kp, conf


def _muestra(ts, angulo, cadera_y, alto=400.0):
    return P.Muestra(ts=ts, angulo=angulo, cadera_y=cadera_y, alto=alto, munecas=(None, None),
                     largo_torso=120.0)


def test_angulo_del_torso_y_manos():
    kp, c = _esqueleto(angulo=0)
    assert P.angulo_torso(kp, c) < 1
    kp, c = _esqueleto(angulo=90)
    assert 89 < P.angulo_torso(kp, c) < 91
    kp, c = _esqueleto(angulo=0)
    c[P.HOMBRO_I] = c[P.HOMBRO_D] = 0.1
    assert P.angulo_torso(kp, c) is None, "sin hombros visibles no se adivina"
    assert P.manos_arriba(*_esqueleto(manos="arriba"))
    assert not P.manos_arriba(*_esqueleto(manos="abajo"))
    assert not P.manos_arriba(*_esqueleto(angulo=80, manos="arriba")), \
        "tendido con los brazos estirados no es 'manos arriba'"
    kp, c = _esqueleto(manos="arriba")
    c[P.MUNECA_D] = 0.1
    assert not P.manos_arriba(kp, c), "hacen falta las dos manos"


def test_caida_contra_agacharse_y_acostarse():
    de_pie = [_muestra(t * 0.125, 2, 700) for t in range(8)]
    caida = de_pie + [_muestra(1.0 + t * 0.125, a, y) for t, (a, y) in
                      enumerate([(35, 740), (70, 820), (85, 850), (88, 855)])]
    assert P.es_caida(caida)
    agachado = de_pie + [_muestra(1.0 + t * 0.125, a, 705) for t, a in enumerate([40, 70, 80, 85])]
    assert not P.es_caida(agachado), "el torso se inclina pero la cadera no baja: se agacho"
    despacio = [_muestra(0, 2, 700)] + [_muestra(3.0 + t * 0.5, a, 850) for t, a in enumerate([70, 80, 85])]
    assert not P.es_caida(despacio), "acostarse en 3 s no es caerse"
    ya_tendido = [_muestra(t * 0.125, 88, 850) for t in range(10)]
    assert not P.es_caida(ya_tendido)
    assert not P.es_caida(caida[:3])


class _T:
    """Tensor de mentira: .cpu().numpy() como en Ultralytics."""

    def __init__(self, a) -> None:
        self.a = np.asarray(a)

    def cpu(self):
        return self

    def numpy(self):
        return self.a

    def __len__(self):
        return len(self.a)


class _ModeloPose:
    """Devuelve, frame a frame, las personas que le programen."""

    def __init__(self) -> None:
        self.cuadro: list[tuple[int, tuple, np.ndarray, np.ndarray]] = []

    def track(self, frame, **_):
        if not self.cuadro:
            return [SimpleNamespace(boxes=None, keypoints=None)]
        ids = [c[0] for c in self.cuadro]
        cajas = [c[1] for c in self.cuadro]
        kps = np.stack([c[2] for c in self.cuadro])
        confs = np.stack([c[3] for c in self.cuadro])
        return [SimpleNamespace(boxes=SimpleNamespace(xyxy=_T(cajas), id=_T(ids)),
                                keypoints=SimpleNamespace(xy=_T(kps), conf=_T(confs)))]


def _cfg_pose(**extra):
    base = dict(camera_id="cam-pose", pose_conf=0.4, imgsz=640, snapshot_dir=_TMP, manos_arriba_s=2.0,
                pose_agresion=True)
    base.update(extra)
    return SimpleNamespace(**base)


def _frame(ts):
    return SimpleNamespace(ts=ts, frame=np.zeros((1000, 1000, 3), np.uint8))


def test_detector_de_pose_caida_y_enfriamiento():
    modelo = _ModeloPose()
    det = P.PoseDetector(_cfg_pose(), modelo=modelo)
    eventos = []
    t = 1000.0
    secuencia = [(0, 900)] * 8 + [(40, 930), (75, 990), (85, 1000), (88, 1000), (88, 1000)]
    for angulo, pies in secuencia:
        kp, c = _esqueleto(angulo=angulo, pies=pies)
        modelo.cuadro = [(3, (400, pies - 400, 600, pies), kp, c)]
        eventos += det.procesar(_frame(t))
        t += 0.125
    assert [e.value for e in eventos] == ["persona_caida"], [e.value for e in eventos]
    ev = eventos[0]
    assert ev.type == EventType.ANOMALY and ev.meta["metodo"] == "pose" and ev.track_id == 3
    assert ev.snapshot_path and Path(ev.snapshot_path).name == f"{ev.event_id}.jpg"
    # Se levanta y se vuelve a caer al rato: dentro del enfriamiento no repite.
    for angulo, pies in secuencia:
        kp, c = _esqueleto(angulo=angulo, pies=pies)
        modelo.cuadro = [(3, (400, pies - 400, 600, pies), kp, c)]
        eventos += det.procesar(_frame(t))
        t += 0.125
    assert len(eventos) == 1
    assert det.stats["persona_caida"] == 1


def test_detector_de_pose_manos_arriba_sostenidas():
    modelo = _ModeloPose()
    det = P.PoseDetector(_cfg_pose(), modelo=modelo)
    eventos = []
    kp_arriba, c = _esqueleto(manos="arriba")
    kp_abajo, _ = _esqueleto(manos="abajo")
    t = 0.0
    # Estirarse 1 s no cuenta...
    for i in range(24):
        modelo.cuadro = [(1, (400, 500, 600, 900), kp_arriba if i < 8 else kp_abajo, c)]
        eventos += det.procesar(_frame(t))
        t += 0.125
    assert eventos == []
    # ...las manos arriba 2.5 s, si.
    for _ in range(20):
        modelo.cuadro = [(1, (400, 500, 600, 900), kp_arriba, c)]
        eventos += det.procesar(_frame(t))
        t += 0.125
    assert [e.value for e in eventos] == ["manos_arriba"]
    assert eventos[0].meta["segundos"] >= 2.0


def test_detector_de_pose_posible_agresion():
    modelo = _ModeloPose()
    det = P.PoseDetector(_cfg_pose(), modelo=modelo)
    eventos, t = [], 0.0
    for i in range(10):
        # Munecas yendo y viniendo 150 px cada 1/8 s (~10 torsos/s), con otra
        # persona pegada.
        kp1, c1 = _esqueleto(cx=450, munecas_dx=150 if i % 2 else -150)
        kp2, c2 = _esqueleto(cx=560)
        modelo.cuadro = [(1, (380, 500, 520, 900), kp1, c1), (2, (500, 500, 640, 900), kp2, c2)]
        eventos += det.procesar(_frame(t))
        t += 0.125
    assert [e.value for e in eventos] == ["posible_agresion"], "un aviso por pareja"
    assert eventos[0].meta["con_track"] == 2
    # La misma agitacion sin nadie cerca no es agresion (alguien bailando).
    det = P.PoseDetector(_cfg_pose(), modelo=modelo)
    eventos = []
    for i in range(10):
        kp1, c1 = _esqueleto(cx=200, munecas_dx=150 if i % 2 else -150)
        kp2, c2 = _esqueleto(cx=850)
        modelo.cuadro = [(1, (130, 500, 270, 900), kp1, c1), (2, (780, 500, 920, 900), kp2, c2)]
        eventos += det.procesar(_frame(t))
        t += 0.125
    assert eventos == []
    det = P.PoseDetector(_cfg_pose(pose_agresion=False), modelo=modelo)
    assert det.agresion is False


def test_api_explica_los_eventos_de_postura():
    def _ev(valor, **meta):
        return DetectionEvent(camera_id="c1", type=EventType.ANOMALY, value=valor, confidence=0.8, meta=meta)

    r = _cruzar_movimiento(_ev("persona_caida", metodo="pose"))
    assert r.severity.value == "warning" and "cadera" in r.reason
    r = _cruzar_movimiento(_ev("persona_caida"))
    assert "tendida" in r.reason
    r = _cruzar_movimiento(_ev("manos_arriba", segundos=3.2))
    assert r.severity.value == "warning" and r.reason.endswith("(3 s)")
    r = _cruzar_movimiento(_ev("posible_agresion"))
    assert "estimación" in r.reason


# --------------------------------------------------------------------------
# Movimiento: pistas de personas y vehiculos
# --------------------------------------------------------------------------

def test_movimiento_publica_personas_y_vehiculos():
    from edge import aceleracion
    from edge.detectors import motion

    class _ModeloDet:
        def __init__(self):
            self.clases_pedidas = None

        def track(self, frame, classes=None, **_):
            self.clases_pedidas = classes
            return [SimpleNamespace(boxes=SimpleNamespace(
                xyxy=_T([[100, 100, 150, 300], [400, 400, 700, 600]]), id=_T([1, 2]),
                cls=_T([0, 2]), conf=_T([0.9, 0.8])))]

    modelo = _ModeloDet()
    original = aceleracion.cargar_yolo
    aceleracion.cargar_yolo = lambda ruta, device: modelo
    try:
        cfg = SimpleNamespace(camera_id="cam-m", motion_model=Path("x.pt"), yolo_half="false",
                              motion_confirm_hits=3, motion_confirm_window=5, snapshot_hd_enabled=False,
                              source="webcam:0", snapshot_hd_channel="101", infer_fps=8.0, imgsz=640,
                              motion_conf=0.45, motion_speed_threshold=2.5, motion_cooldown_s=30.0,
                              enable_zonas=True, enable_pose=True, resolve_device=lambda: "cpu",
                              snapshot_dir=_TMP)
        det = motion.MotionAnomalyDetector(cfg)
        det.procesar(_frame(1.0))
    finally:
        aceleracion.cargar_yolo = original
    assert modelo.clases_pedidas == [0, 2, 3, 5, 7], "vehiculos en el mismo paso del modelo"
    pistas = det.pistas()
    assert [(p.tid, p.clase, p.etiqueta) for p in pistas] == [(1, "persona", "person"), (2, "vehiculo", "car")]
    assert det.confirmador.tracks() == [1] or list(det.confirmador.tracks()) == [1], \
        "el vehiculo no entra a la logica de movimiento anomalo"
    assert det.caida_por_caja is False, "con pose, la caida la decide el esqueleto"


# --------------------------------------------------------------------------
# ISAPI (Hikvision)
# --------------------------------------------------------------------------

def _alerta_xml(tipo, estado="active", canal=1):
    return (f'--boundary\r\nContent-Type: application/xml; charset="UTF-8"\r\n\r\n'
            f'<?xml version="1.0" encoding="UTF-8"?>\r\n'
            f'<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">\r\n'
            f'<ipAddress>192.168.1.64</ipAddress><channelID>{canal}</channelID>'
            f'<dateTime>2026-09-30T23:10:00-06:00</dateTime><activePostCount>1</activePostCount>'
            f'<eventType>{tipo}</eventType><eventState>{estado}</eventState>'
            f'<eventDescription>{tipo} alarm</eventDescription>\r\n</EventNotificationAlert>\r\n').encode()


def _cfg_isapi(**extra):
    base = dict(camera_id="cam-hik", snapshot_dir=_TMP, isapi="auto", isapi_puerto=80, isapi_https=False,
                isapi_eventos="sabotaje,perdida_video,deteccion_linea,intrusion_camara",
                source="rtsp://admin:Clave%40123@192.168.1.64:554/Streaming/Channels/102")
    base.update(extra)
    return SimpleNamespace(**base)


def test_parseo_y_canal():
    a = parsear_alerta(_alerta_xml("shelteralarm").decode())
    assert a == {"tipo": "shelteralarm", "estado": "active", "canal": 1,
                 "descripcion": "shelteralarm alarm", "fecha": "2026-09-30T23:10:00-06:00"}
    assert canal_de_fuente("rtsp://x/Streaming/Channels/102") == 1
    assert canal_de_fuente("rtsp://x/Streaming/Channels/1601") == 16
    assert canal_de_fuente("rtsp://x/h264") is None


def test_flujo_de_eventos_de_la_camara():
    reloj = [1000.0]
    ev = EventosCamara(_cfg_isapi(), host="h", usuario="u", clave="c", canal=1,
                       permitidos=["sabotaje", "perdida_video", "deteccion_linea"], iniciar=False,
                       reloj=lambda: reloj[0])
    ev.al_frame(SimpleNamespace(frame=np.zeros((50, 80, 3), np.uint8)))
    flujo = (_alerta_xml("videoloss", "inactive") + _alerta_xml("shelteralarm") + _alerta_xml("VMD")
             + _alerta_xml("linedetection", canal=2) + _alerta_xml("shelteralarm"))
    # Llega en trozos arbitrarios, como por la red.
    for i in range(0, len(flujo), 37):
        ev.alimentar(flujo[i:i + 37])
    eventos = ev.eventos()
    assert [e.value for e in eventos] == ["sabotaje"], \
        "latido ignorado, VMD no permitido, otro canal del NVR ignorado, repeticion agrupada"
    e = eventos[0]
    assert e.type == EventType.CAMERA and e.meta["tipo_hikvision"] == "shelteralarm"
    assert e.snapshot_path and Path(e.snapshot_path).name == f"{e.event_id}.jpg", \
        "la evidencia es el ultimo cuadro (el lente tapado)"
    reloj[0] += 31
    ev.alimentar(_alerta_xml("shelteralarm") + _alerta_xml("videoloss"))
    assert [x.value for x in ev.eventos()] == ["sabotaje", "perdida_video"]
    assert ev.estado()["isapi"]["eventos"] == 3
    ev.alimentar(b"x" * 300_000)
    assert len(ev._buffer) <= 4096, "basura sin fin de bloque no hace crecer la memoria"


def test_crear_segun_la_fuente():
    assert crear_eventos_camara(_cfg_isapi(source="webcam:0")) is None
    assert crear_eventos_camara(_cfg_isapi(isapi="false")) is None
    ev = crear_eventos_camara(_cfg_isapi(isapi_puerto=8080))
    try:
        assert ev is not None
        assert ev.clave == "Clave@123", "la contraseña de la URL viene codificada"
        assert ev.url == "http://192.168.1.64:8080/ISAPI/Event/notification/alertStream"
        assert ev.canal == 1
    finally:
        ev.cerrar()


def test_camara_sin_isapi_deja_de_intentar():
    llamadas = []

    def _abrir():
        llamadas.append(1)
        raise LookupError("la cámara no tiene ISAPI alertStream")

    ev = EventosCamara(_cfg_isapi(), host="h", usuario="u", clave="c", canal=None,
                       permitidos=["sabotaje"], abrir=_abrir)
    ev._hilo.join(timeout=3)
    assert not ev._hilo.is_alive() and llamadas == [1]
    assert "no tiene" in ev.estado()["isapi"]["ultimo_error"]


def test_alertstream_real_por_http():
    """Un servidor HTTP que se porta como la camara: manda un latido y un
    sabotaje por el mismo flujo abierto."""

    class _Camara(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/ISAPI/Event/notification/alertStream":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/mixed; boundary=boundary")
            self.end_headers()
            for trozo in (_alerta_xml("videoloss", "inactive"), _alerta_xml("shelteralarm")):
                self.wfile.write(trozo)
                self.wfile.flush()
                time.sleep(0.05)
            time.sleep(1.0)

        def log_message(self, *a):
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        puerto = s.getsockname()[1]
    servidor = http.server.ThreadingHTTPServer(("127.0.0.1", puerto), _Camara)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    import os

    anteriores = {k: os.environ.pop(k) for k in list(os.environ) if k.lower() in ("http_proxy", "all_proxy")}
    os.environ["NO_PROXY"] = "127.0.0.1"
    ev = EventosCamara(_cfg_isapi(isapi_puerto=puerto), host="127.0.0.1", usuario="u", clave="c", canal=1,
                       permitidos=["sabotaje"])
    try:
        fin = time.time() + 5
        eventos = []
        while time.time() < fin and not eventos:
            eventos = ev.eventos()
            time.sleep(0.05)
        assert [e.value for e in eventos] == ["sabotaje"], ev.estado()
    finally:
        ev.cerrar()
        servidor.shutdown()
        os.environ.update(anteriores)


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
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
