"""Pruebas del buffer de la vista en vivo.

Se corre sin dependencias:  python tests/test_preview.py
(tambien funciona con pytest si lo instalas)

Se prueba el buffer y no los endpoints porque es donde esta la logica con
aristas: el conteo de espectadores que gobierna si el worker gasta CPU
codificando, y la caducidad que suelta el ultimo frame de una camara apagada.

Dos comportamientos aqui son deliberados y facilmente "corregibles" por error
mas adelante, asi que quedan fijados por prueba:

  - `test_frame_existente_sale_de_inmediato`: el que abre el flujo recibe el
    frame que ya habia, sin esperar al siguiente. A 6 fps, esperar significa
    hasta 170 ms de recuadro en blanco al entrar en Monitoreo.
  - `test_camara_apagada_suelta_el_frame`: pasado el plazo, el JPEG se
    descarta. No es higiene de memoria: es no quedarse con la ultima imagen de
    una persona en RAM indefinidamente porque el worker murio en mal momento.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.preview import SEGUNDOS_PARA_OLVIDAR, BufferPreview  # noqa: E402

JPEG = b"\xff\xd8" + b"falso-jpeg" + b"\xff\xd9"


def _correr(corrutina):
    return asyncio.run(corrutina)


# --------------------------------------------------------------------------

def test_camara_desconocida_no_aparece():
    b = BufferPreview()
    assert b.camaras() == []
    assert b.espectadores("cam-que-no-existe") == 0


def test_publicar_registra_la_camara():
    b = BufferPreview()
    b.publicar("cam-01", JPEG)
    camaras = b.camaras()
    assert len(camaras) == 1
    assert camaras[0]["camera_id"] == "cam-01"


def test_sin_espectadores_reporta_cero():
    """Es el numero con el que el worker decide dejar de subir video."""
    b = BufferPreview()
    assert b.publicar("cam-01", JPEG) == 0


def test_el_flujo_cuenta_y_descuenta_espectadores():
    """Si el conteo no baja al cerrar, el worker sube video para siempre."""
    b = BufferPreview()

    async def caso():
        b.publicar("cam-01", JPEG)
        flujo = b.flujo_mjpeg("cam-01")
        trozo = await flujo.__anext__()          # abrir cuenta como espectador
        assert b.publicar("cam-01", JPEG) == 1
        await flujo.aclose()
        assert b.publicar("cam-01", JPEG) == 0
        return trozo

    trozo = _correr(caso())
    assert b"--frame" in trozo
    assert JPEG in trozo


def test_frame_existente_sale_de_inmediato():
    """Quien entra recibe lo que ya habia, no espera al frame siguiente."""
    b = BufferPreview()
    b.publicar("cam-01", JPEG)

    async def caso():
        flujo = b.flujo_mjpeg("cam-01")
        try:
            # Sin publicar nada nuevo: si esperara el siguiente, esto agotaria
            # el timeout y el generador terminaria sin entregar nada.
            return await asyncio.wait_for(flujo.__anext__(), timeout=1.0)
        finally:
            await flujo.aclose()

    assert JPEG in _correr(caso())


def test_varios_espectadores_reciben_el_mismo_frame():
    """Dos operadores mirando la misma camara no duplican el trabajo del worker."""
    b = BufferPreview()
    b.publicar("cam-01", JPEG)

    async def caso():
        a, c = b.flujo_mjpeg("cam-01"), b.flujo_mjpeg("cam-01")
        try:
            t1, t2 = await a.__anext__(), await c.__anext__()
            assert b.espectadores("cam-01") == 2
            return t1, t2
        finally:
            await a.aclose()
            await c.aclose()

    t1, t2 = _correr(caso())
    assert t1 == t2
    assert b.espectadores("cam-01") == 0


def test_multipart_bien_formado():
    """El navegador solo pinta el <img> si el multipart es exacto."""
    b = BufferPreview()
    b.publicar("cam-01", JPEG)

    async def caso():
        flujo = b.flujo_mjpeg("cam-01")
        try:
            return await flujo.__anext__()
        finally:
            await flujo.aclose()

    trozo = _correr(caso())
    assert trozo.startswith(b"--frame\r\n")
    assert b"Content-Type: image/jpeg\r\n" in trozo
    assert f"Content-Length: {len(JPEG)}".encode() in trozo
    assert trozo.endswith(JPEG + b"\r\n")


def test_camara_apagada_suelta_el_frame():
    b = BufferPreview()
    b.publicar("cam-01", JPEG)
    canal = b._canales["cam-01"]

    # Se envejece el canal en vez de esperar 30 s de reloj.
    canal.recibido -= SEGUNDOS_PARA_OLVIDAR + 1

    assert b.camaras() == []
    assert canal.frame is None, "el JPEG debe soltarse, no quedarse en memoria"


def test_la_camara_vuelve_despues_de_caerse():
    b = BufferPreview()
    b.publicar("cam-01", JPEG)
    b._canales["cam-01"].recibido -= SEGUNDOS_PARA_OLVIDAR + 1
    assert b.camaras() == []

    b.publicar("cam-01", JPEG)
    assert [c["camera_id"] for c in b.camaras()] == ["cam-01"]


def test_camaras_independientes():
    b = BufferPreview()
    b.publicar("cam-01", JPEG)
    b.publicar("cam-02", JPEG + b"otro")

    async def caso():
        flujo = b.flujo_mjpeg("cam-01")
        await flujo.__anext__()
        assert b.espectadores("cam-01") == 1
        assert b.espectadores("cam-02") == 0, "mirar una camara no afecta a la otra"
        await flujo.aclose()

    _correr(caso())
    assert len(b.camaras()) == 2


def test_el_fps_se_mide():
    """El fps que ve el operador en el recuadro sale de aqui."""
    b = BufferPreview()
    canal = b._canal("cam-01")
    for _ in range(6):
        canal.publicar(JPEG)
        canal.recibido -= 0.2          # simula 5 fps
    assert 3.0 <= canal.fps <= 7.0, f"fps medido: {canal.fps}"


def test_una_pausa_larga_no_hunde_el_fps():
    """Tras una reconexion, el fps no debe quedar clavado en casi cero."""
    b = BufferPreview()
    canal = b._canal("cam-01")
    for _ in range(4):
        canal.publicar(JPEG)
        canal.recibido -= 0.2
    antes = canal.fps

    canal.recibido -= 120.0            # la camara estuvo dos minutos caida
    canal.publicar(JPEG)
    assert canal.fps == antes, "un hueco largo no debe entrar en la media"


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
