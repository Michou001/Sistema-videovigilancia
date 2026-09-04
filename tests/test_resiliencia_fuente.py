"""La fuente en vivo aguanta cortes y errores sin apagar el worker.

Se corre sin dependencias:  python tests/test_resiliencia_fuente.py

Existe por una caida real y reproducible. Contra una Hikvision, el worker
terminaba solo -- una vez a los 9 minutos y otra a las 2 horas -- imprimiendo
sus estadisticas y saliendo con codigo 0, como si hubiera acabado bien. El log
tenia la pista:

    Exception in thread reader-192.168.1.21:
      File "edge/sources.py", line 258, in _loop
        ok, frame = self._cap.read()
    cv2.error: Unknown C++ exception from OpenCV code

Eran dos fallos encadenados:

  1. OpenCV no siempre devuelve ok=False cuando el stream RTSP se rompe: con el
     backend de FFmpeg lo sube como excepcion de C++. No estaba capturada, asi
     que mataba al hilo lector -- y con el a la reconexion, que estaba
     implementada justo debajo y nunca se ejecutaba. De ahi el `0 reconexiones`
     del resumen, que era la senal de que ni siquiera se intento.

  2. Aunque el hilo hubiera sobrevivido, `frames()` terminaba al primer hueco
     de 5 s. Para un sistema que corre sin supervision, un parpadeo de red no
     puede ser el final.

Un sistema de vigilancia que se apaga solo y ademas dice que todo salio bien es
peor que uno que se cae con un error: nadie va a mirar el log de algo que
termino con codigo 0.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from edge.sources import FrameSource, LiveSource  # noqa: E402


class _CapFalso:
    """VideoCapture de mentira, con averias programables.

    `guion` es una lista de lo que devuelve cada read(): un array = frame bueno,
    una excepcion = la lanza, None = devuelve (False, None) como OpenCV cuando
    se acaba el stream de forma limpia.
    """

    def __init__(self, guion, en_bucle=True):
        self.guion = list(guion)
        self.en_bucle = en_bucle
        self.i = 0
        self.lecturas = 0
        self.releases = 0
        self.abierto = True

    def isOpened(self):  # noqa: N802 - imita la API de OpenCV
        return self.abierto

    def set(self, *a):
        return True

    def get(self, *a):
        return 0

    def read(self):
        self.lecturas += 1
        if self.i >= len(self.guion):
            if not self.en_bucle:
                return False, None
            self.i = 0
        paso = self.guion[self.i]
        self.i += 1
        if isinstance(paso, BaseException):
            raise paso
        if paso is None:
            return False, None
        return True, paso

    def release(self):
        self.releases += 1


def _frame():
    return np.full((48, 64, 3), 7, np.uint8)


class _FuenteDePrueba(LiveSource):
    """LiveSource que abre _CapFalso en vez de una camara de verdad."""

    def __init__(self, caps, **kw):
        self.caps_pendientes = list(caps)
        self.caps_abiertos = []
        super().__init__("rtsp://falsa/stream", name="prueba", **kw)

    def _open(self) -> bool:
        if not self.caps_pendientes:
            return False
        cap = self.caps_pendientes.pop(0)
        self.caps_abiertos.append(cap)
        self._cap = cap
        with self._cond:
            self._status.connected = True
            self._status.last_error = None
        return True


def _esperar(condicion, limite=6.0):
    fin = time.monotonic() + limite
    while time.monotonic() < fin:
        if condicion():
            return True
        time.sleep(0.02)
    return False


# --------------------------------------------------------------------------

def test_cv2_error_no_mata_el_hilo_lector():
    """El fallo exacto que apagaba el worker."""
    import cv2

    averiado = _CapFalso([_frame(), cv2.error("Unknown C++ exception from OpenCV code")],
                         en_bucle=False)
    sano = _CapFalso([_frame()])
    f = _FuenteDePrueba([averiado, sano], max_backoff=0.1)
    try:
        assert _esperar(lambda: f.status.reconnects >= 1), \
            "no reconecto: el hilo lector murio con la excepcion de OpenCV"
        assert f._thread.is_alive(), "el hilo lector no sobrevivio"
        assert f.read(timeout=3.0) is not None, "no volvio a entregar imagen"
    finally:
        f.release()


def test_cualquier_excepcion_reconecta():
    """No solo cv2.error: nada que se escape puede apagar el hilo."""
    for averia in (RuntimeError("boom"), OSError("cable"), ValueError("raro")):
        malo = _CapFalso([_frame(), averia], en_bucle=False)
        f = _FuenteDePrueba([malo, _CapFalso([_frame()])], max_backoff=0.1)
        try:
            assert _esperar(lambda: f.status.reconnects >= 1), \
                f"{type(averia).__name__} mato al hilo"
            assert f._thread.is_alive()
        finally:
            f.release()


def test_release_que_lanza_no_mata_el_hilo():
    """Un handle roto tambien puede explotar al liberarlo."""
    # Entrega un frame antes de romperse a proposito: `reconnects` no cuenta la
    # conexion inicial, asi que una fuente que falla en su primera lectura
    # nunca llegaria a incrementarlo y la prueba no probaria nada.
    malo = _CapFalso([_frame(), None], en_bucle=False)

    def _explota():
        raise RuntimeError("release sobre handle roto")

    malo.release = _explota
    f = _FuenteDePrueba([malo, _CapFalso([_frame()])], max_backoff=0.1)
    try:
        assert _esperar(lambda: f.status.reconnects >= 1)
        assert f._thread.is_alive()
    finally:
        f.release()


def test_se_cuentan_las_reconexiones():
    """`0 reconexiones` tras una caida era la senal de que algo iba mal."""
    f = _FuenteDePrueba(
        [_CapFalso([_frame(), None], en_bucle=False),
         _CapFalso([_frame(), None], en_bucle=False),
         _CapFalso([_frame()])],
        max_backoff=0.1,
    )
    try:
        assert _esperar(lambda: f.status.reconnects >= 2), \
            f"reconexiones contadas: {f.status.reconnects}"
    finally:
        f.release()


def test_frames_espera_el_corte_si_se_le_pide():
    """El worker: un corte no termina la deteccion."""
    # Un frame, luego un hueco largo, luego imagen de nuevo.
    cap = _CapFalso([_frame()], en_bucle=False)
    f = _FuenteDePrueba([cap, _CapFalso([_frame()])], max_backoff=0.1)
    try:
        recibidos = []
        def consumir():
            for fr in f.frames(esperar_cortes=True):
                recibidos.append(fr)
                if len(recibidos) >= 3:
                    return
        h = threading.Thread(target=consumir, daemon=True)
        h.start()
        assert _esperar(lambda: len(recibidos) >= 3, limite=8.0), \
            f"el generador se rindio tras el corte ({len(recibidos)} frames)"
    finally:
        f.release()


def test_frames_termina_si_el_hilo_lector_murio():
    """Esperar para siempre a algo que ya no puede volver seria peor."""
    f = _FuenteDePrueba([_CapFalso([_frame()])], max_backoff=0.1)
    assert _esperar(lambda: f.status.frames_grabbed > 0)
    # Se simula el peor caso: el lector ya no esta.
    f._stop.set()
    f._thread.join(timeout=3.0)
    assert not f.reconectando, "sin hilo lector no hay reconexion posible"

    t0 = time.monotonic()
    list(f.frames(esperar_cortes=True))
    assert time.monotonic() - t0 < 10, "se quedo colgado en vez de terminar"


def test_una_fuente_sin_reconexion_no_espera():
    """Un archivo que se acaba, se acabo: `reconectando` es False por defecto."""
    assert FrameSource.reconectando.fget(object()) is False


def test_el_diagnostico_conserva_su_comportamiento():
    """frames() sin esperar_cortes debe seguir terminando en un corte.

    El diagnostico mide y sale; si se quedara esperando a una camara muerta,
    dejaria de servir justo para lo que existe.
    """
    f = _FuenteDePrueba([_CapFalso([_frame()], en_bucle=False)], max_backoff=0.1)
    try:
        assert _esperar(lambda: f.status.frames_grabbed > 0)
        t0 = time.monotonic()
        list(f.frames())          # sin esperar_cortes
        assert time.monotonic() - t0 < 20, "no termino en un tiempo razonable"
    finally:
        f.release()


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
