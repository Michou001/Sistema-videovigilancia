"""Busca vulnerabilidades conocidas en las dependencias de Python.

    pip install pip-audit
    python tools/auditar_dependencias.py

Revisa el conjunto EXACTO que se instalaria en cada destino (requirements-*.txt
con las versiones de constraints.txt), no lo que haya en el venv de quien lo
corre: asi el resultado es el mismo en la laptop y en la integracion continua.

    requirements-api.txt   lo que lleva la imagen Docker de la API
    requirements-ci.txt    lo que instala la integracion continua

La base de avisos es la de PyPI/OSV (la misma fuente publica que usan Snyk y
Dependabot para Python). Sale con codigo 1 si encuentra algo, para que la CI
se detenga. torch y los paquetes de la GPU no se revisan aqui: se instalan
desde el indice de PyTorch, no de PyPI (ver la cabecera de requirements.txt).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DESTINOS = ("requirements-api.txt", "requirements-ci.txt")


def resolver(requisitos: Path, carpeta: Path) -> Path:
    """Lista fijada (paquete==version) de lo que pip instalaria, sin instalar."""
    reporte = carpeta / f"{requisitos.stem}.json"
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--dry-run", "--ignore-installed",
         "--report", str(reporte), "-r", str(requisitos), "-c", str(RAIZ / "constraints.txt")],
        check=True,
    )
    instalacion = json.loads(reporte.read_text(encoding="utf-8"))["install"]
    fijados = carpeta / f"{requisitos.stem}.lock.txt"
    fijados.write_text("\n".join(
        f"{p['metadata']['name']}=={p['metadata']['version']}" for p in instalacion) + "\n",
        encoding="utf-8")
    return fijados


def main() -> int:
    fallos = 0
    with tempfile.TemporaryDirectory() as tmp:
        for nombre in DESTINOS:
            print(f"\n== {nombre}", flush=True)
            fijados = resolver(RAIZ / nombre, Path(tmp))
            print(f"   {len(fijados.read_text().split())} paquetes resueltos con constraints.txt",
                  flush=True)
            r = subprocess.run([sys.executable, "-m", "pip_audit", "-r", str(fijados),
                                "--no-deps", "--disable-pip", "--progress-spinner", "off",
                                "--desc", "off"])
            fallos += r.returncode != 0
    print("\nSin vulnerabilidades conocidas." if not fallos else
          "\nHay vulnerabilidades: sube la version en constraints.txt y corre las pruebas.")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
