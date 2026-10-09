"""Pruebas de la verificacion en dos pasos (TOTP).

    python tests/test_doble_factor.py

El algoritmo se comprueba contra los vectores del RFC 6238; el flujo completo
(alta con QR, login en dos pasos, codigos de respaldo, politica EXIGIR_2FA y
restablecimiento por el administrador) contra una base temporal.
"""

from __future__ import annotations

import base64
import os
import shutil
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

_TMP = Path(tempfile.mkdtemp())
from bd_prueba import borrar as _borrar_bd  # noqa: E402
from bd_prueba import url_temporal  # noqa: E402

os.environ["DATABASE_URL"] = url_temporal(_TMP, "doble_factor")
os.environ["API_TOKEN"] = "token-de-prueba-del-worker"
# Llave fija de prueba: no escribe data/.totp_key en el proyecto.
os.environ["TOTP_KEY"] = base64.urlsafe_b64encode(b"k" * 32).decode()

import warnings  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from api import doble_factor  # noqa: E402
from api.config import get_config  # noqa: E402
from api.database import engine, init_db  # noqa: E402
from api.main import app  # noqa: E402
from api.models import AuditLog, Operator  # noqa: E402
from api.security import hash_password, limite_login  # noqa: E402

CLAVE = "clave-de-prueba-larga"


def _preparar() -> TestClient:
    init_db()
    limite_login.exito("testclient")
    with Session(engine) as s:
        for nombre, rol in (("admin", "admin"), ("ana", "operator")):
            if s.exec(select(Operator).where(Operator.username == nombre)).first() is None:
                s.add(Operator(username=nombre, display_name=nombre.title(),
                               password_hash=hash_password(CLAVE), role=rol))
        s.commit()
    return TestClient(app)


def _login(c: TestClient, usuario: str) -> dict:
    r = c.post("/api/auth/login", json={"username": usuario, "password": CLAVE})
    assert r.status_code == 200, r.text
    return r.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _activar(c: TestClient, usuario: str) -> tuple[str, list[str], str]:
    """Da de alta la app para `usuario`. Devuelve (secreto, respaldos, token)."""
    token = _login(c, usuario)["token"]
    r = c.post("/api/auth/2fa/iniciar", json={"password": CLAVE}, headers=_auth(token))
    assert r.status_code == 200, r.text
    secreto = r.json()["secreto"].replace(" ", "")
    codigo = doble_factor.codigo(secreto, doble_factor.paso_actual())
    r = c.post("/api/auth/2fa/activar", json={"codigo": codigo}, headers=_auth(token))
    assert r.status_code == 200, r.text
    return secreto, r.json()["codigos_respaldo"], r.json()["sesion"]["token"]


def _quitar_2fa(usuario: str) -> None:
    with Session(engine) as s:
        op = s.exec(select(Operator).where(Operator.username == usuario)).one()
        op.totp_activo, op.totp_secreto = False, None
        op.totp_ultimo_paso, op.totp_respaldo_json = None, None
        s.add(op)
        s.commit()


# --------------------------------------------------------------------------
# Algoritmo
# --------------------------------------------------------------------------

def test_vectores_del_rfc_6238():
    # Apendice B del RFC 6238, SHA-1, 8 digitos, secreto ASCII "1234567890" x2.
    secreto = base64.b32encode(b"12345678901234567890").decode()
    for t, esperado in ((59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
                        (1234567890, "89005924"), (2000000000, "69279037"),
                        (20000000000, "65353130")):
        assert doble_factor.codigo(secreto, t // 30, digitos=8) == esperado, t


def test_tolerancia_y_codigo_de_un_solo_uso():
    s = doble_factor.generar_secreto()
    ahora = 1_800_000_000.0
    paso = int(ahora // 30)
    assert doble_factor.verificar(s, doble_factor.codigo(s, paso), ahora=ahora) == paso
    # Un paso de desfase hacia cada lado se acepta; dos no.
    assert doble_factor.verificar(s, doble_factor.codigo(s, paso - 1), ahora=ahora) == paso - 1
    assert doble_factor.verificar(s, doble_factor.codigo(s, paso + 1), ahora=ahora) == paso + 1
    assert doble_factor.verificar(s, doble_factor.codigo(s, paso - 2), ahora=ahora) is None
    # Ya usado: el mismo codigo (o uno anterior) no vuelve a servir.
    assert doble_factor.verificar(s, doble_factor.codigo(s, paso), paso, ahora=ahora) is None
    assert doble_factor.verificar(s, "12345", ahora=ahora) is None
    assert doble_factor.verificar(s, "", ahora=ahora) is None


def test_secreto_cifrado_atado_al_usuario():
    s = doble_factor.generar_secreto()
    guardado = doble_factor.cifrar(s, "ana")
    assert s not in guardado
    assert doble_factor.descifrar(guardado, "ana") == s
    # Copiado a la fila de otro usuario, o alterado, ya no se descifra.
    assert doble_factor.descifrar(guardado, "admin") is None
    alterado = guardado[:-4] + ("AAAA" if not guardado.endswith("AAAA") else "BBBB")
    assert doble_factor.descifrar(alterado, "ana") is None
    assert doble_factor.descifrar("texto-plano", "ana") is None


def test_codigos_de_respaldo_un_solo_uso():
    codigos, guardado = doble_factor.generar_respaldo()
    assert len(codigos) == 10 and len(set(codigos)) == 10
    assert all(c not in guardado for c in codigos)
    restantes = doble_factor.usar_respaldo(guardado, codigos[3].upper().replace("-", " "))
    assert restantes is not None and doble_factor.respaldos_restantes(restantes) == 9
    assert doble_factor.usar_respaldo(restantes, codigos[3]) is None
    assert doble_factor.usar_respaldo(guardado, "zzzzz-zzzzz") is None


# --------------------------------------------------------------------------
# Flujo en la API
# --------------------------------------------------------------------------

def test_alta_con_qr_y_login_en_dos_pasos():
    c = _preparar()
    token = _login(c, "ana")["token"]

    r = c.post("/api/auth/2fa/iniciar", json={"password": "otra"}, headers=_auth(token))
    assert r.status_code == 400
    r = c.post("/api/auth/2fa/iniciar", json={"password": CLAVE}, headers=_auth(token))
    assert r.status_code == 200
    datos = r.json()
    assert datos["qr"].startswith("data:image/svg+xml")
    assert datos["uri"].startswith("otpauth://totp/GOSS%20IP%3Aana?")
    secreto = datos["secreto"].replace(" ", "")
    with Session(engine) as s:
        fila = s.exec(select(Operator).where(Operator.username == "ana")).one()
        assert secreto not in (fila.totp_secreto or ""), "el secreto debe ir cifrado"
        assert not fila.totp_activo, "no se activa hasta confirmar un codigo"

    r = c.post("/api/auth/2fa/activar", json={"codigo": "000000"}, headers=_auth(token))
    assert r.status_code == 400
    paso = doble_factor.paso_actual()
    r = c.post("/api/auth/2fa/activar", json={"codigo": doble_factor.codigo(secreto, paso)},
               headers=_auth(token))
    assert r.status_code == 200, r.text
    respaldos = r.json()["codigos_respaldo"]
    assert len(respaldos) == 10
    # Las sesiones abiertas sin segundo paso se cierran; la nueva sirve.
    assert c.get("/api/auth/me", headers=_auth(token)).status_code == 401
    assert c.get("/api/auth/me", headers=_auth(r.json()["sesion"]["token"])).status_code == 200

    # Login: la contrasena sola ya no da sesion.
    c.cookies.clear()
    primero = _login(c, "ana")
    assert primero["requiere_2fa"] and primero["token"] == ""
    assert "goss_sesion" not in c.cookies, "sin segundo paso no hay cookie de sesion"
    # El desafio no sirve como sesion.
    assert c.get("/api/auth/me", headers=_auth(primero["desafio"])).status_code == 401

    r = c.post("/api/auth/login/2fa", json={"desafio": primero["desafio"], "codigo": "111111"})
    assert r.status_code == 401
    # El codigo con el que se activo ya se uso: el siguiente paso si vale.
    usado = doble_factor.codigo(secreto, paso)
    r = c.post("/api/auth/login/2fa", json={"desafio": primero["desafio"], "codigo": usado})
    assert r.status_code == 401
    siguiente = doble_factor.codigo(secreto, paso + 1)
    r = c.post("/api/auth/login/2fa", json={"desafio": primero["desafio"], "codigo": siguiente})
    assert r.status_code == 200, r.text
    assert r.json()["token"] and r.json()["totp_activo"]
    r = c.post("/api/auth/login/2fa", json={"desafio": primero["desafio"], "codigo": siguiente})
    assert r.status_code == 401, "un codigo no sirve dos veces"

    # Codigo de respaldo: una vez.
    r = c.post("/api/auth/login/2fa",
               json={"desafio": _login(c, "ana")["desafio"], "codigo": respaldos[0]})
    assert r.status_code == 200
    r = c.post("/api/auth/login/2fa",
               json={"desafio": _login(c, "ana")["desafio"], "codigo": respaldos[0]})
    assert r.status_code == 401
    estado = c.get("/api/auth/2fa", headers=_auth(_login_completo(c, "ana", respaldos[1]))).json()
    assert estado["activo"] and estado["respaldos_restantes"] == 8

    with Session(engine) as s:
        acciones = [a.accion for a in s.exec(select(AuditLog)).all()]
    assert "sesion.2fa_activada" in acciones and "sesion.2fa_fallido" in acciones
    _quitar_2fa("ana")


def _login_completo(c: TestClient, usuario: str, respaldo: str) -> str:
    desafio = _login(c, usuario)["desafio"]
    r = c.post("/api/auth/login/2fa", json={"desafio": desafio, "codigo": respaldo})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_segundo_paso_comparte_el_limite_de_intentos():
    c = _preparar()
    _activar(c, "ana")
    desafio = _login(c, "ana")["desafio"]
    for _ in range(5):
        r = c.post("/api/auth/login/2fa", json={"desafio": desafio, "codigo": "000000"})
        assert r.status_code == 401
    r = c.post("/api/auth/login/2fa", json={"desafio": desafio, "codigo": "000000"})
    assert r.status_code == 429
    limite_login.exito("testclient")
    _quitar_2fa("ana")


def test_desafio_caduca_si_cambian_las_credenciales():
    c = _preparar()
    secreto, _, _ = _activar(c, "ana")
    desafio = _login(c, "ana")["desafio"]
    with Session(engine) as s:
        op = s.exec(select(Operator).where(Operator.username == "ana")).one()
        op.token_version += 1
        s.add(op)
        s.commit()
    codigo = doble_factor.codigo(secreto, doble_factor.paso_actual() + 1)
    r = c.post("/api/auth/login/2fa", json={"desafio": desafio, "codigo": codigo})
    assert r.status_code == 401
    _quitar_2fa("ana")


def test_exigir_2fa_por_rol():
    c = _preparar()
    cfg = get_config()
    anterior = cfg.exigir_2fa
    cfg.exigir_2fa = frozenset({"admin"})
    try:
        token = _login(c, "admin")["token"]
        me = c.get("/api/auth/me", headers=_auth(token))
        assert me.status_code == 200 and me.json()["debe_activar_2fa"]
        r = c.get("/api/users", headers=_auth(token))
        assert r.status_code == 403 and r.headers.get("X-Requiere-2FA") == "1"
        # Lectura por cookie (video, evidencia) tambien queda cerrada.
        assert c.get("/api/preview/cam-01/live.mjpg").status_code == 403
        # Un operador (rol no exigido) sigue trabajando normal.
        token_ana = _login(c, "ana")["token"]
        assert c.get("/api/alerts", headers=_auth(token_ana)).status_code == 200

        _, respaldos, token = _activar(c, "admin")
        assert c.get("/api/users", headers=_auth(token)).status_code == 200
        # Con el rol exigiendola, no se puede desactivar.
        r = c.post("/api/auth/2fa/desactivar", json={"password": CLAVE, "codigo": respaldos[0]},
                   headers=_auth(token))
        assert r.status_code == 409
    finally:
        cfg.exigir_2fa = anterior
        _quitar_2fa("admin")


def test_desactivar_pide_contrasena_y_codigo():
    c = _preparar()
    _, respaldos, token = _activar(c, "ana")
    r = c.post("/api/auth/2fa/desactivar", json={"password": "mala", "codigo": respaldos[0]},
               headers=_auth(token))
    assert r.status_code == 400
    r = c.post("/api/auth/2fa/desactivar", json={"password": CLAVE, "codigo": "000000"},
               headers=_auth(token))
    assert r.status_code == 400
    r = c.post("/api/auth/2fa/desactivar", json={"password": CLAVE, "codigo": respaldos[0]},
               headers=_auth(token))
    assert r.status_code == 200 and not r.json()["totp_activo"]
    assert _login(c, "ana")["token"], "sin 2FA vuelve a entrar con contrasena"


def test_administrador_restablece_2fa():
    c = _preparar()
    _activar(c, "ana")
    admin = _login(c, "admin")["token"]
    usuarios = c.get("/api/users", headers=_auth(admin)).json()
    ana = next(u for u in usuarios if u["username"] == "ana")
    assert ana["totp_activo"]
    r = c.patch(f"/api/users/{ana['id']}", json={"restablecer_2fa": True}, headers=_auth(admin))
    assert r.status_code == 200 and not r.json()["totp_activo"]
    assert _login(c, "ana")["token"]
    # Un operador no puede restablecer a nadie.
    token_ana = _login(c, "ana")["token"]
    r = c.patch(f"/api/users/{ana['id']}", json={"restablecer_2fa": True},
                headers=_auth(token_ana))
    assert r.status_code == 403


# --------------------------------------------------------------------------

def main() -> int:
    pruebas = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    fallos = 0
    try:
        for nombre, fn in pruebas:
            try:
                try:
                    fn()
                finally:
                    # Cada prueba empieza sin 2FA aunque la anterior falle a medias.
                    if engine.url.database and Path(engine.url.database).exists():
                        _quitar_2fa("ana")
                        _quitar_2fa("admin")
                    limite_login.exito("testclient")
                print(f"  [OK]    {nombre}")
            except AssertionError as e:
                fallos += 1
                print(f"  [FALLA] {nombre}: {e}")
            except Exception as e:  # noqa: BLE001
                fallos += 1
                print(f"  [ERROR] {nombre}: {type(e).__name__}: {e}")
    finally:
        engine.dispose()
        _borrar_bd(os.environ["DATABASE_URL"])
        shutil.rmtree(_TMP, ignore_errors=True)

    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
