"""Pruebas de zonas y reglas: geometria y horarios compartidos, el motor del
worker (intrusion, cruce de linea, merodeo, conteo) y la API (alta, lo que
recibe el worker, severidad por horario, conteo).

    python tests/test_zonas.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

_TMP = Path(tempfile.mkdtemp())
from bd_prueba import borrar as _borrar_bd  # noqa: E402
from bd_prueba import url_temporal  # noqa: E402

os.environ["DATABASE_URL"] = url_temporal(_TMP, "zonas")
os.environ["API_TOKEN"] = "token-de-prueba-del-worker"
os.environ["ZONA_HORARIA"] = "America/Mexico_City"

import warnings  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from api.database import engine, init_db  # noqa: E402
from api.main import app  # noqa: E402
from api.models import Alert, AuditLog, Event, Operator  # noqa: E402
from api.security import hash_password, limite_login  # noqa: E402
from edge.detectors.base import Pista  # noqa: E402
from edge.zonas import REARME_S, ClienteZonas, MotorZonas  # noqa: E402
from shared.zonas import (  # noqa: E402
    describir_horario,
    en_horario,
    lado,
    punto_en_poligono,
    segmentos_se_cruzan,
    sentido_de_cruce,
    validar_horario,
    validar_puntos,
)

WORKER = {"X-API-Token": "token-de-prueba-del-worker"}
MX = ZoneInfo("America/Mexico_City")
CUADRADO = [(0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75)]


# --------------------------------------------------------------------------
# Geometria y horarios
# --------------------------------------------------------------------------

def test_geometria():
    assert punto_en_poligono((0.5, 0.5), CUADRADO)
    assert not punto_en_poligono((0.1, 0.5), CUADRADO)
    assert not punto_en_poligono((0.5, 0.5), CUADRADO[:2]), "dos puntos no son un poligono"
    concavo = [(0, 0), (1, 0), (1, 1), (0.5, 0.3), (0, 1)]
    assert not punto_en_poligono((0.5, 0.8), concavo), "la muesca del poligono concavo queda fuera"
    assert punto_en_poligono((0.2, 0.5), concavo)
    # Linea horizontal A->B hacia la derecha: abajo (y mayor) es la derecha
    # de quien camina de A a B.
    a, b = (0, 0.5), (1, 0.5)
    assert lado((0.5, 0.9), a, b) > 0 and lado((0.5, 0.1), a, b) < 0
    assert sentido_de_cruce(-1, 1) == "entrada" and sentido_de_cruce(1, -1) == "salida"
    assert segmentos_se_cruzan((0.5, 0.1), (0.5, 0.9), a, b)
    assert not segmentos_se_cruzan((1.5, 0.1), (1.5, 0.9), a, b), "por fuera del extremo no cruza"


def test_horarios():
    noches = [{"dias": [4], "desde": "22:00", "hasta": "06:00"}]     # viernes de noche
    viernes_23 = datetime(2026, 10, 2, 23, 0, tzinfo=MX)
    sabado_3 = datetime(2026, 10, 3, 3, 0, tzinfo=MX)
    viernes_3 = datetime(2026, 10, 2, 3, 0, tzinfo=MX)
    sabado_7 = datetime(2026, 10, 3, 7, 0, tzinfo=MX)
    assert en_horario(noches, viernes_23, MX)
    assert en_horario(noches, sabado_3, MX), "la franja nocturna es del dia en que empieza"
    assert not en_horario(noches, viernes_3, MX), "la madrugada del viernes es del jueves"
    assert not en_horario(noches, sabado_7, MX)
    # Un evento en UTC se juzga en hora de Mexico: 05:00 UTC del sabado son
    # las 23:00 del viernes en CDMX.
    assert en_horario(noches, datetime(2026, 10, 3, 5, 0, tzinfo=timezone.utc), MX)
    oficina = [{"dias": [0, 1, 2, 3, 4], "desde": "09:00", "hasta": "18:00"}]
    assert en_horario(oficina, datetime(2026, 9, 30, 9, 0, tzinfo=MX), MX)
    assert not en_horario(oficina, datetime(2026, 9, 30, 18, 0, tzinfo=MX), MX), "hasta es exclusivo"
    assert en_horario([], viernes_3, MX) and en_horario(None, viernes_3, MX)
    assert describir_horario(oficina) == "lun-vie 09:00-18:00"
    assert describir_horario(noches) == "vie 22:00-06:00"
    assert describir_horario(None) == "siempre"


def test_validaciones():
    for malo, texto in [([{"dias": [7], "desde": "22:00", "hasta": "06:00"}], "dias"),
                        ([{"dias": [1], "desde": "25:00", "hasta": "06:00"}], "hora"),
                        ([{"dias": [1], "desde": "08:00", "hasta": "08:00"}], "misma"),
                        ([{"dias": [], "desde": "08:00", "hasta": "09:00"}], "dia")]:
        try:
            validar_horario(malo)
            raise AssertionError(f"debio rechazar {malo}")
        except ValueError as e:
            assert texto in str(e), e
    assert validar_horario([{"dias": [3, 1, 1], "desde": "08:00", "hasta": "09:00"}])[0]["dias"] == [1, 3]
    for tipo, puntos in [("linea", [[0, 0], [1, 1], [0.5, 0.5]]), ("intrusion", [[0, 0], [1, 1]]),
                         ("intrusion", [[0, 0], [0.5, 0.5], [1, 1]]), ("linea", [[0, 0], [1.2, 1]]),
                         ("linea", [[0.5, 0.5], [0.5, 0.501]]), ("otro", [[0, 0], [1, 1]])]:
        try:
            validar_puntos(tipo, puntos)
            raise AssertionError(f"debio rechazar {tipo} {puntos}")
        except ValueError:
            pass


# --------------------------------------------------------------------------
# Motor del worker
# --------------------------------------------------------------------------

def _cfg(**extra) -> SimpleNamespace:
    base = dict(camera_id="cam-zonas", zonas_frames_min=3, snapshot_dir=_TMP, enable_zonas=True,
                enable_motion=True, api_url="", api_token="")
    base.update(extra)
    return SimpleNamespace(**base)


def _zona(id_, tipo, puntos, **extra) -> dict:
    return {"id": id_, "nombre": f"Zona {id_}", "tipo": tipo, "puntos": puntos, **extra}


class _Frame:
    def __init__(self, ts: float, ancho=1000, alto=1000) -> None:
        self.ts = ts
        self.frame = np.zeros((alto, ancho, 3), np.uint8)


def _pista(tid, x, y, clase="persona", alto=200, ancho=80) -> Pista:
    """Persona con los pies en (x, y) pixeles."""
    return Pista(tid, clase, (x - ancho / 2, y - alto, x + ancho / 2, y), 0.9)


T0 = datetime(2026, 9, 30, 23, 0, tzinfo=MX).timestamp()   # miercoles 23:00 CDMX


def _recorrido(motor, puntos, tid=1, clase="persona", t0=T0, paso=0.125) -> list:
    eventos = []
    for i, (x, y) in enumerate(puntos):
        motor.al_pistas(_Frame(t0 + i * paso), [_pista(tid, x, y, clase)])
        eventos += motor.eventos()
    return eventos


def test_intrusion_con_frames_minimos_y_rearme():
    m = MotorZonas(_cfg())
    m.actualizar([_zona(1, "intrusion", CUADRADO)], "v1")
    afuera, adentro = (100, 500), (500, 500)
    eventos = _recorrido(m, [afuera, adentro, adentro])
    assert eventos == [], "dos frames dentro no bastan (ZONAS_FRAMES_MIN=3)"
    eventos = _recorrido(m, [adentro] * 10, t0=T0 + 1)
    assert len(eventos) == 1, "un solo evento por objeto, no uno por frame"
    ev = eventos[0]
    assert ev.type.value == "zone" and ev.value == "intrusion"
    assert ev.meta == {"zona_id": 1, "zona": "Zona 1", "clase": "persona"}
    assert ev.snapshot_path and (Path(ev.snapshot_path).name == f"{ev.event_id}.jpg")
    # Sale un momento y regresa: no se repite...
    assert _recorrido(m, [afuera] * 4 + [adentro] * 4, t0=T0 + 5) == []
    # ...pero si pasa REARME_S fuera, vuelve a contar.
    _recorrido(m, [afuera, afuera], t0=T0 + 10)
    m.al_pistas(_Frame(T0 + 10 + REARME_S + 1), [_pista(1, *afuera)])
    assert len(_recorrido(m, [adentro] * 4, t0=T0 + 12 + REARME_S)) == 1


def test_intrusion_respeta_clase_y_horario():
    m = MotorZonas(_cfg())
    de_noche = [{"dias": list(range(7)), "desde": "22:00", "hasta": "06:00"}]
    m.actualizar([_zona(1, "intrusion", CUADRADO, clases=["persona"], horario=de_noche)], "v1")
    assert _recorrido(m, [(500, 500)] * 5, tid=7, clase="vehiculo") == [], "la regla es solo de personas"
    assert len(_recorrido(m, [(500, 500)] * 5, tid=8)) == 1, "23:00: armada"
    medio_dia = datetime(2026, 9, 30, 12, 0, tzinfo=MX).timestamp()
    assert _recorrido(m, [(500, 500)] * 5, tid=9, t0=medio_dia) == [], "12:00: fuera de horario"


def test_cruce_de_linea_con_sentido():
    m = MotorZonas(_cfg())
    # Linea vertical de arriba hacia abajo en x=0.5: la derecha de quien
    # camina de A (arriba) a B (abajo) es la IZQUIERDA de la imagen.
    m.actualizar([_zona(1, "linea", [[0.5, 0.2], [0.5, 0.8]], direccion="ambas"),
                  _zona(2, "linea", [[0.5, 0.2], [0.5, 0.8]], direccion="salida")], "v1")
    izq_a_der = [(300, 500), (400, 500), (495, 500), (505, 500), (600, 500), (700, 500)]
    eventos = _recorrido(m, izq_a_der)
    assert [(e.meta["zona_id"], e.meta["direccion"]) for e in eventos] == [(1, "salida"), (2, "salida")]
    eventos = _recorrido(m, list(reversed(izq_a_der)), t0=T0 + 5)
    assert [(e.meta["zona_id"], e.meta["direccion"]) for e in eventos] == [(1, "entrada")], \
        "la zona 2 solo cuenta salidas"


def test_linea_sin_cruces_falsos():
    m = MotorZonas(_cfg())
    m.actualizar([_zona(1, "linea", [[0.5, 0.2], [0.5, 0.8]])], "v1")
    temblor = [(505, 500), (495, 500), (508, 500), (492, 500), (503, 500)] * 3
    assert _recorrido(m, [(700, 500)] + temblor) == [], "parado sobre la linea no la cruza"
    por_fuera = [(300, 950), (500, 950), (700, 950)]
    assert _recorrido(m, por_fuera, tid=2, t0=T0 + 10) == [], "paso por debajo del extremo B"


def test_merodeo_y_tolerancia():
    m = MotorZonas(_cfg())
    m.actualizar([_zona(1, "merodeo", CUADRADO, segundos=60)], "v1")
    eventos = []
    for s in range(0, 70):
        pies = (100, 500) if 20 <= s < 23 else (500, 500)   # sale 3 s: se tolera
        m.al_pistas(_Frame(T0 + s), [_pista(1, *pies)])
        eventos += m.eventos()
    assert len(eventos) == 1
    assert eventos[0].value == "merodeo" and 60 <= eventos[0].meta["segundos"] <= 61, \
        "los 3 s fuera no reinician la cuenta"


def test_conteo_sin_foto():
    m = MotorZonas(_cfg())
    m.actualizar([_zona(1, "conteo", [[0.0, 0.5], [1.0, 0.5]], clases=["vehiculo", "persona"])], "v1")
    eventos = []
    for tid in range(3):
        eventos += _recorrido(m, [(500, 300), (500, 450), (500, 550), (500, 700)], tid=tid,
                              clase="vehiculo", t0=T0 + tid * 5)
    assert len(eventos) == 3 and all(e.value == "conteo" for e in eventos)
    assert all(e.snapshot_path is None for e in eventos), "el conteo es estadistica: sin foto"
    assert {e.meta["direccion"] for e in eventos} == {"entrada"}


def test_zona_cambiada_empieza_de_cero():
    m = MotorZonas(_cfg())
    m.actualizar([_zona(1, "intrusion", CUADRADO)], "v1")
    _recorrido(m, [(500, 500)] * 2)
    m.actualizar([_zona(1, "intrusion", [(0.4, 0.4), (0.9, 0.4), (0.9, 0.9)])], "v2")
    assert m.version == "v2"
    assert not m._estados, "el estado de la zona vieja no describe la nueva"
    m.actualizar([{"id": "x"}], "v3")
    assert m.zonas == [], "una zona mal formada se ignora sin tumbar al worker"


def test_cliente_de_zonas_con_cache():
    cfg = _cfg(camera_id=f"cam-cache-{uuid.uuid4().hex[:6]}", zonas_refresco_s=3600)
    respuestas = [{"version": "a", "zonas": [_zona(1, "intrusion", CUADRADO)]}]
    m = MotorZonas(cfg)
    c = ClienteZonas(cfg, m, obtener=lambda: respuestas[-1])
    try:
        assert c._hilo.is_alive()
        c.refrescar()
        assert m.version == "a" and len(m.zonas) == 1
        assert not c.refrescar(), "misma version: no recarga"
        respuestas.append({"version": "b", "zonas": []})
        assert c.refrescar() and m.zonas == []
        # Otro arranque sin API: se usa lo ultimo conocido.
        m2 = MotorZonas(cfg)

        def _caida():
            raise ConnectionError("API apagada")

        c2 = ClienteZonas(cfg, m2, obtener=_caida)
        assert m2.version == "b"
        c2.cerrar()
    finally:
        c.cerrar()
        c.cache.unlink(missing_ok=True)


def test_worker_pasa_las_pistas_al_motor():
    from edge.worker import Camara

    class _Det:
        name = "motion"

        def procesar(self, frame):
            return []

        def pistas(self):
            return [_pista(4, 500, 500)]

    enviados = []
    cam = Camara.__new__(Camara)
    cam.id, cam.frames, cam.total_eventos = "cam-zonas", 0, 0
    cam.detectores = [_Det()]
    cam.fallos = {"motion": 0}
    cam.sink = SimpleNamespace(enviar=enviados.append)
    motor = MotorZonas(_cfg())
    motor.actualizar([_zona(1, "intrusion", CUADRADO)], "v1")
    cam.complementos = [motor]
    for i in range(4):
        cam.procesar(_Frame(T0 + i))
    assert [e.value for e in enviados] == ["intrusion"]
    vista = cam.vista_anotada(_Frame(T0))
    assert vista[250, 500].any(), "la zona se dibuja en la vista en vivo"


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

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


def _camara(c: TestClient, camara: str = "cam-z") -> None:
    assert c.post("/api/events/heartbeat", params={"camera_id": camara}, json={},
                  headers=WORKER).status_code == 200


def _crear(c, h, **datos) -> dict:
    base = {"camera_id": "cam-z", "nombre": "Estacionamiento", "tipo": "intrusion",
            "puntos": [list(p) for p in CUADRADO]}
    base.update(datos)
    r = c.post("/api/zonas", json=base, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _ingerir(c, zona_id, valor="intrusion", ts=None, camara="cam-z", **meta) -> dict:
    evento = {"event_id": str(uuid.uuid4()), "camera_id": camara, "type": "zone", "value": valor,
              "confidence": 0.9, "meta": {"zona_id": zona_id, "zona": "x", "clase": "persona", **meta}}
    if ts:
        evento["ts"] = ts.isoformat()
    r = c.post("/api/events", json={"camera_id": camara, "events": [evento]}, headers=WORKER)
    assert r.status_code == 200, r.text
    return r.json()["matches"][0]


def test_alta_de_zonas_y_permisos():
    c, h = _cliente()
    _, h_op = _cliente("operador-z", "operator")
    _, h_vi = _cliente("consulta-z", "viewer")
    _camara(c)
    z = _crear(c, h, horario=[{"dias": [0, 1, 2, 3, 4, 5, 6], "desde": "22:00", "hasta": "06:00"}])
    assert z["tipo"] == "intrusion" and z["clases"] == ["persona"] and z["segundos"] is None
    assert z["horario_texto"] == "todos los días 22:00-06:00"
    assert z["creada_por"] == "admin"
    assert c.post("/api/zonas", json={"camera_id": "cam-z", "nombre": "x1", "tipo": "intrusion",
                                      "puntos": [list(p) for p in CUADRADO]}, headers=h_op).status_code == 403
    assert c.get("/api/zonas", params={"camera_id": "cam-z"}, headers=h_vi).status_code == 200, \
        "la consulta puede ver las reglas"
    r = c.post("/api/zonas", json={"camera_id": "no-existe", "nombre": "Patio", "tipo": "intrusion",
                                   "puntos": [list(p) for p in CUADRADO]}, headers=h)
    assert r.status_code == 404
    for malo in [{"tipo": "linea", "puntos": [[0, 0], [1, 1], [0, 1]]},
                 {"clases": ["gato"]},
                 {"horario": [{"dias": [1], "desde": "9", "hasta": "10:00"}]},
                 {"severidad": "altisima"},
                 {"puntos": [[0, 0], [2, 2], [0, 1]]}]:
        datos = {"camera_id": "cam-z", "nombre": "Mala", "tipo": "intrusion",
                 "puntos": [list(p) for p in CUADRADO], **malo}
        assert c.post("/api/zonas", json=datos, headers=h).status_code == 422, malo
    m = _crear(c, h, nombre="Banqueta", tipo="merodeo")
    assert m["segundos"] == 60, "el merodeo trae 60 s por defecto"
    linea = _crear(c, h, nombre="Pluma", tipo="linea", puntos=[[0.1, 0.5], [0.9, 0.5]], direccion="entrada")
    assert linea["direccion"] == "entrada"


def test_edicion_baja_y_bitacora():
    c, h = _cliente()
    _camara(c)
    z = _crear(c, h, nombre="Bodega")
    r = c.patch(f"/api/zonas/{z['id']}", json={"severidad": "critical", "activa": False}, headers=h)
    assert r.status_code == 200 and r.json()["severidad"] == "critical" and not r.json()["activa"]
    r = c.patch(f"/api/zonas/{z['id']}", json={"tipo": "linea"}, headers=h)
    assert r.status_code == 200, "el tipo no se cambia por PATCH (no esta en los campos editables)"
    assert r.json()["tipo"] == "intrusion"
    r = c.patch(f"/api/zonas/{z['id']}", json={"puntos": [[0, 0], [1, 1]]}, headers=h)
    assert r.status_code == 422 and "polígono" in r.json()["detail"]
    assert c.delete(f"/api/zonas/{z['id']}", headers=h).status_code == 204
    assert c.delete(f"/api/zonas/{z['id']}", headers=h).status_code == 404
    with Session(engine) as s:
        acciones = [a.accion for a in s.exec(select(AuditLog).where(AuditLog.objetivo == "cam-z: Bodega"))]
    assert acciones == ["zonas.alta", "zonas.edicion", "zonas.baja"], \
        f"una edicion que no cambia nada no ensucia la bitacora: {acciones}"


def test_el_worker_recibe_sus_zonas():
    c, h = _cliente()
    _camara(c, "cam-w")
    assert c.get("/api/zonas/borde/cam-w").status_code == 401
    r1 = c.get("/api/zonas/borde/cam-w", headers=WORKER).json()
    assert r1["zonas"] == []
    z = _crear(c, h, camera_id="cam-w", nombre="Patio")
    r2 = c.get("/api/zonas/borde/cam-w", headers=WORKER).json()
    assert r2["version"] != r1["version"]
    assert r2["zonas"][0]["id"] == z["id"] and r2["zonas"][0]["puntos"] == z["puntos"]
    assert c.get("/api/zonas/borde/cam-w", headers=WORKER).json()["version"] == r2["version"], "estable"
    c.patch(f"/api/zonas/{z['id']}", json={"activa": False}, headers=h)
    assert c.get("/api/zonas/borde/cam-w", headers=WORKER).json()["zonas"] == [], \
        "una regla desactivada no se manda al worker"


def test_severidad_por_horario_y_regla():
    c, h = _cliente()
    _camara(c, "cam-s")
    noche = [{"dias": list(range(7)), "desde": "22:00", "hasta": "06:00"}]
    z = _crear(c, h, camera_id="cam-s", nombre="Almacén", horario=noche, severidad="critical")
    de_noche = datetime(2026, 9, 30, 23, 30, tzinfo=MX)
    de_dia = datetime(2026, 9, 30, 13, 0, tzinfo=MX)
    m = _ingerir(c, z["id"], ts=de_noche, camara="cam-s")
    assert m["severity"] == "critical" and m["titulo"] == "Intrusión en Almacén"
    assert "todos los días 22:00-06:00" in m["reason"]
    m = _ingerir(c, z["id"], ts=de_dia, camara="cam-s")
    assert m["severity"] == "info" and "Fuera del horario" in m["reason"], \
        "de dia la misma presencia no es alerta (se juzga por la hora del evento)"
    assert _ingerir(c, 99999, ts=de_noche, camara="cam-s")["severity"] == "info", "zona inexistente"
    assert _ingerir(c, "abc", ts=de_noche, camara="cam-s")["severity"] == "info"
    otra = _crear(c, h, camera_id="cam-s", nombre="Pluma", tipo="linea", puntos=[[0.1, 0.5], [0.9, 0.5]])
    m = _ingerir(c, otra["id"], valor="cruce_linea", camara="cam-s", direccion="entrada", clase="vehiculo")
    assert m["severity"] == "warning" and m["titulo"] == "Cruce de línea: Pluma"
    assert m["reason"] == "Vehículo cruzó la línea en sentido de entrada"
    with Session(engine) as s:
        alerta = s.exec(select(Alert).where(Alert.event_id == m["event_id"])).one()
        assert alerta.title == "Cruce de línea: Pluma" and alerta.type == "zone"
    c.patch(f"/api/zonas/{otra['id']}", json={"activa": False}, headers=h)
    assert _ingerir(c, otra["id"], valor="cruce_linea", camara="cam-s")["severity"] == "info"
    # La zona de otra camara no aplica.
    _camara(c, "cam-otra")
    assert _ingerir(c, z["id"], ts=de_noche, camara="cam-otra")["severity"] == "info"


def test_merodeo_y_conteo_en_la_api():
    c, h = _cliente()
    _camara(c, "cam-c")
    mer = _crear(c, h, camera_id="cam-c", nombre="Cajero", tipo="merodeo", segundos=90)
    m = _ingerir(c, mer["id"], valor="merodeo", camara="cam-c", segundos=95)
    assert m["severity"] == "warning" and m["reason"] == "Persona lleva 95 s en la zona (umbral 90 s)"
    cont = _crear(c, h, camera_id="cam-c", nombre="Acceso", tipo="conteo",
                  puntos=[[0.0, 0.5], [1.0, 0.5]], clases=["persona", "vehiculo"])
    ahora = datetime.now(timezone.utc)
    for sentido, clase in [("entrada", "vehiculo"), ("entrada", "persona"), ("salida", "vehiculo")]:
        r = _ingerir(c, cont["id"], valor="conteo", camara="cam-c", direccion=sentido, clase=clase,
                     ts=ahora - timedelta(minutes=5))
        assert r["severity"] == "info", "contar nunca es alerta"
    _ingerir(c, cont["id"], valor="conteo", camara="cam-c", direccion="entrada",
             ts=ahora - timedelta(days=3))
    r = c.get(f"/api/zonas/{cont['id']}/conteo", headers=h).json()
    assert r["total"] == 3, r
    assert r["por_sentido"] == {"entrada": 2, "salida": 1}
    assert r["por_clase"] == {"vehiculo": 2, "persona": 1}
    assert sum(x["n"] for x in r["por_hora"]) == 3
    r = c.get(f"/api/zonas/{cont['id']}/conteo", headers=h,
              params={"desde": (ahora - timedelta(days=4)).isoformat()}).json()
    assert r["total"] == 4
    assert c.get(f"/api/zonas/{cont['id']}/conteo", headers=h,
                 params={"desde": (ahora - timedelta(days=60)).isoformat()}).status_code == 422
    with Session(engine) as s:
        assert s.exec(select(Event).where(Event.value == "conteo")).first() is not None


def test_dashboard_se_entera_de_cambios():
    c, h = _cliente()
    _camara(c, "cam-ws")
    with c.websocket_connect("/ws/alerts") as ws:
        _crear(c, h, camera_id="cam-ws", nombre="Nueva")
        mensaje = json.loads(ws.receive_text())
    assert mensaje == {"type": "zonas", "data": {"camera_id": "cam-ws"}}


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
