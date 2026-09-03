"""Pruebas del tracker.

Se corre sin dependencias:  python tests/test_tracking.py
(tambien funciona con pytest si lo instalas)

Existe por una razon concreta: la primera version del tracker fallaba en
silencio con objetos rapidos. Los videos de prueba tenian una placa moviendose
~3 px por frame y todo parecia correcto, pero un coche a velocidad real se
desplaza mas que el ancho de su propia placa entre frames y el track se rompia
en cada uno. El caso `objeto_rapido` de aqui abajo es el que lo detecto.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge.tracking import Deteccion, IoUTracker, iou  # noqa: E402


def _correr(cajas_por_frame, **kwargs):
    """Alimenta el tracker frame a frame y devuelve los tracks confirmados."""
    tr = IoUTracker(min_hits=kwargs.pop("min_hits", 2), **kwargs)
    for i, cajas in enumerate(cajas_por_frame):
        tr.update([Deteccion(bbox=c, confidence=0.9) for c in cajas], ts=float(i))
    return tr.cerrar()


# --------------------------------------------------------------------------

def test_iou_basico():
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    assert 0.1 < iou((0, 0, 10, 10), (5, 0, 15, 10)) < 0.5


def test_objeto_lento():
    """Caso facil: se desplaza mucho menos que su tamano."""
    tracks = _correr([[(100 + i * 3, 200, 150 + i * 3, 225)] for i in range(12)])
    assert len(tracks) == 1
    assert tracks[0].hits == 12


def test_objeto_rapido():
    """REGRESION: 55 px/frame con una caja de 50 px de ancho.

    El IoU entre frames consecutivos es CERO. Solo la asociacion por distancia
    mantiene el track. Sin ella nace uno nuevo cada frame y no se emite ningun
    evento -- que es lo que pasaria con un coche a velocidad real.
    """
    tracks = _correr([[(100 + i * 55, 200, 150 + i * 55, 225)] for i in range(10)])
    assert len(tracks) == 1, f"el track se partio en {len(tracks)}"
    assert tracks[0].hits == 10


def test_salto_excesivo_no_se_asocia():
    """120 px/frame es mas del doble del tamano de la caja: el tracker debe
    NEGARSE a asociar.

    Perder el track es preferible a fusionar dos vehiculos distintos en uno: un
    track equivocado produce un evento con la placa de un coche y la caja de
    otro, y eso en una lista negra significa acusar al vehiculo incorrecto.
    """
    tracks = _correr([[(100 + i * 120, 200, 150 + i * 120, 225)] for i in range(6)])
    assert len(tracks) == 0


def test_dos_objetos_en_sentido_opuesto():
    """Dos objetos que se cruzan no deben intercambiar identidades."""
    cruces = [
        [(100 + i * 40, 200, 150 + i * 40, 225), (600 - i * 40, 200, 650 - i * 40, 225)]
        for i in range(10)
    ]
    tracks = _correr(cruces)
    assert len(tracks) == 2
    assert all(t.hits == 10 for t in tracks)


def test_objeto_que_se_acerca():
    """Un coche acercandose crece de tamano mientras se mueve."""
    crece = [
        [(100 + i * 30, 200, 100 + i * 30 + 50 + i * 12, 225 + i * 5)]
        for i in range(10)
    ]
    tracks = _correr(crece)
    assert len(tracks) == 1
    assert tracks[0].hits == 10


def test_ruido_de_un_frame_se_descarta():
    """Una deteccion que aparece un solo frame es casi siempre un falso
    positivo. min_hits la filtra antes de que genere un evento."""
    assert _correr([[(10, 10, 40, 30)]]) == []


def test_track_expira_y_se_recoge():
    """Tras max_age frames sin verse, el track se retira y queda disponible
    para emitir su evento."""
    tr = IoUTracker(min_hits=2, max_age=3)
    for i in range(5):
        tr.update([Deteccion(bbox=(100, 200, 150, 225), confidence=0.9)], ts=float(i))
    expirados = []
    for i in range(5, 12):
        tr.update([], ts=float(i))
        expirados.extend(tr.recoger_expirados())
    assert len(expirados) == 1
    assert expirados[0].hits == 5
    assert tr.activos == 0


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
