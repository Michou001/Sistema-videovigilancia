"""Pruebas del ciclo de reentrenamiento: el worker guarda el cuadro limpio de
cada alerta, la API da el veredicto de los operadores y la herramienta arma
el dataset YOLO (confirmadas con caja, falsos positivos como fondo).

    python tests/test_reentrenamiento.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

_TMP = Path(tempfile.mkdtemp())
from bd_prueba import borrar as _borrar_bd  # noqa: E402
from bd_prueba import url_temporal  # noqa: E402

os.environ["DATABASE_URL"] = url_temporal(_TMP, "reentrena")
os.environ["API_TOKEN"] = "token-de-prueba-del-worker"

import warnings  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from api.database import engine, init_db  # noqa: E402
from api.main import app  # noqa: E402
from api.models import Operator  # noqa: E402
from api.security import hash_password, limite_login  # noqa: E402
from edge.dataset import RecolectorDataset, crear_recolector  # noqa: E402
from shared.events import BBox, DetectionEvent, EventType  # noqa: E402
from tools import dataset_alertas as D  # noqa: E402

WORKER = {"X-API-Token": "token-de-prueba-del-worker"}


def _cfg(**extra):
    base = dict(camera_id="cam-dataset", dataset_tipos="weapon,anomaly,zone", dataset_dias=30,
                dataset_enabled=True)
    base.update(extra)
    return SimpleNamespace(**base)


def _frame():
    img = np.zeros((400, 800, 3), np.uint8)
    img[100:300, 200:300] = (0, 0, 255)
    return SimpleNamespace(ts=time.time(), frame=img)


def _evento(tipo=EventType.WEAPON, valor="knife", bbox=(200, 100, 300, 300), **meta):
    return DetectionEvent(camera_id="cam-dataset", type=tipo, value=valor, confidence=0.8,
                          bbox=BBox(x1=bbox[0], y1=bbox[1], x2=bbox[2], y2=bbox[3]) if bbox else None,
                          meta=meta)


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------

def test_guarda_el_cuadro_limpio_de_las_alertas():
    carpeta = _TMP / "entrenamiento"
    r = RecolectorDataset(_cfg(), carpeta=carpeta)
    arma = _evento()
    r.al_evento(arma, _frame())
    r.al_evento(_evento(EventType.PLATE, "ABC-123-A"), _frame())
    r.al_evento(_evento(EventType.CAMERA, "sabotaje", bbox=None), _frame())
    base = carpeta / "cam-dataset"
    assert sorted(p.name for p in base.iterdir()) == [f"{arma.event_id}.jpg", f"{arma.event_id}.json"], \
        "solo los tipos configurados y con caja"
    datos = json.loads((base / f"{arma.event_id}.json").read_text())
    assert datos["bbox"] == [200, 100, 300, 300] and (datos["ancho"], datos["alto"]) == (800, 400)
    img = cv2.imread(str(base / f"{arma.event_id}.jpg"))
    assert img.shape == (400, 800, 3) and img[200, 250, 2] > 200, "el cuadro original, sin nada dibujado"

    normal = _evento()
    r.al_evento(normal, _frame())
    r.al_responder({}, {"matches": [{"event_id": normal.event_id, "severity": "info"},
                                    {"event_id": arma.event_id, "severity": "critical"}]})
    assert not (base / f"{normal.event_id}.jpg").exists(), "lo que no fue alerta nadie lo va a revisar"
    assert (base / f"{arma.event_id}.jpg").exists()
    assert r.estado()["dataset"] == {"guardados": 2, "descartados": 1}

    viejo = _evento()
    r.al_evento(viejo, _frame())
    hace_40_dias = time.time() - 40 * 86400
    for ext in (".jpg", ".json"):
        os.utime(base / f"{viejo.event_id}{ext}", (hace_40_dias, hace_40_dias))
    assert r.limpiar() == 2 and not (base / f"{viejo.event_id}.jpg").exists()


def test_se_engancha_a_la_respuesta_de_la_api_y_al_worker():
    llamadas = []
    http = SimpleNamespace(al_responder=lambda lote, resp: llamadas.append("clips"))
    assert crear_recolector(_cfg(dataset_enabled=False), SimpleNamespace(http=http)) is None
    r = crear_recolector(_cfg(camera_id="cam-engancha"), SimpleNamespace(http=http))
    try:
        http.al_responder({}, {"matches": []})
        assert llamadas == ["clips"], "el enganche anterior (clips) se sigue llamando"

        from edge.worker import Camara

        enviados = []
        cam = Camara.__new__(Camara)
        cam.id, cam.total_eventos = "cam-engancha", 0
        cam.sink = SimpleNamespace(enviar=enviados.append)
        cam.complementos = [r]
        ev = _evento()
        cam.emitir(ev, _frame())
        cam.emitir(_evento())          # al cerrar, sin cuadro: no se guarda nada
        assert len(enviados) == 2 and r.guardados == 1
    finally:
        shutil.rmtree(r.carpeta, ignore_errors=True)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def _cliente():
    init_db()
    with Session(engine) as s:
        if s.exec(select(Operator).where(Operator.username == "admin")).first() is None:
            s.add(Operator(username="admin", display_name="admin", role="admin",
                           password_hash=hash_password("clave-de-prueba-1")))
            s.commit()
    limite_login.exito("testclient")
    c = TestClient(app)
    token = c.post("/api/auth/login", json={"username": "admin", "password": "clave-de-prueba-1"}).json()["token"]
    return c, {"Authorization": f"Bearer {token}"}


def test_veredictos_para_el_worker():
    c, h = _cliente()
    ids = [str(uuid.uuid4()) for _ in range(4)]
    eventos = [{"event_id": ids[i], "camera_id": "cam-v", "type": "camera", "value": "sabotaje",
                "confidence": 1.0} for i in range(3)]
    eventos.append({"event_id": ids[3], "camera_id": "cam-v", "type": "zone", "value": "intrusion",
                    "confidence": 0.9, "meta": {"zona_id": 999}})        # info: sin alerta
    assert c.post("/api/events", json={"camera_id": "cam-v", "events": eventos}, headers=WORKER).status_code == 200
    alertas = {a["event_id"]: a["id"] for a in c.get("/api/alerts", headers=h).json()}
    c.post(f"/api/alerts/{alertas[ids[0]]}/resolver", json={"accion": "acknowledge"}, headers=h)
    c.post(f"/api/alerts/{alertas[ids[1]]}/resolver", json={"accion": "dismiss", "motivo": "falso positivo"},
           headers=h)
    assert c.post("/api/events/veredictos", json={"event_ids": ids}).status_code == 401
    r = c.post("/api/events/veredictos", json={"event_ids": ids + ["../../x"]}, headers=WORKER).json()
    v = r["veredictos"]
    assert v[ids[0]]["estado"] == "acknowledged"
    assert v[ids[1]] == {"estado": "dismissed", "motivo": "falso positivo", "tipo": "camera"}
    assert v[ids[2]]["estado"] == "new"
    assert ids[3] not in v, "sin alerta no hay veredicto"


# --------------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------------

def test_arma_el_dataset_yolo():
    carpeta = _TMP / "ds"
    r = RecolectorDataset(_cfg(camera_id="cam-ds"), carpeta=carpeta)
    confirmada, falso, pendiente = _evento(), _evento(), _evento()
    persona = _evento(EventType.ZONE, "intrusion", bbox=(0, 0, 800, 400), clase="persona")
    for ev in (confirmada, falso, pendiente, persona):
        r.al_evento(ev, _frame())
    veredictos = {confirmada.event_id: {"estado": "acknowledged"},
                  falso.event_id: {"estado": "dismissed", "motivo": "era un celular"},
                  pendiente.event_id: {"estado": "new"},
                  persona.event_id: {"estado": "acknowledged"}}
    salida = _TMP / "dataset.zip"
    assert D.main(["--carpeta", str(carpeta), "--salida", str(salida)],
                  obtener=lambda ids: {i: veredictos[i] for i in ids if i in veredictos}) == 0
    with zipfile.ZipFile(salida) as z:
        nombres = z.namelist()
        yaml = z.read("dataset/data.yaml").decode()
        assert "0: knife" in yaml and "1: persona" in yaml
        etiqueta = next(n for n in nombres if n.endswith(f"{confirmada.event_id}.txt"))
        assert z.read(etiqueta).decode() == "0 0.312500 0.500000 0.125000 0.500000\n"
        fondo = next(n for n in nombres if n.endswith(f"{falso.event_id}.txt"))
        assert z.read(fondo).decode() == "", "falso positivo = imagen de fondo"
        assert not any(pendiente.event_id in n for n in nombres), "sin revisar no entra"
        assert z.read(next(n for n in nombres if n.endswith(f"{persona.event_id}.txt"))).decode() \
            == "1 0.500000 0.500000 1.000000 1.000000\n"
        assert sum(n.startswith("dataset/images/") for n in nombres) == 3
        resumen = z.read("dataset/resumen.csv").decode()
        assert "era un celular" in resumen and "LEEME" in " ".join(nombres)
    assert D.es_validacion("abc") == D.es_validacion("abc")
    assert D.main(["--carpeta", str(_TMP / "vacia"), "--salida", str(salida)], obtener=dict) == 1


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
        engine.dispose()
        _borrar_bd(os.environ["DATABASE_URL"])
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
