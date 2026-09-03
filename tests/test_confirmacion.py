"""Pruebas de la confirmacion temporal de armas.

    python tests/test_confirmacion.py

Esta es la pieza de la que depende que el sistema no genere alarmas falsas. Un
detector de armas sin este filtro alerta con cualquier celular o botella, y
tres alertas falsas bastan para que alguien apague el sistema. Por eso se
prueba aparte, sin cargar ningun modelo.

Convencion de las secuencias: cada caracter es un frame.
    'X' = el modelo vio el arma en ese frame
    '.' = no la vio
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge.detectors.weapons import ConfirmacionTemporal  # noqa: E402


def _reproducir(secuencia: str, aciertos: int = 4, ventana: int = 6) -> tuple[bool, int]:
    """Alimenta la secuencia y devuelve (se confirmo alguna vez, en que frame)."""
    c = ConfirmacionTemporal(aciertos, ventana)
    for i, ch in enumerate(secuencia, start=1):
        c.marcar(1, ch == "X")
        if c.confirmado(1):
            return True, i
    return False, -1


# --------------------------------------------------------------------------

def test_deteccion_sostenida_confirma():
    """Un arma real se ve en frames consecutivos."""
    confirmo, frame = _reproducir("XXXX")
    assert confirmo, "4 frames seguidos deberian confirmar"
    assert frame == 4, f"deberia confirmar exactamente en el frame 4, no en {frame}"


def test_destello_de_un_frame_no_confirma():
    """El caso mas comun de falso positivo: una mala inferencia aislada."""
    confirmo, _ = _reproducir("X.....X.....X.....")
    assert not confirmo, "detecciones aisladas no deben alertar"


def test_parpadeo_alterno_no_confirma():
    """Un objeto ambiguo (un celular en la mano) entra y sale de la deteccion.

    Con ventana de 6, alternar da 3 aciertos: por debajo del umbral de 4.
    Es exactamente la clase de senal que NO debe disparar una alarma.
    """
    confirmo, _ = _reproducir("X.X.X.X.X.X.X.")
    assert not confirmo, "el parpadeo alterno no debe confirmar"


def test_deteccion_intermitente_pero_densa_si_confirma():
    """Un cuchillo que se tapa un frame de vez en cuando sigue siendo un cuchillo."""
    confirmo, _ = _reproducir("XX.XX")
    assert confirmo, "4 de 5 frames deberian confirmar"


def test_ventana_es_deslizante():
    """Los aciertos viejos caducan: no se acumulan indefinidamente.

    Sin ventana deslizante, un objeto visto una vez cada 10 segundos acabaria
    confirmando por acumulacion, y eso es justo el ruido que hay que filtrar.
    """
    c = ConfirmacionTemporal(aciertos=4, ventana=6)
    for ch in "X.....X.....X.....X.....":
        c.marcar(1, ch == "X")
        assert not c.confirmado(1), "aciertos separados no deben acumularse"


def test_tracks_independientes():
    """Dos objetos distintos no comparten evidencia."""
    c = ConfirmacionTemporal(aciertos=3, ventana=4)
    for _ in range(3):
        c.marcar(1, True)
        c.marcar(2, False)
    assert c.confirmado(1)
    assert not c.confirmado(2)


def test_no_realertar_del_mismo_objeto():
    """Un arma confirmada genera UNA alerta, no una por frame."""
    c = ConfirmacionTemporal(aciertos=2, ventana=4)
    c.marcar(1, True); c.marcar(1, True)
    assert c.confirmado(1) and not c.ya_alertado(1)
    c.registrar_alerta(1)
    assert c.ya_alertado(1), "tras alertar no debe volver a alertar del mismo track"


def test_objeto_perdido_se_olvida():
    """Cuando la ventana entera queda vacia, el objeto se fue."""
    c = ConfirmacionTemporal(aciertos=2, ventana=3)
    for _ in range(3):
        c.marcar(1, True)
    assert not c.perdido(1)
    for _ in range(3):
        c.marcar(1, False)
    assert c.perdido(1)
    c.olvidar(1)
    assert c.activos == 0


def test_cuenta_descartes_sin_confirmar():
    """Los descartes son la metrica que dice cuanto ruido esta filtrando el
    sistema. Si es cero, el umbral probablemente esta demasiado laxo."""
    c = ConfirmacionTemporal(aciertos=4, ventana=6)
    c.marcar(9, True)
    c.olvidar(9)
    assert c.descartados_sin_confirmar == 1


def test_configuracion_invalida_se_rechaza():
    """Pedir mas aciertos que el tamano de la ventana es imposible de cumplir:
    el detector jamas alertaria y el fallo pasaria inadvertido."""
    try:
        ConfirmacionTemporal(aciertos=7, ventana=6)
    except ValueError:
        return
    raise AssertionError("deberia rechazar aciertos > ventana")


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
