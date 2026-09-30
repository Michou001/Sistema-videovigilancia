"""Genera la configuracion de go2rtc (video por WebRTC) desde los .env de las camaras.

    python tools/go2rtc_config.py --env .env --env .env.cam2 --ip 192.168.1.10
    python tools/go2rtc_config.py --todas --ip 192.168.1.10          # .env y .env.*
    python tools/go2rtc_config.py --todas --ip 192.168.1.10 --principal
    python tools/go2rtc_config.py --carpeta camaras --ip 192.168.1.10  # Docker

Cada camara con SOURCE=rtsp://... queda como un stream de go2rtc con el MISMO
nombre que su CAMERA_ID: asi el dashboard sabe cual pedir. El video va del
navegador a go2rtc por WebRTC sin recodificar (ver web/js/webrtc.js).

  --ip          IP de la PC donde corre go2rtc tal como la ven los navegadores
                (la de la red local). WebRTC la anuncia para el video (UDP/TCP 8555).
  --principal   Usar el canal principal (101, maxima resolucion) en vez del
                sub-flujo (102) que usa el worker. Debe estar en H.264: Chrome no
                reproduce H.265 por WebRTC.

EL ARCHIVO LLEVA LAS CONTRASENAS DE LAS CAMARAS: no lo subas a git (.gitignore
ya lo excluye) y deja que solo lo lea el servicio de go2rtc.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))


def streams_desde(configs) -> dict[str, str]:
    streams = {}
    for cfg in configs:
        fuente = str(cfg.source)
        if fuente.startswith(("rtsp://", "rtsps://")):
            streams[cfg.camera_id] = fuente
    return streams


def canal_principal(url: str) -> str:
    """.../Streaming/Channels/102 -> .../Streaming/Channels/101"""
    return re.sub(r"(/channels/\d+)0[2-9](?=$|[/?])", r"\g<1>01", url, flags=re.IGNORECASE)


def generar(streams: dict[str, str], ip: str | None, principal: bool = False) -> str:
    lineas = [
        "# Generado por tools/go2rtc_config.py -- LLEVA LAS CONTRASENAS DE LAS CAMARAS.",
        "# No lo subas a git. Regeneralo si agregas o cambias una camara.",
        "api:",
        '  listen: ":1984"          # solo dentro de la red de Docker: se publica por Caddy',
        "rtsp:",
        '  listen: ""               # no re-publicar las camaras por RTSP',
        "webrtc:",
        '  listen: ":8555"',
    ]
    if ip:
        lineas += ["  candidates:", f"    - {ip}:8555"]
    lineas.append("streams:")
    if not streams:
        lineas.append("  {}")
    for camara, url in sorted(streams.items()):
        destino = canal_principal(url) if principal else url
        # Comillas simples de YAML: las contrasenas pueden traer ':' o '#'.
        lineas.append(f"  {camara}: '{destino.replace(chr(39), chr(39) * 2)}'")
    return "\n".join(lineas) + "\n"


def main(argv=None) -> int:
    from edge.config import load_config

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", action="append", default=[], help="Archivo .env de una camara (repetible)")
    p.add_argument("--todas", action="store_true", help="Usar .env y todos los .env.<camara>")
    p.add_argument("--carpeta", help="Todos los *.env de una carpeta (Docker: --carpeta camaras)")
    p.add_argument("--ip", help="IP de esta PC en la red local (para WebRTC)")
    p.add_argument("--principal", action="store_true", help="Canal principal (101) en vez del 102")
    p.add_argument("--salida", default=str(RAIZ / "docker" / "go2rtc.yaml"))
    args = p.parse_args(argv)

    archivos = list(args.env)
    if args.carpeta:
        archivos += [str(x) for x in sorted(Path(args.carpeta).glob("*.env")) if x.is_file()]
    if args.todas:
        archivos += [str(x) for x in sorted(RAIZ.glob(".env*"))
                     if x.name != ".env.example" and x.is_file() and str(x) not in archivos]
    if not archivos:
        archivos = [".env"]
    configs = [load_config(a, aplicar_entorno=False) for a in archivos]
    streams = streams_desde(configs)
    Path(args.salida).parent.mkdir(parents=True, exist_ok=True)
    Path(args.salida).write_text(generar(streams, args.ip, args.principal), encoding="utf-8")
    print(f"  [+] {args.salida}: {len(streams)} camara(s): {', '.join(sorted(streams)) or '-'}")
    sin_rtsp = [c.camera_id for c in configs if c.camera_id not in streams]
    if sin_rtsp:
        print(f"      Sin RTSP (webcam/archivo), se quedan en MJPEG: {', '.join(sin_rtsp)}")
    if not args.ip:
        print("  [!] Sin --ip: los navegadores de otras PCs quiza no reciban el video.")
    print("      Lleva contrasenas: no lo subas a git.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
