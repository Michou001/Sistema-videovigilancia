"""Configuracion de la API."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _roles_2fa(valor: str) -> frozenset[str]:
    roles = {"viewer", "operator", "admin"}
    pedidos = {r.strip().lower() for r in valor.split(",") if r.strip()}
    if pedidos & {"todos", "all", "*"}:
        return frozenset(roles)
    return frozenset(pedidos & roles)


def ruta_guardable(ruta: Path) -> str:
    """Como se guarda en la base la ruta de un archivo de evidencia: relativa a
    la raiz del proyecto si esta dentro (portable entre equipos), absoluta si
    SNAPSHOT_DIR/CLIPS_DIR apuntan a otro disco. Quien la lee hace
    `BASE_DIR / ruta`, que con una ruta absoluta devuelve la misma ruta."""
    try:
        return ruta.relative_to(BASE_DIR).as_posix()
    except ValueError:
        return ruta.resolve().as_posix()


def _carpeta(variable: str, defecto: str) -> Path:
    """Carpeta de evidencia. Relativa a la raiz del proyecto si no es
    absoluta: las rutas de evidencia se guardan relativas a la raiz."""
    ruta = Path(os.getenv(variable) or BASE_DIR / "data" / defecto)
    return ruta if ruta.is_absolute() else BASE_DIR / ruta


@dataclass
class ApiConfig:
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", f"sqlite:///{(BASE_DIR / 'data' / 'vigilancia.db').as_posix()}"
        )
    )

    jwt_secret: str = field(default_factory=lambda: os.getenv("JWT_SECRET", ""))
    jwt_hours: int = field(default_factory=lambda: _env_int("JWT_HOURS", 12))

    exigir_2fa: frozenset[str] = field(default_factory=lambda: _roles_2fa(os.getenv("EXIGIR_2FA", "")))
    """Roles que DEBEN tener la verificacion en dos pasos (EXIGIR_2FA=admin,operator,
    o "todos"). Quien no la tenga entra, pero solo puede darla de alta: el resto
    de la API le responde 403 hasta que lo haga. Vacio = opcional para todos."""

    fotos_rostro_sin_coincidencia: bool = field(
        default_factory=lambda: os.getenv("FOTOS_ROSTRO_SIN_COINCIDENCIA", "").strip().lower()
        in {"1", "true", "si", "sí", "yes"})
    """Conservar la foto de un rostro que NO coincidio con la lista. Por defecto
    no: de quien solo paso frente a la camara queda el evento (hora, camara),
    no su cara. Activarlo exige declararlo en el aviso de privacidad."""

    ingest_token: str = field(default_factory=lambda: os.getenv("API_TOKEN", ""))
    """Token que el worker de borde presenta para publicar eventos. Es un
    secreto compartido, no un JWT: el borde es una maquina, no una persona, y
    no tiene sesion que expirar."""

    # --- Umbrales de coincidencia -----------------------------------------
    plate_fuzzy_max_dist: int = field(default_factory=lambda: _env_int("PLATE_FUZZY_MAX_DIST", 1))
    face_match_threshold: float = field(
        default_factory=lambda: _env_float("FACE_MATCH_THRESHOLD", 0.50)
    )
    """Similitud coseno minima para declarar coincidencia facial (InsightFace).
    0.50 es el punto de partida habitual; subirlo reduce falsos positivos a
    costa de perder coincidencias reales."""

    snapshot_dir: Path = field(default_factory=lambda: _carpeta("SNAPSHOT_DIR", "snapshots"))
    clips_dir: Path = field(default_factory=lambda: _carpeta("CLIPS_DIR", "clips"))
    """Clips de video de las alertas (ver edge/clips.py). Carpeta aparte de las
    fotos porque pesan 100 veces mas y conviene poder moverla a otro disco."""
    web_dir: Path = BASE_DIR / "web"

    def __post_init__(self) -> None:
        (BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.clips_dir.mkdir(parents=True, exist_ok=True)

        if not self.jwt_secret:
            # Se genera y se persiste en disco en vez de usar un valor por
            # defecto en el codigo: un secreto de desarrollo hardcodeado acaba
            # siempre en produccion. Al ser un archivo, .gitignore lo cubre.
            archivo = BASE_DIR / "data" / ".jwt_secret"
            if archivo.exists():
                self.jwt_secret = archivo.read_text(encoding="utf-8").strip()
            else:
                self.jwt_secret = secrets.token_urlsafe(48)
                archivo.write_text(self.jwt_secret, encoding="utf-8")

        if not self.ingest_token:
            archivo = BASE_DIR / "data" / ".ingest_token"
            if archivo.exists():
                self.ingest_token = archivo.read_text(encoding="utf-8").strip()
            else:
                self.ingest_token = secrets.token_urlsafe(32)
                archivo.write_text(self.ingest_token, encoding="utf-8")


_config: ApiConfig | None = None


def get_config() -> ApiConfig:
    global _config
    if _config is None:
        # Reutiliza el cargador de .env del borde para no duplicar la logica.
        from edge.config import load_config as _cargar_env

        _cargar_env()
        _config = ApiConfig()
    return _config
