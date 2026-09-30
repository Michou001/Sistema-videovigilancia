"""Pruebas de la API contra una base de datos temporal.

    python tests/test_api.py

No tocan data/vigilancia.db: usan un SQLite en una carpeta temporal. Cubren el
camino completo que recorre una deteccion: ingesta del worker, cruce contra la
lista negra, alerta, busqueda en el registro y salud de la camara.
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
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'prueba.db').as_posix()}"
os.environ["API_TOKEN"] = "token-de-prueba-del-worker"

import warnings  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session  # noqa: E402

from api.database import engine, init_db  # noqa: E402
from api.main import app  # noqa: E402
from api.matching import lista_negra  # noqa: E402
from api.models import BlacklistFace, Operator  # noqa: E402
from api.security import hash_password, limite_login  # noqa: E402

WORKER = {"X-API-Token": "token-de-prueba-del-worker"}
_archivos_creados: list[Path] = []


def _cliente() -> tuple[TestClient, dict]:
    init_db()
    with Session(engine) as s:
        if s.get(Operator, 1) is None:
            s.add(Operator(username="admin", display_name="Admin",
                           password_hash=hash_password("clave-prueba"), role="admin"))
            s.commit()
    c = TestClient(app)
    token = c.post("/api/auth/login", json={"username": "admin", "password": "clave-prueba"}).json()["token"]
    return c, {"Authorization": f"Bearer {token}"}


def _evento(tipo="plate", valor="ABC-123", **extra) -> dict:
    return {"event_id": str(uuid.uuid4()), "camera_id": "cam-prueba", "type": tipo,
            "value": valor, "confidence": 0.9, **extra}


def _ingerir(c: TestClient, *eventos) -> dict:
    r = c.post("/api/events", json={"camera_id": "cam-prueba", "events": list(eventos)}, headers=WORKER)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------

def test_login_y_limite_de_intentos():
    c, _ = _cliente()
    limite_login.exito("testclient")
    for _ in range(5):
        assert c.post("/api/auth/login", json={"username": "admin", "password": "mala"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "admin", "password": "clave-prueba"})
    assert r.status_code == 429, "tras 5 fallos debe frenar, aun con la contrasena correcta"
    limite_login.exito("testclient")
    assert c.post("/api/auth/login", json={"username": "admin", "password": "clave-prueba"}).status_code == 200


def test_ingesta_sin_token_se_rechaza():
    c, _ = _cliente()
    r = c.post("/api/events", json={"camera_id": "x", "events": []})
    assert r.status_code == 401


def test_placa_vencida_no_alerta():
    """REGRESION: expires_at se guardaba pero nadie lo consultaba."""
    c, h = _cliente()
    ayer = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    r = c.post("/api/blacklist/plates", headers=h,
               json={"plate": "VEN-111", "reason": "prueba vencida", "expires_at": ayer})
    assert r.status_code == 201, r.text
    resp = _ingerir(c, _evento(valor="VEN-111"))
    assert resp["matches"][0]["severity"] == "info"


def test_placa_vigente_alerta_y_cache_se_invalida():
    c, h = _cliente()
    # Primero se lee sin la placa en la lista (queda en cache)...
    assert _ingerir(c, _evento(valor="XYZ-789"))["matches"][0]["severity"] == "info"
    # ...y el alta debe verse de inmediato, no a los 30 s de la cache.
    r = c.post("/api/blacklist/plates", headers=h, json={"plate": "XYZ-789", "reason": "robo"})
    assert r.status_code == 201, r.text
    m = _ingerir(c, _evento(valor="XYZ-789"))["matches"][0]
    assert m["severity"] == "critical" and m["match_kind"] == "exact"
    # Lectura con un error de OCR: coincidencia difusa, degradada a aviso.
    m = _ingerir(c, _evento(valor="XYZ-788"))["matches"][0]
    assert m["severity"] == "warning" and m["match_kind"] == "fuzzy"

    alertas = c.get("/api/alerts", headers=h).json()
    assert any(a["title"].startswith("Placa XYZ-789") for a in alertas)
    assert any(a["title"].startswith("Posible placa XYZ-789") for a in alertas)


def test_idempotencia():
    c, _ = _cliente()
    ev = _evento(valor="IDM-100")
    assert _ingerir(c, ev)["accepted"] == 1
    r = _ingerir(c, ev, ev)
    assert r["accepted"] == 0 and r["duplicates"] == 2


def test_rostro_vectorizado():
    c, _ = _cliente()
    rng = np.random.default_rng(7)
    ref = rng.normal(size=512).astype(np.float32)
    ref /= np.linalg.norm(ref)
    with Session(engine) as s:
        s.add(BlacklistFace(label="Persona de prueba", vector=ref.tobytes(), reason="prueba",
                            legal_basis="prueba automatizada"))
        otro = rng.normal(size=512).astype(np.float32)
        s.add(BlacklistFace(label="Otra persona", vector=(otro / np.linalg.norm(otro)).tobytes(),
                            reason="prueba", legal_basis="prueba automatizada"))
        s.commit()
    lista_negra.invalidar()

    parecido = ref + rng.normal(scale=0.02, size=512).astype(np.float32)
    m = _ingerir(c, _evento("face", "rostro", embedding=parecido.tolist()))["matches"][0]
    assert m["severity"] == "critical" and m["matched_value"] == "Persona de prueba", m

    ajeno = rng.normal(size=512).astype(np.float32)
    m = _ingerir(c, _evento("face", "rostro", embedding=ajeno.tolist()))["matches"][0]
    assert m["severity"] == "info"


def test_busqueda_en_registro():
    c, h = _cliente()
    _ingerir(c, _evento(valor="BUS-321"), _evento("anomaly", "movimiento_subito",
                                                  meta={"velocidad_alturas_por_s": 3.1, "umbral": 2.5}))
    # Sin guiones, en minusculas: como lo escribiria el operador.
    encontrados = c.get("/api/events", params={"q": "bus321"}, headers=h).json()
    assert [e["value"] for e in encontrados] == ["BUS-321"]
    solo_mov = c.get("/api/events", params={"tipo": "anomaly"}, headers=h).json()
    assert solo_mov and all(e["type"] == "anomaly" for e in solo_mov)
    futuro = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert c.get("/api/events", params={"desde": futuro}, headers=h).json() == []


def test_texto_de_alerta_de_movimiento():
    c, h = _cliente()
    _ingerir(c, _evento("anomaly", "movimiento_subito",
                        meta={"velocidad_alturas_por_s": 5.0, "umbral": 2.5}))
    alerta = next(a for a in c.get("/api/alerts", headers=h).json() if a["type"] == "anomaly")
    assert "5.0 alturas de cuerpo/s" in alerta["detail"]
    assert "2.0 veces el umbral" in alerta["detail"]


def test_foto_adjunta_se_guarda_en_la_api():
    c, h = _cliente()
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
    ev = _evento(valor="FOT-555", snapshot_b64=base64.b64encode(jpeg).decode(),
                 snapshot_path="C:/otra/maquina/data/snapshots/lo-que-sea.jpg")
    _ingerir(c, ev)
    fila = c.get("/api/events", params={"q": "FOT555"}, headers=h).json()[0]
    assert fila["snapshot_path"] == f"data/snapshots/{ev['event_id']}.jpg"
    destino = RAIZ / fila["snapshot_path"]
    _archivos_creados.append(destino)
    assert destino.read_bytes() == jpeg
    assert c.get("/media/" + destino.name).status_code == 200


def test_latido_marca_la_camara_en_linea():
    c, h = _cliente()
    r = c.post("/api/events/heartbeat", params={"camera_id": "cam-latido"}, headers=WORKER,
               json={"connected": True, "fps_procesados": 7.9, "reconnects": 2})
    assert r.status_code == 200, r.text
    cam = next(x for x in c.get("/api/stats", headers=h).json()["camaras"]
               if x["camera_id"] == "cam-latido")
    assert cam["online"] and cam["fps"] == 7.9 and cam["reconexiones"] == 2

    c.post("/api/events/heartbeat", params={"camera_id": "cam-latido"}, headers=WORKER,
           json={"connected": False})
    cam = next(x for x in c.get("/api/stats", headers=h).json()["camaras"]
               if x["camera_id"] == "cam-latido")
    assert not cam["online"], "worker vivo pero sin imagen de la camara = caida"


def test_nota_de_atencion_y_color_en_alerta():
    c, h = _cliente()
    c.post("/api/blacklist/plates", headers=h, json={"plate": "NTA-404", "reason": "robo"})
    _ingerir(c, _evento(valor="NTA-404", meta={"color_vehiculo": "rojo"}))
    alerta = next(a for a in c.get("/api/alerts", headers=h).json() if "NTA-404" in a["title"])
    assert "Vehículo rojo" in alerta["detail"]
    r = c.post(f"/api/alerts/{alerta['id']}/resolver", headers=h,
               json={"accion": "acknowledge", "nota": "Se avisó a patrulla"})
    assert r.status_code == 200, r.text
    assert r.json()["notes"] == "Se avisó a patrulla" and r.json()["status"] == "acknowledged"


def test_reporte_csv():
    c, h = _cliente()
    _ingerir(c, _evento(valor="CSV-777", meta={"color_vehiculo": "gris"}))
    r = c.get("/api/events/export.csv", params={"q": "csv777"}, headers=h)
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    lineas = r.text.lstrip("\ufeff").strip().splitlines()
    assert lineas[0].startswith("fecha_hora_local,camara,tipo,valor,color_vehiculo")
    assert len(lineas) == 2 and "CSV-777" in lineas[1] and "gris" in lineas[1]
    assert c.get("/api/events/export.csv").status_code == 401


def test_nombre_y_ubicacion_de_camara():
    c, h = _cliente()
    _ingerir(c, _evento(valor="UBI-100"))
    r = c.put("/api/cameras/cam-prueba", headers=h,
              json={"name": "Acceso norte", "location": "Av. Juárez esq. Hidalgo"})
    assert r.status_code == 200, r.text
    cam = next(x for x in c.get("/api/stats", headers=h).json()["camaras"]
               if x["camera_id"] == "cam-prueba")
    assert cam["name"] == "Acceso norte" and cam["location"] == "Av. Juárez esq. Hidalgo"


def test_alerta_de_persona_caida():
    c, h = _cliente()
    m = _ingerir(c, _evento("anomaly", "persona_caida"))["matches"][0]
    assert m["severity"] == "warning"
    alerta = next(a for a in c.get("/api/alerts", headers=h).json() if a["type"] == "anomaly"
                  and a["title"] == "Posible persona caída")
    assert "tendida" in alerta["detail"]


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
                print(f"  [ERROR] {nombre}: {type(e).__name__}: {e}")
    finally:
        for f in _archivos_creados:
            f.unlink(missing_ok=True)
        engine.dispose()
        shutil.rmtree(_TMP, ignore_errors=True)

    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
