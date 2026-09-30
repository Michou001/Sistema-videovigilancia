"""Exporta el dataset de placas para reentrenar el OCR con placas mexicanas.

    python tools/exportar_dataset.py                       # solo lecturas corregidas
    python tools/exportar_dataset.py --automaticas         # + lecturas muy seguras
    python tools/exportar_dataset.py --salida placas.zip --desde 2026-09-01

Mismo contenido que el boton "Dataset de placas" del dashboard (Administracion),
para correrlo desde el servidor o programarlo. El ZIP trae anotaciones.csv en
el formato de fast-plate-ocr y un LEEME con los pasos para entrenar
(ver docs/placas-mexicanas.md).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from sqlmodel import Session  # noqa: E402

from api.auditoria import registrar  # noqa: E402
from api.database import engine, init_db  # noqa: E402
from api.dataset import exportar  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--salida", default=f"dataset-placas-{datetime.now():%Y%m%d-%H%M}.zip")
    p.add_argument("--automaticas", action="store_true",
                   help="Incluir lecturas automaticas de alta confianza (pueden tener errores)")
    p.add_argument("--min-conf", type=float, default=0.9)
    p.add_argument("--min-frames", type=int, default=5)
    p.add_argument("--desde", type=datetime.fromisoformat, help="Fecha inicial, ej. 2026-09-01")
    args = p.parse_args()

    init_db()
    salida = Path(args.salida)
    with Session(engine) as session, salida.open("wb") as f:
        resumen = exportar(session, f, incluir_automaticas=args.automaticas,
                           min_conf=args.min_conf, min_frames=args.min_frames, desde=args.desde)
        registrar(session, "reportes.dataset", usuario="(consola)", objetivo=salida.name,
                  detalle={"corregidas": resumen.corregidas, "automaticas": resumen.automaticas},
                  confirmar=True)

    print(f"  [+] {salida}  ({resumen.total} placas: {resumen.corregidas} corregidas, "
          f"{resumen.automaticas} automaticas; {resumen.sin_foto} sin foto disponible)")
    if resumen.total == 0:
        print("      No hay lecturas corregidas todavia. Corrige lecturas desde el Registro")
        print("      del dashboard (lapiz en la fila de la placa) o usa --automaticas.")
    print("      Son placas reales: guarda el archivo cifrado y borralo al terminar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
