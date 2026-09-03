"""Aplica la politica de retencion: borra lo que ya cumplio su plazo.

    python tools/purgar_datos.py --simular    # cuenta sin borrar (hazlo primero)
    python tools/purgar_datos.py              # borra de verdad

Pensado para correr una vez al dia. En Linux con cron:

    0 3 * * *  cd /ruta/al/proyecto && venv/bin/python tools/purgar_datos.py

En Windows, con el Programador de tareas apuntando a este script.

La API tambien lo ejecuta sola cada 24 h mientras esta arriba; el cron es la
red de seguridad para cuando el servidor se reinicia seguido.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from sqlmodel import Session, func, select  # noqa: E402

from api.database import engine, init_db  # noqa: E402
from api.models import Alert, Event, FaceEmbedding  # noqa: E402
from api.retention import Politica, formatear, purgar  # noqa: E402


def inventario(session: Session) -> None:
    ev = session.exec(select(func.count()).select_from(Event)).one()
    al = session.exec(select(func.count()).select_from(Alert)).one()
    em = session.exec(select(func.count()).select_from(FaceEmbedding)).one()
    info = session.exec(
        select(func.count()).select_from(Event).where(Event.severity == "info")
    ).one()
    print(f"  eventos            : {ev}  ({info} sin coincidencia)")
    print(f"  alertas            : {al}")
    print(f"  embeddings faciales: {em}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--simular", action="store_true",
                   help="Cuenta que se borraria, sin borrar nada")
    args = p.parse_args()

    init_db()
    politica = Politica()

    print("=" * 68)
    print("PURGA DE DATOS" + ("  (SIMULACION)" if args.simular else ""))
    print("=" * 68)
    print(f"  politica: {politica.resumen()}")
    print()

    with Session(engine) as session:
        print("ANTES")
        inventario(session)
        print()
        cuenta = purgar(session, politica, simular=args.simular)
        print(formatear(cuenta, args.simular))
        if not args.simular:
            print()
            print("DESPUES")
            inventario(session)

    print("=" * 68)
    if args.simular:
        print("Nada se borro. Quita --simular para aplicar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
