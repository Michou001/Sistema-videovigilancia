"""Pruebas de varias camaras en un proceso y de la configuracion de aceleracion.

    python tests/test_multicamara.py

Sin GPU ni modelos: los detectores son falsos y el video se genera al vuelo.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from edge import worker  # noqa: E402
from edge.aceleracion import (  # noqa: E402
    _tarea_por_nombre,
    parametros_captura,
    proveedores_onnx,
    usar_half,
)
from edge.config import cargar_configuraciones  # noqa: E402
from edge.detectors.base import Detector  # noqa: E402
from edge.modelos import cargados, compartido, olvidar_todos  # noqa: E402
from edge.sink import adoptar_spool_legado  # noqa: E402
from shared.events import DetectionEvent, EventType  # noqa: E402

_TMP = Path(tempfile.mkdtemp())


def _video(nombre: str, frames: int = 40) -> Path:
    ruta = _TMP / nombre
    escritor = cv2.VideoWriter(str(ruta), cv2.VideoWriter_fourcc(*"MJPG"), 20, (160, 120))
    for i in range(frames):
        img = np.full((120, 160, 3), (i * 5) % 255, np.uint8)
        escritor.write(img)
    escritor.release()
    return ruta


def _env(nombre: str, **valores) -> Path:
    ruta = _TMP / nombre
    ruta.write_text("\n".join(f"{k}={v}" for k, v in valores.items()) + "\n", encoding="utf-8")
    return ruta


class _DetectorFalso(Detector):
    """Emite un evento cada 10 frames y usa un "modelo" compartido."""

    name = "falso"

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.modelo = compartido(("modelo-falso", "v1"), lambda: object())
        self.n = 0

    def procesar(self, frame):
        self.n += 1
        if self.n % 10:
            return []
        return [DetectionEvent(camera_id=self.cfg.camera_id, type=EventType.PLATE,
                               value="ABC-123-A", confidence=0.9)]

    @property
    def stats(self) -> dict:
        return {"frames": self.n}


# --------------------------------------------------------------------------

def test_cada_archivo_env_arma_su_camara_sin_contaminar_el_entorno():
    a = _env(".env.uno", CAMERA_ID="cam-uno", SOURCE="file:a.mp4", INFER_FPS="5")
    b = _env(".env.dos", CAMERA_ID="cam-dos", SOURCE="file:b.mp4")
    antes = {k: os.environ.get(k) for k in ("CAMERA_ID", "SOURCE", "INFER_FPS")}
    c1, c2 = cargar_configuraciones([str(a), str(b)])
    assert (c1.camera_id, c1.source, c1.infer_fps) == ("cam-uno", "file:a.mp4", 5.0)
    assert (c2.camera_id, c2.source) == ("cam-dos", "file:b.mp4")
    assert c2.infer_fps == 8.0, "el INFER_FPS de la primera camara no debe pasar a la segunda"
    assert {k: os.environ.get(k) for k in antes} == antes


def test_camera_id_repetido_se_rechaza():
    a = _env(".env.r1", CAMERA_ID="cam-igual", SOURCE="file:a.mp4")
    b = _env(".env.r2", CAMERA_ID="cam-igual", SOURCE="file:b.mp4")
    try:
        cargar_configuraciones([str(a), str(b)])
        raise AssertionError("dos camaras con el mismo CAMERA_ID deben rechazarse")
    except ValueError as e:
        assert "cam-igual" in str(e)


def test_dos_camaras_en_un_proceso_comparten_modelos():
    olvidar_todos()
    worker.DETENER.clear()
    a = _env(".env.m1", CAMERA_ID="cam-m1", SOURCE=f"file:{_video('m1.avi', 40)}",
             API_TOKEN="", PREVIEW_ENABLED="false", SNAPSHOT_HD_ENABLED="false")
    b = _env(".env.m2", CAMERA_ID="cam-m2", SOURCE=f"file:{_video('m2.avi', 60)}",
             API_TOKEN="", PREVIEW_ENABLED="false", SNAPSHOT_HD_ENABLED="false")
    configs = cargar_configuraciones([str(a), str(b)])
    for c in configs:
        c.offline_dir = _TMP / "spool"
        c.api_token = ""

    original = worker.construir_detectores
    worker.construir_detectores = lambda cfg: [_DetectorFalso(cfg)]
    try:
        codigo = worker.ejecutar(configs)
    finally:
        worker.construir_detectores = original
    assert codigo == 0
    assert cargados() == {"modelo-falso": 2}, f"el modelo se cargo una vez y lo usan 2: {cargados()}"
    # Cada camara escribe su propio JSONL: dos hilos no comparten archivo.
    archivos = {p.name.split("-")[1] + "-" + p.name.split("-")[2] for p in (_TMP).glob("eventos-cam-*.jsonl")}
    assert archivos == {"cam-m1", "cam-m2"}, archivos
    lineas = {}
    for p in _TMP.glob("eventos-cam-*.jsonl"):
        lineas[p.name] = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    total = sum(len(v) for v in lineas.values())
    assert total == 4 + 6, f"40/10 + 60/10 eventos, hubo {total}"
    for nombre, eventos in lineas.items():
        assert all(nombre.startswith(f"eventos-{e['camera_id']}-") for e in eventos)


def test_spool_legado_se_reparte_por_camara():
    raiz = _TMP / "spool-legado"
    raiz.mkdir()
    (raiz / "1.json").write_text(json.dumps({"camera_id": "cam-a", "events": []}))
    (raiz / "2.json").write_text(json.dumps({"camera_id": "cam-b", "events": []}))
    (raiz / "3.json").write_text("{roto")
    assert adoptar_spool_legado(raiz, raiz / "cam-a", "cam-a") == 1
    assert (raiz / "cam-a" / "1.json").exists()
    assert (raiz / "2.json").exists() and (raiz / "3.json").exists()


# --------------------------------------------------------------------------
# Aceleracion

def test_proveedores_onnx():
    assert proveedores_onnx("cpu", "true") == ["CPUExecutionProvider"]
    solo_cuda = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    assert proveedores_onnx("cuda", "false", solo_cuda) == solo_cuda
    # Pedir TensorRT sin tenerlo no rompe: se sigue con CUDA.
    assert proveedores_onnx("cuda", "true", solo_cuda) == solo_cuda
    con_trt = ["TensorrtExecutionProvider", *solo_cuda]
    p = proveedores_onnx("cuda", "true", con_trt)
    assert p[0][0] == "TensorrtExecutionProvider" and p[0][1]["trt_fp16_enable"]
    assert p[1:] == solo_cuda


def test_half_solo_en_gpu():
    assert usar_half("auto", "cuda") and usar_half("true", "cuda")
    assert not usar_half("false", "cuda") and not usar_half("true", "cpu")


def test_parametros_de_captura():
    assert parametros_captura("off") == []
    p = parametros_captura("auto")
    if hasattr(cv2, "CAP_PROP_HW_ACCELERATION"):
        assert p == [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY]
    assert parametros_captura("no-existe") == []


def test_tarea_del_modelo_por_nombre():
    assert _tarea_por_nombre(Path("models/yolo11s.pt")) is None
    assert _tarea_por_nombre(Path("models/yolo11s.engine")) == "detect"
    assert _tarea_por_nombre(Path("models/yolo11s-pose.engine")) == "pose"


def test_fuente_gstreamer_se_reconoce():
    from edge.sources import LiveSource, open_source

    fuente = open_source("gst:videotestsrc ! appsink", open_timeout=0.1)
    try:
        assert isinstance(fuente, LiveSource) and fuente.gstreamer
    finally:
        fuente.reconnect = False
        fuente.release()


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
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
