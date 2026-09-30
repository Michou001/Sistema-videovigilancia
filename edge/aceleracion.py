"""Aceleracion de inferencia y de decodificacion de video.

Tres palancas, todas opcionales y con caida segura a lo que ya funcionaba:

1. FP16 en YOLO (YOLO_HALF). En GPUs NVIDIA desde Turing (RTX 20xx en
   adelante) la media precision usa los Tensor Cores: suele bajar la latencia
   y casi la mitad de la memoria de activaciones, sin cambio de precision
   apreciable para deteccion. `auto` la activa si hay CUDA.

2. TensorRT.
   - YOLO: un modelo exportado a `.engine` (tools/optimizar_modelos.py) se
     carga directo con MOTION_MODEL / WEAPON_MODEL. Es lo mas rapido, pero el
     motor queda atado a esa GPU y a esa version de TensorRT.
   - ONNX (placas y rostros): ORT_TENSORRT=true antepone el proveedor
     TensorRT de onnxruntime, con FP16 y cache de motores en models/trt_cache.
     La PRIMERA vez construye los motores y tarda minutos; despues arranca
     normal. Si el proveedor no esta instalado, se sigue con CUDA.

3. Decodificacion por hardware (HW_DECODE). Con varias camaras RTSP el CPU
   se va en decodificar H.264, no en los modelos. OpenCV puede pedirle a
   FFmpeg que use la GPU (D3D11VA en Windows, VAAPI/CUDA en Linux). Si la
   camara no abre con aceleracion, se reintenta sin ella automaticamente.
   Para NVDEC con GStreamer (Jetson, DeepStream) se puede dar un pipeline
   completo: SOURCE=gst:rtspsrc location=... ! ... ! appsink drop=1

Medir antes y despues: `python -m edge.worker --diagnostico` reporta fps,
latencia y uso de CPU; el reporte periodico del worker, los ms por detector.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_TRT = BASE_DIR / "models" / "trt_cache"


def _modo(valor: str) -> str:
    valor = (valor or "").strip().lower()
    if valor in {"1", "true", "si", "yes", "on"}:
        return "true"
    if valor in {"0", "false", "no", "off"}:
        return "false"
    return "auto"


# --------------------------------------------------------------------------
# ONNX Runtime (placas, rostros)
# --------------------------------------------------------------------------

def proveedores_disponibles() -> list[str]:
    try:
        import onnxruntime as ort

        return list(ort.get_available_providers())
    except Exception:  # noqa: BLE001
        return ["CPUExecutionProvider"]


def proveedores_onnx(device: str, tensorrt: str = "false",
                     disponibles: list[str] | None = None) -> list[Any]:
    """Lista de proveedores para una sesion de onnxruntime, en orden de
    preferencia. Cada entrada es un nombre o (nombre, opciones)."""
    if device != "cuda":
        return ["CPUExecutionProvider"]
    disponibles = proveedores_disponibles() if disponibles is None else disponibles
    proveedores: list[Any] = []
    modo = _modo(tensorrt)
    if modo != "false" and "TensorrtExecutionProvider" in disponibles:
        CACHE_TRT.mkdir(parents=True, exist_ok=True)
        proveedores.append(("TensorrtExecutionProvider", {
            "trt_fp16_enable": True,
            "trt_engine_cache_enable": True,
            "trt_engine_cache_path": str(CACHE_TRT),
            "trt_timing_cache_enable": True,
            "trt_timing_cache_path": str(CACHE_TRT),
        }))
        log.info("onnxruntime con TensorRT (FP16). La primera vez construye los motores: "
                 "puede tardar varios minutos; quedan en %s", CACHE_TRT)
    elif modo == "true":
        log.warning("ORT_TENSORRT=true pero onnxruntime no trae TensorrtExecutionProvider "
                    "(proveedores: %s). Se sigue con CUDA.", ", ".join(disponibles))
    if "CUDAExecutionProvider" in disponibles:
        proveedores.append("CUDAExecutionProvider")
    proveedores.append("CPUExecutionProvider")
    return proveedores


def nombre_proveedor(p: Any) -> str:
    return p[0] if isinstance(p, tuple) else str(p)


# --------------------------------------------------------------------------
# YOLO (movimiento, armas)
# --------------------------------------------------------------------------

def usar_half(modo: str, device: str) -> bool:
    """FP16 solo tiene sentido en GPU: en CPU es mas lento o no esta soportado."""
    m = _modo(modo)
    return device == "cuda" and m != "false"


_ARGUMENTO_PRECISION: Optional[str] = None


def precision(half: bool) -> dict:
    """Argumentos de media precision para predict/track segun la version de
    Ultralytics: las recientes cambiaron `half=True` por `quantize=16` y avisan
    en CADA frame si se usa el nombre viejo; las anteriores no conocen el
    nuevo y lo rechazan."""
    global _ARGUMENTO_PRECISION
    if not half:
        return {}
    if _ARGUMENTO_PRECISION is None:
        try:
            from ultralytics.cfg import DEFAULT_CFG_DICT

            _ARGUMENTO_PRECISION = "quantize" if "quantize" in DEFAULT_CFG_DICT else "half"
        except Exception:  # noqa: BLE001
            _ARGUMENTO_PRECISION = "half"
    return {"quantize": 16} if _ARGUMENTO_PRECISION == "quantize" else {"half": True}


def cargar_yolo(ruta: Path | str, device: str):
    """Carga un modelo de Ultralytics: .pt, .onnx o .engine (TensorRT).

    `.to(device)` solo aplica a los .pt: un motor de TensorRT ya vive en la
    GPU para la que se construyo y un ONNX lo mueve su propio runtime.
    """
    from ultralytics import YOLO

    ruta = Path(ruta)
    modelo = YOLO(str(ruta), task=_tarea_por_nombre(ruta))
    if ruta.suffix == ".pt":
        modelo.to(device)
    else:
        log.info("Modelo %s cargado (%s): el dispositivo lo fija el propio formato",
                 ruta.name, ruta.suffix)
    return modelo


def _tarea_por_nombre(ruta: Path) -> str | None:
    """Ultralytics no puede adivinar la tarea de un .engine/.onnx por el
    contenido: se deduce del nombre (yolo11s-pose.engine -> pose)."""
    if ruta.suffix == ".pt":
        return None
    return "pose" if "pose" in ruta.stem else "detect"


# --------------------------------------------------------------------------
# Decodificacion de video
# --------------------------------------------------------------------------

def parametros_captura(hw: str) -> list[int]:
    """Parametros de cv2.VideoCapture para decodificar por hardware.

    Devuelve [] si no se pide aceleracion o si esta version de OpenCV no la
    soporta (las ruedas de pip anteriores a 4.5.2 no tienen estas constantes).
    """
    import cv2

    hw = (hw or "off").strip().lower()
    if hw in {"", "off", "false", "no", "0"}:
        return []
    constantes = {
        "auto": "VIDEO_ACCELERATION_ANY", "any": "VIDEO_ACCELERATION_ANY",
        "d3d11": "VIDEO_ACCELERATION_D3D11", "vaapi": "VIDEO_ACCELERATION_VAAPI",
        "mfx": "VIDEO_ACCELERATION_MFX", "qsv": "VIDEO_ACCELERATION_MFX",
        "cuda": "VIDEO_ACCELERATION_ANY",
    }
    nombre = constantes.get(hw)
    if nombre is None or not hasattr(cv2, "CAP_PROP_HW_ACCELERATION") or not hasattr(cv2, nombre):
        log.warning("HW_DECODE=%s no disponible en esta version de OpenCV; se decodifica por CPU", hw)
        return []
    return [cv2.CAP_PROP_HW_ACCELERATION, int(getattr(cv2, nombre))]
