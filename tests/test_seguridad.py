"""Pruebas de seguridad y administracion de la plataforma.

    python tests/test_seguridad.py

Evidencia protegida, cabeceras de seguridad, roles, usuarios, bitacora de
auditoria, ingesta tolerante a eventos mal formados y paso de una base de
datos anterior a las migraciones. Usan un SQLite temporal.
"""

from __future__ import annotations

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

os.environ["DATABASE_URL"] = url_temporal(_TMP, "seguridad")
os.environ["API_TOKEN"] = "token-de-prueba-del-worker"

import warnings  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from api.config import get_config  # noqa: E402
from api.database import engine, init_db  # noqa: E402
from api.main import app  # noqa: E402
from api.models import AuditLog, Operator  # noqa: E402
from api.security import COOKIE_SESION, hash_password, limite_login  # noqa: E402

WORKER = {"X-API-Token": "token-de-prueba-del-worker"}
_archivos: list[Path] = []


def _usuario(nombre: str, rol: str, clave: str = "clave-de-prueba-1") -> None:
    with Session(engine) as s:
        if s.exec(select(Operator).where(Operator.username == nombre)).first() is None:
            s.add(Operator(username=nombre, display_name=nombre.title(),
                           password_hash=hash_password(clave), role=rol))
            s.commit()


def _entrar(nombre: str, clave: str = "clave-de-prueba-1") -> tuple[TestClient, dict]:
    init_db()
    limite_login.exito("testclient")
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": nombre, "password": clave})
    assert r.status_code == 200, r.text
    return c, {"Authorization": f"Bearer {r.json()['token']}"}


def _admin() -> tuple[TestClient, dict]:
    init_db()
    _usuario("admin", "admin")
    return _entrar("admin")


def _evento(tipo="plate", valor="ABC-123", **extra) -> dict:
    return {"event_id": str(uuid.uuid4()), "camera_id": "cam-seg", "type": tipo,
            "value": valor, "confidence": 0.9, **extra}


def _alerta(c: TestClient, h: dict) -> int:
    c.post("/api/blacklist/plates", json={"plate": "SEG-123-A", "reason": "prueba"}, headers=h)
    r = c.post("/api/events", headers=WORKER,
               json={"camera_id": "cam-seg", "events": [_evento(valor="SEG-123-A")]})
    assert r.status_code == 200, r.text
    alertas = c.get("/api/alerts", params={"estado": "new"}, headers=h).json()
    return next(a["id"] for a in alertas if "SEG-123-A" in a["title"])


# --------------------------------------------------------------------------
# Evidencia
# --------------------------------------------------------------------------

def test_evidencia_exige_sesion():
    c, h = _admin()
    archivo = get_config().snapshot_dir / f"{uuid.uuid4()}.jpg"
    archivo.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9")
    _archivos.append(archivo)

    anonimo = TestClient(app)
    assert anonimo.get(f"/media/{archivo.name}").status_code == 401, \
        "sin sesion la foto no debe servirse"
    # El login dejo la cookie HttpOnly: un <img> la manda sola.
    assert c.cookies.get(COOKIE_SESION)
    r = c.get(f"/media/{archivo.name}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert "private" in r.headers["cache-control"]
    # Tambien con la cabecera, sin cookie.
    assert TestClient(app).get(f"/media/{archivo.name}", headers=h).status_code == 200


def test_evidencia_rechaza_rutas_raras():
    c, _ = _admin()
    for nombre in ["..%2F..%2Fapi%2Fconfig.py", "../.jwt_secret", ".jwt_secret",
                   "x.py", "no-existe.jpg", "%2e%2e%2fvigilancia.db"]:
        r = c.get(f"/media/{nombre}")
        assert r.status_code == 404, f"{nombre} -> {r.status_code}"


def test_cabeceras_de_seguridad():
    c, _ = _admin()
    r = c.get("/api/health")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "strict-transport-security" not in r.headers, "HSTS solo por HTTPS"
    # La pantalla de acceso lee esto sin sesion: solo sitio y version.
    assert set(r.json()) == {"status", "dashboards", "multiproceso", "version", "sitio"}


def test_websocket_con_cookie_y_sin_sesion():
    c, _ = _admin()
    with c.websocket_connect("/ws/alerts") as ws:
        ws.send_text("hola")   # la conexion se acepto
    anonimo = TestClient(app)
    try:
        with anonimo.websocket_connect("/ws/alerts") as ws:
            ws.receive_text()
        raise AssertionError("sin sesion el WebSocket no debe aceptarse")
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001 - WebSocketDisconnect: rechazado
        pass


def test_video_en_vivo_exige_sesion():
    c, _ = _admin()
    assert TestClient(app).get("/api/preview/cam-seg/live.mjpg").status_code == 401
    assert c.get("/api/preview/cam%20x/live.mjpg").status_code == 422


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------

def test_roles_viewer_operator():
    c, h = _admin()
    alerta = _alerta(c, h)
    _usuario("vigilante", "viewer")
    _usuario("turno", "operator")
    cv, hv = _entrar("vigilante")
    co, ho = _entrar("turno")

    # El viewer mira pero no opera.
    assert cv.get("/api/alerts", headers=hv).status_code == 200
    r = cv.post(f"/api/alerts/{alerta}/resolver", json={"accion": "acknowledge"}, headers=hv)
    assert r.status_code == 403, r.text
    assert cv.get("/api/events/export.csv", headers=hv).status_code == 403
    assert cv.post("/api/blacklist/plates", json={"plate": "XYZ-123-A", "reason": "x"},
                   headers=hv).status_code == 403

    # El operador atiende, pero no administra.
    r = co.post(f"/api/alerts/{alerta}/resolver",
                json={"accion": "acknowledge", "nota": "Se avisó a patrulla"}, headers=ho)
    assert r.status_code == 200, r.text
    assert r.json()["acknowledged_by"] == "turno"
    assert co.get("/api/events/export.csv", headers=ho).status_code == 200
    assert co.get("/api/users", headers=ho).status_code == 403
    assert co.get("/api/audit", headers=ho).status_code == 403

    # Cerrar dos veces la misma alerta: gana el primero.
    r = c.post(f"/api/alerts/{alerta}/resolver", json={"accion": "dismiss"}, headers=h)
    assert r.status_code == 409 and "turno" in r.json()["detail"]


# --------------------------------------------------------------------------
# Usuarios y sesiones
# --------------------------------------------------------------------------

def test_alta_y_edicion_de_usuarios():
    c, h = _admin()
    r = c.post("/api/users", headers=h, json={"username": "Maria.Lopez", "display_name": "María López",
                                              "role": "operator", "password": "corta"})
    assert r.status_code == 422 and "10 caracteres" in r.text
    r = c.post("/api/users", headers=h, json={"username": "maria.lopez", "display_name": "María López",
                                              "role": "operator", "password": "una frase larga"})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    assert "password_hash" not in r.json()
    assert c.post("/api/users", headers=h, json={"username": "maria.lopez", "display_name": "Otra",
                                                 "role": "viewer", "password": "otra frase larga"}
                  ).status_code == 409

    cm, hm = _entrar("maria.lopez", "una frase larga")
    assert cm.get("/api/auth/me", headers=hm).status_code == 200

    # Bajarle el rol cierra su sesion abierta en ese momento.
    r = c.patch(f"/api/users/{uid}", headers=h, json={"role": "viewer"})
    assert r.status_code == 200 and r.json()["role"] == "viewer"
    assert cm.get("/api/auth/me", headers=hm).status_code == 401
    cm, hm = _entrar("maria.lopez", "una frase larga")
    assert cm.get("/api/auth/me", headers=hm).json()["role"] == "viewer"

    # Desactivarla tambien.
    c.patch(f"/api/users/{uid}", headers=h, json={"active": False})
    assert cm.get("/api/auth/me", headers=hm).status_code == 401
    limite_login.exito("testclient")
    assert TestClient(app).post("/api/auth/login", json={"username": "maria.lopez",
                                                         "password": "una frase larga"}).status_code == 401


def test_siempre_queda_un_admin():
    c, h = _admin()
    with Session(engine) as s:
        admins = s.exec(select(Operator).where(Operator.role == "admin", Operator.active)).all()
        yo = next(a for a in admins if a.username == "admin")
        otros = [a for a in admins if a.username != "admin"]
        for a in otros:
            a.active = False
        s.commit()
        yo_id = yo.id
    r = c.patch(f"/api/users/{yo_id}", headers=h, json={"role": "operator"})
    assert r.status_code == 409, r.text
    r = c.patch(f"/api/users/{yo_id}", headers=h, json={"active": False})
    assert r.status_code == 409


def test_cambio_de_password_cierra_otras_sesiones():
    _admin()
    _usuario("cambia", "operator", "clave-original-1")
    c1, h1 = _entrar("cambia", "clave-original-1")
    c2, h2 = _entrar("cambia", "clave-original-1")
    assert c1.post("/api/auth/password", headers=h1,
                   json={"actual": "equivocada", "nueva": "clave-nueva-larga"}).status_code == 400
    r = c1.post("/api/auth/password", headers=h1,
                json={"actual": "clave-original-1", "nueva": "clave-nueva-larga"})
    assert r.status_code == 200, r.text
    nuevo = {"Authorization": f"Bearer {r.json()['token']}"}
    assert c1.get("/api/auth/me", headers=nuevo).status_code == 200
    assert c2.get("/api/auth/me", headers=h2).status_code == 401, "la otra sesion debe cerrarse"
    assert c1.get("/api/auth/me", headers=h1).status_code == 401


def test_logout_borra_la_cookie():
    c, h = _admin()
    assert c.cookies.get(COOKIE_SESION)
    r = c.post("/api/auth/logout", headers=h)
    assert r.status_code == 204
    assert not c.cookies.get(COOKIE_SESION)


# --------------------------------------------------------------------------
# Bitacora
# --------------------------------------------------------------------------

def test_bitacora_registra_acciones():
    c, h = _admin()
    limite_login.exito("testclient")
    TestClient(app).post("/api/auth/login", json={"username": "admin", "password": "mala"})
    c.post("/api/blacklist/plates", json={"plate": "AUD-123-B", "reason": "robo"}, headers=h)
    c.get("/api/events/export.csv", headers=h)

    entradas = c.get("/api/audit", headers=h).json()
    acciones = {e["accion"] for e in entradas}
    for esperada in ("sesion.login", "sesion.login_fallido", "lista_negra.alta_placa", "reportes.csv"):
        assert esperada in acciones, f"falta {esperada} en {acciones}"
    alta = next(e for e in entradas if e["accion"] == "lista_negra.alta_placa"
                and e["objetivo"] == "AUD-123-B")
    assert alta["usuario"] == "admin" and "robo" in alta["detalle"] and alta["ip"]
    # La contrasena nunca va a la bitacora.
    assert all("mala" not in (e["detalle"] or "") for e in entradas)

    filtradas = c.get("/api/audit", params={"accion": "lista_negra"}, headers=h).json()
    assert filtradas and all(e["accion"].startswith("lista_negra") for e in filtradas)
    csv = c.get("/api/audit/export.csv", headers=h)
    assert csv.status_code == 200 and "lista_negra.alta_placa" in csv.text


def test_csv_neutraliza_formulas():
    c, h = _admin()
    r = c.post("/api/events", headers=WORKER, json={
        "camera_id": "cam-seg",
        "events": [_evento(valor="=HYPERLINK(1)")]})
    assert r.status_code == 200
    reporte = c.get("/api/events/export.csv", params={"q": "HYPERLINK"}, headers=h).text
    assert "'=HYPERLINK(1)" in reporte


# --------------------------------------------------------------------------
# Ingesta
# --------------------------------------------------------------------------

def test_lote_con_un_evento_malo_no_pierde_los_demas():
    c, _ = _admin()
    bueno = _evento(valor="BUE-123-A")
    malo = _evento(valor="MAL-123-A", confidence=7.5)      # confianza fuera de rango
    r = c.post("/api/events", headers=WORKER, json={"camera_id": "cam-seg", "events": [bueno, malo]})
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["accepted"] == 1
    assert len(cuerpo["rejected"]) == 1 and cuerpo["rejected"][0]["event_id"] == malo["event_id"]
    assert "confidence" in cuerpo["rejected"][0]["motivo"]


def test_identificador_de_camara_se_valida():
    c, _ = _admin()
    r = c.post("/api/events", headers=WORKER,
               json={"camera_id": "cam');alert(1);//", "events": []})
    assert r.status_code == 422
    r = c.post("/api/events", headers=WORKER, json={
        "camera_id": "cam-seg", "events": [{**_evento(), "camera_id": "<script>"}]})
    assert r.json()["accepted"] == 0 and r.json()["rejected"]
    r = c.post("/api/events/heartbeat", params={"camera_id": "a b"}, headers=WORKER, json={})
    assert r.status_code == 422


def test_fechas_sin_zona_del_worker_se_toman_como_utc():
    c, h = _admin()
    ev = _evento(valor="UTC-123-A", ts="2026-09-30T12:00:00")
    assert c.post("/api/events", headers=WORKER,
                  json={"camera_id": "cam-seg", "events": [ev]}).json()["accepted"] == 1
    fila = c.get("/api/events", params={"q": "UTC123A"}, headers=h).json()[0]
    assert fila["ts"].startswith("2026-09-30T12:00:00") and fila["ts"].endswith(("Z", "+00:00"))
    # Busqueda con limites en hora local de Mexico (UTC-6).
    desde = datetime(2026, 9, 30, 5, 0, tzinfo=timezone(timedelta(hours=-6)))
    encontrados = c.get("/api/events", params={"q": "UTC123A", "desde": desde.isoformat()},
                        headers=h).json()
    assert len(encontrados) == 1


# --------------------------------------------------------------------------
# Migraciones
# --------------------------------------------------------------------------

def test_base_de_datos_anterior_pasa_a_migraciones():
    """Una base creada por la version anterior (sin Alembic, sin la columna
    token_version ni la bitacora) se actualiza sin perder datos."""
    from alembic import command

    from api.database import _config_alembic

    ruta = _TMP / "legado.db"
    from sqlalchemy import create_engine

    motor = create_engine(f"sqlite:///{ruta.as_posix()}")
    with motor.begin() as con:
        command.upgrade(_config_alembic(con), "0001")
        con.execute(text("DROP TABLE alembic_version"))
        con.execute(text("DROP INDEX ix_events_type_ts"))       # como una base muy vieja
        con.execute(text(
            "INSERT INTO operators (username, display_name, password_hash, role, active, created_at) "
            "VALUES ('viejo', 'Viejo', 'x', 'admin', 1, '2025-01-01 00:00:00')"))

    import api.database as db

    original = db.engine
    db.engine = motor
    try:
        db.init_db()
    finally:
        db.engine = original
    with motor.connect() as con:
        tablas = set(inspect(con).get_table_names())
        assert {"audit_log", "alembic_version"} <= tablas
        fila = con.execute(text("SELECT username, token_version FROM operators")).one()
        assert tuple(fila) == ("viejo", 0)
        indices = {i["name"] for i in inspect(con).get_indexes("events")}
        assert "ix_events_type_ts" in indices
        version = con.execute(text("SELECT version_num FROM alembic_version")).scalar()
    from alembic.script import ScriptDirectory

    with motor.connect() as con:
        cabeza = ScriptDirectory.from_config(_config_alembic(con)).get_current_head()
    assert version == cabeza
    motor.dispose()


def test_migraciones_coinciden_con_los_modelos():
    """Si alguien cambia api/models.py sin crear la migracion, esto lo detecta."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlmodel import SQLModel

    init_db()
    with engine.connect() as con:
        diferencias = compare_metadata(MigrationContext.configure(con), SQLModel.metadata)
    assert not diferencias, f"el esquema difiere de los modelos: {diferencias}"


def test_bitacora_no_se_edita_desde_la_api():
    c, h = _admin()
    rutas = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
    assert not any(p.startswith("/api/audit") and m in {"PUT", "PATCH", "DELETE", "POST"}
                   for p, m in rutas)
    with Session(engine) as s:
        assert s.exec(select(AuditLog)).first() is not None


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
        for f in _archivos:
            f.unlink(missing_ok=True)
        engine.dispose()
        _borrar_bd(os.environ["DATABASE_URL"])
        shutil.rmtree(_TMP, ignore_errors=True)

    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
