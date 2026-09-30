"""Pruebas de la busqueda por descripcion: indexado de capturas, orden por
parecido, filtros, bitacora y retencion. El modelo se simula con un
codificador de colores (una foto roja se parece a "rojo"): la mecanica es la
misma que con SigLIP, sin descargar 1.5 GB.

    python tests/test_busqueda.py
"""

from __future__ import annotations

import base64
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

_TMP = Path(tempfile.mkdtemp())
from bd_prueba import borrar as _borrar_bd  # noqa: E402
from bd_prueba import url_temporal  # noqa: E402

os.environ["DATABASE_URL"] = url_temporal(_TMP, "busqueda")
os.environ["API_TOKEN"] = "token-de-prueba-del-worker"

import warnings  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from api import semantica  # noqa: E402
from api.config import BASE_DIR, get_config  # noqa: E402
from api.database import engine, init_db  # noqa: E402
from api.main import app  # noqa: E402
from api.models import AuditLog, Event, Operator, SemanticEmbedding  # noqa: E402
from api.retention import Politica, purgar  # noqa: E402
from api.security import hash_password, limite_login  # noqa: E402

WORKER = {"X-API-Token": "token-de-prueba-del-worker"}
COLORES = {"rojo": (0, 0, 255), "azul": (255, 0, 0), "verde": (0, 255, 0)}
_archivos: list[Path] = []


class CodificadorDeColores:
    """Vector = color promedio de la imagen; texto = el color que menciona."""

    nombre = "prueba/colores"
    dim = 3

    def imagenes(self, imagenes):
        v = np.array([img.reshape(-1, 3).mean(axis=0)[::-1] for img in imagenes], dtype=np.float32) + 1.0
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def texto(self, frase):
        for nombre, (b, g, r) in COLORES.items():
            if nombre in frase:
                v = np.array([r, g, b], dtype=np.float32) + 1.0
                return v / np.linalg.norm(v)
        return np.ones(3, dtype=np.float32) / np.sqrt(3)


def _foto(color) -> str:
    img = np.zeros((60, 90, 3), np.uint8)
    img[:] = COLORES[color]
    return base64.b64encode(cv2.imencode(".jpg", img)[1].tobytes()).decode()


def _usuario(nombre: str, rol: str) -> None:
    with Session(engine) as s:
        if s.exec(select(Operator).where(Operator.username == nombre)).first() is None:
            s.add(Operator(username=nombre, display_name=nombre, role=rol,
                           password_hash=hash_password("clave-de-prueba-1")))
            s.commit()


def _cliente(nombre: str = "admin", rol: str = "admin") -> tuple[TestClient, dict]:
    init_db()
    _usuario(nombre, rol)
    limite_login.exito("testclient")
    c = TestClient(app)
    token = c.post("/api/auth/login", json={"username": nombre, "password": "clave-de-prueba-1"}).json()["token"]
    return c, {"Authorization": f"Bearer {token}"}


VALORES = {"zone": "intrusion", "plate": "XYZ-987-B"}   # sin regla ni lista negra: eventos normales


def _evento(c, color, camara="cam-a", tipo="zone", ts=None) -> str:
    eid = str(uuid.uuid4())
    ev = {"event_id": eid, "camera_id": camara, "type": tipo, "value": VALORES[tipo],
          "confidence": 0.7, "snapshot_b64": _foto(color)}
    if ts:
        ev["ts"] = ts.isoformat()
    r = c.post("/api/events", json={"camera_id": camara, "events": [ev]}, headers=WORKER)
    assert r.status_code == 200, r.text
    _archivos.append(get_config().snapshot_dir / f"{eid}.jpg")
    return eid


def _indice() -> semantica.IndiceSemantico:
    idx = semantica.IndiceSemantico(engine, fabrica=CodificadorDeColores, min_similitud=0.5)
    idx.cargar()
    return idx


# --------------------------------------------------------------------------

def test_indexa_y_ordena_por_parecido():
    c, h = _cliente()
    rojo, azul, verde = _evento(c, "rojo"), _evento(c, "azul"), _evento(c, "verde")
    idx = _indice()
    assert idx.listo and idx.pendientes() >= 3
    assert idx.indexar() >= 3
    assert idx.pendientes() == 0
    assert idx.indexar() == 0, "lo ya indexado no se repite"
    r = idx.buscar("coche rojo", limite=500)
    parecidos = [x.event_id for x in r if x.similitud > 0.9]
    assert rojo in parecidos and azul not in parecidos and verde not in parecidos
    r = idx.buscar("mochila azul", candidatos={azul, verde})
    assert [x.event_id for x in r][:1] == [azul]
    assert rojo not in [x.event_id for x in r], "fuera de los candidatos del filtro"
    assert verde in [x.event_id for x in idx.buscar("verde", limite=500) if x.similitud > 0.9]


def test_endpoint_con_filtros_y_bitacora():
    c, h = _cliente()
    semantica.indice = None
    assert c.get("/api/busqueda", params={"q": "rojo"}, headers=h).status_code == 404
    assert c.get("/api/busqueda/estado", headers=h).json() == {"estado": "desactivado"}

    rojo_a = _evento(c, "rojo", camara="cam-a")
    rojo_b = _evento(c, "rojo", camara="cam-b", tipo="plate")
    idx = _indice()
    idx.indexar(500)
    semantica.indice = idx
    try:
        r = c.get("/api/busqueda", params={"q": "persona de rojo"}, headers=h)
        assert r.status_code == 200, r.text
        ids = [x["event_id"] for x in r.json()["resultados"]]
        assert rojo_a in ids and rojo_b in ids
        primero = r.json()["resultados"][0]
        assert primero["similitud"] >= 0.9 and primero["snapshot_path"] and primero["type"] in ("plate", "zone")
        r = c.get("/api/busqueda", params={"q": "persona de rojo", "camera_id": "cam-b"}, headers=h).json()
        assert [x["event_id"] for x in r["resultados"]] == [rojo_b]
        r = c.get("/api/busqueda", params={"q": "persona de rojo", "tipo": "zone"}, headers=h).json()
        assert rojo_b not in [x["event_id"] for x in r["resultados"]]
        assert rojo_a in [x["event_id"] for x in r["resultados"]]
        ayer = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        r = c.get("/api/busqueda", params={"q": "rojo", "hasta": ayer}, headers=h).json()
        assert r["resultados"] == []
        assert c.get("/api/busqueda", params={"q": "x"}, headers=h).status_code == 422, "minimo 2 letras"
        _, h_vi = _cliente("consulta-b", "viewer")
        assert c.get("/api/busqueda", params={"q": "rojo"}, headers=h_vi).status_code == 200
        estado = c.get("/api/busqueda/estado", headers=h).json()
        assert estado["estado"] == "listo" and estado["pendientes"] == 0 and estado["modelo"] == "prueba/colores"
    finally:
        semantica.indice = None
    with Session(engine) as s:
        entradas = s.exec(select(AuditLog).where(AuditLog.accion == "busqueda.semantica")).all()
    assert entradas and entradas[0].objetivo == "persona de rojo", "cada busqueda queda en la bitacora"


def test_modelo_cargando_o_con_error():
    c, h = _cliente()
    idx = semantica.IndiceSemantico(engine, fabrica=CodificadorDeColores)
    idx.estado = "cargando"
    semantica.indice = idx
    try:
        r = c.get("/api/busqueda", params={"q": "rojo"}, headers=h)
        assert r.status_code == 503 and "cargando" in r.json()["detail"]

        def _falla():
            raise ImportError("No module named 'open_clip'")

        idx = semantica.IndiceSemantico(engine, fabrica=_falla)
        idx.cargar()
        semantica.indice = idx
        r = c.get("/api/busqueda", params={"q": "rojo"}, headers=h)
        assert r.status_code == 503 and "open_clip" in r.json()["detail"]
        assert c.get("/api/busqueda/estado", headers=h).json()["estado"] == "error"
    finally:
        semantica.indice = None


def test_foto_perdida_no_se_reintenta():
    c, _ = _cliente()
    eid = _evento(c, "azul")
    (get_config().snapshot_dir / f"{eid}.jpg").unlink()
    idx = _indice()
    idx.indexar(500)
    with Session(engine) as s:
        fila = s.exec(select(SemanticEmbedding).where(SemanticEmbedding.event_id == eid)).one()
        assert fila.dim == 0, "queda marcada para no volver a intentarlo"
    assert eid not in [x.event_id for x in idx.buscar("azul", limite=500)]


def test_el_vector_se_va_con_la_foto():
    c, _ = _cliente()
    viejo = _evento(c, "verde", ts=datetime.now(timezone.utc) - timedelta(days=10))
    nuevo = _evento(c, "verde")
    idx = _indice()
    idx.indexar(500)
    assert viejo in [x.event_id for x in idx.buscar("verde", limite=500)]
    with Session(engine) as s:
        purgar(s, Politica())          # fotos de eventos normales: 7 dias
    with Session(engine) as s:
        assert s.exec(select(SemanticEmbedding).where(SemanticEmbedding.event_id == viejo)).first() is None
        assert s.exec(select(SemanticEmbedding).where(SemanticEmbedding.event_id == nuevo)).first() is not None
        assert s.exec(select(Event).where(Event.event_id == viejo)).one().snapshot_path is None
    idx._reconstruida = 0            # la reconstruccion periodica suelta lo borrado
    ids = [x.event_id for x in idx.buscar("verde", limite=500)]
    assert viejo not in ids and nuevo in ids
    # Y con el evento entero (30 dias) tambien.
    antiguo = _evento(c, "rojo", ts=datetime.now(timezone.utc) - timedelta(days=40))
    idx.indexar(500)
    with Session(engine) as s:
        purgar(s, Politica())
        assert s.exec(select(Event).where(Event.event_id == antiguo)).first() is None


def test_vectores_en_memoria_incrementales():
    c, _ = _cliente()
    idx = _indice()
    idx.indexar(500)
    antes = idx.buscar("rojo", limite=500)
    nuevo = _evento(c, "rojo")
    idx.indexar(500)
    despues = idx.buscar("rojo", limite=500)
    assert nuevo in [x.event_id for x in despues] and len(despues) == len(antes) + 1
    assert idx.resumen()["en_memoria"] >= len(despues)
    assert BASE_DIR.exists()


# --------------------------------------------------------------------------

def main() -> int:
    pruebas = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    fallos = 0
    try:
        for nombre, fn in pruebas:
            try:
                fn()
                print(f"  [OK]    {nombre}")
            except AssertionError as e:
                fallos += 1
                print(f"  [FALLA] {nombre}: {e}")
            except Exception as e:  # noqa: BLE001
                fallos += 1
                import traceback

                traceback.print_exc()
                print(f"  [ERROR] {nombre}: {type(e).__name__}: {e}")
    finally:
        for f in _archivos:
            f.unlink(missing_ok=True)
        engine.dispose()
        _borrar_bd(os.environ["DATABASE_URL"])
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
