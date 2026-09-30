"""Entorno de Alembic para las migraciones de la base de datos.

Las migraciones se aplican solas al arrancar la API (ver api/database.py,
init_db). Este archivo tambien sirve para generar migraciones nuevas desde la
linea de comandos cuando cambia un modelo:

    alembic revision --autogenerate -m "que cambio"
    alembic upgrade head

La URL de la base de datos sale de la misma configuracion que usa la API
(DATABASE_URL en el .env), no de alembic.ini: asi no hay dos lugares donde
declarar a que base se conecta el sistema.
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import api.models  # noqa: E402,F401  - registra las tablas en la metadata

config = context.config
target_metadata = SQLModel.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from api.config import get_config

    return get_config().database_url


def _opciones(es_sqlite: bool) -> dict:
    # render_as_batch: SQLite no soporta ALTER TABLE completo (quitar o cambiar
    # columnas). En modo batch Alembic recrea la tabla por debajo, y en
    # PostgreSQL usa ALTER normal.
    return {"target_metadata": target_metadata, "render_as_batch": es_sqlite,
            "compare_type": False}


def run_migrations_offline() -> None:
    url = _url()
    context.configure(url=url, literal_binds=True,
                      dialect_opts={"paramstyle": "named"},
                      **_opciones(url.startswith("sqlite")))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # La API pasa su propia conexion (ver init_db): reutilizarla evita abrir un
    # segundo motor contra el mismo archivo SQLite con otros PRAGMA.
    conexion = config.attributes.get("connection")
    if conexion is not None:
        context.configure(connection=conexion,
                          **_opciones(conexion.dialect.name == "sqlite"))
        with context.begin_transaction():
            context.run_migrations()
        return

    seccion = config.get_section(config.config_ini_section) or {}
    seccion["sqlalchemy.url"] = _url()
    motor = engine_from_config(seccion, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with motor.connect() as conexion:
        context.configure(connection=conexion,
                          **_opciones(conexion.dialect.name == "sqlite"))
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
