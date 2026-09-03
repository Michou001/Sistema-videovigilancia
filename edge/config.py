"""Configuracion del worker de borde, leida de variables de entorno.

Nada de credenciales en el codigo. Copia `.env.example` a `.env` y editalo;
`.env` esta en .gitignore.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "si", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class EdgeConfig:
    # --- Identidad y fuente ------------------------------------------------
    camera_id: str = field(default_factory=lambda: os.getenv("CAMERA_ID", "cam-01"))
    source: str = field(default_factory=lambda: os.getenv("SOURCE", "webcam:0"))
    """Especificacion de fuente. Formatos aceptados (ver edge/sources.py):
         webcam:0            camara USB / integrada
         rtsp://user:pass@ip:554/Streaming/Channels/102
         file:videos/prueba.mp4
    """

    # --- Destino -----------------------------------------------------------
    api_url: str = field(default_factory=lambda: os.getenv("API_URL", "http://127.0.0.1:8000"))
    api_token: str = field(default_factory=lambda: os.getenv("API_TOKEN", ""))
    offline_dir: Path = BASE_DIR / "data" / "spool"
    """Si la API esta caida, los eventos se escriben aqui y se reenvian despues.
    El worker NUNCA debe morir porque la web no responda."""

    # --- Computo -----------------------------------------------------------
    device: str = field(default_factory=lambda: os.getenv("DEVICE", "auto"))  # auto|cuda|cpu
    infer_fps: float = field(default_factory=lambda: _env_float("INFER_FPS", 8.0))
    """FPS objetivo de INFERENCIA, no de captura. La camara entrega 25-30 fps;
    procesarlos todos satura la GPU sin ganar nada: un coche no cambia de placa
    en 33 ms. 8 fps es un buen punto de partida para 6 GB de VRAM."""

    imgsz: int = field(default_factory=lambda: _env_int("IMGSZ", 640))

    # --- Detectores activos ------------------------------------------------
    enable_plates: bool = field(default_factory=lambda: _env_bool("ENABLE_PLATES", True))
    enable_faces: bool = field(default_factory=lambda: _env_bool("ENABLE_FACES", False))
    enable_weapons: bool = field(default_factory=lambda: _env_bool("ENABLE_WEAPONS", False))

    # --- Modelos -----------------------------------------------------------
    plate_model: Path = field(
        default_factory=lambda: Path(os.getenv("PLATE_MODEL", "models/plates_yolov5.pt"))
    )
    """OJO: este .pt esta en formato YOLOv5 y Ultralytics NO puede cargarlo
    (verificado: lanza TypeError de incompatibilidad). En la Fase 2 hay que
    exportarlo a ONNX o reentrenarlo con YOLO11. Ver docs/camara-hikvision.md
    y el README."""
    weapon_model: Path = field(
        default_factory=lambda: Path(os.getenv("WEAPON_MODEL", "models/weapons.pt"))
    )
    face_model: str = field(default_factory=lambda: os.getenv("FACE_MODEL", "buffalo_l"))

    # --- Umbrales ----------------------------------------------------------
    plate_conf: float = field(default_factory=lambda: _env_float("PLATE_CONF", 0.45))
    ocr_conf: float = field(default_factory=lambda: _env_float("OCR_CONF", 0.35))
    weapon_conf: float = field(default_factory=lambda: _env_float("WEAPON_CONF", 0.60))
    face_conf: float = field(default_factory=lambda: _env_float("FACE_CONF", 0.50))

    # --- Confirmacion temporal (anti falso positivo) ----------------------
    weapon_confirm_hits: int = field(default_factory=lambda: _env_int("WEAPON_CONFIRM_HITS", 4))
    weapon_confirm_window: int = field(default_factory=lambda: _env_int("WEAPON_CONFIRM_WINDOW", 6))
    """Un arma solo alerta si se sostiene en >=4 de los ultimos 6 frames del
    MISMO track. Sin esto, cualquier celular o desarmador dispara alarmas y el
    sistema se vuelve inservible por saturacion de falsos positivos."""

    # --- Agregacion de tracks ---------------------------------------------
    track_timeout_s: float = field(default_factory=lambda: _env_float("TRACK_TIMEOUT_S", 2.0))
    """Segundos sin ver un track antes de darlo por cerrado y emitir su evento."""

    plate_dedupe_s: float = field(default_factory=lambda: _env_float("PLATE_DEDUPE_S", 60.0))
    """Ventana de supresion de placas repetidas, como red de seguridad.

    El tracking es el mecanismo principal para no duplicar (un track = un
    vehiculo), pero no es infalible: en pruebas se midio al detector perdiendo
    una placa durante 72 frames seguidos por el angulo. Cuando eso pasa, el
    track se cierra y al reaparecer nace uno nuevo, generando un segundo evento
    del mismo vehiculo.

    Esta ventana descarta la repeticion. Es distinta del COOLDOWN_SEGUNDOS=15
    del codigo original: aquello era el UNICO mecanismo y se aplicaba a ciegas;
    esto solo entra cuando el tracking ya fallo.

    Ponlo en 0 para desactivarlo (util en un estacionamiento donde el mismo
    vehiculo puede pasar dos veces en un minuto de forma legitima)."""

    # --- Evidencia ---------------------------------------------------------
    snapshot_dir: Path = BASE_DIR / "data" / "snapshots"
    send_snapshot_b64: bool = field(default_factory=lambda: _env_bool("SEND_SNAPSHOT_B64", True))

    # --- Depuracion --------------------------------------------------------
    show_window: bool = field(default_factory=lambda: _env_bool("SHOW_WINDOW", False))
    """Ventana de OpenCV con las cajas dibujadas. Util en desarrollo, SIEMPRE
    apagado en produccion (bloquea el hilo y no hay pantalla en un servidor)."""

    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    def __post_init__(self) -> None:
        # Rutas de modelo relativas se resuelven contra la raiz del proyecto
        if not self.plate_model.is_absolute():
            self.plate_model = BASE_DIR / self.plate_model
        if not self.weapon_model.is_absolute():
            self.weapon_model = BASE_DIR / self.weapon_model
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.offline_dir.mkdir(parents=True, exist_ok=True)

    def resolve_device(self) -> str:
        """Traduce device='auto' al dispositivo real disponible."""
        if self.device != "auto":
            return self.device
        try:
            import torch

            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:  # noqa: BLE001
            # No solo ImportError: una instalacion de torch a medias o sin los
            # DLL de CUDA truena con otras excepciones. Caer a CPU siempre es
            # preferible a que el worker no arranque.
            return "cpu"


def load_config() -> EdgeConfig:
    """Carga .env si existe (sin dependencia dura de python-dotenv) y arma la config."""
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return EdgeConfig()
