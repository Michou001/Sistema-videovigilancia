"""Optimiza los modelos para la GPU de ESTA maquina y mide la diferencia.

    python tools/optimizar_modelos.py              # mide y exporta YOLO a TensorRT
    python tools/optimizar_modelos.py --solo-medir # solo compara FP32 / FP16
    python tools/optimizar_modelos.py --onnx-trt   # ademas, motores TensorRT de placas y rostros

Que hace:
  1. Mide los YOLO de movimiento y de pose tal cual (FP32) y en media
     precision (FP16).
  2. Los exporta a motores de TensorRT (.engine, FP16) y los mide.
  3. Con --onnx-trt, construye los motores de TensorRT para los modelos ONNX
     de placas y rostros (quedan en models/trt_cache) y los mide.
  4. Imprime las lineas del .env para usar lo mas rapido.

Un motor .engine queda atado a la GPU, al driver y a la version de TensorRT con
que se construyo: se genera EN la maquina donde va a correr, no se copia.
Requiere `pip install tensorrt` (o el paquete de TensorRT de NVIDIA) ademas de
torch con CUDA.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import numpy as np  # noqa: E402

from edge.config import load_config  # noqa: E402


def _medir(fn, n: int = 40, calentamiento: int = 5) -> float:
    for _ in range(calentamiento):
        fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t0) * 1000 / n


def _frame(ancho: int = 1280, alto: int = 720) -> np.ndarray:
    rng = np.random.default_rng(0)
    return rng.integers(0, 255, (alto, ancho, 3), dtype=np.uint8)


def medir_yolo(ruta: Path, imgsz: int, half: bool) -> float:
    from edge.aceleracion import cargar_yolo, precision

    modelo = cargar_yolo(ruta, "cuda")
    frame = _frame()
    return _medir(lambda: modelo.predict(frame, imgsz=imgsz, verbose=False, device=0, **precision(half)))


def exportar_engine(ruta: Path, imgsz: int) -> Path:
    from ultralytics import YOLO

    destino = ruta.with_suffix(".engine")
    print(f"  Exportando {ruta.name} a TensorRT FP16 (imgsz={imgsz}); tarda unos minutos...")
    salida = YOLO(str(ruta)).export(format="engine", half=True, imgsz=imgsz, device=0,
                                    workspace=4, verbose=False)
    salida = Path(salida)
    if salida != destino and salida.exists():
        salida.replace(destino)
    return destino


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", help="Archivo de entorno de la camara (default .env)")
    p.add_argument("--solo-medir", action="store_true")
    p.add_argument("--onnx-trt", action="store_true",
                   help="Construir y medir TensorRT para los modelos ONNX (placas, rostros)")
    args = p.parse_args()

    cfg = load_config(args.env)
    try:
        import torch
    except ImportError:
        print("[x] Falta torch con CUDA (ver requirements.txt).")
        return 1
    if not torch.cuda.is_available():
        print("[x] No hay GPU CUDA disponible: estas optimizaciones son para GPU NVIDIA.")
        return 1
    print(f"  GPU: {torch.cuda.get_device_name(0)}")

    lineas_env: list[str] = []
    modelos = [("MOTION_MODEL", cfg.motion_model)]
    if cfg.enable_pose:
        modelos.append(("POSE_MODEL", cfg.pose_model))
    for variable, modelo in modelos:
        if modelo.suffix != ".pt":
            print(f"  {variable} ya es {modelo.name}; se mide tal cual.")
            print(f"    {modelo.name}: {medir_yolo(modelo, cfg.imgsz, True):.1f} ms/frame")
            continue
        fp32 = medir_yolo(modelo, cfg.imgsz, False)
        fp16 = medir_yolo(modelo, cfg.imgsz, True)
        print(f"  {modelo.name}  FP32: {fp32:.1f} ms/frame   FP16: {fp16:.1f} ms/frame")
        if "YOLO_HALF=true" not in lineas_env:
            lineas_env.append("YOLO_HALF=true")
        if args.solo_medir:
            continue
        try:
            import tensorrt  # noqa: F401
        except ImportError:
            print("  [!] Sin el paquete tensorrt no se puede exportar a .engine: pip install tensorrt")
            continue
        engine = exportar_engine(modelo, cfg.imgsz)
        trt = medir_yolo(engine, cfg.imgsz, True)
        print(f"  {engine.name}  TensorRT FP16: {trt:.1f} ms/frame  "
              f"({fp32 / max(trt, 1e-6):.1f}x frente a FP32)")
        lineas_env.append(f"{variable}={engine.relative_to(RAIZ).as_posix()}")

    if args.onnx_trt:
        from edge.aceleracion import proveedores_disponibles

        if "TensorrtExecutionProvider" not in proveedores_disponibles():
            print("  [!] onnxruntime no trae TensorrtExecutionProvider: se omiten placas y rostros.")
        else:
            from edge.detectors.plates import PlateDetector

            frame = _frame()
            for nombre, trt in (("CUDA", "false"), ("TensorRT", "true")):
                cfg.ort_tensorrt = trt
                from edge.modelos import olvidar_todos

                olvidar_todos()
                det = PlateDetector(cfg)
                ms = _medir(lambda d=det: d.detector.predict(frame), n=30)
                print(f"  Detector de placas con {nombre}: {ms:.1f} ms/frame")
            lineas_env.append("ORT_TENSORRT=true")

    if lineas_env:
        print("\n  Para usar lo mas rapido, agrega al .env del worker:")
        for linea in lineas_env:
            print(f"      {linea}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
