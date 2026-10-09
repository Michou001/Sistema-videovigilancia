"""Corre todas las pruebas del proyecto.

    python tests/correr_todas.py

Cada archivo test_*.py se ejecuta en su propio proceso (algunos fijan
variables de entorno antes de importar la API) y al final se resume cuales
pasaron. No hace falta camara ni GPU.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

CARPETA = Path(__file__).resolve().parent


def main() -> int:
    archivos = sorted(CARPETA.glob("test_*.py"))
    fallidos = []
    for archivo in archivos:
        t0 = time.perf_counter()
        r = subprocess.run([sys.executable, str(archivo)], cwd=CARPETA.parent,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        resumen = next((linea for linea in reversed(r.stdout.splitlines())
                        if "pruebas pasan" in linea), None)
        if resumen is None:
            # Los archivos con unittest dejan su resumen ("Ran 5 tests") en stderr.
            corridas = re.search(r"^Ran (\d+) tests?", r.stderr or "", re.M)
            resumen = (f"{corridas.group(1)}/{corridas.group(1)} pruebas pasan"
                       if corridas and r.returncode == 0 else "sin resumen")
        estado = "OK   " if r.returncode == 0 else "FALLA"
        print(f"  [{estado}] {archivo.name:32} {resumen.strip():22} ({time.perf_counter() - t0:.1f}s)")
        if r.returncode != 0:
            fallidos.append(archivo.name)
            print("\n".join("        " + linea for linea in r.stdout.splitlines()
                            if "[FALLA]" in linea or "[ERROR]" in linea))
            if r.stderr.strip() and "Traceback" in r.stderr:
                print("        " + r.stderr.strip().splitlines()[-1])

    print()
    if fallidos:
        print(f"{len(fallidos)} de {len(archivos)} archivos con fallas: {', '.join(fallidos)}")
        return 1
    print(f"Todas las pruebas pasan ({len(archivos)} archivos).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
