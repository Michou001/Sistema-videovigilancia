"""Base de datos temporal para las pruebas: SQLite por defecto, o PostgreSQL.

Las pruebas de la API corren contra un SQLite en una carpeta temporal. Para
correrlas contra PostgreSQL (el motor recomendado con varias camaras), define
PRUEBAS_POSTGRES con la URL de un servidor donde se puedan crear bases:

    PRUEBAS_POSTGRES="postgresql+psycopg://postgres@localhost:5432/postgres" \\
        python tests/correr_todas.py

Cada archivo de pruebas crea su propia base con nombre unico y la borra al
terminar.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path


def url_temporal(carpeta: Path, nombre: str) -> str:
    base = os.getenv("PRUEBAS_POSTGRES", "").strip()
    if not base:
        return f"sqlite:///{(carpeta / f'{nombre}.db').as_posix()}"
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    bd = f"goss_prueba_{nombre}_{uuid.uuid4().hex[:8]}"
    motor = create_engine(base, isolation_level="AUTOCOMMIT")
    with motor.connect() as con:
        con.execute(text(f'CREATE DATABASE "{bd}"'))
    motor.dispose()
    return make_url(base).set(database=bd).render_as_string(hide_password=False)


def borrar(url: str) -> None:
    if not url.startswith("postgresql"):
        return
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    destino = make_url(url)
    motor = create_engine(destino.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with motor.connect() as con:
        con.execute(text(f'DROP DATABASE IF EXISTS "{destino.database}" WITH (FORCE)'))
    motor.dispose()
