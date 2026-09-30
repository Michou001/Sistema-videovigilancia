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

    weapon_imgsz: int = field(default_factory=lambda: _env_int("WEAPON_IMGSZ", 960))
    """Resolucion de inferencia SOLO para el detector de armas, aparte de IMGSZ.

    Un cuchillo en una mano ocupa una fraccion minuscula del cuadro. Si se
    reescala a 640 px como el resto de detectores, se encoge todavia mas y el
    modelo pierde el poco detalle que tenia para distinguirlo. Subirlo a 960
    cuesta mas computo, pero hay margen de sobra: los tres detectores juntos
    usan ~50ms de un presupuesto de 125ms a 8 fps."""

    # --- Detectores activos ------------------------------------------------
    enable_plates: bool = field(default_factory=lambda: _env_bool("ENABLE_PLATES", True))
    enable_faces: bool = field(default_factory=lambda: _env_bool("ENABLE_FACES", False))
    enable_weapons: bool = field(default_factory=lambda: _env_bool("ENABLE_WEAPONS", False))
    """Apagado por defecto: COCO clasifica objetos limpios sobre fondo simple,
    no un cuchillo en la mano de alguien en penumbra. Medido en pruebas reales:
    no detecto. El codigo se deja listo para cuando exista un modelo entrenado
    especificamente para armas (ver la Fase 5 del README)."""
    enable_motion: bool = field(default_factory=lambda: _env_bool("ENABLE_MOTION", True))

    # --- Modelos -----------------------------------------------------------
    plate_detector_model: str = field(
        default_factory=lambda: os.getenv("PLATE_DETECTOR", "yolo-v9-s-608-license-plate-end2end")
    )
    """Detector de placas (open-image-models, ONNX). La variante "t-640" es
    mas ligera si la GPU va justa; la "s-608" es la mas precisa."""

    plate_ocr_model: str = field(
        default_factory=lambda: os.getenv("PLATE_OCR", "cct-s-v2-global-model")
    )
    """OCR de placas (fast-plate-ocr, ONNX). "cct-xs-v2-global-model" es la
    variante mas rapida."""
    weapon_model: Path = field(
        default_factory=lambda: Path(os.getenv("WEAPON_MODEL", "models/weapons.pt"))
    )
    face_model: str = field(default_factory=lambda: os.getenv("FACE_MODEL", "buffalo_l"))

    # --- Umbrales ----------------------------------------------------------
    plate_conf: float = field(default_factory=lambda: _env_float("PLATE_CONF", 0.45))
    ocr_conf: float = field(default_factory=lambda: _env_float("OCR_CONF", 0.35))
    weapon_conf: float = field(default_factory=lambda: _env_float("WEAPON_CONF", 0.40))
    face_conf: float = field(default_factory=lambda: _env_float("FACE_CONF", 0.50))
    motion_conf: float = field(default_factory=lambda: _env_float("MOTION_CONF", 0.45))
    """Confianza para la deteccion de PERSONAS (clase 'person' de COCO), no del
    movimiento en si. Es una de las clases mejor entrenadas de COCO -- a
    diferencia de un cuchillo, aqui 0.45 es holgado, no arriesgado."""

    motion_speed_threshold: float = field(
        default_factory=lambda: _env_float("MOTION_SPEED_THRESHOLD", 2.5)
    )
    """Velocidad, en 'alturas de cuerpo por segundo', a partir de la cual un
    desplazamiento se considera subito.

    Se normaliza por la altura de la caja (no en pixeles crudos) para que una
    persona lejos de la camara -- que se mueve pocos pixeles para el mismo
    movimiento fisico -- no quede exenta, y una persona cerca no dispare por
    simple cercania. Caminar normal ronda 0.8-1.2; correr o un movimiento
    brusco (forcejeo, un golpe, un arranque subito) pasa de 2.5 con margen."""

    # --- Confirmacion temporal (anti falso positivo) ----------------------
    weapon_confirm_hits: int = field(default_factory=lambda: _env_int("WEAPON_CONFIRM_HITS", 4))
    weapon_confirm_window: int = field(default_factory=lambda: _env_int("WEAPON_CONFIRM_WINDOW", 6))
    """Un arma solo alerta si se sostiene en >=4 de los ultimos 6 frames del
    MISMO track. Sin esto, cualquier celular o desarmador dispara alarmas y el
    sistema se vuelve inservible por saturacion de falsos positivos."""

    motion_confirm_hits: int = field(default_factory=lambda: _env_int("MOTION_CONFIRM_HITS", 3))
    motion_confirm_window: int = field(default_factory=lambda: _env_int("MOTION_CONFIRM_WINDOW", 5))
    """Un movimiento subito solo alerta si >=3 de las ultimas 5 lecturas de
    velocidad del MISMO track superaron el umbral. La ventana es mas corta que
    la de armas a proposito: un forcejeo o un golpe dura menos de un segundo,
    y esperar una ventana larga significaria perderlo por completo."""

    motion_cooldown_s: float = field(default_factory=lambda: _env_float("MOTION_COOLDOWN_S", 30.0))
    """Tras alertar por una persona, cuanto esperar antes de poder alertar otra
    vez por la MISMA persona seguida. Antes una persona solo podia alertar una
    vez mientras siguiera en cuadro: un segundo incidente minutos despues, con
    el mismo track vivo, pasaba en silencio."""

    # --- Rostros -----------------------------------------------------------
    face_min_width: int = field(default_factory=lambda: _env_int("FACE_MIN_WIDTH", 50))
    """Ancho minimo en pixeles para usar un rostro. Por debajo, el embedding
    no tiene resolucion suficiente y un falso positivo biometrico senala a la
    persona equivocada."""

    face_template_size: int = field(default_factory=lambda: _env_int("FACE_TEMPLATE_SIZE", 3))
    """Cuantos embeddings de mejor calidad se promedian por persona seguida.
    Promediar varias vistas buenas del mismo rostro da un vector mas estable
    que la mejor vista sola (menos sensible a un gesto o a un desenfoque)."""

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
    send_snapshot_b64: str = field(
        default_factory=lambda: os.getenv("SEND_SNAPSHOT_B64", "auto").strip().lower()
    )
    """auto | true | false. Adjunta la captura al evento en base64 para que la
    API la guarde. Solo hace falta si la API corre en OTRA maquina (no comparte
    el disco del worker); `auto` lo activa cuando API_URL no es localhost."""

    heartbeat_s: float = field(default_factory=lambda: _env_float("HEARTBEAT_S", 15.0))
    """Cada cuanto el worker reporta su salud a la API aunque no haya eventos."""

    snapshot_hd_enabled: bool = field(default_factory=lambda: _env_bool("SNAPSHOT_HD_ENABLED", True))
    """Pide una foto del canal PRINCIPAL de la Hikvision (no el sub-stream de
    deteccion) para guardar como evidencia de rostros, placas y movimiento.
    Solo funciona si SOURCE es una URL rtsp:// con usuario y contrasena (o
    sea, una Hikvision real). Con webcam o archivo de video no hace nada."""

    snapshot_hd_channel: str = field(default_factory=lambda: os.getenv("SNAPSHOT_HD_CHANNEL", "101"))
    """Canal ISAPI del stream principal. 101 es el de fabrica en Hikvision
    para el primer canal; en un NVR con varias camaras seria 201, 301, etc."""

    # --- Vista en vivo en el dashboard -------------------------------------
    preview_enabled: bool = field(default_factory=lambda: _env_bool("PREVIEW_ENABLED", True))
    """Manda el frame anotado a la API para que el operador vea la camara en el
    navegador. No cuesta nada mientras nadie tenga el dashboard abierto: la API
    responde cuantos lo estan mirando y el worker deja de enviar si son cero.

    Apagalo si el enlace hasta la API es estrecho (un 4G compartido) o si por
    politica el video no debe salir de la red de las camaras: los eventos y sus
    capturas siguen llegando igual, solo se pierde el video en vivo."""

    preview_fps: float = field(default_factory=lambda: _env_float("PREVIEW_FPS", 6.0))
    """Fps del preview, independiente de INFER_FPS. Por encima de ~8 no se
    aprecia diferencia en una rejilla de camaras y cada frame cuesta red."""

    preview_width: int = field(default_factory=lambda: _env_int("PREVIEW_WIDTH", 640))
    """Ancho al que se reduce antes de enviar. 640 px se ve bien en un recuadro
    del dashboard y pesa ~8x menos que 1080p."""

    preview_quality: int = field(default_factory=lambda: _env_int("PREVIEW_QUALITY", 70))
    """Calidad JPEG (20-95). 70 es el punto donde el artefacto todavia no se
    nota y el tamano ya bajo bastante."""

    # --- Depuracion --------------------------------------------------------
    show_window: bool = field(default_factory=lambda: _env_bool("SHOW_WINDOW", False))
    """Ventana de OpenCV con las cajas dibujadas. Util en desarrollo, SIEMPRE
    apagado en produccion (bloquea el hilo y no hay pantalla en un servidor)."""

    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    def __post_init__(self) -> None:
        # Rutas de modelo relativas se resuelven contra la raiz del proyecto
        if not self.weapon_model.is_absolute():
            self.weapon_model = BASE_DIR / self.weapon_model
        if self.send_snapshot_b64 in {"1", "yes", "si", "on"}:
            self.send_snapshot_b64 = "true"
        elif self.send_snapshot_b64 not in {"auto", "true"}:
            self.send_snapshot_b64 = "false"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.offline_dir.mkdir(parents=True, exist_ok=True)

    def resolve_device(self) -> str:
        """Traduce device='auto' al dispositivo real disponible."""
        if self.device != "auto":
            return self.device
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
        except Exception:  # noqa: BLE001
            # No solo ImportError: una instalacion de torch a medias o sin los
            # DLL de CUDA truena con otras excepciones. Caer a CPU siempre es
            # preferible a que el worker no arranque.
            pass

        # Sin esto, un worker corriendo por error con el Python global (sin
        # CUDA) en vez del venv del proyecto simplemente se ve "lento" en la
        # consola, sin ninguna pista de por que. Tres modelos (placas, rostros,
        # armas) por CPU en cada frame es la causa mas comun de que la camara
        # "no rinda": el cuello de botella no es la camara ni la red, es correr
        # el interprete equivocado. Usa iniciar_worker.bat o activa el venv.
        import sys

        print("=" * 68, file=sys.stderr)
        print("[!] SIN GPU: corriendo por CPU. Va a ir MUY lento (varios", file=sys.stderr)
        print("    detectores por frame en CPU pueden tardar 10-20x mas que en GPU).",
              file=sys.stderr)
        print(f"    Interprete actual: {sys.executable}", file=sys.stderr)
        print("    Si tienes GPU NVIDIA, seguramente estas corriendo con el", file=sys.stderr)
        print("    Python del sistema en vez del venv del proyecto. Usa:", file=sys.stderr)
        print("      iniciar_worker.bat   (o venv\\Scripts\\python.exe -m edge.worker)",
              file=sys.stderr)
        print("=" * 68, file=sys.stderr)
        return "cpu"


def parse_env_line(line: str) -> "tuple[str, str] | None":
    """Interpreta una linea `CLAVE=valor` de un archivo de entorno.

    Quita el comentario al final de la linea (`ENABLE_MOTION=true  # nota`).
    Sin esto el valor era "true  # nota": no contaba como verdadero y el
    detector quedaba apagado, y DEVICE=auto con comentario llegaba a torch
    como nombre de dispositivo. Solo se corta en un `#` precedido de espacio,
    para no romper una contrasena que lleve `#` pegado.
    """
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, _, value = line.partition("=")
    value = value.strip()
    if value[:1] in {'"', "'"} and value[:1] in value[1:]:
        value = value[1:value.index(value[0], 1)]
    else:
        for i, c in enumerate(value):
            if c == "#" and i > 0 and value[i - 1] in " \t":
                value = value[:i].rstrip()
                break
    return key.strip(), value


def load_config(env_file: "str | os.PathLike | None" = None) -> EdgeConfig:
    """Carga un archivo de entorno (sin dependencia dura de python-dotenv) y arma la config.

    Por que un parametro y no solo `.env` fijo: dos camaras necesitan dos
    procesos de worker, y cada uno necesita su PROPIO CAMERA_ID/SOURCE. Con un
    unico `.env` fijo, el segundo worker pisaria la config del primero. La
    resolucion es, en orden: el argumento explicito (--env del CLI) > la
    variable EDGE_ENV_FILE (por si se prefiere fijarla en el .bat de arranque
    en vez de pasarla por linea de comandos) > `.env` de siempre.
    """
    ruta = Path(env_file) if env_file else Path(os.getenv("EDGE_ENV_FILE", "") or (BASE_DIR / ".env"))
    if not ruta.is_absolute():
        ruta = BASE_DIR / ruta
    if ruta.exists():
        for line in ruta.read_text(encoding="utf-8-sig").splitlines():
            par = parse_env_line(line)
            if par is not None:
                os.environ.setdefault(*par)
    return EdgeConfig()
