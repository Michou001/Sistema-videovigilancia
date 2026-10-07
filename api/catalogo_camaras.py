"""Catalogo de camaras: encontrarlas, identificarlas y saber que se puede hacer con ellas.

Tres pasos, cada uno con lo minimo que necesita:

1. DESCUBRIR (sin credenciales). Se juntan tres pistas independientes:
     - ONVIF WS-Discovery: un mensaje multicast al que las camaras ONVIF
       responden con su direccion, marca y modelo.
     - Puertos abiertos: RTSP (554), web (80/443), SDK Hikvision (8000), SDK
       Dahua (37777)...
     - Huella sin credenciales: si /ISAPI/ responde 401 es una Hikvision (o
       compatible); el prefijo de la MAC dice el fabricante de la tarjeta.
   Ninguna de estas pruebas manda una contrasena, asi que no puede bloquear
   una camara por intentos fallidos.

2. DIAGNOSTICAR (con credenciales). Las credenciales se prueban UNA vez, por
   ISAPI o por ONVIF, antes de tocar el video: Hikvision bloquea la IP tras
   ~5 intentos fallidos. Despues se leen los streams que la camara reporta
   (codec, resolucion, fps) y se confirman con una peticion RTSP DESCRIBE,
   que distingue "ruta inexistente" (404) de "contrasena mala" (401) sin abrir
   el video. Solo el stream elegido se abre de verdad, para la vista previa.

3. CATALOGO por familia. Lo que GOSS sabe hacer con cada familia (rutas,
   eventos que integra, mañas conocidas). No se inventan especificaciones: cada
   dato va con su fuente -- lo que reporto la camara, lo que capturo el
   instalador o lo que es una regla de GOSS.

Estados de identificacion:
    identificada  marca y modelo leidos de la camara y al menos un stream confirmado
    parcial       se encontro el equipo pero faltan credenciales o capacidades
    manual        no respondio al descubrimiento: se da de alta por IP/RTSP a mano
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import ipaddress
import logging
import os
import re
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from html import unescape
from typing import Optional
from urllib.parse import quote, unquote, urlparse

log = logging.getLogger(__name__)

PUERTOS = {
    554: "RTSP",
    80: "Web",
    443: "HTTPS",
    8000: "SDK Hikvision",
    37777: "SDK Dahua",
    34567: "SDK XMEye",
    8554: "RTSP alterno",
    8899: "ONVIF",
    2020: "ONVIF",
}
PUERTOS_CAMARA = {554, 8000, 37777, 34567, 8554, 8899, 2020}

# Prefijos MAC (OUI) registrados ante la IEEE. Solo es una pista: dice quien
# fabrico la tarjeta de red, no el modelo.
OUI = {
    "Hikvision": {
        "00BC99", "040312", "04EECD", "083BC1", "085411", "08A189", "08CC81",
        "0C75D2", "1012FB", "1868CB", "188025", "240F9B", "2428FD", "2432AE", "244845",
        "2857BE", "2CA59C", "340962", "3C1BF8", "40ACBF", "40B570", "4419B6", "4447CC",
        "44A642", "48785B", "4C1F86", "4C62DF", "4CBD8F", "4CF5DC", "50E538", "548C81",
        "54C415", "5803FB", "5850ED", "5C345B", "64DB8B", "686DBC", "743FC2", "80489F",
        "807C62", "80BEAF", "80F5AE", "849459", "849A40", "88DE39", "8C22D2", "8CE748",
        "94E1AC", "988B0A", "989DE5", "98DF82", "98F112", "A0FF0C", "A41437",
        "A42902", "A44BD9", "A4A459", "A4D5C2", "ACB92F", "ACCB51", "B0FF0D", "B4A382",
        "BC5E33", "BC9B5E", "BCAD28", "BCBAC2", "C0517E", "C056E3", "C06DED", "C42F90",
        "C8A702", "CC13F3", "D4E853", "DC07F8", "DCD26A", "E0BAAD", "E0CA3C", "E0DF13",
        "E4D58B", "E8A0ED", "ECA971", "ECC89C", "F84DFC", "FC9FFD",
    },
    "Dahua": {
        "08EDED", "14A78B", "24526A", "38AF29", "3CE36B", "3CEF8C", "4C11BF", "5CF51A",
        "64FD29", "6C1C71", "74C929", "8CE9B4", "9002A9", "98F9CC", "9C1463", "A0BD1D",
        "B44C3B", "BC325F", "C0395A", "C4AAC4", "D4430E", "E02EFE", "E0508B", "E4246C",
        "F4B1C2", "FC5F49", "FCB69D",
    },
}

# --------------------------------------------------------------------------
# Catalogo por familia: lo que GOSS sabe hacer con cada una
# --------------------------------------------------------------------------

FAMILIAS = (
    {
        "clave": "hikvision-nvr", "fabricante": "Hikvision",
        "patron": r"^i?DS-(7|8|9)\d|^ERI-",
        "nombre": "Grabador NVR/DVR Hikvision",
        "rutas": {"principal": "/Streaming/Channels/{canal}01", "secundario": "/Streaming/Channels/{canal}02"},
        "capacidades": [
            "Un canal por cámara: 101, 201, 301… (principal) y 102, 202… (secundario).",
            "Eventos ISAPI de todos los canales; GOSS se queda con los del canal de la cámara.",
        ],
        "notas": ["Bloquea la IP tras ~5 contraseñas incorrectas."],
        "isapi": True,
    },
    {
        "clave": "hikvision-ptz", "fabricante": "Hikvision",
        "patron": r"^i?DS-2(DE|DF|SE|DY|TD)",
        "nombre": "Cámara PTZ Hikvision",
        "rutas": {"principal": "/Streaming/Channels/101", "secundario": "/Streaming/Channels/102"},
        "capacidades": [
            "Video por RTSP: principal 101 y secundario 102.",
            "Eventos ISAPI: sabotaje, pérdida de video, cruce de línea e intrusión.",
            "Foto en alta resolución del canal principal para la evidencia.",
        ],
        "notas": [
            "Si la cámara gira, las zonas dibujadas dejan de coincidir: para zonas y placas "
            "usar una posición fija (preset) y desactivar los recorridos.",
            "Bloquea la IP tras ~5 contraseñas incorrectas.",
        ],
        "isapi": True,
    },
    {
        "clave": "hikvision-ip", "fabricante": "Hikvision",
        "patron": r"^i?DS-2(CD|XS|CV|XM)",
        "nombre": "Cámara IP Hikvision",
        "rutas": {"principal": "/Streaming/Channels/101", "secundario": "/Streaming/Channels/102"},
        "capacidades": [
            "Video por RTSP: principal 101 (evidencia) y secundario 102 (análisis).",
            "Eventos ISAPI: sabotaje, pérdida de video, cruce de línea e intrusión.",
            "Foto en alta resolución del canal principal para la evidencia.",
        ],
        "notas": ["Bloquea la IP tras ~5 contraseñas incorrectas: GOSS prueba la contraseña "
                  "una sola vez antes de tocar el video."],
        "isapi": True,
    },
    {
        "clave": "hikvision", "fabricante": "Hikvision", "patron": None,
        "nombre": "Equipo Hikvision",
        "rutas": {"principal": "/Streaming/Channels/101", "secundario": "/Streaming/Channels/102"},
        "capacidades": ["Video por RTSP y eventos ISAPI (si el equipo los expone)."],
        "notas": ["Modelo sin familia conocida en el catálogo: confirmar capacidades en sitio."],
        "isapi": True,
    },
    {
        "clave": "dahua", "fabricante": "Dahua", "patron": None,
        "nombre": "Cámara Dahua",
        "rutas": {"principal": "/cam/realmonitor?channel=1&subtype=0",
                  "secundario": "/cam/realmonitor?channel=1&subtype=1"},
        "capacidades": ["Video por RTSP: subtype=0 (principal) y subtype=1 (secundario)."],
        "notas": ["Los eventos propios de la cámara no están integrados: GOSS analiza el video."],
        "isapi": False,
    },
    {
        "clave": "onvif", "fabricante": None, "patron": None,
        "nombre": "Cámara ONVIF",
        "rutas": {},
        "capacidades": ["Ruta de video obtenida por ONVIF (GetStreamUri), sin adivinar."],
        "notas": ["Los eventos propios de la cámara no están integrados: GOSS analiza el video."],
        "isapi": False,
    },
    {
        "clave": "rtsp", "fabricante": None, "patron": None,
        "nombre": "Fuente RTSP (configuración manual)",
        "rutas": {},
        "capacidades": ["Solo video: sin identificación de marca ni eventos de la cámara."],
        "notas": [],
        "isapi": False,
    },
)


def familia_de(fabricante: Optional[str], modelo: Optional[str], onvif: bool = False) -> dict:
    """La familia del catalogo que corresponde. Nunca falla: lo desconocido
    cae en 'onvif' o 'rtsp'."""
    marca = (fabricante or "").lower()
    for f in FAMILIAS:
        if f["fabricante"] and f["fabricante"].lower() not in marca:
            continue
        if f["fabricante"] and f["patron"] and not (modelo and re.search(f["patron"], modelo, re.I)):
            continue
        if f["fabricante"]:
            return f
    return next(f for f in FAMILIAS if f["clave"] == ("onvif" if onvif else "rtsp"))


def _sin_patron(f: dict) -> dict:
    return {k: v for k, v in f.items() if k != "patron"}


# --------------------------------------------------------------------------
# Utilidades de red
# --------------------------------------------------------------------------

def ip_local() -> Optional[str]:
    """IP de esta maquina en la LAN (sin mandar trafico real)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


# Adaptadores que no llevan a ninguna camara: maquinas virtuales, WSL, VPN.
_VIRTUALES = ("vethernet", "wsl", "hyper-v", "docker", "virtualbox", "vmware", "loopback",
              "bluetooth", "vpn", "tailscale", "zerotier", "veth", "br-", "virbr")


def interfaces_locales() -> list[tuple[str, str, ipaddress.IPv4Network]]:
    """(nombre, ip, red) de cada tarjeta encendida con IPv4 real.

    En la sede de la demo la camara va por cable a la laptop y la laptop puede
    estar ademas en el Wi-Fi: hay que buscar en las dos redes, no solo en la
    que tiene salida a internet. Y sin internet tiene que funcionar igual."""
    try:
        import psutil
    except ImportError:
        local = ip_local()
        return [("red", local, ipaddress.ip_network(f"{local}/24", strict=False))] if local else []
    salida = []
    estados = psutil.net_if_stats()
    for nombre, direcciones in psutil.net_if_addrs().items():
        st = estados.get(nombre)
        if st is None or not st.isup or any(v in nombre.lower() for v in _VIRTUALES):
            continue
        for d in direcciones:
            if d.family != socket.AF_INET or not d.netmask:
                continue
            ip = ipaddress.ip_address(d.address)
            if ip.is_loopback or ip.is_link_local:
                continue
            salida.append((nombre, d.address, ipaddress.ip_network(f"{d.address}/{d.netmask}", strict=False)))
    return salida


def es_direccion_local(host: str) -> bool:
    """IP privada, loopback, o de una red conectada directamente a este equipo.

    Diagnosticar manda credenciales y hace peticiones desde el servidor:
    limitarlo a la red local evita que el endpoint sirva para escanear o
    autenticarse contra equipos de internet. "Red directa" cubre redes como la
    de la UAEMex, que usa direcciones publicas (148.215.x.x) adentro del campus."""
    if os.getenv("CAMARAS_FUERA_DE_LAN", "").lower() in ("1", "true", "si", "yes"):
        return True
    try:
        direcciones = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    redes = [red for _, _, red in interfaces_locales()]
    for d in direcciones:
        ip = ipaddress.ip_address(d.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            continue
        if not any(ip in red for red in redes):
            return False
    return bool(direcciones)


def puerto_abierto(host: str, puerto: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            return s.connect_ex((host, puerto)) == 0
        except OSError:
            return False


def tabla_arp() -> dict[str, str]:
    """IP -> MAC de la tabla ARP del sistema (se llena sola al escanear)."""
    comandos = (["arp", "-a"], ["ip", "neigh"])
    for comando in comandos:
        try:
            salida = subprocess.run(comando, capture_output=True, text=True, timeout=5,
                                    errors="ignore").stdout
        except (OSError, subprocess.SubprocessError):
            continue
        tabla = {}
        for ip, mac in re.findall(
                r"(\d+\.\d+\.\d+\.\d+)[^\n]*?((?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2})", salida):
            mac = mac.replace("-", ":").lower()
            if mac not in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"):
                tabla[ip] = mac
        if tabla:
            return tabla
    return {}


def fabricante_por_mac(mac: Optional[str]) -> tuple[Optional[str], bool]:
    """(fabricante, mac_aleatoria). Una MAC 'administrada localmente' (bit 1
    del primer byte) la inventa el sistema operativo: telefonos y laptops con
    privacidad de MAC. Ninguna camara la usa."""
    if not mac:
        return None, False
    limpia = re.sub(r"[^0-9a-f]", "", mac.lower())
    if len(limpia) != 12:
        return None, False
    aleatoria = bool(int(limpia[:2], 16) & 0x02)
    prefijo = limpia[:6].upper()
    for marca, prefijos in OUI.items():
        if prefijo in prefijos:
            return marca, aleatoria
    return None, aleatoria


# --------------------------------------------------------------------------
# ONVIF WS-Discovery
# --------------------------------------------------------------------------

_PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header><w:MessageID>uuid:{id}</w:MessageID>
<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
</e:Header><e:Body><d:Probe>{tipos}</d:Probe></e:Body></e:Envelope>"""


def _texto_tag(xml: str, nombre: str) -> Optional[str]:
    """Contenido de <x:nombre> con cualquier prefijo de espacio de nombres.
    Con expresiones regulares, como edge/isapi.py: el XML viene de un equipo
    de la red y solo hacen falta unas cuantas etiquetas planas."""
    m = re.search(rf"<(?:[\w.-]+:)?{nombre}\b[^>]*>\s*([^<]*?)\s*</(?:[\w.-]+:)?{nombre}>", xml, re.S)
    return unescape(m.group(1)) if m else None


def parsear_probe_match(xml: str) -> Optional[dict]:
    """XAddrs y Scopes de una respuesta ProbeMatch de WS-Discovery.

    Solo cuenta si es ONVIF de verdad: al sondeo sin tipo (el que necesitan
    algunos grabadores) tambien contestan las computadoras con Windows y las
    impresoras (WSD, puerto 5357). Medido en la red de pruebas: una laptop
    aparecia como "camara ONVIF" sin marca ni puertos."""
    xaddrs = (_texto_tag(xml, "XAddrs") or "").split()
    scopes = (_texto_tag(xml, "Scopes") or "").split()
    tipos = _texto_tag(xml, "Types") or ""
    es_onvif = ("NetworkVideoTransmitter" in tipos or "onvif" in tipos.lower()
                or any(s.startswith("onvif://www.onvif.org") for s in scopes))
    if not xaddrs or not es_onvif:
        return None
    datos: dict = {"xaddrs": xaddrs, "nombre": None, "modelo": None, "ubicacion": None, "mac": None}
    for s in scopes:
        m = re.match(r"onvif://www\.onvif\.org/(name|hardware|location|MAC|mac)/(.+)", s)
        if not m:
            continue
        clave = {"name": "nombre", "hardware": "modelo", "location": "ubicacion"}.get(m.group(1), "mac")
        datos[clave] = unquote(m.group(2)).replace("_", " ").strip() or None
    host = urlparse(xaddrs[0]).hostname
    datos["host"] = host
    return datos


def ws_discovery(espera: float = 2.5, ips_locales: Optional[list[str]] = None) -> dict[str, dict]:
    """IP -> datos de las camaras ONVIF que respondieron. El sondeo sale por
    cada tarjeta: con Wi-Fi y cable a la vez, el sistema operativo solo lo
    mandaria por una."""
    encontrados: dict[str, dict] = {}
    sockets = []
    for local in ips_locales or [ip_local()]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            if local:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local))
            s.settimeout(0.1)
            # Con tipo (lo estandar) y sin tipo (grabadores que solo responden asi).
            for tipos in ("<d:Types>dn:NetworkVideoTransmitter</d:Types>", ""):
                mensaje = _PROBE.format(id=uuid.uuid4(), tipos=tipos).encode()
                s.sendto(mensaje, ("239.255.255.250", 3702))
            sockets.append(s)
        except OSError as e:
            log.info("WS-Discovery no disponible por %s: %s", local, e)
            s.close()
    try:
        fin = time.monotonic() + espera
        while sockets and time.monotonic() < fin:
            for s in sockets:
                try:
                    datos, origen = s.recvfrom(65535)
                except (socket.timeout, ConnectionResetError, OSError):
                    continue
                match = parsear_probe_match(datos.decode("utf-8", "ignore"))
                if match:
                    match["host"] = match.get("host") or origen[0]
                    encontrados[origen[0]] = match
    finally:
        for s in sockets:
            s.close()
    return encontrados


# --------------------------------------------------------------------------
# Huella HTTP sin credenciales
# --------------------------------------------------------------------------

def _get_sin_credenciales(url: str, timeout: float = 2.0) -> tuple[Optional[int], dict, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(4096).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), ""
    except Exception:  # noqa: BLE001 - cualquier fallo es "no responde"
        return None, {}, ""


def huella_http(host: str, puerto: int = 80) -> dict:
    """Que dice el equipo sin credenciales. Hikvision responde 401 en /ISAPI/
    (el servicio existe y pide usuario); un equipo sin ISAPI da 404."""
    base = f"http://{host}" if puerto == 80 else f"http://{host}:{puerto}"
    codigo, cabeceras, cuerpo = _get_sin_credenciales(base + "/")
    servidor = cabeceras.get("Server") or cabeceras.get("server")
    titulo = None
    m = re.search(r"<title>\s*([^<]{1,80})\s*</title>", cuerpo, re.I)
    if m:
        titulo = unescape(m.group(1)).strip()
    isapi, _, _ = _get_sin_credenciales(base + "/ISAPI/System/deviceInfo")
    dahua, _, _ = _get_sin_credenciales(base + "/cgi-bin/magicBox.cgi?action=getDeviceType")
    return {
        "responde": codigo is not None,
        "servidor": servidor,
        "titulo": titulo,
        "isapi": isapi in (200, 401),
        "dahua_cgi": dahua == 401,
    }


# --------------------------------------------------------------------------
# Descubrimiento
# --------------------------------------------------------------------------

def descubrir(subred: Optional[str] = None, registradas: Optional[dict[str, str]] = None) -> dict:
    """Busca equipos en la LAN /24 combinando WS-Discovery, puertos y huellas.

    `registradas` es host -> camera_id de las que ya estan dadas de alta, para
    marcarlas en vez de ofrecerlas otra vez."""
    t0 = time.monotonic()
    registradas = registradas or {}
    interfaces = interfaces_locales()
    propias = {ip for _, ip, _ in interfaces}
    if subred:
        redes = [ipaddress.ip_network(subred, strict=False)]
        if redes[0].num_addresses > 1024:
            raise ValueError("La red es demasiado grande: usa una /22 o más chica.")
    else:
        # El /24 de cada tarjeta: una red /16 de la universidad son 65 mil
        # direcciones; el /24 donde esta la laptop es donde suele estar la camara.
        redes = list(dict.fromkeys(ipaddress.ip_network(f"{ip}/24", strict=False) for _, ip, _ in interfaces))[:4]
        if not redes:
            raise RuntimeError("Este equipo no tiene ninguna red conectada (ni cable ni Wi-Fi).")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as unico:
        onvif_futuro = unico.submit(ws_discovery, 2.5, sorted(propias) or None)

        pares = [(str(ip), p) for red in redes for ip in red.hosts() if str(ip) not in propias
                 for p in PUERTOS]
        abiertos: dict[str, list[int]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=256) as pool:
            for (ip, p), ok in zip(pares, pool.map(lambda par: puerto_abierto(*par), pares)):
                if ok:
                    abiertos.setdefault(ip, []).append(p)
        onvif = onvif_futuro.result()

    for ip in onvif:
        abiertos.setdefault(ip, [])
    arp = tabla_arp()

    def _ficha(ip: str) -> dict:
        puertos = sorted(abiertos[ip])
        web = next((p for p in (80, 8080) if p in puertos), None)
        huella = huella_http(ip, web) if web else {"responde": False, "isapi": False, "dahua_cgi": False}
        return _clasificar(ip, puertos, onvif.get(ip), huella, arp.get(ip), registradas.get(ip))

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        dispositivos = list(pool.map(_ficha, sorted(abiertos, key=ipaddress.ip_address)))

    dispositivos.sort(key=lambda d: (not d["es_camara"], ipaddress.ip_address(d["host"])))
    return {
        "subred": ", ".join(str(r) for r in redes),
        "redes": [{"interfaz": n, "ip": ip, "red": str(r)} for n, ip, r in interfaces],
        "ip_local": ", ".join(sorted(propias)),
        "duracion_s": round(time.monotonic() - t0, 1),
        "onvif_respondieron": len(onvif),
        "dispositivos": dispositivos,
    }


def _dato(valor, fuente):
    return {"valor": valor, "fuente": fuente} if valor else None


def _clasificar(ip: str, puertos: list[int], onvif: Optional[dict], huella: dict,
                mac: Optional[str], registrada: Optional[str]) -> dict:
    marca_mac, aleatoria = fabricante_por_mac(mac or (onvif or {}).get("mac"))

    fabricante = None
    if onvif and onvif.get("nombre"):
        nombre = onvif["nombre"]
        for marca in OUI:
            if marca.lower() in nombre.lower():
                fabricante = _dato(marca, "ONVIF")
        fabricante = fabricante or _dato(nombre, "ONVIF")
    if fabricante is None and marca_mac:
        fabricante = _dato(marca_mac, "MAC")
    if fabricante is None and huella.get("isapi") and 8000 in puertos:
        fabricante = _dato("Hikvision", "puertos ISAPI + SDK")
    if fabricante is None and (huella.get("dahua_cgi") or 37777 in puertos):
        fabricante = _dato("Dahua", "puertos")
    modelo = _dato((onvif or {}).get("modelo"), "ONVIF")

    es_camara = bool(onvif or huella.get("isapi") or huella.get("dahua_cgi")
                     or (set(puertos) & PUERTOS_CAMARA))
    protocolos = {
        "rtsp": 554 in puertos or 8554 in puertos,
        "onvif": bool(onvif),
        "isapi": bool(huella.get("isapi")),
        "web": bool(set(puertos) & {80, 443, 8080}),
    }

    if registrada:
        estado, motivo = "registrada", f"Ya está dada de alta como {registrada}."
    elif not es_camara:
        estado = "otro"
        motivo = ("MAC aleatoria: es un teléfono o una computadora." if aleatoria
                  else "Sin RTSP ni ONVIF: probablemente no es una cámara (router, impresora…).")
    elif not protocolos["rtsp"] and not onvif:
        estado = "parcial"
        motivo = "Parece cámara pero el RTSP (554) está cerrado: actívalo en la cámara."
    else:
        estado = "parcial"
        motivo = "Faltan credenciales para leer el modelo y los streams."

    return {
        "host": ip,
        "mac": mac,
        "mac_aleatoria": aleatoria,
        "fabricante": fabricante,
        "modelo": modelo,
        "nombre": _dato((onvif or {}).get("nombre"), "ONVIF"),
        "servidor_web": huella.get("servidor"),
        "puertos": [{"puerto": p, "etiqueta": PUERTOS.get(p, str(p))} for p in puertos],
        "protocolos": protocolos,
        "onvif_xaddr": (onvif or {}).get("xaddrs", [None])[0],
        "es_camara": es_camara,
        "estado": estado,
        "motivo": motivo,
        "registrada": registrada,
    }


# --------------------------------------------------------------------------
# ISAPI (Hikvision) con credenciales
# --------------------------------------------------------------------------

class CredencialesRechazadas(Exception):
    """La camara dijo 401 con usuario y contrasena: no se reintenta."""


def autorizacion_http(reto: str, user: str, password: str, metodo: str, uri: str) -> Optional[str]:
    """Cabecera Authorization para un reto WWW-Authenticate (Digest o Basic).

    Se arma a mano y no con HTTPDigestAuthHandler de urllib porque ese
    manejador, ante un 401, REINTENTA solo hasta 5 veces con la misma
    contrasena: medido contra la camara simulada, una contrasena mala eran 6
    intentos fallidos, justo lo que bloquea una Hikvision."""
    if reto.lower().startswith("digest"):
        campos = dict(re.findall(r'(\w+)="?([^",]*)"?', reto[6:]))
        realm, nonce = campos.get("realm", ""), campos.get("nonce", "")
        qop = "auth" if "auth" in campos.get("qop", "").split(",") else None
        ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{metodo}:{uri}".encode()).hexdigest()
        partes = [f'username="{user}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
        if qop:
            cnonce, nc = secrets.token_hex(8), "00000001"
            resp = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
            partes += [f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"']
        else:
            resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        partes.append(f'response="{resp}"')
        if campos.get("opaque"):
            partes.append(f'opaque="{campos["opaque"]}"')
        return "Digest " + ", ".join(partes)
    if reto.lower().startswith("basic"):
        return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    return None


class ClienteIsapi:
    def __init__(self, host: str, user: str, password: str, puerto: int = 80,
                 timeout: float = 5.0) -> None:
        self.base = f"http://{host}" if puerto == 80 else f"http://{host}:{puerto}"
        self.user, self.password = user, password
        self.timeout = timeout

    def _abrir(self, ruta: str, autorizacion: Optional[str] = None):
        cabeceras = {"Authorization": autorizacion} if autorizacion else {}
        peticion = urllib.request.Request(self.base + ruta, headers=cabeceras)
        return urllib.request.urlopen(peticion, timeout=self.timeout)

    def get(self, ruta: str) -> Optional[str]:
        """Texto de la respuesta, None si el recurso no existe. Lanza
        CredencialesRechazadas si la camara dice 401 a la peticion que SI
        llevaba credenciales. A lo mucho un intento con contrasena por
        llamada: el primer 401 (sin credenciales) solo trae el reto."""
        try:
            with self._abrir(ruta) as r:
                return r.read(256 * 1024).decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code != 401:
                return None
            reto = e.headers.get("WWW-Authenticate") or ""
        autorizacion = autorizacion_http(reto, self.user, self.password, "GET", ruta)
        if autorizacion is None:
            return None
        try:
            with self._abrir(ruta, autorizacion) as r:
                return r.read(256 * 1024).decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise CredencialesRechazadas() from None
            return None


def parsear_device_info(xml: str) -> dict:
    return {
        "nombre": _texto_tag(xml, "deviceName"),
        "modelo": _texto_tag(xml, "model"),
        "serie": _texto_tag(xml, "serialNumber"),
        "firmware": " ".join(x for x in (_texto_tag(xml, "firmwareVersion"),
                                         _texto_tag(xml, "firmwareReleasedDate")) if x) or None,
        "mac": _texto_tag(xml, "macAddress"),
        "tipo": _texto_tag(xml, "deviceType"),
    }


def parsear_canales_isapi(xml: str) -> list[dict]:
    """Un perfil por <StreamingChannel>. maxFrameRate viene en centesimas
    (2500 = 25 fps); el bitrate en kbps."""
    perfiles = []
    for bloque in re.findall(r"<StreamingChannel\b.*?</StreamingChannel>", xml, re.S):
        canal = _texto_tag(bloque, "id")
        if not canal or not canal.isdigit():
            continue
        video = re.search(r"<Video\b.*?</Video>", bloque, re.S)
        v = video.group(0) if video else bloque
        if (_texto_tag(v, "enabled") or "true").lower() == "false":
            continue

        def entero(tag, texto=v):
            t = _texto_tag(texto, tag)
            return int(t) if t and t.isdigit() else None

        fps = entero("maxFrameRate")
        bitrate = entero("vbrUpperCap") or entero("constantBitRate")
        sufijo = int(canal) % 100
        perfiles.append({
            "clave": canal,
            "nombre": {1: "Principal", 2: "Secundario", 3: "Tercer stream"}.get(sufijo, f"Canal {canal}"),
            "ruta": f"/Streaming/Channels/{canal}",
            "codec": _texto_tag(v, "videoCodecType"),
            "ancho": entero("videoResolutionWidth"),
            "alto": entero("videoResolutionHeight"),
            "fps": round(fps / 100, 1) if fps else None,
            "bitrate_kbps": bitrate,
            "fuente": "ISAPI",
            "verificado": False,
        })
    return perfiles


# --------------------------------------------------------------------------
# ONVIF con credenciales (SOAP minimo, sin dependencias)
# --------------------------------------------------------------------------

_SOBRE = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
 xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
 xmlns:tt="http://www.onvif.org/ver10/schema">
<s:Header>{seguridad}</s:Header><s:Body>{cuerpo}</s:Body></s:Envelope>"""

_SEGURIDAD = """<Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
<UsernameToken><Username>{user}</Username>
<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>
<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce}</Nonce>
<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{creado}</Created>
</UsernameToken></Security>"""


def token_ws_security(user: str, password: str, creado: str, nonce: bytes) -> str:
    """PasswordDigest = Base64(SHA1(nonce + creado + contrasena)), WS-Security 1.0."""
    digest = base64.b64encode(hashlib.sha1(nonce + creado.encode() + password.encode()).digest()).decode()
    return _SEGURIDAD.format(user=_xml(user), digest=digest, nonce=base64.b64encode(nonce).decode(),
                             creado=creado)


def _xml(texto: str) -> str:
    return (texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


class ClienteOnvif:
    def __init__(self, xaddr: str, user: str, password: str, timeout: float = 5.0) -> None:
        self.xaddr = xaddr
        self.user, self.password = user, password
        self.timeout = timeout
        self.desfase = 0.0   # reloj de la camara - reloj local, en segundos

    def _enviar(self, url: str, cuerpo: str, autenticar: bool) -> tuple[int, str]:
        """(codigo HTTP, texto). Codigo 0 si no hubo respuesta."""
        seguridad = ""
        if autenticar:
            creado = datetime.fromtimestamp(time.time() + self.desfase, timezone.utc)
            seguridad = token_ws_security(self.user, self.password,
                                          creado.strftime("%Y-%m-%dT%H:%M:%SZ"), secrets.token_bytes(16))
        datos = _SOBRE.format(seguridad=seguridad, cuerpo=cuerpo).encode()
        peticion = urllib.request.Request(url, data=datos, headers={
            "Content-Type": "application/soap+xml; charset=utf-8"})
        try:
            with urllib.request.urlopen(peticion, timeout=self.timeout) as r:
                return r.status, r.read(512 * 1024).decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            try:
                texto = e.read(64 * 1024).decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                texto = ""
            return e.code, texto
        except Exception:  # noqa: BLE001
            return 0, ""

    def _post(self, url: str, cuerpo: str) -> Optional[str]:
        codigo, texto = self._enviar(url, cuerpo, autenticar=True)
        if codigo == 401 or re.search(r"NotAuthorized", texto, re.I):
            raise CredencialesRechazadas()
        return texto if codigo == 200 else None

    def sincronizar_reloj(self) -> bool:
        """GetSystemDateAndTime no pide credenciales. Sirve para saber si hay
        ONVIF y para firmar con la hora de la camara: muchas rechazan un token
        con varios minutos de diferencia."""
        codigo, r = self._enviar(self.xaddr, "<tds:GetSystemDateAndTime/>", autenticar=False)
        if codigo in (400, 401) and "Envelope" in r:
            return True   # hay ONVIF, solo que pide credenciales hasta para la hora
        if codigo != 200:
            return False
        utc = re.search(r"UTCDateTime\b.*?</(?:\w+:)?UTCDateTime>", r, re.S)
        if utc:
            b = utc.group(0)
            try:
                partes = [int(_texto_tag(b, t) or 0) for t in ("Year", "Month", "Day", "Hour", "Minute", "Second")]
                camara = datetime(*partes, tzinfo=timezone.utc).timestamp()
                self.desfase = camara - time.time()
            except ValueError:
                pass
        return True

    def informacion(self) -> dict:
        r = self._post(self.xaddr, "<tds:GetDeviceInformation/>") or ""
        return {
            "fabricante": _texto_tag(r, "Manufacturer"),
            "modelo": _texto_tag(r, "Model"),
            "firmware": _texto_tag(r, "FirmwareVersion"),
            "serie": _texto_tag(r, "SerialNumber"),
        }

    def url_media(self) -> str:
        r = self._post(self.xaddr, "<tds:GetCapabilities><tds:Category>Media</tds:Category>"
                                   "</tds:GetCapabilities>") or ""
        media = re.search(r"<(?:\w+:)?Media\b.*?</(?:\w+:)?Media>", r, re.S)
        return (_texto_tag(media.group(0), "XAddr") if media else None) or self.xaddr

    def perfiles(self, maximo: int = 4) -> list[dict]:
        media = self.url_media()
        r = self._post(media, "<trt:GetProfiles/>") or ""
        salida = []
        for token, cuerpo in re.findall(
                r"<(?:\w+:)?Profiles\b[^>]*\btoken=\"([^\"]+)\"[^>]*>(.*?)</(?:\w+:)?Profiles>", r, re.S)[:maximo]:
            enc = re.search(r"<(?:\w+:)?VideoEncoderConfiguration\b.*?</(?:\w+:)?VideoEncoderConfiguration>",
                            cuerpo, re.S)
            e = enc.group(0) if enc else ""

            def entero(tag, texto=e):
                t = _texto_tag(texto, tag)
                return int(float(t)) if t and re.fullmatch(r"[\d.]+", t) else None

            uri = self._post(media, (
                "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                f"<trt:ProfileToken>{_xml(token)}</trt:ProfileToken></trt:GetStreamUri>")) or ""
            url = _texto_tag(uri, "Uri")
            if not url:
                continue
            u = urlparse(url)
            salida.append({
                "clave": token,
                "nombre": _texto_tag(cuerpo, "Name") or token,
                "ruta": (u.path or "/") + (f"?{u.query}" if u.query else ""),
                "puerto": u.port or 554,
                "codec": _texto_tag(e, "Encoding"),
                "ancho": entero("Width"),
                "alto": entero("Height"),
                "fps": entero("FrameRateLimit"),
                "bitrate_kbps": entero("BitrateLimit"),
                "fuente": "ONVIF",
                "verificado": False,
            })
        return salida


# --------------------------------------------------------------------------
# RTSP DESCRIBE: confirma ruta y credenciales sin abrir el video
# --------------------------------------------------------------------------

def _leer_respuesta_rtsp(s: socket.socket) -> tuple[int, dict, str]:
    datos = b""
    while b"\r\n\r\n" not in datos and len(datos) < 65536:
        bloque = s.recv(4096)
        if not bloque:
            break
        datos += bloque
    cabecera, _, resto = datos.partition(b"\r\n\r\n")
    lineas = cabecera.decode("utf-8", "ignore").split("\r\n")
    m = re.match(r"RTSP/1\.\d\s+(\d{3})", lineas[0] if lineas else "")
    codigo = int(m.group(1)) if m else 0
    cabeceras = {}
    for linea in lineas[1:]:
        if ":" in linea:
            k, v = linea.split(":", 1)
            cabeceras.setdefault(k.strip().lower(), v.strip())
    largo = int(cabeceras.get("content-length", "0") or 0)
    while len(resto) < largo and len(resto) < 65536:
        bloque = s.recv(4096)
        if not bloque:
            break
        resto += bloque
    return codigo, cabeceras, resto.decode("utf-8", "ignore")


def _autorizacion_rtsp(reto: str, user: str, password: str, metodo: str, uri: str) -> Optional[str]:
    return autorizacion_http(reto, user, password, metodo, uri)


def codec_de_sdp(sdp: str) -> Optional[str]:
    m = re.search(r"a=rtpmap:\d+\s+([A-Za-z0-9-]+)/", sdp)
    if not m:
        return None
    return {"H264": "H.264", "H265": "H.265", "HEVC": "H.265", "JPEG": "MJPEG"}.get(m.group(1).upper(),
                                                                                    m.group(1))


def describe_rtsp(host: str, puerto: int, ruta: str, user: str, password: str,
                  timeout: float = 4.0) -> dict:
    """Pide la descripcion del stream. Respuestas que importan:
        200  la ruta existe y las credenciales sirven (el SDP trae el codec)
        401  credenciales rechazadas  -> no seguir probando rutas
        404  la ruta no existe        -> probar la siguiente
    Sin credenciales en la primera peticion: el 401 de esa no es un intento
    fallido, es la camara diciendo que algoritmo usa."""
    uri = f"rtsp://{host}:{puerto}{ruta}"

    def pedir(s: socket.socket, cseq: int, autorizacion: str = "") -> tuple[int, dict, str]:
        extra = f"Authorization: {autorizacion}\r\n" if autorizacion else ""
        s.sendall((f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: {cseq}\r\n"
                   f"Accept: application/sdp\r\nUser-Agent: GOSS-IP\r\n{extra}\r\n").encode())
        return _leer_respuesta_rtsp(s)

    def conectar() -> socket.socket:
        s = socket.create_connection((host, puerto), timeout=timeout)
        s.settimeout(timeout)
        return s

    s = None
    try:
        s = conectar()
        codigo, cab, cuerpo = pedir(s, 1)
        if codigo == 401:
            auth = _autorizacion_rtsp(cab.get("www-authenticate", ""), user, password, "DESCRIBE", uri)
            if not auth:
                return {"codigo": 401}
            # En la MISMA conexion: Hikvision liga el nonce del reto a ella y
            # rechaza (401) una respuesta correcta que llega por otra. Solo si
            # la camara cerro el socket se abre uno nuevo.
            try:
                codigo, cab, cuerpo = pedir(s, 2, auth)
            except OSError:
                codigo = 0
            if codigo == 0:
                s.close()
                s = conectar()
                codigo, cab, cuerpo = pedir(s, 2, auth)
        return {"codigo": codigo, "codec": codec_de_sdp(cuerpo) if codigo == 200 else None}
    except OSError as e:
        return {"codigo": 0, "error": type(e).__name__}
    finally:
        if s is not None:
            s.close()


def url_rtsp(host: str, puerto: int, ruta: str, user: str, password: str) -> str:
    return f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{puerto}{ruta}"


def abrir_stream(fuente: str, timeout: float = 8.0) -> Optional[dict]:
    """Abre la fuente con OpenCV (como lo hara el worker) y lee un frame."""
    import cv2

    from tools.probe_camara import abrir_con_timeout

    t0 = time.monotonic()
    if fuente.startswith(("rtsp://", "rtsps://", "http://", "https://")):
        cap = abrir_con_timeout(fuente, timeout)
    elif fuente.startswith("webcam:"):
        cap = cv2.VideoCapture(int(fuente.split(":", 1)[1]))
    else:
        cap = cv2.VideoCapture(fuente.removeprefix("file:"))
    try:
        if not cap.isOpened():
            return None
        ok, frame = cap.read()
        if not ok or frame is None:
            return None
        fourcc = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
        codec = "".join(chr((fourcc >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00 ").upper() or None
        codec = {"H264": "H.264", "AVC1": "H.264", "HEVC": "H.265", "H265": "H.265",
                 "HVC1": "H.265"}.get(codec or "", codec)
        return {
            "ancho": int(frame.shape[1]),
            "alto": int(frame.shape[0]),
            "fps": round(cap.get(cv2.CAP_PROP_FPS) or 0, 1) or None,
            "codec": codec,
            "latencia_s": round(time.monotonic() - t0, 2),
            "frame": frame,
        }
    finally:
        cap.release()


def miniatura_b64(frame, ancho: int = 640) -> Optional[str]:
    import cv2

    h, w = frame.shape[:2]
    if w > ancho:
        frame = cv2.resize(frame, (ancho, int(h * ancho / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return base64.b64encode(buf).decode() if ok else None


def compatibilidad_webrtc(codec: Optional[str]) -> dict:
    if not codec:
        return {"compatible": None, "detalle": "Codec no reportado: se confirma al abrir el video."}
    if "264" in codec:
        return {"compatible": True, "detalle": "H.264: video WebRTC en cualquier navegador."}
    if "265" in codec:
        return {"compatible": False,
                "detalle": "H.265: WebRTC no está garantizado en todos los navegadores. Cambia este "
                           "stream a H.264 en la cámara o se usará la vista MJPEG."}
    return {"compatible": False, "detalle": f"{codec}: se mostrará por la vista MJPEG."}


# --------------------------------------------------------------------------
# Diagnostico completo
# --------------------------------------------------------------------------

def _elegir_perfiles(perfiles: list[dict]) -> tuple[Optional[dict], Optional[dict]]:
    """(analisis, evidencia). Para analizar, el de menor resolucion que aun
    tenga al menos 640 px (el sub-stream); para evidencia, el de mas pixeles."""
    con_tamano = [p for p in perfiles if p.get("ancho")]
    if not con_tamano:
        return (perfiles[0] if perfiles else None), None
    principal = max(con_tamano, key=lambda p: p["ancho"] * (p["alto"] or 1))
    aptos = [p for p in con_tamano if p["ancho"] >= 640] or con_tamano
    analisis = min(aptos, key=lambda p: p["ancho"] * (p["alto"] or 1))
    return analisis, (principal if principal is not analisis else None)


def diagnosticar(host: str, user: str, password: str, puerto_rtsp: int = 554,
                 puerto_http: int = 80, onvif_xaddr: Optional[str] = None) -> dict:
    """Identifica la camara, lista sus streams, confirma el de analisis y
    devuelve una vista previa. Las credenciales se prueban una sola vez."""
    from tools.probe_camara import RUTAS_CANDIDATAS

    t0 = time.monotonic()
    dispositivo: dict = {}
    protocolos: dict = {}
    perfiles: list[dict] = []
    advertencias: list[str] = []

    abiertos = {p: puerto_abierto(host, p, 1.0) for p in {puerto_rtsp, puerto_http, 443, 8000}}
    protocolos["rtsp"] = {"estado": "abierto" if abiertos[puerto_rtsp] else "cerrado"}

    def resultado(estado: str, motivo: str, **extra) -> dict:
        fabricante = dispositivo.get("fabricante")
        modelo = dispositivo.get("modelo")
        fam = familia_de(fabricante and fabricante["valor"], modelo and modelo["valor"],
                         onvif=protocolos.get("onvif", {}).get("estado") == "ok")
        base = {
            "host": host, "estado": estado, "motivo": motivo,
            "dispositivo": dispositivo, "protocolos": protocolos, "perfiles": perfiles,
            "familia": _sin_patron(fam), "advertencias": advertencias,
            "duracion_s": round(time.monotonic() - t0, 1),
        }
        base.update(extra)
        return base

    # --- 1. Credenciales: ISAPI primero, ONVIF si no hay ISAPI -------------
    credenciales_ok = None
    if abiertos[puerto_http]:
        isapi = ClienteIsapi(host, user, password, puerto_http)
        try:
            xml = isapi.get("/ISAPI/System/deviceInfo")
        except CredencialesRechazadas:
            protocolos["isapi"] = {"estado": "credenciales"}
            return resultado("parcial", "La cámara rechazó usuario/contraseña. No se probó nada más "
                             "para no arriesgar el bloqueo por intentos fallidos (Hikvision bloquea "
                             "la IP tras ~5).", credenciales="rechazadas")
        except Exception:  # noqa: BLE001
            xml = None
        if xml and "<" in xml:
            credenciales_ok = True
            info = parsear_device_info(xml)
            protocolos["isapi"] = {"estado": "ok"}
            dispositivo["fabricante"] = _dato("Hikvision", "ISAPI")
            for clave in ("modelo", "nombre", "firmware", "serie", "mac"):
                if info.get(clave):
                    dispositivo[clave] = _dato(info[clave], "ISAPI")
            try:
                canales = isapi.get("/ISAPI/Streaming/channels")
                perfiles = parsear_canales_isapi(canales or "")
            except CredencialesRechazadas:
                perfiles = []
        else:
            protocolos["isapi"] = {"estado": "no disponible"}

    if credenciales_ok is None:
        xaddr = onvif_xaddr or (f"http://{host}/onvif/device_service" if abiertos[puerto_http] else None)
        if xaddr and urlparse(xaddr).hostname == host:
            onvif = ClienteOnvif(xaddr, user, password)
            if onvif.sincronizar_reloj():
                try:
                    info = onvif.informacion()
                    credenciales_ok = bool(info.get("modelo") or info.get("fabricante"))
                    protocolos["onvif"] = {"estado": "ok" if credenciales_ok else "sin datos"}
                    if info.get("fabricante"):
                        marca = next((m for m in OUI if m.lower() in info["fabricante"].lower()),
                                     info["fabricante"])
                        dispositivo["fabricante"] = _dato(marca, "ONVIF")
                    for clave in ("modelo", "firmware", "serie"):
                        if info.get(clave):
                            dispositivo[clave] = _dato(info[clave], "ONVIF")
                    if credenciales_ok:
                        perfiles = onvif.perfiles()
                except CredencialesRechazadas:
                    protocolos["onvif"] = {"estado": "credenciales"}
                    return resultado("parcial", "La cámara rechazó usuario/contraseña por ONVIF. "
                                     "En Hikvision, ONVIF usa un usuario propio que se crea en la "
                                     "cámara.", credenciales="rechazadas")
            else:
                protocolos["onvif"] = {"estado": "no disponible"}
    if "fabricante" not in dispositivo:
        marca, _ = fabricante_por_mac(tabla_arp().get(host))
        if marca:
            dispositivo["fabricante"] = _dato(marca, "MAC")

    if not abiertos[puerto_rtsp] and not any(p.get("puerto") not in (None, puerto_rtsp) for p in perfiles):
        return resultado("parcial", f"El puerto RTSP ({puerto_rtsp}) está cerrado: actívalo en la "
                         "cámara (en Hikvision: Configuración > Red > Avanzada > Protocolos).",
                         credenciales="validas" if credenciales_ok else "sin probar")

    # --- 2. Confirmar rutas con DESCRIBE (sin abrir video) ------------------
    if not perfiles:
        fam = familia_de(dispositivo.get("fabricante", {}).get("valor") if dispositivo.get("fabricante") else None,
                         None)
        # El secundario primero: es el que se analiza y el que casi siempre existe.
        rutas = [fam["rutas"][k] for k in ("secundario", "principal") if k in fam["rutas"]]
        rutas += [r for r, _ in RUTAS_CANDIDATAS]
        vistos = set()
        for ruta in rutas:
            if ruta in vistos or "{" in ruta:
                continue
            vistos.add(ruta)
            r = describe_rtsp(host, puerto_rtsp, ruta, user, password)
            if r["codigo"] == 401:
                return resultado("parcial", "El RTSP rechazó usuario/contraseña. No se probaron más "
                                 "rutas para no bloquear la cámara.", credenciales="rechazadas")
            if r["codigo"] == 200:
                perfiles.append({"clave": ruta, "nombre": "Encontrado por RTSP", "ruta": ruta,
                                 "codec": r.get("codec"), "ancho": None, "alto": None, "fps": None,
                                 "bitrate_kbps": None, "fuente": "RTSP", "verificado": True})
                credenciales_ok = True
                if len(perfiles) >= 2:
                    break
    else:
        for p in perfiles:
            r = describe_rtsp(host, p.get("puerto") or puerto_rtsp, p["ruta"], user, password)
            if r["codigo"] == 401:
                advertencias.append(f"El stream {p['nombre']} rechazó las credenciales por RTSP.")
                break
            p["verificado"] = r["codigo"] == 200
            if r.get("codec") and not p.get("codec"):
                p["codec"] = r["codec"]

    if not any(p["verificado"] for p in perfiles):
        return resultado("parcial", "Las credenciales son correctas pero ningún stream respondió por "
                         "RTSP. Revisa que RTSP esté activo y que esta PC esté en la misma red.",
                         credenciales="validas" if credenciales_ok else "sin probar")

    # --- 3. Abrir de verdad el stream de analisis --------------------------
    verificados = [p for p in perfiles if p["verificado"]]
    analisis, principal = _elegir_perfiles(verificados)
    abierto = abrir_stream(url_rtsp(host, analisis.get("puerto") or puerto_rtsp, analisis["ruta"],
                                    user, password))
    preview = None
    if abierto:
        for k in ("ancho", "alto"):
            analisis[k] = analisis.get(k) or abierto[k]
        analisis["fps"] = analisis.get("fps") or abierto["fps"]
        analisis["codec"] = analisis.get("codec") or abierto["codec"]
        analisis["latencia_s"] = abierto["latencia_s"]
        preview = miniatura_b64(abierto["frame"])
        if principal is None:
            analisis, principal = _elegir_perfiles(verificados)
        if principal is None and len(verificados) > 1:
            # Rutas encontradas por RTSP, sin tamano reportado: la otra es la
            # principal (en el orden de prueba va primero el secundario).
            principal = next(p for p in verificados if p is not analisis)
    else:
        advertencias.append("El stream respondió pero OpenCV no pudo decodificar un frame "
                            "(¿codec no soportado?).")

    for p in perfiles:
        p["webrtc"] = compatibilidad_webrtc(p.get("codec"))
    if analisis.get("ancho") and analisis["ancho"] < 640:
        advertencias.append(f"El stream de análisis es de {analisis['ancho']} px de ancho: poco "
                            "para placas y rostros. Sube la resolución del sub-stream a 1280.")
    if analisis.get("codec") and "265" in analisis["codec"]:
        advertencias.append(compatibilidad_webrtc(analisis["codec"])["detalle"])

    tiene_modelo = bool(dispositivo.get("fabricante") and dispositivo.get("modelo"))
    estado = "identificada" if tiene_modelo and abierto else "parcial"
    motivo = ("Marca, modelo y streams confirmados." if estado == "identificada" else
              "Video confirmado, pero la cámara no reportó su modelo (sin ISAPI ni ONVIF)."
              if abierto else "No se pudo abrir el video.")
    return resultado(estado, motivo, credenciales="validas",
                     perfil_analisis=analisis["clave"],
                     perfil_principal=principal["clave"] if principal else None,
                     preview_b64=preview)


def diagnosticar_manual(fuente: str) -> dict:
    """Alta manual: una URL RTSP completa, una webcam o un video de demo."""
    t0 = time.monotonic()
    abierto = abrir_stream(fuente)
    tipo = ("webcam" if fuente.startswith("webcam:") else
            "archivo" if fuente.startswith("file:") else "rtsp")
    familia = _sin_patron(familia_de(None, None))
    if tipo != "rtsp":
        familia = {**familia, "nombre": "Webcam local" if tipo == "webcam" else "Video de demostración",
                   "capacidades": ["Solo video: sirve para pruebas y para el plan de contingencia."]}
    perfil = None
    if abierto:
        perfil = {"clave": "manual", "nombre": "Manual", "ruta": None, "codec": abierto["codec"],
                  "ancho": abierto["ancho"], "alto": abierto["alto"], "fps": abierto["fps"],
                  "latencia_s": abierto["latencia_s"], "fuente": "OpenCV", "verificado": True,
                  "webrtc": compatibilidad_webrtc(abierto["codec"]) if tipo == "rtsp" else
                  {"compatible": None, "detalle": "Fuente local: se ve por la vista MJPEG del worker."}}
    return {
        "host": urlparse(fuente).hostname if tipo == "rtsp" else None,
        "estado": "manual" if abierto else "parcial",
        "motivo": "Configuración manual: video confirmado." if abierto else
                  "No se pudo abrir la fuente indicada.",
        "tipo": tipo,
        "dispositivo": {},
        "protocolos": {},
        "perfiles": [perfil] if perfil else [],
        "perfil_analisis": "manual" if perfil else None,
        "perfil_principal": None,
        "familia": familia,
        "advertencias": [],
        "preview_b64": miniatura_b64(abierto["frame"]) if abierto else None,
        "duracion_s": round(time.monotonic() - t0, 1),
    }
