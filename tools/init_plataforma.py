"""Inicializa la plataforma: crea las tablas, el usuario admin y muestra el
token que necesita el worker.

    python tools/init_plataforma.py

Es idempotente: correrlo dos veces no rompe nada ni duplica el usuario.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from sqlmodel import Session, select  # noqa: E402

from api.config import get_config  # noqa: E402
from api.database import engine, init_db  # noqa: E402
from api.models import BlacklistPlate, Operator  # noqa: E402
from api.security import hash_password  # noqa: E402
from shared.plates import es_placa_valida, formatear, normalizar  # noqa: E402


def crear_admin(session: Session, usuario: str, password: str | None) -> str | None:
    existente = session.exec(select(Operator).where(Operator.username == usuario)).first()
    if existente is not None:
        print(f"  [=] El usuario '{usuario}' ya existe, no se toca")
        return None

    generada = password or secrets.token_urlsafe(12)
    session.add(Operator(
        username=usuario,
        display_name="Administrador",
        password_hash=hash_password(generada),
        role="admin",
    ))
    session.commit()
    print(f"  [+] Usuario admin '{usuario}' creado")
    return generada


def agregar_placa_demo(session: Session, placa: str) -> None:
    valida, limpio, _ = es_placa_valida(placa)
    if not valida:
        print(f"  [x] '{placa}' no tiene formato de placa valida, se omite")
        return
    normalizada = normalizar(limpio)
    if session.exec(select(BlacklistPlate)
                    .where(BlacklistPlate.plate_normalized == normalizada)).first():
        print(f"  [=] La placa {formatear(limpio)} ya estaba en la lista negra")
        return
    session.add(BlacklistPlate(
        plate=formatear(limpio),
        plate_normalized=normalizada,
        reason="Registro de prueba del sistema",
        severity="critical",
        created_by="init",
    ))
    session.commit()
    print(f"  [+] Placa {formatear(limpio)} agregada a la lista negra")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--usuario", default="admin")
    p.add_argument("--password", help="Si se omite, se genera una y se muestra una sola vez")
    p.add_argument("--placa-demo", action="append", default=[],
                   help="Placa a sembrar en la lista negra (repetible)")
    args = p.parse_args()

    cfg = get_config()
    print("=" * 68)
    print("INICIALIZACION DE LA PLATAFORMA")
    print("=" * 68)
    print(f"  base de datos: {cfg.database_url}")
    print()

    init_db()

    with Session(engine) as session:
        password = crear_admin(session, args.usuario, args.password)
        for placa in args.placa_demo:
            agregar_placa_demo(session, placa)

    print()
    print("=" * 68)
    if password:
        print("CREDENCIALES  (se muestran una sola vez)")
        print("=" * 68)
        print(f"  usuario    : {args.usuario}")
        print(f"  contrasena : {password}")
        print()
    print("TOKEN DEL WORKER")
    print("=" * 68)
    print("  Agrega esta linea al .env para que el worker publique eventos:")
    print()
    print(f"      API_TOKEN={cfg.ingest_token}")
    print()
    print("=" * 68)
    print("SIGUIENTE PASO")
    print("=" * 68)
    print("  1. uvicorn api.main:app --host 0.0.0.0 --port 8000")
    print("  2. Abre http://localhost:8000")
    print("  3. En otra terminal: python -m edge.worker")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
