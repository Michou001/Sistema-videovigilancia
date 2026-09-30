"""Pruebas de las notificaciones externas (Telegram, correo, WhatsApp, webhook)
y del vigilante de camaras caidas. Ningun mensaje sale a internet: los
servicios se simulan con httpx.MockTransport y un SMTP falso.

    python tests/test_notificaciones.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
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

os.environ["DATABASE_URL"] = url_temporal(_TMP, "notif")
os.environ["API_TOKEN"] = "token-de-prueba-del-worker"
for _var in [v for v in os.environ if v.startswith("NOTIFY_")]:
    del os.environ[_var]

import warnings  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from api import notificaciones  # noqa: E402
from api.camaras_caidas import procesar, segundos_sin_imagen  # noqa: E402
from api.database import engine, init_db  # noqa: E402
from api.hub import hub  # noqa: E402
from api.main import app  # noqa: E402
from api.models import Alert, AuditLog, Camera, Event, Operator  # noqa: E402
from api.notificaciones import ConfigNotificaciones, Mensaje, Notificador  # noqa: E402
from api.security import hash_password, limite_login  # noqa: E402

WORKER = {"X-API-Token": "token-de-prueba-del-worker"}
TOKEN_BOT = "123456:ABCDEF-token-secreto"


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

class _Servicios:
    """Telegram, Twilio y el webhook, simulados. Guarda cada peticion."""

    def __init__(self, fallar: set[str] | None = None, fallas_restantes: int = 10**6) -> None:
        self.peticiones: list[httpx.Request] = []
        self.fallar = fallar or set()
        self.fallas_restantes = fallas_restantes

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.peticiones.append(request)
        host = request.url.host
        if host in self.fallar and self.fallas_restantes > 0:
            self.fallas_restantes -= 1
            return httpx.Response(500, json={"ok": False})
        return httpx.Response(200, json={"ok": True})

    def de(self, host: str) -> list[httpx.Request]:
        return [p for p in self.peticiones if p.url.host == host]


def _config(**extra) -> ConfigNotificaciones:
    base = dict(telegram_token=TOKEN_BOT, telegram_chats=["111", "-222"],
                webhook_url="https://hooks.ejemplo.mx/alertas?llave=xyz", webhook_secreto="firma-secreta",
                min_severidad="critical", camara_caida_s=120, max_por_minuto=20)
    base.update(extra)
    return ConfigNotificaciones(**base)


def _notificador(servicios: _Servicios, **extra) -> Notificador:
    n = Notificador(_config(**extra), transporte=httpx.MockTransport(servicios),
                    datos_camara=lambda cid: (f"Cámara {cid}", "Acceso principal"),
                    foto_de=lambda ruta: b"\xff\xd8\xff\xe0JPEG-falso")
    n.ESPERA_BASE = 0.01
    return n


def _alerta(**extra) -> dict:
    datos = {"id": 42, "title": "ARMA DETECTADA: pistol", "detail": "Confirmada en 4 de 6 frames",
             "severity": "critical", "type": "weapon", "camera_id": "cam-1",
             "snapshot_path": "data/snapshots/x.jpg", "ts": "2026-09-30T18:05:00+00:00"}
    datos.update(extra)
    return datos


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


def _latido(c: TestClient, camara: str, estado: dict | None = None) -> None:
    r = c.post("/api/events/heartbeat", params={"camera_id": camara}, json=estado or {"connected": True},
               headers=WORKER)
    assert r.status_code == 200, r.text


def _envejecer(camara: str, segundos: float) -> None:
    with Session(engine) as s:
        cam = s.exec(select(Camera).where(Camera.camera_id == camara)).one()
        cam.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=segundos)
        s.add(cam)
        s.commit()


class _NotificadorEspia:
    def __init__(self) -> None:
        self.alertas: list[dict] = []
        self.avisos: list[Mensaje] = []

    def alerta(self, datos: dict) -> None:
        self.alertas.append(datos)

    def aviso(self, m: Mensaje) -> None:
        self.avisos.append(m)


# --------------------------------------------------------------------------
# Configuracion
# --------------------------------------------------------------------------

def test_configuracion_desde_entorno():
    os.environ.update({"NOTIFY_TELEGRAM_TOKEN": "t", "NOTIFY_TELEGRAM_CHATS": " 1, 2 ,,",
                       "NOTIFY_SMTP_HOST": "smtp.x.mx", "NOTIFY_SMTP_TO": "a@x.mx",
                       "NOTIFY_SMTP_USER": "yo@x.mx", "NOTIFY_MIN_SEVERITY": "Warning",
                       "NOTIFY_SMTP_PORT": "no-es-numero", "NOTIFY_CAMARA_CAIDA_S": "0"})
    try:
        cfg = ConfigNotificaciones.desde_entorno()
    finally:
        for v in [v for v in os.environ if v.startswith("NOTIFY_")]:
            del os.environ[v]
    assert cfg.telegram_chats == ["1", "2"]
    assert cfg.min_severidad == "warning"
    assert cfg.smtp_puerto == 587, "un valor invalido no tumba la API"
    assert cfg.smtp_de == "yo@x.mx", "sin remitente se usa el usuario"
    assert cfg.camara_caida_s == 0
    assert [c.nombre for c in notificaciones.canales_de(cfg)] == ["telegram", "correo"]
    assert not Notificador(ConfigNotificaciones()).activo, "sin datos no hay canales"

    os.environ["NOTIFY_MIN_SEVERITY"] = "altisima"
    try:
        assert ConfigNotificaciones.desde_entorno().min_severidad == "critical"
    finally:
        del os.environ["NOTIFY_MIN_SEVERITY"]


# --------------------------------------------------------------------------
# Canales
# --------------------------------------------------------------------------

def test_telegram_y_webhook_firmado():
    servicios = _Servicios()
    n = _notificador(servicios)
    m = asyncio.run(n._armar(_alerta(title="Placa <b>ABC-123-D</b> en lista negra")))
    resultados = asyncio.run(n.enviar_a_todos(m))
    assert resultados == {"telegram": "ok", "webhook": "ok"}

    tg = servicios.de("api.telegram.org")
    assert len(tg) == 2, "un mensaje por chat"
    cuerpo = json.loads(tg[0].content)
    assert tg[0].url.path == f"/bot{TOKEN_BOT}/sendMessage"
    assert cuerpo["parse_mode"] == "HTML"
    assert "&lt;b&gt;ABC-123-D&lt;/b&gt;" in cuerpo["text"], "el texto de la alerta se escapa"
    assert "Cámara cam-1 · Acceso principal" in cuerpo["text"]
    assert "Folio: ALR-000042" in cuerpo["text"]

    (hook,) = servicios.de("hooks.ejemplo.mx")
    firma = hmac.new(b"firma-secreta", hook.content, hashlib.sha256).hexdigest()
    assert hook.headers["X-Goss-Firma"] == f"sha256={firma}"
    datos = json.loads(hook.content)
    assert datos["severidad"] == "critical" and datos["folio"] == "ALR-000042"
    assert datos["camara"] == "Cámara cam-1"


def test_foto_solo_si_el_aviso_de_privacidad_lo_permite():
    servicios = _Servicios()
    n = _notificador(servicios)
    m = asyncio.run(n._armar(_alerta()))
    assert m.foto is None, "por defecto la foto no sale del sistema"

    servicios = _Servicios()
    n = _notificador(servicios, incluir_foto=True)
    m = asyncio.run(n._armar(_alerta()))
    assert m.foto is not None
    asyncio.run(n.enviar_a_todos(m))
    tg = servicios.de("api.telegram.org")
    assert tg[0].url.path.endswith("/sendPhoto")
    assert b"JPEG-falso" in tg[0].content
    hook = servicios.de("hooks.ejemplo.mx")[0]
    assert b"JPEG-falso" not in hook.content, "el webhook nunca lleva la foto"


def test_whatsapp_por_twilio():
    servicios = _Servicios()
    n = _notificador(servicios, telegram_token="", webhook_url="", twilio_sid="AC123",
                     twilio_token="tw-secreto", twilio_de="+14155238886",
                     twilio_para=["+5213312345678", "whatsapp:+5215512345678"])
    resultados = asyncio.run(n.enviar_a_todos(Mensaje(titulo="Prueba", severidad="critical")))
    assert resultados == {"whatsapp": "ok"}
    peticiones = servicios.de("api.twilio.com")
    assert len(peticiones) == 2
    p = peticiones[0]
    assert p.url.path == "/2010-04-01/Accounts/AC123/Messages.json"
    esperado = "Basic " + base64.b64encode(b"AC123:tw-secreto").decode()
    assert p.headers["Authorization"] == esperado
    form = dict(x.split("=", 1) for x in p.content.decode().split("&"))
    assert form["From"] == "whatsapp%3A%2B14155238886"
    assert form["To"] == "whatsapp%3A%2B5213312345678"
    assert dict(x.split("=", 1) for x in peticiones[1].content.decode().split("&"))["To"] == \
        "whatsapp%3A%2B5215512345678", "no se duplica el prefijo"


def test_correo_por_smtp():
    enviados = []

    class SMTPFalso:
        def __init__(self, host, puerto, timeout=None, context=None):
            self.host, self.puerto = host, puerto
            self.pasos = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            enviados.append(self)

        def starttls(self, context=None):
            self.pasos.append("starttls")

        def login(self, usuario, password):
            self.pasos.append(("login", usuario, password))

        def send_message(self, msg):
            self.mensaje = msg

    original = notificaciones.smtplib.SMTP
    notificaciones.smtplib.SMTP = SMTPFalso
    try:
        n = _notificador(_Servicios(), telegram_token="", webhook_url="", smtp_host="smtp.x.mx",
                         smtp_usuario="alertas@x.mx", smtp_password="clave", smtp_de="alertas@x.mx",
                         smtp_para=["a@x.mx", "b@x.mx"], incluir_foto=True)
        m = asyncio.run(n._armar(_alerta()))
        assert asyncio.run(n.enviar_a_todos(m)) == {"correo": "ok"}
    finally:
        notificaciones.smtplib.SMTP = original
    (s,) = enviados
    assert (s.host, s.puerto) == ("smtp.x.mx", 587)
    assert s.pasos == ["starttls", ("login", "alertas@x.mx", "clave")]
    assert s.mensaje["To"] == "a@x.mx, b@x.mx"
    assert s.mensaje["Subject"] == "[GOSS IP] ARMA DETECTADA: pistol"
    adjuntos = [p.get_filename() for p in s.mensaje.iter_attachments()]
    assert adjuntos == ["evidencia.jpg"]


def test_reintentos_y_errores_sin_secretos():
    servicios = _Servicios(fallar={"api.telegram.org"}, fallas_restantes=1)
    n = _notificador(servicios, webhook_url="")
    assert asyncio.run(n.enviar_a_todos(Mensaje(titulo="x"))) == {"telegram": "ok"}, \
        "un fallo pasajero se reintenta"

    servicios = _Servicios(fallar={"api.telegram.org", "hooks.ejemplo.mx"})
    n = _notificador(servicios)
    registros: list[str] = []

    class _Captura(logging.Handler):
        def emit(self, record):
            registros.append(record.getMessage())

    captura = _Captura()
    logging.getLogger("httpx").addHandler(captura)
    try:
        resultados = asyncio.run(n.enviar_a_todos(Mensaje(titulo="x")))
    finally:
        logging.getLogger("httpx").removeHandler(captura)
    assert registros and not any(TOKEN_BOT in r for r in registros), "el log de httpx no lleva el token"
    assert set(resultados) == {"telegram", "webhook"}
    assert all(r != "ok" for r in resultados.values())
    assert len(servicios.de("hooks.ejemplo.mx")) == Notificador.REINTENTOS
    texto = json.dumps(resultados)
    assert TOKEN_BOT not in texto, "el token del bot no puede terminar en pantalla ni en la bitacora"
    assert "llave=xyz" not in texto
    assert n.fallidos == 2


# --------------------------------------------------------------------------
# Filtros
# --------------------------------------------------------------------------

def test_filtro_de_severidad_y_camaras():
    n = _notificador(_Servicios())
    assert n.debe_notificar(_alerta(severity="critical"))
    assert not n.debe_notificar(_alerta(severity="warning"))
    assert n.debe_notificar(_alerta(severity="warning", type="camera")), \
        "una camara caida deja un punto ciego: se avisa aunque el minimo sea critical"
    assert not n.debe_notificar(_alerta(severity="info", type="camera"))
    n = _notificador(_Servicios(), camara_caida_s=0)
    assert not n.debe_notificar(_alerta(severity="warning", type="camera"))
    n = _notificador(_Servicios(), min_severidad="warning")
    assert n.debe_notificar(_alerta(severity="warning"))


def test_sin_repetidos_y_con_tope_por_minuto():
    n = _notificador(_Servicios(), max_por_minuto=2)
    assert n._permitido(Mensaje(titulo="A", camara="c1"))
    assert not n._permitido(Mensaje(titulo="A", camara="c1")), "la misma alerta no se repite en 60 s"
    assert n._permitido(Mensaje(titulo="A", camara="c2")), "otra camara si"
    assert not n._permitido(Mensaje(titulo="B", camara="c1")), "tope de 2 por minuto"


def test_cola_entrega_en_segundo_plano():
    servicios = _Servicios()

    async def _escenario():
        n = _notificador(servicios)
        await n.iniciar()
        n.alerta(_alerta())
        n.alerta(_alerta(severity="warning", title="Solo warning"))
        for _ in range(100):
            if n.enviados >= 2:
                break
            await asyncio.sleep(0.02)
        await n.detener()
        return n

    n = asyncio.run(_escenario())
    assert n.enviados == 2, n.enviados
    textos = [json.loads(p.content)["text"] for p in servicios.de("api.telegram.org")]
    assert all("Solo warning" not in t for t in textos)


# --------------------------------------------------------------------------
# Integracion con la API
# --------------------------------------------------------------------------

def test_hub_pasa_las_alertas_al_notificador():
    espia = _NotificadorEspia()
    hub.al_alertar = espia.alerta
    try:
        asyncio.run(hub.difundir("alert", {"title": "x", "severity": "critical"}))
        asyncio.run(hub.difundir("event", {"value": "y"}))
        asyncio.run(hub.difundir("alert", {"title": "retro", "severity": "critical"}, notificar=False))
    finally:
        hub.al_alertar = None
    assert [a["title"] for a in espia.alertas] == ["x"]


def test_ingesta_notifica_la_alerta():
    c, _ = _cliente()
    espia = _NotificadorEspia()
    hub.al_alertar = espia.alerta
    try:
        r = c.post("/api/events", headers=WORKER, json={"camera_id": "cam-n", "events": [
            {"event_id": str(uuid.uuid4()), "camera_id": "cam-n", "type": "camera",
             "value": "sabotaje", "confidence": 1.0}]})
        assert r.status_code == 200
    finally:
        hub.al_alertar = None
    assert [a["title"] for a in espia.alertas] == ["SABOTAJE DE CÁMARA"]
    assert espia.alertas[0]["id"], "el folio ya viene de la base de datos"


def test_endpoint_de_estado_y_prueba():
    c, h = _cliente()
    _, h_op = _cliente("operador1", "operator")
    notificaciones.notificador = Notificador(ConfigNotificaciones())
    try:
        assert c.get("/api/notificaciones", headers=h_op).status_code == 403
        r = c.get("/api/notificaciones", headers=h)
        assert r.status_code == 200 and r.json()["canales"] == []
        assert c.post("/api/notificaciones/prueba", headers=h).status_code == 409

        servicios = _Servicios(fallar={"hooks.ejemplo.mx"})
        notificaciones.notificador = _notificador(servicios)
        r = c.get("/api/notificaciones", headers=h).json()
        assert [x["nombre"] for x in r["canales"]] == ["telegram", "webhook"]
        assert TOKEN_BOT not in json.dumps(r) and "firma-secreta" not in json.dumps(r)
        r = c.post("/api/notificaciones/prueba", headers=h)
        assert r.status_code == 200
        datos = r.json()
        assert datos["resultados"]["telegram"] == "ok" and not datos["ok"]
        assert len(servicios.de("hooks.ejemplo.mx")) == 1, "la prueba no reintenta: el admin espera"
        assert c.post("/api/notificaciones/prueba", headers=h_op).status_code == 403
        # Mutacion: solo con cabecera, la cookie sola no basta (CSRF).
        assert c.post("/api/notificaciones/prueba").status_code == 401
    finally:
        notificaciones.notificador = None
    with Session(engine) as s:
        entrada = s.exec(select(AuditLog).where(AuditLog.accion == "notificaciones.prueba")).one()
        assert entrada.usuario == "admin" and TOKEN_BOT not in (entrada.detalle or "")


# --------------------------------------------------------------------------
# Vigilante de camaras caidas
# --------------------------------------------------------------------------

def test_segundos_sin_imagen():
    ahora = datetime.now(timezone.utc)
    cam = Camera(camera_id="x", last_heartbeat=ahora - timedelta(seconds=10),
                 status_json=json.dumps({"connected": True, "seconds_since_frame": 0.2}))
    assert 10 <= segundos_sin_imagen(cam, ahora) < 11
    cam.status_json = json.dumps({"connected": False, "seconds_since_frame": 50})
    assert 60 <= segundos_sin_imagen(cam, ahora) < 61, "el worker vive pero la camara no"
    cam.status_json = json.dumps({"connected": False})
    assert segundos_sin_imagen(cam, ahora) == float("inf"), "nunca entrego imagen"
    cam.status_json = "no es json"
    assert 10 <= segundos_sin_imagen(cam, ahora) < 11
    assert segundos_sin_imagen(Camera(camera_id="y"), ahora) is None, "nunca ha latido"


def test_camara_caida_y_recuperada():
    c, _ = _cliente()
    espia = _NotificadorEspia()
    hub.al_alertar = espia.alerta
    notificaciones.notificador = espia
    try:
        _latido(c, "cam-caida")
        _latido(c, "cam-sana")
        assert asyncio.run(procesar(120)) == [], "todo en linea"

        _envejecer("cam-caida", 300)
        (t,) = asyncio.run(procesar(120))
        assert t.camera_id == "cam-caida" and t.caida
        assert asyncio.run(procesar(120)) == [], "el aviso sale UNA vez, no cada 30 s"

        with Session(engine) as s:
            cam = s.exec(select(Camera).where(Camera.camera_id == "cam-caida")).one()
            assert cam.caida_desde is not None
            alerta = s.exec(select(Alert).where(Alert.camera_id == "cam-caida")).one()
            assert alerta.title == "Cámara sin señal" and alerta.severity == "warning"
            assert "Sin imagen desde hace 5 min" in alerta.detail, alerta.detail
            ev = s.exec(select(Event).where(Event.event_id == alerta.event_id)).one()
            assert ev.type == "camera" and ev.value == "sin_senal"
            ult = cam.last_heartbeat
        assert [a["title"] for a in espia.alertas] == ["Cámara sin señal"]
        with Session(engine) as s:
            cam = s.exec(select(Camera).where(Camera.camera_id == "cam-caida")).one()
            assert cam.last_heartbeat == ult, "el aviso de caida no cuenta como latido"

        _latido(c, "cam-caida")
        (t,) = asyncio.run(procesar(120))
        assert t.camera_id == "cam-caida" and not t.caida
        with Session(engine) as s:
            cam = s.exec(select(Camera).where(Camera.camera_id == "cam-caida")).one()
            assert cam.caida_desde is None
            recuperada = s.exec(select(Event).where(Event.camera_id == "cam-caida",
                                                    Event.value == "senal_recuperada")).one()
            assert recuperada.severity == "info"
            assert len(s.exec(select(Alert).where(Alert.camera_id == "cam-caida")).all()) == 1, \
                "la recuperacion no es alerta"
        (aviso,) = espia.avisos
        assert aviso.titulo == "Cámara recuperada" and "Estuvo sin señal" in aviso.detalle
        assert aviso.camara == "cam-caida"
    finally:
        hub.al_alertar = None
        notificaciones.notificador = None


def test_camara_sin_video_con_worker_vivo():
    c, _ = _cliente()
    _latido(c, "cam-sin-video", {"connected": False, "seconds_since_frame": None,
                                 "last_error": "401 Unauthorized"})
    (t,) = asyncio.run(procesar(120))
    assert t.camera_id == "cam-sin-video" and t.caida
    with Session(engine) as s:
        alerta = s.exec(select(Alert).where(Alert.camera_id == "cam-sin-video")).one()
        assert "no entrega imagen" in alerta.detail


def test_camara_deshabilitada_no_avisa():
    c, _ = _cliente()
    _latido(c, "cam-retirada")
    _envejecer("cam-retirada", 10_000)
    with Session(engine) as s:
        cam = s.exec(select(Camera).where(Camera.camera_id == "cam-retirada")).one()
        cam.enabled = False
        s.add(cam)
        s.commit()
    assert all(t.camera_id != "cam-retirada" for t in asyncio.run(procesar(120)))


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
        engine.dispose()
        _borrar_bd(os.environ["DATABASE_URL"])
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
