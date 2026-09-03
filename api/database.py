"""Motor de base de datos y sesiones."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlmodel import Session, SQLModel, create_engine

from api.config import get_config

log = logging.getLogger(__name__)

_cfg = get_config()

_conectar_args = {}
if _cfg.database_url.startswith("sqlite"):
    # check_same_thread=False: FastAPI atiende peticiones en varios hilos y
    # SQLite por defecto rechaza usar una conexion fuera del hilo que la creo.
    _conectar_args["check_same_thread"] = False

engine = create_engine(_cfg.database_url, echo=False, connect_args=_conectar_args)


def init_db() -> None:
    """Crea las tablas si no existen. Importa los modelos primero para que
    SQLModel los registre en su metadata."""
    import api.models  # noqa: F401

    SQLModel.metadata.create_all(engine)

    if _cfg.database_url.startswith("sqlite"):
        # WAL permite leer mientras se escribe. Sin esto, el dashboard
        # consultando eventos bloquea al worker que intenta insertarlos.
        with engine.connect() as con:
            from sqlalchemy import text

            con.execute(text("PRAGMA journal_mode=WAL"))
            con.execute(text("PRAGMA synchronous=NORMAL"))
            con.commit()

    log.info("Base de datos lista: %s", _cfg.database_url)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
