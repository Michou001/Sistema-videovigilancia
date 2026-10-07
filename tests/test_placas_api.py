"""Pruebas de la API de placas: lista negra con placas mexicanas y extranjeras,
descripcion del vehiculo en la alerta, correccion de lecturas por el operador,
dataset para reentrenar el OCR y retencion de las lecturas corregidas.

    python tests/test_placas_api.py
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

_TMP = Path(tempfile.mkdtemp())
from bd_prueba import borrar as _borrar_bd  # noqa: E402
from bd_prueba import url_temporal  # noqa: E402

os.environ["DATABASE_URL"] = url_temporal(_TMP, "placas")
os.environ["API_TOKEN"] = "token-de-prueba-del-worker"

import warnings  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from api.config import get_config  # noqa: E402
from api.database import engine, init_db  # noqa: E402
from api.main import app  # noqa: E402
from api.models import AuditLog, Event, Operator  # noqa: E402
from api.retention import Politica, purgar  # noqa: E402
from api.security import hash_password, limite_login  # noqa: E402

WORKER = {"X-API-Token": "token-de-prueba-del-worker"}
_archivos: list[Path] = []


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


def _evento(valor: str, **extra) -> dict:
    return {"event_id": str(uuid.uuid4()), "camera_id": "cam-placas", "type": "plate",
            "value": valor, "confidence": 0.9, **extra}


def _ingerir(c: TestClient, *eventos) -> dict:
    r = c.post("/api/events", json={"camera_id": "cam-placas", "events": list(eventos)}, headers=WORKER)
    assert r.status_code == 200, r.text
    return r.json()


def _foto(nombre: str, ancho=300, alto=150) -> str:
    img = np.full((alto, ancho, 3), 200, np.uint8)
    cv2.rectangle(img, (ancho // 4, alto // 4), (3 * ancho // 4, 3 * alto // 4), (20, 20, 20), -1)
    ruta = get_config().snapshot_dir / f"{nombre}.jpg"
    cv2.imwrite(str(ruta), img)
    _archivos.append(ruta)
    return ruta.relative_to(RAIZ).as_posix()


# --------------------------------------------------------------------------

def test_lista_negra_sugiere_correccion():
    c, h = _cliente()
    r = c.post("/api/blacklist/plates", json={"plate": "POW-123-A", "reason": "robo"}, headers=h)
    assert r.status_code == 422
    detalle = r.json()["detail"]
    assert "PDW-123-A" in detalle and "I, Ñ, O ni Q" in detalle, detalle


def test_lista_negra_con_tipo_entidad_y_extranjeras():
    c, h = _cliente()
    r = c.post("/api/blacklist/plates", json={"plate": "jhk123a", "reason": "robo"}, headers=h)
    assert r.status_code == 201, r.text
    assert (r.json()["plate"], r.json()["tipo"], r.json()["entidad"]) == \
        ("JHK-123-A", "Automóvil particular", "Jalisco")
    r = c.post("/api/blacklist/plates", json={"plate": "7ABC123", "reason": "reporte EUA"}, headers=h)
    assert r.status_code == 422 and "extranjera" in r.json()["detail"]
    r = c.post("/api/blacklist/plates", headers=h,
               json={"plate": "7ABC123", "reason": "reporte EUA", "extranjera": True})
    assert r.status_code == 201, r.text
    assert r.json()["extranjera"] and r.json()["tipo"] == "Placa extranjera"
    listado = {p["plate"]: p for p in c.get("/api/blacklist/plates", headers=h).json()}
    assert listado["JHK-123-A"]["entidad"] == "Jalisco"


def test_alerta_describe_el_vehiculo():
    c, h = _cliente()
    c.post("/api/blacklist/plates", json={"plate": "RKA-456-B", "reason": "orden judicial"}, headers=h)
    _ingerir(c, _evento("RKA-456-B", meta={"color_vehiculo": "rojo", "tipo_placa": "Automóvil particular",
                                           "entidad": "Nuevo León", "pais": "México"}))
    alerta = next(a for a in c.get("/api/alerts", headers=h).json() if "RKA-456-B" in a["title"])
    assert "Vehículo rojo (color aprox.)" in alerta["detail"]
    assert "Automóvil particular de Nuevo León" in alerta["detail"]


def test_correccion_de_lectura_genera_la_alerta():
    c, h = _cliente()
    c.post("/api/blacklist/plates", json={"plate": "GHT-903-K", "reason": "robado"}, headers=h)
    ev = _evento("GHT-908-X")         # dos caracteres mal leidos: no coincide
    m = _ingerir(c, ev)["matches"][0]
    assert m["severity"] == "info"

    _usuario("vigia", "viewer")
    cv, hv = _cliente("vigia", "viewer")
    assert cv.post(f"/api/events/{ev['event_id']}/correccion", json={"valor": "GHT-903-K"},
                   headers=hv).status_code == 403

    _usuario("turno", "operator")
    co, ho = _cliente("turno", "operator")
    r = co.post(f"/api/events/{ev['event_id']}/correccion", json={"valor": "GHT-9O3-K"}, headers=ho)
    assert r.status_code == 422 and "GHT-903-K" in r.json()["detail"], "debe sugerir la correccion"
    r = co.post(f"/api/events/{ev['event_id']}/correccion", json={"valor": "ght903k"}, headers=ho)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["value"] == "GHT-903-K" and cuerpo["severity"] == "critical"
    assert cuerpo["entidad"] == "Guanajuato"
    assert cuerpo["alerta"] and "GHT-903-K" in cuerpo["alerta"]["title"]
    assert "corregida por turno" in cuerpo["alerta"]["detail"]

    fila = c.get("/api/events", params={"q": "GHT903K"}, headers=h).json()[0]
    assert fila["meta"].get("lectura_original") == "GHT-908-X"
    with Session(engine) as s:
        evento = s.exec(select(Event).where(Event.event_id == ev["event_id"])).one()
        assert evento.corregido
        bitacora = s.exec(select(AuditLog).where(AuditLog.accion == "eventos.correccion")).all()
        assert any("GHT-908-X" in (b.detalle or "") for b in bitacora)


def test_dataset_de_placas_para_el_ocr():
    c, h = _cliente()
    ev = _evento("XYZ-111-A", snapshot_path=_foto("dataset-1"),
                 meta={"placa_en_evidencia": [0.25, 0.25, 0.75, 0.75]})
    _ingerir(c, ev)
    c.post(f"/api/events/{ev['event_id']}/correccion", json={"valor": "XYZ-777-A"}, headers=h)
    automatica = _evento("ABC-222-B", confidence=0.97, observations=8, snapshot_path=_foto("dataset-2"))
    _ingerir(c, automatica)

    r = c.get("/api/dataset/placas.zip", headers=h)
    assert r.status_code == 200 and r.headers["x-placas-corregidas"] == "1"
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        filas = list(csv.DictReader(io.StringIO(z.read("anotaciones.csv").decode())))
        textos = {f["plate_text"]: f for f in filas}
        assert "XYZ777A" in textos and textos["XYZ777A"]["plate_region"] == "Mexico"
        assert "ABC222B" not in textos, "sin ?automaticas=true solo van las corregidas"
        img = cv2.imdecode(np.frombuffer(z.read(textos["XYZ777A"]["image_path"]), np.uint8),
                           cv2.IMREAD_COLOR)
        assert img.shape[:2] == (75, 150), f"recorte justo de la placa, no la evidencia: {img.shape}"
        assert "LEEME.txt" in z.namelist()

    r = c.get("/api/dataset/placas.zip", params={"automaticas": "true"}, headers=h)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert "ABC222B" in z.read("anotaciones.csv").decode()

    _usuario("turno2", "operator")
    co, ho = _cliente("turno2", "operator")
    assert co.get("/api/dataset/placas.zip", headers=ho).status_code == 403


def test_analizar_placa():
    c, h = _cliente()
    r = c.get("/api/placas/analizar", params={"texto": "A01-AAA"}, headers=h).json()
    assert r["valida"] and r["entidad"] == "Ciudad de México"
    r = c.get("/api/placas/analizar", params={"texto": "POW123A"}, headers=h).json()
    assert not r["valida"] and r["sugerencia"] == "PDW-123-A"


def test_retencion_conserva_las_corregidas():
    c, h = _cliente()
    vieja = _evento("DEF-333-C", ts=(datetime.now(timezone.utc) - timedelta(days=60)).isoformat())
    corregida = _evento("DEF-334-C", ts=(datetime.now(timezone.utc) - timedelta(days=60)).isoformat())
    _ingerir(c, vieja, corregida)
    c.post(f"/api/events/{corregida['event_id']}/correccion", json={"valor": "DEF-338-C"}, headers=h)
    with Session(engine) as s:
        purgar(s, Politica())
        ids = {e.event_id for e in s.exec(select(Event)).all()}
    assert vieja["event_id"] not in ids, "un evento normal de 60 dias se purga (30 dias)"
    assert corregida["event_id"] in ids, "una lectura corregida vive RETENCION_CORREGIDOS_DIAS"


def test_retencion_borra_alertas_viejas_con_su_evento():
    """Alerta y evento se borran en el orden que exige la llave foranea
    (PostgreSQL la revisa; SQLite no)."""
    from api.models import Alert

    c, h = _cliente()
    c.post("/api/blacklist/plates", json={"plate": "PLD-123-A", "reason": "viejo"}, headers=h)
    ev = _evento("PLD-123-A")
    _ingerir(c, ev)
    with Session(engine) as s:
        alerta = s.exec(select(Alert).where(Alert.event_id == ev["event_id"])).one()
        alerta.created_at = datetime.now(timezone.utc) - timedelta(days=400)
        s.commit()
        cuenta = purgar(s, Politica())
        assert cuenta["alertas"] >= 1
        assert s.exec(select(Alert).where(Alert.event_id == ev["event_id"])).first() is None
        assert s.exec(select(Event).where(Event.event_id == ev["event_id"])).first() is None


def test_meta_del_evento_llega_al_dashboard():
    c, h = _cliente()
    with c.websocket_connect("/ws/alerts") as ws:
        _ingerir(c, _evento("MNP-128-J", meta={"tipo_placa": "Automóvil particular",
                                               "entidad": "Estado de México", "pais": "México",
                                               "lecturas_ocr": [["MNP128J", 0.9]]}))
        mensaje = json.loads(ws.receive_text())
    assert mensaje["type"] == "event"
    assert mensaje["data"]["meta"]["entidad"] == "Estado de México"
    assert "lecturas_ocr" not in mensaje["data"]["meta"], "lo interno no viaja al navegador"


# --------------------------------------------------------------------------

def test_revision_excluye_negativo_del_ocr_y_conserva_evidencia():
    c, h = _cliente()
    ev = _evento('JHK-123-A', snapshot_path=_foto('negativa-' + uuid.uuid4().hex), observations=10)
    _ingerir(c, ev)
    ruta = f"/api/events/{ev['event_id']}/revision-placa"
    assert c.post(ruta, json={'resultado': 'no_es_placa'}).status_code == 401
    assert c.post(ruta, headers=h, json={'resultado': 'incorrecto'}).status_code == 422
    assert c.post(ruta, headers=h, json={'resultado': 'no_es_placa'}).status_code == 200
    from api.dataset import exportar
    buf = io.BytesIO()
    with Session(engine) as s:
        resumen = exportar(s, buf, incluir_automaticas=True)
    with zipfile.ZipFile(buf) as z:
        assert Path(ev['snapshot_path']).stem not in z.read('anotaciones.csv').decode()
        assert ev['event_id'] in z.read('negativas.jsonl').decode()
    assert resumen.negativas >= 1
    assert (RAIZ / ev['snapshot_path']).is_file()
    assert c.post(ruta, headers=h, json={'resultado': 'confirmada'}).status_code == 200
    buf = io.BytesIO()
    with Session(engine) as s:
        exportar(s, buf)
    with zipfile.ZipFile(buf) as z:
        assert Path(ev['snapshot_path']).stem in z.read('anotaciones.csv').decode()
        assert ev['event_id'] not in z.read('negativas.jsonl').decode()


def test_galeria_solo_fotos_y_pagina_sin_repetir():
    c, h = _cliente()
    camara = "cam-galeria-" + uuid.uuid4().hex[:6]
    ids = []
    for i in range(7):
        ev = _evento(f"GAL-{i:03d}", camera_id=camara, snapshot_path=_foto(f"galeria-{uuid.uuid4().hex}"))
        ids.append(ev["event_id"])
        _ingerir(c, ev)
    _ingerir(c, _evento("SIN-FOTO", camera_id=camara))
    ruta = f"/api/events?camera_id={camara}&con_foto=true&limite=4"
    pag1 = c.get(ruta, headers=h).json()
    pag2 = c.get(ruta + "&desde_n=4", headers=h).json()
    assert len(pag1) == 4 and len(pag2) == 3
    vistos = [e["event_id"] for e in pag1 + pag2]
    assert sorted(vistos) == sorted(ids), "las dos paginas cubren las 7 fotos sin repetir"
    assert all(e["snapshot_path"] for e in pag1 + pag2)
    assert len(c.get(f"/api/events?camera_id={camara}", headers=h).json()) == 8


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
