"""Respaldo completo para volver a una version funcional en minutos.

    python tools/respaldo.py                        # a respaldos/AAAAMMDD-HHMM/
    python tools/respaldo.py --destino D:/GOSS      # a una memoria USB
    python tools/respaldo.py --nube                 # ademas, copia a OneDrive (sin credenciales)
    python tools/respaldo.py --con-evidencia        # incluye fotos y clips

Que guarda:

    repo.bundle      todo el historial y TODAS las ramas, en un solo archivo.
                     Se restaura sin internet:  git clone repo.bundle GOSS
    entorno/         .env y los .env.<camara>. Llevan la contrasena de las
                     camaras: por eso esta carpeta nunca va a Git.
    datos/           la base de datos (copia consistente aunque la API este
                     corriendo), zonas, token del worker y secreto de sesiones.
    evidencia/       (opcional) fotos y clips de las alertas.
    MANIFIESTO.txt   que se guardo, su huella SHA-256 y como restaurar.

La copia de la base de datos usa la API de respaldo de SQLite y no un simple
copiar: con la API escribiendo, copiar el archivo puede dejar una base de
datos corrupta (el WAL y el archivo principal quedan desfasados).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

RESTAURAR = """\
COMO RESTAURAR
==============
1. Codigo:     git clone repo.bundle GOSS-restaurado
               (o, en el repo existente:  git fetch repo.bundle 'refs/heads/*:refs/remotes/respaldo/*')
2. Entorno:    copiar entorno/.env* a la raiz del proyecto.
3. Datos:      copiar datos/vigilancia.db a data/ (con la API DETENIDA) y
               datos/.ingest_token, datos/.jwt_secret y datos/zonas/ a data/.
4. Evidencia:  copiar evidencia/snapshots y evidencia/clips a data/.
5. Arrancar:   iniciar_api.bat  y  iniciar_worker.bat
"""


def _sha256(ruta: Path) -> str:
    h = hashlib.sha256()
    with ruta.open("rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def _bundle(destino: Path) -> Path | None:
    archivo = destino / "repo.bundle"
    r = subprocess.run(["git", "bundle", "create", str(archivo), "--all"],
                       cwd=RAIZ, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  [x] git bundle fallo: {r.stderr.strip()}")
        return None
    pendientes = subprocess.run(["git", "status", "--porcelain"], cwd=RAIZ,
                                capture_output=True, text=True).stdout.strip()
    if pendientes:
        # El bundle solo lleva lo que esta en commits. Se avisa en vez de
        # hacer commit por el usuario.
        print("  [!] Hay cambios sin commit; el bundle NO los incluye:")
        for linea in pendientes.splitlines()[:10]:
            print(f"        {linea}")
    return archivo


def _entorno(destino: Path) -> list[Path]:
    carpeta = destino / "entorno"
    carpeta.mkdir()
    copiados = []
    for origen in sorted(RAIZ.glob(".env*")):
        if origen.name == ".env.example" or not origen.is_file():
            continue
        copiados.append(Path(shutil.copy2(origen, carpeta / origen.name)))
    camaras = RAIZ / "camaras"
    if camaras.is_dir():
        shutil.copytree(camaras, carpeta / "camaras")
        copiados.extend(p for p in (carpeta / "camaras").rglob("*") if p.is_file())
    return copiados


def _base_de_datos(origen: Path, destino: Path) -> Path | None:
    if not origen.exists():
        return None
    copia = destino / origen.name
    fuente = sqlite3.connect(f"file:{origen.as_posix()}?mode=ro", uri=True)
    try:
        objetivo = sqlite3.connect(copia)
        with objetivo:
            fuente.backup(objetivo)
        objetivo.close()
    finally:
        fuente.close()
    return copia


def _datos(destino: Path) -> list[Path]:
    carpeta = destino / "datos"
    carpeta.mkdir()
    copiados = []
    url = os.getenv("DATABASE_URL", "")
    if url and not url.startswith("sqlite"):
        print("  [!] DATABASE_URL apunta a PostgreSQL: respaldala con pg_dump.")
    else:
        bd = _base_de_datos(RAIZ / "data" / "vigilancia.db", carpeta)
        if bd:
            copiados.append(bd)
    for nombre in (".ingest_token", ".jwt_secret"):
        f = RAIZ / "data" / nombre
        if f.exists():
            copiados.append(Path(shutil.copy2(f, carpeta / nombre)))
    zonas = RAIZ / "data" / "zonas"
    if zonas.is_dir():
        shutil.copytree(zonas, carpeta / "zonas")
        copiados.extend(p for p in (carpeta / "zonas").rglob("*") if p.is_file())
    return copiados


def _evidencia(destino: Path) -> list[Path]:
    carpeta = destino / "evidencia"
    copiados = []
    for nombre in ("snapshots", "clips"):
        origen = RAIZ / "data" / nombre
        if origen.is_dir():
            shutil.copytree(origen, carpeta / nombre)
            copiados.extend(p for p in (carpeta / nombre).rglob("*") if p.is_file())
    return copiados


def _manifiesto(destino: Path, archivos: list[Path]) -> None:
    rama = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=RAIZ,
                          capture_output=True, text=True).stdout.strip()
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=RAIZ,
                            capture_output=True, text=True).stdout.strip()
    lineas = [
        "RESPALDO GOSS IP",
        f"Fecha:   {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Rama:    {rama} @ {commit}",
        f"Equipo:  {os.getenv('COMPUTERNAME') or os.getenv('HOSTNAME') or '?'}",
        "",
        "ARCHIVOS (sha256  bytes  ruta)",
    ]
    for a in sorted(archivos):
        lineas.append(f"{_sha256(a)}  {a.stat().st_size:>10}  {a.relative_to(destino).as_posix()}")
    lineas += ["", RESTAURAR]
    (destino / "MANIFIESTO.txt").write_text("\n".join(lineas), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--destino", type=Path, default=RAIZ / "respaldos",
                   help="Carpeta donde se crea el respaldo fechado (default: respaldos/)")
    p.add_argument("--nube", action="store_true",
                   help="Copiar tambien a OneDrive/GOSS-respaldos, SIN los .env ni los "
                        "secretos (sincroniza a la nube)")
    p.add_argument("--con-evidencia", action="store_true",
                   help="Incluir fotos y clips de las alertas (pesan mas)")
    args = p.parse_args()

    sys.path.insert(0, str(RAIZ))
    from edge.config import leer_env, ruta_env

    # Solo para saber si DATABASE_URL apunta a otra base de datos.
    for clave, valor in leer_env(ruta_env()).items():
        os.environ.setdefault(clave, valor)

    marca = datetime.now().strftime("%Y%m%d-%H%M")
    destino = args.destino / marca
    destino.mkdir(parents=True, exist_ok=False)
    print(f"[i] Respaldo en {destino}")

    archivos: list[Path] = []
    bundle = _bundle(destino)
    if bundle:
        archivos.append(bundle)
        print("  [OK] codigo (todas las ramas)")
    entorno = _entorno(destino)
    archivos += entorno
    print(f"  [OK] entorno: {len(entorno)} archivo(s) .env")
    datos = _datos(destino)
    archivos += datos
    print(f"  [OK] datos: {len(datos)} archivo(s)")
    if args.con_evidencia:
        evidencia = _evidencia(destino)
        archivos += evidencia
        print(f"  [OK] evidencia: {len(evidencia)} archivo(s)")
    _manifiesto(destino, archivos)

    total = sum(a.stat().st_size for a in archivos)
    print(f"[OK] {len(archivos)} archivos, {total / 1e6:.1f} MB. Ver MANIFIESTO.txt")

    if args.nube:
        onedrive = os.getenv("OneDrive")
        if not onedrive or not Path(onedrive).is_dir():
            print("  [!] No encontre la carpeta de OneDrive; copia la carpeta a mano.")
            return 1
        nube = Path(onedrive) / "GOSS-respaldos" / marca
        # Sin entorno/ ni secretos: las contrasenas de las camaras y el token
        # del worker no salen del equipo. Se reponen a mano al restaurar.
        shutil.copytree(destino, nube, ignore=shutil.ignore_patterns(
            "entorno", ".ingest_token", ".jwt_secret"))
        print(f"[OK] Copiado a {nube} SIN credenciales (OneDrive lo sube solo)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
