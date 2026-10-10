"""Regresion de la rectificacion optica para placas vistas de lado."""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge.detectors.plates import enderezar_placa  # noqa: E402


def test_placa_oblicua_recupera_proporcion_y_orientacion():
    placa = np.full((120, 240, 3), 230, dtype=np.uint8)
    cv2.rectangle(placa, (2, 2), (237, 117), (0, 0, 0), 3)
    cv2.circle(placa, (24, 24), 10, (0, 0, 255), -1)
    cv2.circle(placa, (215, 95), 10, (255, 0, 0), -1)
    origen = np.float32([[0, 0], [239, 0], [239, 119], [0, 119]])
    destino = np.float32([[75, 30], [215, 8], [195, 105], [50, 135]])
    vista = cv2.warpPerspective(placa, cv2.getPerspectiveTransform(origen, destino),
                               (270, 145))

    recta = enderezar_placa(vista)

    assert recta is not None
    alto, ancho = recta.shape[:2]
    assert 1.9 <= ancho / alto <= 2.1
    # Los puntos de color deben conservar sus esquinas, sin invertir el texto.
    rojo = cv2.inRange(recta, (0, 0, 100), (80, 80, 255))
    azul = cv2.inRange(recta, (100, 0, 0), (255, 80, 80))
    rx, ry = np.mean(np.where(rojo > 0)[1]), np.mean(np.where(rojo > 0)[0])
    bx, by = np.mean(np.where(azul > 0)[1]), np.mean(np.where(azul > 0)[0])
    assert rx < bx and ry < by


def test_placa_frontal_no_gasta_otra_lectura():
    placa = np.full((100, 200, 3), 230, dtype=np.uint8)
    cv2.rectangle(placa, (2, 2), (197, 97), (0, 0, 0), 3)
    assert enderezar_placa(placa) is None


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"[OK] {nombre}")
