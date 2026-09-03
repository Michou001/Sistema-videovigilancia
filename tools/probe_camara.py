"""Diagnostico de camara IP: encuentra la Hikvision en la red y averigua su URL RTSP.

Reemplaza a la herramienta SADP de Hikvision (que solo corre en Windows y hay
que descargar) y ademas prueba de verdad el stream, cosa que SADP no hace.

Uso tipico, en orden:

    # 1. Buscar la camara en tu red local
    python tools/probe_camara.py --descubrir

    # 2. Probar credenciales y encontrar la ruta RTSP que funciona
    python tools/probe_camara.py --host 192.168.1.64 --user admin --password TuPass

    # 3. Ver el video para confirmar encuadre
    python tools/probe_camara.py --host 192.168.1.64 --user admin --password TuPass --ver

Al terminar imprime la linea SOURCE=... lista para pegar en tu .env
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import quote

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import cv2  # noqa: E402


def cargar_env() -> None:
    """Lee el .env para no tener que pasar la contrasena por linea de comandos.

    Escribir --password en la terminal la deja en el historial del shell, y si
    alguien mas lee esa sesion se lleva la credencial de la camara. El .env
    esta en .gitignore, que es el lugar correcto para esto.
    """
    archivo = RAIZ / ".env"
    if not archivo.exists():
        return
    for linea in archivo.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))

# Rutas RTSP por fabricante. Hikvision primero porque es la camara del proyecto.
RUTAS_CANDIDATAS = [
    # --- Hikvision (firmware moderno) ---
    ("/Streaming/Channels/102", "Hikvision sub-stream  <-- el recomendado para inferencia"),
    ("/Streaming/Channels/101", "Hikvision main-stream (alta resolucion)"),
    ("/Streaming/Channels/103", "Hikvision tercer stream"),
    ("/ISAPI/Streaming/channels/102", "Hikvision via ISAPI"),
    # --- Hikvision (firmware viejo) ---
    ("/h264/ch1/sub/av_stream", "Hikvision antiguo, sub"),
    ("/h264/ch1/main/av_stream", "Hikvision antiguo, main"),
    # --- Genericos / otras marcas, por si acaso ---
    ("/cam/realmonitor?channel=1&subtype=1", "Dahua sub"),
    ("/onvif1", "ONVIF generico"),
    ("/live/ch0", "Generico"),
]

PUERTOS_INTERES = {
    554: "RTSP (video)",
    80: "HTTP (interfaz web / ISAPI)",
    8000: "SDK Hikvision",
    443: "HTTPS",
}


# --------------------------------------------------------------------------
# Descubrimiento en LAN
# --------------------------------------------------------------------------

def _ip_local() -> str | None:
    """IP de esta maquina en la LAN (sin mandar trafico real)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _puerto_abierto(ip: str, puerto: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((str(ip), puerto)) == 0


def descubrir(subred: str | None = None) -> list[tuple[str, list[int]]]:
    """Escanea la subred /24 buscando equipos con puertos de camara abiertos."""
    if subred is None:
        local = _ip_local()
        if not local:
            print("[!] No pude determinar tu IP local. Pasa --subred 192.168.1.0/24")
            return []
        subred = str(ipaddress.ip_network(f"{local}/24", strict=False))
        print(f"[i] Tu IP local: {local}")

    red = ipaddress.ip_network(subred, strict=False)
    print(f"[i] Escaneando {red} en busca de camaras (puertos 554/80/8000)...")
    print("[i] Esto tarda ~20 s. La camara suele venir de fabrica en 192.168.1.64\n")

    encontrados: list[tuple[str, list[int]]] = []

    def revisar(ip):
        # Se revisan TODOS los puertos de interes, no solo el 554. Una Hikvision
        # con RTSP desactivado de fabrica, o un NVR, tienen el 554 cerrado pero
        # el 80 y el 8000 abiertos: filtrar por 554 los haria invisibles, que es
        # justo cuando mas necesitas encontrarlos.
        abiertos = [p for p in PUERTOS_INTERES if _puerto_abierto(str(ip), p, timeout=0.6)]
        if not abiertos:
            return None
        return (str(ip), abiertos)

    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as pool:
        for resultado in pool.map(revisar, red.hosts()):
            if resultado:
                ip, puertos = resultado
                encontrados.append(resultado)
                etiquetas = ", ".join(f"{p} ({PUERTOS_INTERES[p]})" for p in puertos)
                print(f"  [OK] {ip}  ->  {etiquetas}")

    if not encontrados:
        print("  [x] Ningun dispositivo con RTSP abierto.")
        print("      Revisa que la camara este encendida y en el MISMO router que esta PC.")
    return encontrados


# --------------------------------------------------------------------------
# Identificacion via ISAPI (marca, modelo, firmware)
# --------------------------------------------------------------------------

def identificar(host: str, user: str, password: str) -> bool:
    """Consulta ISAPI para confirmar el dispositivo y validar las credenciales.

    Devuelve False solo si la camara RECHAZA explicitamente las credenciales
    (401). Esa distincion es critica: probar las 9 rutas RTSP con una
    contrasena incorrecta son 9 intentos fallidos, y Hikvision bloquea la IP
    tras ~5. Un solo intento HTTP aqui evita dejarte fuera 30 minutos.
    """
    import urllib.error
    import urllib.request

    url = f"http://{host}/ISAPI/System/deviceInfo"
    gestor = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    gestor.add_password(None, url, user, password)
    opener = urllib.request.build_opener(
        urllib.request.HTTPDigestAuthHandler(gestor),  # Hikvision usa Digest
        urllib.request.HTTPBasicAuthHandler(gestor),
    )
    try:
        with opener.open(url, timeout=5) as resp:
            xml = resp.read().decode("utf-8", errors="ignore")
        print("[OK] Credenciales validas. Dispositivo:")
        for etiqueta in ("deviceName", "model", "firmwareVersion", "serialNumber"):
            ini, fin = f"<{etiqueta}>", f"</{etiqueta}>"
            if ini in xml:
                valor = xml.split(ini)[1].split(fin)[0]
                print(f"      {etiqueta:18} {valor}")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("[x] La camara RECHAZO las credenciales (401).")
            print("    No voy a probar las rutas RTSP: serian 9 intentos fallidos mas")
            print("    y Hikvision bloquea la IP tras ~5. Revisa usuario y contrasena")
            print("    en el .env antes de reintentar.")
            return False
        print(f"[!] ISAPI respondio {e.code} (puede ser normal segun el firmware)")
    except Exception as e:  # noqa: BLE001 - diagnostico, cualquier fallo es informativo
        print(f"[!] Sin respuesta de ISAPI en http://{host} ({type(e).__name__})")
    # Sin confirmacion pero sin rechazo explicito: seguimos, puede ser firmware raro.
    return True


# --------------------------------------------------------------------------
# Prueba de rutas RTSP
# --------------------------------------------------------------------------

def construir_url(host: str, user: str, password: str, ruta: str, puerto: int = 554) -> str:
    # quote() es indispensable: si la contrasena trae @ / : / # la URL se rompe
    # y el error que da OpenCV no lo dice ("no se pudo abrir"). Es la causa
    # numero uno de "mi RTSP no funciona".
    return f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{puerto}{ruta}"


def probar_ruta(url: str, timeout: float = 8.0) -> dict | None:
    """Abre la URL y trata de leer un frame. Devuelve metricas o None."""
    import os
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"

    t0 = time.monotonic()
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        cap.release()
        return None

    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    ok, frame = cap.read()
    latencia = time.monotonic() - t0

    if not ok or frame is None:
        cap.release()
        return None

    info = {
        "url": url,
        "ancho": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "alto": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        "fps": round(cap.get(cv2.CAP_PROP_FPS), 1),
        "latencia_apertura": round(latencia, 2),
        "frame": frame,
    }
    cap.release()
    return info


def probar_camara(host: str, user: str, password: str, puerto: int, ver: bool) -> dict | None:
    print(f"\n[i] Probando rutas RTSP en {host}:{puerto} como '{user}'...\n")
    funcionales: list[dict] = []

    for ruta, descripcion in RUTAS_CANDIDATAS:
        url = construir_url(host, user, password, ruta, puerto)
        print(f"  probando {ruta:42} ", end="", flush=True)
        info = probar_ruta(url)
        if info:
            print(f"[OK] {info['ancho']}x{info['alto']} @ {info['fps']}fps "
                  f"({info['latencia_apertura']}s)  {descripcion}")
            info["ruta"] = ruta
            funcionales.append(info)
        else:
            print("[x]")

    if not funcionales:
        print("\n[x] Ninguna ruta funciono. Causas mas comunes, en orden:")
        print("    1. Contrasena incorrecta (Hikvision bloquea la IP tras varios intentos:")
        print("       espera 30 min o reinicia la camara).")
        print("    2. RTSP desactivado en la camara -> entra a http://%s" % host)
        print("       y activalo en Configuracion > Red > Avanzada > Protocolos.")
        print("    3. La camara esta en otra subred que esta PC.")
        return None

    # El sub-stream es el mejor para inferencia: menos pixeles, menos decodificacion.
    mejor = min(funcionales, key=lambda i: i["ancho"] * i["alto"])

    print("\n" + "=" * 70)
    print("RESULTADO")
    print("=" * 70)
    print(f"Rutas funcionales: {len(funcionales)}")
    print(f"\nRecomendada para inferencia (la de menor resolucion):")
    print(f"   {mejor['ruta']}  ->  {mejor['ancho']}x{mejor['alto']} @ {mejor['fps']}fps")

    salida = RAIZ / "data" / "snapshots"
    salida.mkdir(parents=True, exist_ok=True)
    destino = salida / "prueba_camara.jpg"
    cv2.imwrite(str(destino), mejor["frame"])
    print(f"\nCaptura de prueba guardada en: {destino}")

    # La URL lleva la contrasena embebida, asi que nunca se imprime completa:
    # esta salida acaba en terminales, capturas de pantalla y tickets.
    print(f"\nSOURCE (censurado): {ocultar_password(mejor['url'])}")
    print("=" * 70)

    if ver:
        print("\n[i] Abriendo ventana de video. Presiona 'q' para cerrar.")
        _ver_stream(mejor["url"])

    return mejor


def ocultar_password(url: str) -> str:
    """Censura la contrasena de una URL RTSP antes de imprimirla."""
    if "@" not in url:
        return url
    esquema, _, resto = url.partition("://")
    cred, _, hostpart = resto.rpartition("@")
    usuario = cred.split(":", 1)[0] if cred else ""
    return f"{esquema}://{usuario}:***@{hostpart}"


def guardar_source_en_env(url: str) -> bool:
    """Escribe la linea SOURCE= en el .env, reemplazando la que hubiera.

    Se hace desde aqui en vez de pedirle al usuario que copie y pegue, para que
    la URL con la contrasena no tenga que pasar por el portapapeles ni por la
    pantalla.
    """
    archivo = RAIZ / ".env"
    if not archivo.exists():
        print(f"[!] No existe {archivo}; no se guardo SOURCE.")
        return False

    lineas = archivo.read_text(encoding="utf-8").splitlines()
    nueva = f"SOURCE={url}"
    reemplazada = False
    for i, linea in enumerate(lineas):
        if linea.strip().startswith("SOURCE="):
            # Conserva la anterior comentada: si el RTSP falla, volver a la
            # webcam es descomentar una linea.
            lineas[i] = f"# anterior: {linea.strip()}\n{nueva}"
            reemplazada = True
            break
    if not reemplazada:
        lineas.append(nueva)

    archivo.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    print(f"[OK] SOURCE actualizado en {archivo}")
    return True


def _ver_stream(url: str) -> None:
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    while True:
        ok, frame = cap.read()
        if not ok:
            print("[!] Stream interrumpido")
            break
        cv2.imshow("Camara - 'q' para salir", frame)
        if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
            break
    cap.release()
    cv2.destroyAllWindows()


# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Encuentra y prueba una camara IP (optimizado para Hikvision)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--descubrir", action="store_true", help="Escanea la LAN buscando camaras")
    p.add_argument("--subred", help="Subred a escanear, ej. 192.168.1.0/24")
    p.add_argument("--host", help="IP de la camara (o CAMERA_HOST en el .env)")
    p.add_argument("--user", help="Usuario (o CAMERA_USER en el .env; default: admin)")
    p.add_argument("--password",
                   help="Contrasena. PREFERIBLE dejarla en CAMERA_PASSWORD dentro del .env "
                        "para que no quede en el historial de la terminal.")
    p.add_argument("--puerto", type=int, default=554, help="Puerto RTSP (default: 554)")
    p.add_argument("--ver", action="store_true", help="Mostrar el video al terminar")
    p.add_argument("--guardar", action="store_true",
                   help="Escribir la linea SOURCE= en el .env automaticamente")
    args = p.parse_args()

    cargar_env()

    # Prioridad: argumento explicito > .env > default
    host = args.host or os.getenv("CAMERA_HOST")
    user = args.user or os.getenv("CAMERA_USER") or "admin"
    password = args.password or os.getenv("CAMERA_PASSWORD")

    if args.descubrir or not host:
        encontrados = descubrir(args.subred)
        if not host:
            if encontrados:
                print("\n[i] Ahora pon las credenciales en el archivo .env:")
                print(f"      CAMERA_HOST={encontrados[-1][0]}")
                print("      CAMERA_USER=admin")
                print("      CAMERA_PASSWORD=tu_contrasena")
                print("\n    y corre:  python tools/probe_camara.py")
            return 0 if encontrados else 1

    if not password:
        print("[x] Falta la contrasena de la camara.")
        print("    Agregala al archivo .env (recomendado):")
        print("        CAMERA_HOST=192.168.1.21")
        print("        CAMERA_USER=admin")
        print("        CAMERA_PASSWORD=tu_contrasena")
        print("    o pasala con --password (queda en el historial de la terminal).")
        return 1

    # Un solo intento de autenticacion antes de tocar RTSP (proteccion contra
    # el bloqueo por intentos fallidos de Hikvision).
    if not identificar(host, user, password):
        return 1

    resultado = probar_camara(host, user, password, args.puerto, args.ver)
    if not resultado:
        return 1

    if args.guardar:
        guardar_source_en_env(resultado["url"])
    else:
        print("\n[i] Para escribirlo en el .env automaticamente, corre con --guardar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
