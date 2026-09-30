"""Configuracion del worker de borde, leida de variables de entorno.

Nada de credenciales en el codigo. Copia `.env.example` a `.env` y editalo;
`.env` esta en .gitignore.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
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
    enable_pose: bool = field(default_factory=lambda: _env_bool("ENABLE_POSE", True))
    """Esqueleto de cada persona (YOLO11-pose): caida por el angulo del torso,
    manos arriba y posible agresion. Con pose, la caida ya no se estima por la
    forma de la caja (ver edge/detectors/pose.py)."""
    enable_zonas: bool = field(default_factory=lambda: _env_bool("ENABLE_ZONAS", True))
    """Reglas por zona definidas en el dashboard (intrusion, cruce de linea,
    merodeo, conteo). Usan las personas y vehiculos que sigue el detector de
    movimiento: requieren ENABLE_MOTION=true."""

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
    motion_model: Path = field(
        default_factory=lambda: Path(os.getenv("MOTION_MODEL", "models/yolo11s.pt"))
    )
    """Modelo de personas para movimiento anomalo: .pt, o .engine/.onnx
    exportado con tools/optimizar_modelos.py (TensorRT es lo mas rapido)."""

    # --- Aceleracion (ver edge/aceleracion.py) ------------------------------
    yolo_half: str = field(default_factory=lambda: os.getenv("YOLO_HALF", "auto"))
    """auto | true | false. Media precision (FP16) en YOLO cuando hay CUDA."""
    ort_tensorrt: str = field(default_factory=lambda: os.getenv("ORT_TENSORRT", "false"))
    """true: onnxruntime usa TensorRT (FP16) para placas y rostros. La primera
    vez construye los motores (minutos); quedan en models/trt_cache."""
    hw_decode: str = field(default_factory=lambda: os.getenv("HW_DECODE", "off"))
    """off | auto | d3d11 | vaapi | qsv: decodificar el video en la GPU.
    Con varias camaras, el CPU se va en decodificar H.264."""

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

    # --- Clips de video de las alertas (ver edge/clips.py) -----------------
    clip_enabled: bool = field(default_factory=lambda: _env_bool("CLIP_ENABLED", True))
    clip_pre_s: float = field(default_factory=lambda: _env_float("CLIP_PRE_S", 10.0))
    """Segundos ANTES del evento. En placas el evento sale cuando el vehiculo
    deja la escena, asi que esto cubre su paso completo."""
    clip_post_s: float = field(default_factory=lambda: _env_float("CLIP_POST_S", 10.0))
    clip_ancho: int = field(default_factory=lambda: _env_int("CLIP_ANCHO", 960))
    clip_calidad: int = field(default_factory=lambda: _env_int("CLIP_CALIDAD", 80))
    clip_codec: str = field(default_factory=lambda: os.getenv("CLIP_CODEC", "auto"))
    """auto | vp8 | h264. h264 requiere `pip install av`."""

    # --- Zonas (ver edge/zonas.py) -------------------------------------------
    zonas_refresco_s: float = field(default_factory=lambda: _env_float("ZONAS_REFRESCO_S", 15.0))
    """Cada cuanto se pregunta a la API si cambiaron las zonas de la camara."""
    zonas_frames_min: int = field(default_factory=lambda: _env_int("ZONAS_FRAMES_MIN", 3))
    """Frames seguidos dentro de una zona para contar como intrusion: una caja
    que parpadea sobre el borde no es alguien entrando."""

    # --- Pose (ver edge/detectors/pose.py) -----------------------------------
    pose_model: Path = field(
        default_factory=lambda: Path(os.getenv("POSE_MODEL", "models/yolo11n-pose.pt")))
    pose_conf: float = field(default_factory=lambda: _env_float("POSE_CONF", 0.45))
    manos_arriba_s: float = field(default_factory=lambda: _env_float("MANOS_ARRIBA_S", 2.0))
    """Segundos con las dos manos por encima de la cabeza para avisar."""
    pose_agresion: bool = field(default_factory=lambda: _env_bool("POSE_AGRESION", True))

    # --- Reentrenamiento (ver edge/dataset.py) --------------------------------
    dataset_enabled: bool = field(default_factory=lambda: _env_bool("DATASET_ENABLED", False))
    """Guardar el cuadro limpio de cada alerta para armar datasets con el
    veredicto de los operadores (tools/dataset_alertas.py)."""
    dataset_tipos: str = field(default_factory=lambda: os.getenv("DATASET_TIPOS", "weapon,anomaly,zone"))
    dataset_dias: int = field(default_factory=lambda: _env_int("RETENCION_DATASET_DIAS", 30))

    # --- Eventos de la propia camara Hikvision (ver edge/isapi.py) -----------
    isapi: str = field(default_factory=lambda: os.getenv("ISAPI", "auto"))
    """auto | true | false. auto: si SOURCE es rtsp:// con usuario y
    contrasena, se escucha el alertStream de la camara (sabotaje, perdida de
    video, cruce de linea e intrusion de su propia analitica)."""
    isapi_puerto: int = field(default_factory=lambda: _env_int("ISAPI_PUERTO", 80))
    isapi_https: bool = field(default_factory=lambda: _env_bool("ISAPI_HTTPS", False))
    isapi_eventos: str = field(default_factory=lambda: os.getenv(
        "ISAPI_EVENTOS", "sabotaje,perdida_video,deteccion_linea,intrusion_camara"))
    """Cuales reenviar a la API. movimiento_camara (VMD) es muy ruidoso y va
    apagado por defecto."""

    # --- Depuracion --------------------------------------------------------
    show_window: bool = field(default_factory=lambda: _env_bool("SHOW_WINDOW", False))
    """Ventana de OpenCV con las cajas dibujadas. Util en desarrollo, SIEMPRE
    apagado en produccion (bloquea el hilo y no hay pantalla en un servidor)."""

    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    def __post_init__(self) -> None:
        import re

        from shared.events import PATRON_CAMARA

        # El identificador termina en URLs, nombres de archivo y en el HTML del
        # dashboard; la API rechaza cualquier otro. Mejor fallar al arrancar
        # con un mensaje claro que ver todos los eventos rebotar con 422.
        if not re.fullmatch(PATRON_CAMARA, self.camera_id):
            raise ValueError(f"CAMERA_ID='{self.camera_id}' no es valido: usa letras, numeros, "
                             "'-', '_' o '.' (maximo 64), p.ej. cam-entrada")
        # Rutas de modelo relativas se resuelven contra la raiz del proyecto
        if not self.weapon_model.is_absolute():
            self.weapon_model = BASE_DIR / self.weapon_model
        if not self.motion_model.is_absolute():
            self.motion_model = BASE_DIR / self.motion_model
        if not self.pose_model.is_absolute():
            self.pose_model = BASE_DIR / self.pose_model
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


def ruta_env(env_file: "str | os.PathLike | None" = None) -> Path:
    """Archivo de entorno a usar: el argumento explicito (--env del CLI) > la
    variable EDGE_ENV_FILE (por si se prefiere fijarla en el .bat de arranque)
    > `.env` de siempre."""
    ruta = Path(env_file) if env_file else Path(os.getenv("EDGE_ENV_FILE", "") or (BASE_DIR / ".env"))
    return ruta if ruta.is_absolute() else BASE_DIR / ruta


def leer_env(ruta: Path) -> dict[str, str]:
    valores: dict[str, str] = {}
    if ruta.exists():
        for line in ruta.read_text(encoding="utf-8-sig").splitlines():
            par = parse_env_line(line)
            if par is not None:
                valores[par[0]] = par[1]
    return valores


@contextmanager
def _entorno_superpuesto(valores: dict[str, str]):
    """Pone `valores` en el entorno solo mientras se arma una config.

    Mismo criterio que setdefault: una variable que ya existe en el entorno
    real gana sobre el archivo. Al salir, el entorno queda como estaba.
    """
    agregadas = [k for k in valores if k not in os.environ]
    for k in agregadas:
        os.environ[k] = valores[k]
    try:
        yield
    finally:
        for k in agregadas:
            os.environ.pop(k, None)


def load_config(env_file: "str | os.PathLike | None" = None, *,
                aplicar_entorno: bool = True) -> EdgeConfig:
    """Carga un archivo de entorno (sin dependencia dura de python-dotenv) y arma la config.

    Por que un parametro y no solo `.env` fijo: cada camara necesita su PROPIO
    CAMERA_ID/SOURCE, y con un unico `.env` fijo la segunda pisaria la config
    de la primera.

    `aplicar_entorno=True` (lo de siempre) deja los valores en os.environ: la
    API lee de ahi su configuracion. Con varias camaras en un proceso se usa
    False: cada archivo se aplica solo mientras se arma SU config, y el de una
    camara no contamina a la siguiente.
    """
    valores = leer_env(ruta_env(env_file))
    if aplicar_entorno:
        for clave, valor in valores.items():
            os.environ.setdefault(clave, valor)
        return EdgeConfig()
    with _entorno_superpuesto(valores):
        return EdgeConfig()


def cargar_configuraciones(archivos: list[str]) -> list[EdgeConfig]:
    """Una config por archivo de entorno, para correr varias camaras en un
    solo proceso. Los identificadores de camara no pueden repetirse."""
    if len(archivos) <= 1:
        return [load_config(archivos[0] if archivos else None)]
    configs = [load_config(a, aplicar_entorno=False) for a in archivos]
    ids = [c.camera_id for c in configs]
    repetidos = {i for i in ids if ids.count(i) > 1}
    if repetidos:
        raise ValueError(f"CAMERA_ID repetido en varios archivos: {', '.join(sorted(repetidos))}")
    return configs
