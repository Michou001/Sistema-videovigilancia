"""Motor de base de datos y sesiones."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine

from api.config import get_config

log = logging.getLogger(__name__)

_cfg = get_config()
_es_sqlite = _cfg.database_url.startswith("sqlite")

_conectar_args = {}
if _es_sqlite:
    # check_same_thread=False: FastAPI atiende peticiones en varios hilos y
    # SQLite por defecto rechaza usar una conexion fuera del hilo que la creo.
    _conectar_args["check_same_thread"] = False
    _conectar_args["timeout"] = 10

engine = create_engine(_cfg.database_url, echo=False, connect_args=_conectar_args,
                       pool_pre_ping=not _es_sqlite)


if _es_sqlite:
    @event.listens_for(engine, "connect")
    def _pragmas(conexion, _registro) -> None:
        """Ajustes por CONEXION, no por base de datos.

        journal_mode=WAL se queda grabado en el archivo, pero synchronous y
        busy_timeout valen solo para la conexion que los ejecuta. Antes se
        fijaban una vez en init_db() y el resto de conexiones del pool corria
        con synchronous=FULL (un fsync por commit) y sin espera ante bloqueo:
        con la ingesta, la purga y el re-escaneo escribiendo a la vez, la
        segunda escritura fallaba con "database is locked" en vez de esperar.
        """
        cur = conexion.cursor()
        cur.execute("PRAGMA journal_mode=WAL")      # lectores no bloquean al escritor
        cur.execute("PRAGMA synchronous=NORMAL")    # seguro con WAL y mucho mas rapido
        cur.execute("PRAGMA busy_timeout=10000")    # esperar hasta 10 s un bloqueo
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA cache_size=-20000")     # ~20 MB de cache de paginas
        cur.close()


# Indices que se agregaron despues de crear las primeras bases de datos.
# create_all() solo crea tablas nuevas; sobre una tabla existente no agrega
# indices, asi que se crean aqui con IF NOT EXISTS.
_INDICES_EXTRA = [
    # Retencion: "eventos info anteriores a X" y re-escaneo por tipo y fecha.
    "CREATE INDEX IF NOT EXISTS ix_events_severity_ts ON events (severity, ts)",
    "CREATE INDEX IF NOT EXISTS ix_events_type_ts ON events (type, ts)",
]


def init_db() -> None:
    """Crea las tablas si no existen. Importa los modelos primero para que
    SQLModel los registre en su metadata."""
    import api.models  # noqa: F401

    SQLModel.metadata.create_all(engine)

    if _es_sqlite:
        with engine.connect() as con:
            for sentencia in _INDICES_EXTRA:
                con.execute(text(sentencia))
            # Estadisticas para el planificador de consultas: con ellas elige
            # el indice correcto en vez de recorrer la tabla.
            con.execute(text("PRAGMA optimize"))
            con.commit()

    log.info("Base de datos lista: %s", _cfg.database_url)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
