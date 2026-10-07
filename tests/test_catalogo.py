"""Pruebas del catalogo de camaras y del recomendador de instalacion.

    python tests/test_catalogo.py

No hace falta camara: una camara simulada en localhost responde ISAPI (con
Digest, como Hikvision) y RTSP DESCRIBE. Lo mas importante que se prueba es
que con una contrasena mala se hace UN solo intento y nada mas: Hikvision
bloquea la IP tras ~5.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

_TMP = Path(tempfile.mkdtemp())
from bd_prueba import borrar as _borrar_bd  # noqa: E402
from bd_prueba import url_temporal  # noqa: E402

os.environ["DATABASE_URL"] = url_temporal(_TMP, "catalogo")
os.environ["API_TOKEN"] = "token-de-prueba-del-worker"
os.environ["PERMITIR_INICIAR_WORKER"] = "false"

import warnings  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from api import catalogo_camaras as cat  # noqa: E402
from shared.instalacion import DatosInvalidos, alcance_m, recomendar  # noqa: E402

USUARIO, CLAVE = "admin", "Clave.Buena1"

DEVICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<DeviceInfo version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<deviceName>Acceso Norte</deviceName><deviceID>abc</deviceID>
<model>DS-2CD2143G2-I</model><serialNumber>DS-2CD2143G2-I20240101AAWRF12345</serialNumber>
<macAddress>28:57:be:11:22:33</macAddress><firmwareVersion>V5.7.3</firmwareVersion>
<firmwareReleasedDate>build 220112</firmwareReleasedDate><deviceType>IPCamera</deviceType>
</DeviceInfo>"""

CANALES = """<?xml version="1.0" encoding="UTF-8"?>
<StreamingChannelList version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<StreamingChannel version="2.0"><id>101</id><channelName>Camera 01</channelName><enabled>true</enabled>
<Video><enabled>true</enabled><videoInputChannelID>1</videoInputChannelID>
<videoCodecType>H.264</videoCodecType><videoResolutionWidth>2560</videoResolutionWidth>
<videoResolutionHeight>1440</videoResolutionHeight><vbrUpperCap>4096</vbrUpperCap>
<maxFrameRate>2000</maxFrameRate></Video></StreamingChannel>
<StreamingChannel version="2.0"><id>102</id><channelName>Camera 01</channelName><enabled>true</enabled>
<Video><enabled>true</enabled><videoInputChannelID>1</videoInputChannelID>
<videoCodecType>H.265</videoCodecType><videoResolutionWidth>1280</videoResolutionWidth>
<videoResolutionHeight>720</videoResolutionHeight><constantBitRate>1024</constantBitRate>
<maxFrameRate>2500</maxFrameRate></Video></StreamingChannel>
<StreamingChannel version="2.0"><id>103</id><enabled>true</enabled>
<Video><enabled>false</enabled><videoResolutionWidth>640</videoResolutionWidth></Video></StreamingChannel>
</StreamingChannelList>"""


# --------------------------------------------------------------------------
# Camara simulada
# --------------------------------------------------------------------------

def _digest_valido(cabecera: str, metodo: str, realm: str, nonce: str) -> bool:
    import re

    campos = dict(re.findall(r'(\w+)="([^"]*)"', cabecera))
    if campos.get("username") != USUARIO or campos.get("nonce") != nonce:
        return False
    ha1 = hashlib.md5(f"{USUARIO}:{realm}:{CLAVE}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{metodo}:{campos.get('uri')}".encode()).hexdigest()
    return campos.get("response") == hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()


class CamaraSimulada:
    """ISAPI por HTTP (Digest) y RTSP DESCRIBE, cada uno en su puerto. Cuenta
    los intentos con credenciales malas, que es lo que bloquea una Hikvision."""

    REALM = "DS-2CD2143G2-I"

    def __init__(self) -> None:
        self.fallidos = 0
        self.describes = 0
        self.nonce = secrets.token_hex(8)
        camara = self

        class Http(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                auth = self.headers.get("Authorization")
                if not auth:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate",
                                     f'Digest realm="{camara.REALM}", nonce="{camara.nonce}"')
                    self.end_headers()
                    return
                if not _digest_valido(auth, "GET", camara.REALM, camara.nonce):
                    camara.fallidos += 1
                    self.send_response(401)
                    self.send_header("WWW-Authenticate",
                                     f'Digest realm="{camara.REALM}", nonce="{camara.nonce}"')
                    self.end_headers()
                    return
                cuerpo = {"/ISAPI/System/deviceInfo": DEVICE_INFO,
                          "/ISAPI/Streaming/channels": CANALES}.get(self.path)
                if cuerpo is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                datos = cuerpo.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(datos)))
                self.end_headers()
                self.wfile.write(datos)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Http)
        self.puerto_http = self.http.server_address[1]
        self.rtsp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.rtsp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.rtsp.bind(("127.0.0.1", 0))
        self.rtsp.listen(8)
        self.puerto_rtsp = self.rtsp.getsockname()[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        threading.Thread(target=self._rtsp, daemon=True).start()

    def _rtsp(self) -> None:
        import re

        while True:
            try:
                con, _ = self.rtsp.accept()
            except OSError:
                return
            with con:
                datos = b""
                while b"\r\n\r\n" not in datos:
                    bloque = con.recv(4096)
                    if not bloque:
                        break
                    datos += bloque
                texto = datos.decode()
                if not texto.strip():
                    continue          # solo revisaron si el puerto estaba abierto
                self.describes += 1
                uri = texto.split(" ")[1]
                cseq = re.search(r"CSeq: (\d+)", texto).group(1)
                auth = re.search(r"Authorization: (.+)\r\n", texto)
                if not auth:
                    con.sendall((f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n"
                                 f'WWW-Authenticate: Digest realm="{self.REALM}", nonce="{self.nonce}"\r\n\r\n').encode())
                    continue
                if not _digest_valido(auth.group(1), "DESCRIBE", self.REALM, self.nonce):
                    self.fallidos += 1
                    con.sendall(f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n\r\n".encode())
                    continue
                ruta = uri.split(str(self.puerto_rtsp), 1)[1]
                if ruta not in ("/Streaming/Channels/101", "/Streaming/Channels/102"):
                    con.sendall(f"RTSP/1.0 404 Not Found\r\nCSeq: {cseq}\r\n\r\n".encode())
                    continue
                codec = "H264" if ruta.endswith("101") else "H265"
                sdp = f"v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 {codec}/90000\r\n"
                con.sendall((f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Type: application/sdp\r\n"
                             f"Content-Length: {len(sdp)}\r\n\r\n{sdp}").encode())

    def cerrar(self) -> None:
        self.http.shutdown()
        self.rtsp.close()


def _abrir_falso(fuente, timeout=8.0):
    """OpenCV no puede decodificar la camara simulada (no manda RTP): se
    simula la apertura con un frame del tamano del sub-stream."""
    return {"ancho": 1280, "alto": 720, "fps": 25.0, "codec": "H.265", "latencia_s": 0.4,
            "frame": np.zeros((720, 1280, 3), np.uint8)}


# --------------------------------------------------------------------------
# Recomendador de instalacion
# --------------------------------------------------------------------------

def test_alcance_coincide_con_la_tabla_medida():
    """La tabla de alcance del README sale de tools/calibrar_distancia.py; el
    recomendador usa la misma geometria y tiene que dar lo mismo."""
    casos = [(1280, 106.0, 4.1), (1280, 84.0, 6.0), (1280, 54.0, 10.6),
             (3200, 106.0, 10.2), (3200, 84.0, 15.1), (3200, 54.0, 26.6)]
    for ancho, hfov, esperado in casos:
        assert abs(alcance_m(ancho, hfov, 0.305, 36) - esperado) < 0.15, (ancho, hfov)


def test_lpr_lejos_sugiere_el_stream_principal():
    r = recomendar("lpr", 3.0, 6.0, 1280, lente_mm=4, ancho_px_principal=3200)
    placas = next(u for u in r["usos"] if u["uso"] == "placas")
    assert placas["estado"] == "no"
    assert any("principal" in a for a in r["advertencias"])
    assert r["detectores"]["ENABLE_PLATES"] and not r["detectores"]["ENABLE_WEAPONS"]


def test_lpr_bien_montada():
    r = recomendar("lpr", 2.6, 4.0, 1280, lente_mm=6)
    placas = next(u for u in r["usos"] if u["uso"] == "placas")
    assert placas["estado"] == "ok", placas
    assert r["geometria"]["angulo_vertical"] < 30


def test_angulo_vertical_excesivo_se_advierte():
    r = recomendar("lpr", 6.0, 2.0, 3200, lente_mm=6)
    assert r["geometria"]["angulo_vertical"] > 60
    assert any("Ángulo vertical" in a for a in r["advertencias"])


def test_armas_nunca_se_recomiendan():
    for funcion in ("lpr", "peatonal", "patio", "zona", "pasillo", "estacionamiento"):
        r = recomendar(funcion, 3.0, 5.0, 1920, lente_mm=4)
        assert next(u for u in r["usos"] if u["uso"] == "armas")["estado"] == "no"
        assert r["detectores"]["ENABLE_WEAPONS"] is False


def test_patio_lejano_limita_la_pose():
    r = recomendar("patio", 4.0, 15.0, 1280, lente_mm=2.8)
    estados = {u["uso"]: u["estado"] for u in r["usos"]}
    assert estados["movimiento"] == "ok" and estados["pose"] == "limite"
    assert r["dori"]["clave"] == "deteccion"


def test_lente_desconocido_pide_el_hfov():
    try:
        recomendar("lpr", 3, 5, 1280, lente_mm=12)
        raise AssertionError("debia pedir el campo de vision")
    except DatosInvalidos as e:
        assert "HFOV" in str(e)
    r = recomendar("lpr", 3, 5, 1280, lente_mm=12, hfov_grados=26)
    assert r["entrada"]["hfov_fuente"] == "instalador"


def test_h265_avisa_webrtc():
    r = recomendar("patio", 3, 5, 1280, lente_mm=4, codec="H.265")
    assert any("WebRTC" in a for a in r["advertencias"])


# --------------------------------------------------------------------------
# Piezas del descubrimiento
# --------------------------------------------------------------------------

def test_probe_match_de_onvif():
    xml = """<s:Envelope><s:Body><d:ProbeMatches><d:ProbeMatch>
      <d:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/name/HIKVISION
        onvif://www.onvif.org/hardware/DS-2CD1043G0-I onvif://www.onvif.org/location/city/hangzhou</d:Scopes>
      <d:XAddrs>http://192.168.1.64/onvif/device_service http://[fe80::1]/onvif/device_service</d:XAddrs>
      </d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"""
    m = cat.parsear_probe_match(xml)
    assert m["host"] == "192.168.1.64"
    assert m["nombre"] == "HIKVISION" and m["modelo"] == "DS-2CD1043G0-I"


def test_windows_wsd_no_es_camara():
    """REGRESION (red real): una PC con Windows contesto el sondeo sin tipo
    por WSD (puerto 5357) y salia como camara ONVIF."""
    xml = """<soap:Envelope><soap:Body><wsd:ProbeMatches><wsd:ProbeMatch>
      <wsd:Types>wsdp:Device pub:Computer</wsd:Types>
      <wsd:XAddrs>http://192.168.1.15:5357/3a9b0642-8ad9-43e0-91de-2c0db14043d9/</wsd:XAddrs>
      </wsd:ProbeMatch></wsd:ProbeMatches></soap:Body></soap:Envelope>"""
    assert cat.parsear_probe_match(xml) is None


def test_fabricante_por_mac():
    assert cat.fabricante_por_mac("28:57:be:aa:bb:cc") == ("Hikvision", False)
    assert cat.fabricante_por_mac("3c-ef-8c-00-11-22") == ("Dahua", False)
    # Las MAC aleatorias de telefonos y laptops (bit "administrada localmente").
    assert cat.fabricante_por_mac("6a:fa:8f:0c:c9:1e") == (None, True)
    assert cat.fabricante_por_mac(None) == (None, False)


def test_clasificacion_sin_credenciales():
    sin_rtsp = cat._clasificar("192.168.1.13", [80, 443], None, {"isapi": False}, "82:2c:96:42:68:c5", None)
    assert sin_rtsp["estado"] == "otro" and not sin_rtsp["es_camara"] and sin_rtsp["mac_aleatoria"]
    hik = cat._clasificar("192.168.1.64", [80, 554, 8000], None, {"isapi": True}, "c0:56:e3:00:00:01", None)
    assert hik["es_camara"] and hik["estado"] == "parcial"
    assert hik["fabricante"] == {"valor": "Hikvision", "fuente": "MAC"}
    ya = cat._clasificar("192.168.1.14", [554], None, {}, None, "cam-01")
    assert ya["estado"] == "registrada"


def test_canales_isapi():
    perfiles = cat.parsear_canales_isapi(CANALES)
    assert [p["clave"] for p in perfiles] == ["101", "102"]          # el 103 esta apagado
    principal, secundario = perfiles
    assert principal["ancho"] == 2560 and principal["fps"] == 20.0 and principal["codec"] == "H.264"
    assert secundario["bitrate_kbps"] == 1024 and secundario["fps"] == 25.0
    analisis, evidencia = cat._elegir_perfiles(perfiles)
    assert analisis["clave"] == "102" and evidencia["clave"] == "101"


def test_familias_del_catalogo():
    assert cat.familia_de("Hikvision", "DS-2CD2143G2-I")["clave"] == "hikvision-ip"
    assert cat.familia_de("Hikvision", "DS-7608NI-K2")["clave"] == "hikvision-nvr"
    assert cat.familia_de("Hikvision", "DS-2DE4425IW-DE")["clave"] == "hikvision-ptz"
    assert cat.familia_de("Hikvision", None)["clave"] == "hikvision"
    assert cat.familia_de("Dahua", "IPC-HFW2431S")["clave"] == "dahua"
    assert cat.familia_de("Axis", "P1375", onvif=True)["clave"] == "onvif"
    assert cat.familia_de(None, None)["clave"] == "rtsp"


def test_ws_security_digest():
    import base64

    nonce = b"0123456789abcdef"
    token = cat.token_ws_security("admin", "x", "2026-10-06T10:00:00Z", nonce)
    esperado = base64.b64encode(hashlib.sha1(nonce + b"2026-10-06T10:00:00Z" + b"x").digest()).decode()
    assert esperado in token and "PasswordDigest" in token


def test_codec_desde_sdp():
    assert cat.codec_de_sdp("m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000") == "H.264"
    assert cat.codec_de_sdp("a=rtpmap:96 H265/90000") == "H.265"
    assert cat.codec_de_sdp("") is None


def test_direcciones_fuera_de_la_lan_se_rechazan():
    assert cat.es_direccion_local("192.168.1.64")
    assert cat.es_direccion_local("127.0.0.1")
    assert not cat.es_direccion_local("8.8.8.8")


# --------------------------------------------------------------------------
# Diagnostico contra la camara simulada
# --------------------------------------------------------------------------

def test_describe_rtsp():
    cam = CamaraSimulada()
    try:
        ok = cat.describe_rtsp("127.0.0.1", cam.puerto_rtsp, "/Streaming/Channels/101", USUARIO, CLAVE)
        assert ok == {"codigo": 200, "codec": "H.264"}
        assert cat.describe_rtsp("127.0.0.1", cam.puerto_rtsp, "/no/existe", USUARIO, CLAVE)["codigo"] == 404
        assert cat.describe_rtsp("127.0.0.1", cam.puerto_rtsp, "/Streaming/Channels/101",
                                 USUARIO, "mala")["codigo"] == 401
        assert cam.fallidos == 1
    finally:
        cam.cerrar()


def test_diagnostico_identifica_la_camara():
    cam = CamaraSimulada()
    original = cat.abrir_stream
    cat.abrir_stream = _abrir_falso
    try:
        r = cat.diagnosticar("127.0.0.1", USUARIO, CLAVE, cam.puerto_rtsp, cam.puerto_http)
        assert r["estado"] == "identificada", r["motivo"]
        assert r["dispositivo"]["modelo"] == {"valor": "DS-2CD2143G2-I", "fuente": "ISAPI"}
        assert r["dispositivo"]["fabricante"]["valor"] == "Hikvision"
        assert r["familia"]["clave"] == "hikvision-ip"
        assert r["perfil_analisis"] == "102" and r["perfil_principal"] == "101"
        assert all(p["verificado"] for p in r["perfiles"])
        assert r["preview_b64"]
        # El secundario va en H.265: se avisa que el WebRTC no esta garantizado.
        assert any("H.265" in a for a in r["advertencias"])
        assert cam.fallidos == 0
    finally:
        cat.abrir_stream = original
        cam.cerrar()


def test_contrasena_mala_un_solo_intento():
    """REGLA DE SEGURIDAD: con credenciales malas no se prueba el video. Un
    solo intento fallido; cinco bloquearian la IP del servidor en la camara."""
    cam = CamaraSimulada()
    try:
        r = cat.diagnosticar("127.0.0.1", USUARIO, "mala", cam.puerto_rtsp, cam.puerto_http)
        assert r["estado"] == "parcial" and r["credenciales"] == "rechazadas"
        assert cam.fallidos == 1, cam.fallidos
        assert cam.describes == 0
    finally:
        cam.cerrar()


def test_identificar_de_probe_camara_un_solo_intento():
    """REGRESION: tools/probe_camara.identificar (el boton "Probar conexion")
    usaba el manejador Digest de urllib, que reintenta solo: con una
    contrasena mala hacia 6 intentos fallidos."""
    from tools import probe_camara

    cam = CamaraSimulada()
    original = cat.ClienteIsapi.__init__

    def con_puerto(self, host, user, password, puerto=80, timeout=5.0):
        original(self, host, user, password, cam.puerto_http, timeout)

    cat.ClienteIsapi.__init__ = con_puerto
    try:
        assert probe_camara.identificar("127.0.0.1", USUARIO, "mala") is False
        assert cam.fallidos == 1, cam.fallidos
        assert probe_camara.identificar("127.0.0.1", USUARIO, CLAVE) is True
        assert cam.fallidos == 1
    finally:
        cat.ClienteIsapi.__init__ = original
        cam.cerrar()


def test_sin_isapi_encuentra_la_ruta_por_rtsp():
    """Camara de otra marca: sin ISAPI ni ONVIF, las rutas se confirman con
    DESCRIBE (404 = no existe, sigue; 200 = esa es)."""
    cam = CamaraSimulada()
    original = cat.abrir_stream
    cat.abrir_stream = _abrir_falso
    puerto_cerrado = _puerto_libre()
    try:
        r = cat.diagnosticar("127.0.0.1", USUARIO, CLAVE, cam.puerto_rtsp, puerto_cerrado)
        assert r["estado"] == "parcial"          # video si, modelo no
        assert r["perfiles"][0]["ruta"] == "/Streaming/Channels/102"
        assert r["perfil_analisis"] == "/Streaming/Channels/102"
        assert cam.fallidos == 0
    finally:
        cat.abrir_stream = original
        cam.cerrar()


def _puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------
# API: alta manual y catalogo
# --------------------------------------------------------------------------

def _video_demo(destino: Path, frames: int = 12, fps: float = 50.0) -> Path:
    destino.parent.mkdir(parents=True, exist_ok=True)
    escritor = cv2.VideoWriter(str(destino), cv2.VideoWriter_fourcc(*"mp4v"), fps, (320, 240))
    for i in range(frames):
        cuadro = np.full((240, 320, 3), i * 10 % 255, np.uint8)
        escritor.write(cuadro)
    escritor.release()
    return destino


def _cliente():
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from api.database import engine, init_db
    from api.main import app
    from api.models import Operator
    from api.security import hash_password

    init_db()
    with Session(engine) as s:
        if s.get(Operator, 1) is None:
            s.add(Operator(username="admin", display_name="Admin",
                           password_hash=hash_password("clave-prueba"), role="admin"))
            s.commit()
    c = TestClient(app)
    token = c.post("/api/auth/login", json={"username": "admin", "password": "clave-prueba"}).json()["token"]
    return c, {"Authorization": f"Bearer {token}"}


def _proyecto_temporal():
    """Los .env de prueba se escriben en una carpeta temporal, nunca en el proyecto."""
    from api.routers import camera_setup
    from api.routers import catalogo as rc

    base = _TMP / "proyecto"
    base.mkdir(exist_ok=True)
    (base / ".env").write_text("API_URL=http://localhost:8000\nCAMERA_ID=cam-01\n"
                               "SOURCE=rtsp://u:p@192.168.1.64:554/Streaming/Channels/102\n"
                               "ENABLE_PLATES=true\n", encoding="utf-8")
    camera_setup.ENV_PATH = rc.ENV_PATH = base / ".env"
    camera_setup.BASE_DIR = rc.BASE_DIR = base
    return base


def test_api_recomendar():
    c, h = _cliente()
    r = c.post("/api/cameras/recommend", headers=h,
               json={"funcion": "lpr", "altura_m": 2.6, "distancia_m": 4, "ancho_px": 1280, "lente_mm": 6})
    assert r.status_code == 200 and r.json()["usos"][0]["estado"] == "ok"
    r = c.post("/api/cameras/recommend", headers=h,
               json={"funcion": "lpr", "altura_m": 2.5, "distancia_m": 3, "ancho_px": 1280, "lente_mm": 9})
    assert r.status_code == 422 and "HFOV" in r.json()["detail"]
    assert c.post("/api/cameras/recommend", json={}).status_code == 401


def test_api_alta_manual_con_video_de_demo():
    c, h = _cliente()
    base = _proyecto_temporal()
    _video_demo(base / "demo" / "videos" / "acceso.mp4")

    r = c.post("/api/cameras/probe", headers=h, json={"modo": "manual", "fuente": "file:demo/videos/acceso.mp4"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["estado"] == "manual" and d["perfiles"][0]["ancho"] == 320
    assert d["id_sugerido"] == "cam-02"

    # Fuera de demo/ o videos/ no se acepta (ni con ../).
    r = c.post("/api/cameras/probe", headers=h, json={"modo": "manual", "fuente": "file:../secreto.mp4"})
    assert r.status_code == 422

    r = c.post("/api/cameras/register", headers=h, json={
        "modo": "manual", "fuente": "file:demo/videos/acceso.mp4", "camera_id": "cam-02",
        "nombre": "Acceso de prueba", "ubicacion": "Stand", "funcion": "lpr",
        "detectores": {"ENABLE_PLATES": True, "ENABLE_MOTION": False},
        "ficha": {"estado": "manual", "perfil": {"ancho": 320, "alto": 240, "codec": "MP4V"}}})
    assert r.status_code == 201, r.text
    assert r.json()["archivo"] == ".env.cam-02"
    env = (base / ".env.cam-02").read_text(encoding="utf-8")
    assert "SOURCE=file:demo/videos/acceso.mp4" in env and "SOURCE_LOOP=true" in env
    assert "ENABLE_PLATES=true" in env and "ENABLE_WEAPONS=false" in env
    # El .env de la primera camara queda intacto.
    assert "cam-01" in (base / ".env").read_text(encoding="utf-8")

    cat_r = c.get("/api/cameras/catalog", headers=h).json()
    fila = next(x for x in cat_r["camaras"] if x["camera_id"] == "cam-02")
    assert fila["name"] == "Acceso de prueba" and fila["funcion_nombre"] == "LPR / acceso vehicular"
    assert fila["ficha"]["estado"] == "manual" and fila["archivo"] == ".env.cam-02"
    assert "file:demo/videos/acceso.mp4" in cat_r["videos_demo"]


def test_api_alta_de_red_y_credenciales_fuera_de_la_ficha():
    c, h = _cliente()
    base = _proyecto_temporal()
    r = c.post("/api/cameras/register", headers=h, json={
        "modo": "red", "host": "192.168.1.70", "user": "admin", "password": "S3creta@#",
        "ruta": "/Streaming/Channels/102", "canal_principal": "101", "isapi": True,
        "camera_id": "cam-03", "nombre": "Patio", "funcion": "patio",
        "detectores": {"ENABLE_MOTION": True, "ENABLE_POSE": True, "ENABLE_ZONAS": True},
        "ficha": {"estado": "identificada", "modelo": {"valor": "DS-2CD2143G2-I", "fuente": "ISAPI"}}})
    assert r.status_code == 201, r.text
    env = (base / ".env.cam-03").read_text(encoding="utf-8")
    # La contrasena va codificada en la URL (la @ y # rompian el RTSP).
    assert "SOURCE=rtsp://admin:S3creta%40%23@192.168.1.70:554/Streaming/Channels/102" in env
    assert "SNAPSHOT_HD_CHANNEL=101" in env and "ISAPI=auto" in env
    from sqlmodel import Session, select

    from api.database import engine
    from api.models import AuditLog, Camera
    with Session(engine) as s:
        camara = s.exec(select(Camera).where(Camera.camera_id == "cam-03")).one()
        assert "S3creta" not in (camara.ficha_json or "")
        bitacora = " ".join(e.detalle or "" for e in s.exec(select(AuditLog)).all())
        assert "S3creta" not in bitacora
    # Una IP publica no se acepta.
    r = c.post("/api/cameras/register", headers=h, json={
        "modo": "red", "host": "8.8.8.8", "password": "x", "ruta": "/a", "camera_id": "cam-09", "nombre": "x"})
    assert r.status_code == 422


def test_api_worker_respeta_el_permiso():
    c, h = _cliente()
    _proyecto_temporal()
    assert c.post("/api/cameras/cam-01/worker", headers=h).status_code == 403
    os.environ["PERMITIR_INICIAR_WORKER"] = "true"
    try:
        assert c.post("/api/cameras/cam-99/worker", headers=h).status_code == 404
        assert c.delete("/api/cameras/cam-01/worker", headers=h).status_code == 404
    finally:
        os.environ["PERMITIR_INICIAR_WORKER"] = "false"


# --------------------------------------------------------------------------
# Fuente de video en bucle (plan de contingencia)
# --------------------------------------------------------------------------

def test_video_en_bucle_a_velocidad_real():
    """REGRESION: al dar la vuelta, la fuente se dormia la duracion completa
    del video porque el contador de frames no volvia a cero con el reloj."""
    from edge.sources import open_source

    video = _video_demo(_TMP / "bucle.mp4", frames=10, fps=50.0)
    fuente = open_source(f"file:{video}", loop=True, realtime=True)
    t0 = time.monotonic()
    for _ in range(25):                        # dos vueltas y media
        assert fuente.read() is not None
    transcurrido = time.monotonic() - t0
    fuente.release()
    assert transcurrido < 0.9, transcurrido    # 25 frames a 50 fps = 0.5 s


def test_rtsp_ignora_opciones_de_archivo():
    """loop/realtime son de archivos: pasarselos a la fuente RTSP la rompia
    con TypeError al arrancar el worker."""
    import edge.sources as fuentes

    recibidos = {}

    class Falsa:
        def __init__(self, destino, **kwargs):
            recibidos.update(kwargs)

    original = fuentes.LiveSource
    fuentes.LiveSource = Falsa
    try:
        fuentes.open_source("rtsp://u:p@127.0.0.1:1/x", loop=True, realtime=True)
    finally:
        fuentes.LiveSource = original
    assert "loop" not in recibidos and "realtime" not in recibidos


# --------------------------------------------------------------------------

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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
        from api.database import engine

        engine.dispose()
        _borrar_bd(os.environ["DATABASE_URL"])
        shutil.rmtree(_TMP, ignore_errors=True)

    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
