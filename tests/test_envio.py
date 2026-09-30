"""Pruebas del envio de eventos del worker (edge/sink.py) y de su configuracion.

    python tests/test_envio.py

La regla que se protege aqui: si la plataforma web no responde, el worker ni
se frena ni pierde eventos. Se levanta un servidor HTTP de mentira que se
puede "tumbar" y "levantar" a voluntad.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge.config import parse_env_line  # noqa: E402
from edge.sink import HttpSink, JsonlSink  # noqa: E402
from shared.events import DetectionEvent, EventType  # noqa: E402


class _Api:
    """Servidor de mentira: acepta lotes o responde 503 segun `caida`."""

    def __init__(self) -> None:
        self.caida = False
        self.recibidos: list[dict] = []
        api = self

        class Manejador(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                largo = int(self.headers.get("Content-Length", 0))
                cuerpo = self.rfile.read(largo)
                if api.caida:
                    self.send_response(503)
                    self.end_headers()
                    return
                lote = json.loads(cuerpo)
                api.recibidos.extend(lote["events"])
                respuesta = json.dumps({"accepted": len(lote["events"]), "matches": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(respuesta)))
                self.end_headers()
                self.wfile.write(respuesta)

            def log_message(self, *args):  # silencio en la salida de las pruebas
                pass

        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), Manejador)
        self.url = f"http://127.0.0.1:{self.servidor.server_address[1]}"
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()

    def cerrar(self) -> None:
        self.servidor.shutdown()


def _evento(i: int) -> DetectionEvent:
    return DetectionEvent(camera_id="cam-prueba", type=EventType.PLATE,
                          value=f"ABC-{100 + i}", confidence=0.9, track_id=i)


def _esperar(condicion, segundos: float = 10.0) -> bool:
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(0.05)
    return condicion()


# --------------------------------------------------------------------------

def test_envio_normal_por_lotes():
    api, spool = _Api(), Path(tempfile.mkdtemp())
    sink = HttpSink(api.url, "token", spool, camera_id="cam-prueba")
    try:
        for i in range(7):
            sink.enviar(_evento(i))
        assert _esperar(lambda: len(api.recibidos) == 7), api.recibidos
        assert [e["value"] for e in api.recibidos] == [f"ABC-{100 + i}" for i in range(7)]
    finally:
        sink.cerrar()
        api.cerrar()
        shutil.rmtree(spool, ignore_errors=True)


def test_api_caida_no_frena_ni_pierde():
    """REGRESION: el POST corria dentro del bucle de deteccion; con la API
    caida cada evento esperaba el timeout completo."""
    api, spool = _Api(), Path(tempfile.mkdtemp())
    api.caida = True
    sink = HttpSink(api.url, "token", spool, camera_id="cam-prueba")
    try:
        t0 = time.perf_counter()
        for i in range(10):
            sink.enviar(_evento(i))
        assert time.perf_counter() - t0 < 0.1, "enviar() no debe esperar a la red"

        assert _esperar(lambda: sink.stats["en_spool"] > 0), "debio guardar en disco"
        assert not api.recibidos

        api.caida = False
        # Reintenta solo, sin que lleguen eventos nuevos.
        assert _esperar(lambda: len(api.recibidos) == 10, 40), len(api.recibidos)
        assert _esperar(lambda: not list(spool.glob("*.json")))
    finally:
        sink.cerrar()
        api.cerrar()
        shutil.rmtree(spool, ignore_errors=True)


def test_lo_pendiente_queda_en_disco_al_cerrar():
    spool = Path(tempfile.mkdtemp())
    # Puerto sin nadie escuchando: todo falla.
    sink = HttpSink("http://127.0.0.1:9", "token", spool, camera_id="cam-prueba", timeout=0.5)
    try:
        for i in range(5):
            sink.enviar(_evento(i))
        sink.cerrar()
        guardados = sum(len(json.loads(f.read_text())["events"]) for f in spool.glob("*.json"))
        assert guardados == 5, guardados
    finally:
        shutil.rmtree(spool, ignore_errors=True)


def test_jsonl_no_escribe_embedding():
    carpeta = Path(tempfile.mkdtemp())
    sink = JsonlSink(carpeta)
    try:
        ev = _evento(1)
        ev.embedding = [0.1] * 512
        sink.enviar(ev)
        sink.cerrar()
        linea = next(carpeta.glob("eventos-*.jsonl")).read_text(encoding="utf-8")
        assert "embedding" not in json.loads(linea)
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)


# --------------------------------------------------------------------------
# Archivo de entorno

def test_env_comentario_al_final():
    """REGRESION: "ENABLE_MOTION=true  # nota" se leia como falso."""
    assert parse_env_line("ENABLE_MOTION=true     # velocidad anomala") == ("ENABLE_MOTION", "true")
    assert parse_env_line("DEVICE=auto   # auto | cuda | cpu") == ("DEVICE", "auto")


def test_env_contrasena_con_gato():
    assert parse_env_line("SOURCE=rtsp://admin:ab#12@10.0.0.5/x") == \
        ("SOURCE", "rtsp://admin:ab#12@10.0.0.5/x")


def test_env_comillas_y_lineas_vacias():
    assert parse_env_line('NOMBRE="Acceso # norte"') == ("NOMBRE", "Acceso # norte")
    assert parse_env_line("# comentario") is None
    assert parse_env_line("   ") is None
    assert parse_env_line("SIN_VALOR=") == ("SIN_VALOR", "")


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
