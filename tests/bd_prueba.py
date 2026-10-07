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

import atexit
import os
import shutil
import uuid
from pathlib import Path

# Fotos y clips de las pruebas en una carpeta PROPIA, nunca en data/snapshots
# ni data/clips. Antes las pruebas usaban una base de datos temporal con las
# carpetas reales, y la purga de "huerfanos" borro la evidencia de una prueba
# con la camara real (para la base temporal, ninguna de esas fotos tenia
# registro). Va dentro del proyecto porque la API guarda las rutas de
# evidencia relativas a la raiz. Todos los archivos de pruebas importan este
# modulo antes que la API; la carpeta se borra al terminar.
_EVIDENCIA = Path(__file__).resolve().parent.parent / "data" / f"pruebas-{uuid.uuid4().hex[:8]}"
os.environ["SNAPSHOT_DIR"] = str(_EVIDENCIA / "snapshots")
os.environ["CLIPS_DIR"] = str(_EVIDENCIA / "clips")
atexit.register(shutil.rmtree, _EVIDENCIA, ignore_errors=True)


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
