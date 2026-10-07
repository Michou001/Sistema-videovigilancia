"""Graba un video de la camara para el plan de contingencia de la demo.

    python tools/grabar_video.py --segundos 90
    python tools/grabar_video.py --segundos 60 --env .env.cam-02 --salida demo/videos/patio.mp4

Graba el stream de SOURCE (el del .env, o el del --env indicado) tal cual lo
ve el worker. Durante el ensayo se graba la escena de la demo -la placa de
prueba frente a la camara, alguien entrando a la zona- y, si en la sede falla
la camara o la red, ese video entra como camara desde el catalogo (Agregar
manualmente > Video de demostracion): en bucle y a velocidad real, el resto
del sistema funciona igual.

Los videos van a demo/videos/, que no se sube a Git (pesan y pueden mostrar
personas).
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import cv2  # noqa: E402

from edge.config import leer_env, ruta_env  # noqa: E402
from tools.probe_camara import abrir_con_timeout, ocultar_password  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--segundos", type=float, default=60.0)
    p.add_argument("--env", help="Archivo de entorno de la camara (default: .env)")
    p.add_argument("--salida", type=Path, help="Archivo de salida (default: demo/videos/camara-FECHA.mp4)")
    args = p.parse_args()

    fuente = leer_env(ruta_env(args.env)).get("SOURCE", "")
    if not fuente:
        print("[x] No hay SOURCE en el archivo de entorno.")
        return 1
    destino = args.salida or RAIZ / "demo" / "videos" / f"camara-{datetime.now():%Y%m%d-%H%M}.mp4"
    destino.parent.mkdir(parents=True, exist_ok=True)

    print(f"[i] Abriendo {ocultar_password(fuente)} ...")
    if fuente.startswith(("rtsp://", "rtsps://", "http://", "https://")):
        cap = abrir_con_timeout(fuente, 10.0)
    elif fuente.startswith("webcam:"):
        cap = cv2.VideoCapture(int(fuente.split(":", 1)[1]))
    else:
        print("[x] SOURCE no es una camara (es un archivo): no hay nada que grabar.")
        return 1
    if not cap.isOpened():
        print("[x] No se pudo abrir la camara. Revisa que este en la red: python tools/probe_camara.py")
        return 1

    ok, frame = cap.read()
    if not ok:
        print("[x] La camara no entrego imagen.")
        return 1
    fps = cap.get(cv2.CAP_PROP_FPS) or 20.0
    if not 1 <= fps <= 60:
        fps = 20.0
    alto, ancho = frame.shape[:2]
    escritor = cv2.VideoWriter(str(destino), cv2.VideoWriter_fourcc(*"mp4v"), fps, (ancho, alto))
    print(f"[i] Grabando {args.segundos:.0f} s a {ancho}x{alto} @ {fps:.0f} fps -> {destino.relative_to(RAIZ)}")
    print("    Ctrl+C para terminar antes.")

    t0 = time.monotonic()
    cuadros = 0
    try:
        while ok and time.monotonic() - t0 < args.segundos:
            escritor.write(frame)
            cuadros += 1
            ok, frame = cap.read()
    except KeyboardInterrupt:
        pass
    finally:
        escritor.release()
        cap.release()
    print(f"[OK] {cuadros} cuadros ({cuadros / fps:.0f} s de video) en {destino.relative_to(RAIZ)}")
    return 0 if cuadros else 1


if __name__ == "__main__":
    raise SystemExit(main())
