"""Pruebas de la calidad de rostros y del filtro de saltos del tracker.

    python tests/test_rostros_movimiento.py

No cargan modelos: prueban las funciones que deciden que vista de un rostro
se usa y cuando un salto de posicion es del tracker y no de la persona.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from edge.detectors.confirmacion import ConfirmacionTemporal  # noqa: E402
from edge.detectors.faces import factor_nitidez, factor_pose, plantilla_promedio  # noqa: E402
from edge.detectors.motion import MotionAnomalyDetector, es_caida  # noqa: E402
from edge.detectors.plates import color_vehiculo  # noqa: E402

# 5 puntos de InsightFace: ojo izq, ojo der, nariz, comisura izq, comisura der.
FRONTAL = [[40, 50], [80, 50], [60, 72], [45, 90], [75, 90]]
PERFIL = [[40, 50], [80, 50], [84, 72], [60, 90], [85, 90]]


def _unitario(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


# --------------------------------------------------------------------------
# Rostros

def test_pose_frontal_vale_mas_que_perfil():
    assert factor_pose(FRONTAL) > 0.9
    assert factor_pose(PERFIL) < 0.5
    assert factor_pose(None) == 1.0          # sin puntos no se penaliza


def test_nitidez_distingue_enfoque():
    rng = np.random.default_rng(1)
    nitido = (rng.random((120, 120, 3)) * 255).astype(np.uint8)
    borroso = cv2.GaussianBlur(nitido, (21, 21), 8)
    assert factor_nitidez(nitido) > factor_nitidez(borroso)
    assert 0.3 <= factor_nitidez(borroso) <= 1.0


def test_plantilla_promedia_las_mejores_vistas():
    rng = np.random.default_rng(2)
    base = _unitario(rng.normal(size=512))
    # Ruido de 0.03 por dimension: similitud ~0.8 entre vista y persona, lo
    # tipico de ArcFace entre dos fotos distintas de la misma cara.
    vistas = [(10.0 - i, _unitario(base + rng.normal(scale=0.03, size=512))) for i in range(5)]
    plantilla = plantilla_promedio(vistas, k=3)
    assert abs(float(np.linalg.norm(plantilla)) - 1.0) < 1e-4
    # El promedio se parece mas a la persona que cualquier vista suelta.
    assert float(plantilla @ base) > max(float(v @ base) for _, v in vistas[:3])


def test_plantilla_descarta_vista_de_otra_persona():
    rng = np.random.default_rng(3)
    persona = _unitario(rng.normal(size=512))
    intruso = _unitario(rng.normal(size=512))   # el tracker confundio a dos personas
    vistas = [(9.0, persona), (8.0, _unitario(persona + rng.normal(scale=0.02, size=512))),
              (8.5, intruso)]
    plantilla = plantilla_promedio(vistas, k=3)
    assert float(plantilla @ persona) > 0.95
    assert plantilla_promedio([], k=3) is None


# --------------------------------------------------------------------------
# Movimiento

def test_salto_imposible_es_del_tracker():
    salto = MotionAnomalyDetector._salto_del_tracker
    # Persona de 200 px corriendo: 0.4 alturas en 1/8 s = 3.2 alturas/s. Real.
    assert not salto((0.0, 100, 300, 200), (0.125, 180, 300, 200))
    # Misma persona "aparece" 600 px mas alla en 1/8 s: 24 alturas/s. Imposible.
    assert salto((0.0, 100, 300, 200), (0.125, 700, 300, 200))
    # La caja paso de cuerpo entero a la mitad: oclusion o cambio de identidad.
    assert salto((0.0, 100, 300, 200), (0.125, 105, 300, 90))
    # Agacharse: la altura cambia pero no a la mitad en un frame.
    assert not salto((0.0, 100, 300, 200), (0.125, 100, 330, 150))


def test_rearmar_permite_una_segunda_alerta():
    c = ConfirmacionTemporal(aciertos=3, ventana=5)
    for _ in range(3):
        c.marcar(7, True)
    assert c.confirmado(7)
    c.registrar_alerta(7)
    assert c.ya_alertado(7)
    c.rearmar(7)
    assert not c.ya_alertado(7) and not c.confirmado(7)
    for _ in range(3):
        c.marcar(7, True)
    assert c.confirmado(7)


def _postura(proporciones, dt=0.125):
    return [(i * dt, r) for i, r in enumerate(proporciones)]


def test_caida_de_pie_a_tendida():
    # De pie (2.6), cae en medio segundo y queda tendida.
    assert es_caida(_postura([2.6, 2.6, 2.5, 1.6, 1.1, 0.7, 0.6, 0.6]))


def test_no_es_caida_sentarse_ni_agacharse():
    # Sentarse o agacharse deja la caja "cuadrada", no tendida.
    assert not es_caida(_postura([2.6, 2.5, 1.8, 1.3, 1.2, 1.2, 1.2]))


def test_no_es_caida_acostarse_despacio():
    # Tarda 4 s en pasar de pie a tendida: alguien acostandose en una banca.
    lento = [2.6] + [1.2] * 32 + [0.7, 0.7, 0.7]
    assert not es_caida(_postura(lento))


def test_no_es_caida_si_ya_estaba_tendida():
    assert not es_caida(_postura([0.6, 0.6, 0.6, 0.6, 0.6]))


def test_color_de_vehiculo():
    lienzo = np.zeros((300, 600, 3), np.uint8)
    lienzo[:] = (200, 60, 20)                   # BGR: carroceria azul
    lienzo[120:170, 250:350] = (255, 255, 255)  # la placa, blanca
    assert color_vehiculo(lienzo, (250, 120, 350, 170)) == "azul"
    lienzo[:] = (30, 30, 30)
    assert color_vehiculo(lienzo, (250, 120, 350, 170)) == "negro"


# --------------------------------------------------------------------------

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
