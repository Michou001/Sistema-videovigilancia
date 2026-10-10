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
from bd_prueba import borrar as _borrar_bd  # noqa: E402
from bd_prueba import url_temporal  # noqa: E402

os.environ["DATABASE_URL"] = url_temporal(_TMP, "api")
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
    from api.config import get_config
    esperado = (get_config().snapshot_dir / f"{ev['event_id']}.jpg").relative_to(RAIZ).as_posix()
    assert fila["snapshot_path"] == esperado, fila["snapshot_path"]
    destino = RAIZ / fila["snapshot_path"]
    _archivos_creados.append(destino)
    assert destino.read_bytes() == jpeg
    assert c.get("/media/" + destino.name).status_code == 200


def test_foto_de_rostro_sin_coincidencia_no_se_conserva():
    """De quien solo paso frente a la camara queda el evento, no su cara."""
    c, h = _cliente()
    from api.config import get_config
    cfg = get_config()
    # Se fija aqui: el .env de quien corre las pruebas puede tenerlo activado.
    previo = cfg.fotos_rostro_sin_coincidencia
    cfg.fotos_rostro_sin_coincidencia = False
    jpeg = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + bytes([2]) * 64 + bytes([0xFF, 0xD9])
    # Un rostro que no se parece a nadie de la lista (vector aleatorio).
    ajeno = np.random.default_rng(99).normal(size=512).astype(np.float32).tolist()
    # Mismo equipo: el worker ya dejo la foto en la carpeta de evidencia.
    local = _evento("face", "rostro", embedding=ajeno)
    archivo = cfg.snapshot_dir / f"{local['event_id']}.jpg"
    archivo.write_bytes(jpeg)
    local["snapshot_path"] = archivo.relative_to(RAIZ).as_posix()
    # Otro equipo: la manda adjunta.
    remoto = _evento("face", "rostro", embedding=ajeno,
                     snapshot_b64=base64.b64encode(jpeg).decode())
    _ingerir(c, local, remoto)
    filas = {e["event_id"]: e for e in c.get("/api/events", params={"tipo": "face"}, headers=h).json()}
    assert filas[local["event_id"]]["snapshot_path"] is None
    assert filas[remoto["event_id"]]["snapshot_path"] is None
    assert not archivo.exists(), "la foto que dejo el worker se borra"
    assert not (cfg.snapshot_dir / f"{remoto['event_id']}.jpg").exists()

    # Si la institucion lo declara y lo activa, se conserva.
    cfg.fotos_rostro_sin_coincidencia = True
    try:
        otro = _evento("face", "rostro", embedding=ajeno,
                       snapshot_b64=base64.b64encode(jpeg).decode())
        _ingerir(c, otro)
        destino = cfg.snapshot_dir / f"{otro['event_id']}.jpg"
        _archivos_creados.append(destino)
        assert destino.exists()
    finally:
        cfg.fotos_rostro_sin_coincidencia = previo


def test_baja_de_rostro_borra_vector_y_foto():
    """La baja conserva la trazabilidad pero no el rostro de la persona."""
    c, h = _cliente()
    from api.config import get_config
    carpeta = get_config().snapshot_dir.parent / "referencias"
    carpeta.mkdir(parents=True, exist_ok=True)
    foto = carpeta / "rostro-baja-prueba.jpg"
    foto.write_bytes(b"x")
    vector = np.ones(512, dtype=np.float32)
    with Session(engine) as s:
        r = BlacklistFace(label="Baja de prueba", vector=vector.tobytes(), reason="prueba",
                          legal_basis="prueba automatizada",
                          photo_path=foto.relative_to(RAIZ).as_posix())
        s.add(r)
        s.commit()
        rid = r.id
    assert c.delete(f"/api/blacklist/faces/{rid}", headers=h).status_code == 204
    with Session(engine) as s:
        r = s.get(BlacklistFace, rid)
        assert not r.active and r.photo_path is None
        assert not np.frombuffer(r.vector, dtype=np.float32).any(), "el vector se borra"
        assert r.label == "Baja de prueba" and r.legal_basis, "queda la trazabilidad"
    assert not foto.exists()


def test_evidencia_fuera_del_proyecto_no_rompe_la_ingesta():
    """SNAPSHOT_DIR en otro disco: antes cada evento con foto daba error 500."""
    c, h = _cliente()
    from api.config import get_config
    cfg = get_config()
    anterior = cfg.snapshot_dir
    fuera = Path(tempfile.mkdtemp())
    cfg.snapshot_dir = fuera
    try:
        jpeg = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + bytes([3]) * 64 + bytes([0xFF, 0xD9])
        ev = _evento(valor="DIS-777", snapshot_b64=base64.b64encode(jpeg).decode())
        _ingerir(c, ev)
        fila = c.get("/api/events", params={"q": "DIS777"}, headers=h).json()[0]
        assert Path(fila["snapshot_path"]).is_absolute()
        assert (fuera / f"{ev['event_id']}.jpg").read_bytes() == jpeg
        assert c.get("/media/" + Path(fila["snapshot_path"]).name).status_code == 200
    finally:
        cfg.snapshot_dir = anterior
        shutil.rmtree(fuera, ignore_errors=True)


def test_latido_marca_la_camara_en_linea():
    c, h = _cliente()
    r = c.post("/api/events/heartbeat", params={"camera_id": "cam-latido"}, headers=WORKER,
               json={"connected": True, "fps_procesados": 7.9, "reconnects": 2})
    assert r.status_code == 200, r.text
    cam = next(x for x in c.get("/api/stats", headers=h).json()["camaras"]
               if x["camera_id"] == "cam-latido")
    assert cam["online"] and cam["fps"] == 7.9 and cam["reconexiones"] == 2
    assert cam["connected"] is True and cam["enabled"] is True

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


def test_canalizar_y_ficha_de_evidencia():
    import hashlib
    import io
    import zipfile

    c, h = _cliente()
    c.post("/api/blacklist/plates", headers=h, json={"plate": "CNL-911", "reason": "robo"})
    jpeg = b"\xff\xd8\xff\xe0" + b"\x01" * 64 + b"\xff\xd9"
    _ingerir(c, _evento(valor="CNL-911", snapshot_b64=base64.b64encode(jpeg).decode()))
    alerta = next(a for a in c.get("/api/alerts", headers=h).json() if "CNL-911" in a["title"])
    assert alerta["canalizaciones"] == [] and "canalizaciones_json" not in alerta
    _archivos_creados.append(RAIZ / alerta["snapshot_path"])
    folio = f"ALR-{alerta['id']:06d}"

    r = c.post(f"/api/alerts/{alerta['id']}/canalizar", headers=h,
               json={"destino": "911_c5", "referencia": "F-2026-1234", "nota": "Se reportó vehículo"})
    assert r.status_code == 200, r.text
    canal = r.json()["canalizaciones"]
    assert len(canal) == 1 and canal[0]["destino"] == "911_c5"
    assert canal[0]["referencia"] == "F-2026-1234" and canal[0]["por"] == "admin"
    # Se puede canalizar a otra instancia aunque la alerta ya este cerrada.
    c.post(f"/api/alerts/{alerta['id']}/resolver", headers=h, json={"accion": "acknowledge"})
    r = c.post(f"/api/alerts/{alerta['id']}/canalizar", headers=h,
               json={"destino": "proteccion_universitaria"})
    assert r.status_code == 200 and len(r.json()["canalizaciones"]) == 2
    assert c.post(f"/api/alerts/{alerta['id']}/canalizar", headers=h,
                  json={"destino": "vecinos"}).status_code == 422
    assert c.post(f"/api/alerts/{alerta['id']}/canalizar",
                  json={"destino": "911_c5"}).status_code == 401

    r = c.get(f"/api/alerts/{alerta['id']}/ficha.zip", headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert f"{folio}-evidencia.zip" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert set(z.namelist()) == {"ficha.html", "foto.jpg", "SHA256SUMS.txt"}
    assert z.read("foto.jpg") == jpeg
    huella = hashlib.sha256(jpeg).hexdigest()
    sumas = z.read("SHA256SUMS.txt").decode().splitlines()
    assert f"{huella}  foto.jpg" in sumas
    assert f"{hashlib.sha256(z.read('ficha.html')).hexdigest()}  ficha.html" in sumas, "la ficha va al manifiesto"
    ficha = z.read("ficha.html").decode()
    assert folio in ficha and "CNL-911" in ficha and "F-2026-1234" in ficha
    assert "911 / C5 Edomex" in ficha and "Seguridad institucional" in ficha
    assert "Alerta canalizada a otra instancia" in ficha and huella in ficha
    # La leyenda dice lo que paso, no asume una verificacion.
    assert "La atendió admin" in ficha and "verificó" not in ficha

    bitacora = c.get("/api/audit", params={"accion": "alertas.ficha_evidencia"}, headers=h)
    assert bitacora.status_code == 200, bitacora.text
    assert any(huella in (f.get("detalle") or "") for f in bitacora.json()), "la huella va a la bitacora"
    assert c.get("/api/alerts/999999/ficha.zip", headers=h).status_code == 404


def test_resultado_de_revision_y_metricas():
    c, h = _cliente()
    c.post("/api/blacklist/plates", headers=h, json={"plate": "RES-001", "reason": "prueba"})
    for _ in range(4):
        _ingerir(c, _evento(valor="RES-001"))
    ids = [a["id"] for a in c.get("/api/alerts", headers=h).json() if "RES-001" in a["title"]][:4]
    assert len(ids) == 4, ids
    base = c.get("/api/alerts/metricas", headers=h).json()

    def resolver(i, **cuerpo):
        return c.post(f"/api/alerts/{i}/resolver", headers=h, json=cuerpo)

    r = resolver(ids[0], accion="acknowledge", resultado="confirmado", nota="Se verificó en video")
    assert r.status_code == 200 and r.json()["resultado"] == "confirmado"
    r = resolver(ids[1], accion="dismiss", motivo="falso positivo")
    assert r.json()["resultado"] == "falso_aviso", "descartar = falso aviso por defecto"
    assert resolver(ids[2], accion="dismiss", resultado="confirmado").status_code == 422
    assert resolver(ids[2], accion="acknowledge", resultado="falso_aviso").status_code == 422
    assert resolver(ids[2], accion="acknowledge", resultado="ensayo").status_code == 200

    m = c.get("/api/alerts/metricas", headers=h).json()
    assert m["por_resultado"]["confirmado"] == base["por_resultado"]["confirmado"] + 1
    assert m["por_resultado"]["falso_aviso"] == base["por_resultado"]["falso_aviso"] + 1
    assert m["ensayos_excluidos"] == base["ensayos_excluidos"] + 1, "los ensayos no se mezclan"
    assert m["sin_revisar"] >= 1 and m["denominador_precision"] >= 2
    assert 0 <= m["precision"] <= 1 and m["segundos_hasta_revision"]["n"] >= 2
    con = c.get("/api/alerts/metricas", params={"incluir_ensayos": True}, headers=h).json()
    assert con["ensayos_excluidos"] == 0

    ficha = c.get(f"/api/alerts/{ids[0]}/ficha.zip", headers=h)
    import io
    import zipfile
    html = zipfile.ZipFile(io.BytesIO(ficha.content)).read("ficha.html").decode()
    assert "Resultado de la revisión" in html and "registró el resultado como confirmado" in html


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


def test_rol_de_camara_se_conserva_al_editar_nombre():
    c, h = _cliente()
    _ingerir(c, _evento(valor="ROL-100"))
    ruta = '/api/cameras/cam-prueba'
    assert c.put(ruta, headers=h, json={'name': 'Piloto vehicular', 'funcion': 'lpr'}).status_code == 200
    assert c.put(ruta, headers=h, json={'name': 'Nuevo nombre'}).status_code == 200
    cam = next(x for x in c.get('/api/stats', headers=h).json()['camaras'] if x['camera_id'] == 'cam-prueba')
    assert cam['funcion'] == 'lpr'
    assert c.put(ruta, headers=h, json={'name': 'Nuevo nombre', 'funcion': 'inventado'}).status_code == 422
    assert c.put(ruta, json={'name': 'Cambio sin permiso', 'funcion': 'peatonal'}).status_code == 401
    assert c.put(ruta, headers=h, json={'name': 'Nuevo nombre', 'funcion': None}).status_code == 200
    cam = next(x for x in c.get('/api/stats', headers=h).json()['camaras'] if x['camera_id'] == 'cam-prueba')
    assert cam['funcion'] is None


def test_estadisticas_del_dia_y_ultima_deteccion():
    c, h = _cliente()
    _ingerir(c, _evento("plate", "XYZ-987-A"))
    s = c.get("/api/stats", headers=h).json()
    assert s["eventos_hoy"] >= 1 and s["eventos_hoy_por_tipo"].get("plate", 0) >= 1
    assert s["ultimo_evento"]["value"] == "XYZ-987-A"
    assert s["ultimo_evento"]["type"] == "plate"
    assert set(s["lista_negra"]) == {"placas", "rostros"}
    # Los avisos de camara caida no cuentan como "detecciones de hoy".
    assert "camera" not in s["ultimo_evento"]["type"]


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
        _borrar_bd(os.environ["DATABASE_URL"])
        shutil.rmtree(_TMP, ignore_errors=True)

    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
