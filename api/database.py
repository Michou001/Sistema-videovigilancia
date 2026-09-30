"""Motor de base de datos y sesiones."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlmodel import Session, create_engine

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

_opciones_pool: dict = {}
if not _es_sqlite:
    # PostgreSQL (recomendado desde 2-3 camaras: SQLite serializa todas las
    # escrituras). La ingesta, la vista en vivo y los dashboards piden
    # conexiones a la vez; pool_recycle evita usar conexiones que un firewall
    # o el propio servidor cerraron por inactividad.
    _opciones_pool = {"pool_size": 10, "max_overflow": 20, "pool_recycle": 1800,
                      "pool_pre_ping": True}

engine = create_engine(_cfg.database_url, echo=False, connect_args=_conectar_args,
                       **_opciones_pool)


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


# Ajustes que las versiones anteriores a Alembic aplicaban a mano en cada
# arranque: create_all() no agrega columnas ni indices a una tabla que ya
# existe. Solo se usan una vez, al pasar una base de datos vieja a Alembic,
# para garantizar que coincide exactamente con la revision base (0001).
_INDICES_LEGADO = [
    "CREATE INDEX IF NOT EXISTS ix_events_severity_ts ON events (severity, ts)",
    "CREATE INDEX IF NOT EXISTS ix_events_type_ts ON events (type, ts)",
]
_COLUMNAS_LEGADO = [
    ("alerts", "notes", "TEXT"),
]

_MIGRACIONES = Path(__file__).resolve().parent / "migraciones"
REVISION_BASE = "0001"


def _config_alembic(conexion):
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(_MIGRACIONES))
    # Alembic guarda las opciones en un ConfigParser, que interpreta "%": una
    # contrasena con "%" o una URL con caracteres codificados (host=%2Ftmp)
    # rompia el arranque con "invalid interpolation syntax".
    url = conexion.engine.url.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.attributes["connection"] = conexion
    return config


def _preparar_base_legada(conexion) -> None:
    for tabla, columna, tipo in _COLUMNAS_LEGADO:
        existentes = {c["name"] for c in inspect(conexion).get_columns(tabla)}
        if columna not in existentes:
            conexion.execute(text(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}"))
            log.info("Columna %s.%s agregada", tabla, columna)
    for sentencia in _INDICES_LEGADO:
        conexion.execute(text(sentencia))


def init_db() -> None:
    """Crea o actualiza el esquema con las migraciones de Alembic.

    Tres casos:
      - Base de datos nueva: se aplican todas las migraciones desde cero.
      - Base creada por una version anterior (sin Alembic): se completa a la
        revision base, se marca como tal y se aplican solo las posteriores.
        No se pierde ni se reescribe ningun dato.
      - Base ya bajo Alembic: se aplican las migraciones pendientes, si hay.

    Antes esto era create_all() mas una lista de ALTER TABLE a mano. Con
    PostgreSQL y varias versiones del sistema en campo, eso no escala: cada
    columna nueva era un parche mas en el arranque.
    """
    from alembic import command

    import api.models  # noqa: F401 - registra las tablas en la metadata

    # Alembic anuncia en INFO cada arranque ("Context impl SQLiteImpl..."): en
    # la consola de la API solo interesa cuando aplica una migracion de verdad.
    logging.getLogger("alembic").setLevel(logging.WARNING)

    with engine.begin() as conexion:
        tablas = set(inspect(conexion).get_table_names())
        config = _config_alembic(conexion)
        if "alembic_version" not in tablas and {"events", "operators"} <= tablas:
            log.info("Base de datos de una version anterior: se pasa a migraciones")
            _preparar_base_legada(conexion)
            command.stamp(config, REVISION_BASE)
        antes = _revision_actual(conexion)
        command.upgrade(config, "head")
        despues = _revision_actual(conexion)
        if antes != despues:
            log.info("Esquema de base de datos actualizado: %s -> %s", antes or "vacio", despues)

    if _es_sqlite:
        with engine.connect() as conexion:
            # Estadisticas para el planificador de consultas: con ellas elige
            # el indice correcto en vez de recorrer la tabla.
            conexion.execute(text("PRAGMA optimize"))
            conexion.commit()

    log.info("Base de datos lista: %s", _url_sin_clave(_cfg.database_url))


def _revision_actual(conexion) -> str | None:
    from alembic.migration import MigrationContext

    return MigrationContext.configure(conexion).get_current_revision()


def _url_sin_clave(url: str) -> str:
    """La URL de PostgreSQL lleva la contrasena: no va a los logs."""
    from sqlalchemy.engine import make_url

    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001
        return url.split("@")[-1]


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
