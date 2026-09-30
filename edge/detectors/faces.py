"""Detector de rostros con InsightFace: deteccion + embedding de 512 dimensiones.

Por que InsightFace y no face_recognition/dlib (lo que usa FamNet): dlib exige
compilar con CMake y Visual Studio Build Tools en Windows, y cuando falla, el
proyecto termina cayendo a un comparador de histogramas que no sirve para
identificar a nadie. InsightFace instala con pip, corre en GPU y da embeddings
de 512-d entrenados con ArcFace, muy por encima de los 128-d de dlib.

AVISO DE PRIVACIDAD: los embeddings que produce este modulo son datos
personales biometricos y, bajo la LFPDPPP, DATOS SENSIBLES. Requieren aviso
visible en el punto de captura, consentimiento expreso y politica de retencion.
No los escribas en logs ni los expongas en endpoints publicos.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import warnings
from typing import Any, Optional

# InsightFace llama a una API de scikit-image ya deprecada (`tform.estimate`).
# El aviso se emite UNA VEZ POR ROSTRO Y POR FRAME, asi que con una persona
# frente a la camara salen ~20 lineas por segundo y la salida util del worker
# queda enterrada. No es un error y el codigo es de terceros: no hay nada que
# corregir, solo que callar.
warnings.filterwarnings("ignore", category=FutureWarning, module="insightface.*")
warnings.filterwarnings("ignore", message=r".*`estimate` is deprecated.*")
warnings.filterwarnings("ignore", message=r".*SimilarityTransform\.from_estimate.*")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from edge.config import BASE_DIR, EdgeConfig  # noqa: E402
from edge.detectors.base import Detector  # noqa: E402
from edge.snapshot_hd import SnapshotHD, escalar_bbox  # noqa: E402
from edge.sources import FrameInfo  # noqa: E402
from edge.tracking import Deteccion, IoUTracker, Track  # noqa: E402
from shared.events import BBox, DetectionEvent, EventType  # noqa: E402

log = logging.getLogger(__name__)

_onnx_configurado = False


def configurar_onnx_gpu() -> None:
    """Hace visibles a onnxruntime las DLL de CUDA que ya trae PyTorch.

    onnxruntime-gpu no incluye el runtime de CUDA: lo busca en el PATH del
    sistema. Como PyTorch ya empaqueta cuDNN 9 y cuBLAS 12 en torch/lib, basta
    con anadir ese directorio y evitamos instalar CUDA aparte.

    Ojo con la version: onnxruntime-gpu 1.23+ esta compilado contra CUDA 13 y
    pide cudart64_13.dll, que PyTorch cu126 no trae. Falla EN SILENCIO cayendo
    a CPU (6x mas lento) sin lanzar ningun error. Por eso requirements.txt fija
    onnxruntime-gpu==1.22.0, que es la ultima build de CUDA 12.
    """
    global _onnx_configurado
    if _onnx_configurado:
        return
    _onnx_configurado = True

    # os.add_dll_directory solo existe en Windows: es el mecanismo de busqueda
    # de DLLs de ese sistema. En Linux las bibliotecas de CUDA se resuelven por
    # LD_LIBRARY_PATH o por los paquetes nvidia-*-cu12 de pip, sin intervencion.
    if sys.platform != "win32":
        return

    try:
        import torch

        ruta = os.path.join(os.path.dirname(torch.__file__), "lib")
        if os.path.isdir(ruta):
            os.add_dll_directory(ruta)
    except Exception as e:  # noqa: BLE001
        log.debug("No se pudo anadir el directorio de DLLs de torch: %s", e)


class FaceEmbedder:
    """Envoltorio de InsightFace. Lo usan el detector y el alta en lista negra.

    Se aisla en su propia clase para que la API pueda calcular el embedding de
    una foto al dar de alta a una persona, sin arrastrar todo el detector.
    """

    _instancia: Optional["FaceEmbedder"] = None

    def __init__(self, nombre_modelo: str = "buffalo_l", device: str = "cuda",
                 det_size: int = 640, tensorrt: str = "false") -> None:
        configurar_onnx_gpu()
        from insightface.app import FaceAnalysis

        from edge.aceleracion import proveedores_onnx

        proveedores = proveedores_onnx(device, tensorrt)

        t0 = time.perf_counter()
        # Solo deteccion y reconocimiento: los modulos de landmarks y de
        # edad/genero no se usan y ocupan VRAM que hace falta para el modelo de
        # placas. Con 6 GB compartidos, cada modelo de mas cuenta.
        #
        # InsightFace imprime directo a stdout el volcado completo de opciones
        # de cada proveedor de onnxruntime -- decenas de lineas por modelo, sin
        # opcion de silenciarlo. Ahoga la salida util del worker, asi que se
        # captura durante la carga. Los errores siguen saliendo por stderr.
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            self.app = FaceAnalysis(
                name=nombre_modelo,
                providers=proveedores,
                allowed_modules=["detection", "recognition"],
            )
            self.app.prepare(ctx_id=0 if device == "cuda" else -1,
                             det_size=(det_size, det_size))

        real = self.app.models["detection"].session.get_providers()[0]
        log.info("InsightFace '%s' listo en %.1fs (%s)",
                 nombre_modelo, time.perf_counter() - t0, real)
        if device == "cuda" and real == "CPUExecutionProvider":
            log.warning(
                "InsightFace cayo a CPU pese a pedir GPU (6x mas lento). "
                "Causa habitual: onnxruntime-gpu compilado para otra version "
                "de CUDA que la de torch. Ver configurar_onnx_gpu()."
            )
        self.provider = real

    @classmethod
    def compartido(cls, cfg: EdgeConfig) -> "FaceEmbedder":
        """Una instancia por configuracion en todo el proceso: cargar el modelo
        dos veces duplicaria el uso de VRAM (ver edge/modelos.py). Las
        sesiones de onnxruntime admiten llamadas concurrentes de varias
        camaras."""
        from edge.modelos import compartido

        device = cfg.resolve_device()
        clave = ("insightface", cfg.face_model, device, cfg.imgsz, cfg.ort_tensorrt)
        instancia = compartido(clave, lambda: cls(cfg.face_model, device, cfg.imgsz, cfg.ort_tensorrt))
        cls._instancia = instancia
        return instancia

    def detectar(self, frame: np.ndarray) -> list:
        return self.app.get(frame)

    def embedding_de_foto(self, imagen: np.ndarray) -> Optional[np.ndarray]:
        """Calcula el embedding de referencia de una foto de alta.

        Si hay varios rostros se toma el mas grande, asumiendo que es el sujeto
        de la foto. Devuelve None si no se detecta ninguno.
        """
        rostros = self.app.get(imagen)
        if not rostros:
            return None
        mejor = max(rostros, key=lambda r: (r.bbox[2] - r.bbox[0]) * (r.bbox[3] - r.bbox[1]))
        return np.asarray(mejor.normed_embedding, dtype=np.float32)

    def analizar_foto_alta(self, imagen: np.ndarray,
                           min_ancho: int = 60) -> tuple[Optional[np.ndarray], str]:
        """Como `embedding_de_foto`, pero explica por que rechaza una foto.

        La foto de alta es la referencia contra la que se compara a todo el que
        pase frente a la camara: una referencia mala (rostro diminuto, de
        perfil) da coincidencias falsas o ninguna, y el operador no se entera.
        Devuelve (vector, "") o (None, motivo legible).
        """
        rostros = self.app.get(imagen)
        if not rostros:
            return None, "No se detectó ningún rostro en la foto."
        mejor = max(rostros, key=lambda r: (r.bbox[2] - r.bbox[0]) * (r.bbox[3] - r.bbox[1]))
        ancho = float(mejor.bbox[2] - mejor.bbox[0])
        if ancho < min_ancho:
            return None, (f"El rostro mide {ancho:.0f} px de ancho; hacen falta al menos "
                          f"{min_ancho}. Usa una foto más cercana o de mayor resolución.")
        if factor_pose(getattr(mejor, "kps", None)) < 0.45:
            return None, "El rostro no está de frente. Usa una foto frontal."
        return np.asarray(mejor.normed_embedding, dtype=np.float32), ""


def factor_pose(kps) -> float:
    """Que tan de frente esta el rostro, de 0.2 (perfil) a 1.0 (frontal).

    Usa los 5 puntos que ya entrega el detector (ojos, nariz, comisuras): en un
    rostro de frente la nariz cae a la mitad entre los ojos y a media altura
    entre ojos y boca. Un rostro de perfil da un embedding mucho peor aunque
    sea grande y nitido, y sin esto le ganaba a una vista frontal mas chica.
    """
    if kps is None:
        return 1.0
    try:
        p = np.asarray(kps, dtype=np.float32).reshape(5, 2)
    except (ValueError, TypeError):
        return 1.0
    ojo_i, ojo_d, nariz, boca_i, boca_d = p
    dist_ojos = float(np.linalg.norm(ojo_d - ojo_i))
    if dist_ojos < 1e-3:
        return 0.2
    centro_ojos = (ojo_i + ojo_d) / 2
    giro = abs(float(nariz[0] - centro_ojos[0])) / dist_ojos          # 0 de frente, ~0.5 de perfil

    centro_boca = (boca_i + boca_d) / 2
    alto_cara = float(centro_boca[1] - centro_ojos[1])
    if alto_cara <= 1e-3:
        return 0.2
    cabeceo = abs(float(nariz[1] - centro_ojos[1]) / alto_cara - 0.55)  # 0 de frente

    factor = (1.0 - min(1.0, giro / 0.6)) * (1.0 - min(1.0, cabeceo / 0.45))
    return max(0.2, factor)


def factor_nitidez(recorte: np.ndarray) -> float:
    """Nitidez del recorte (varianza del laplaciano), de 0.3 a 1.0.

    Se mide a un tamano fijo para que no dependa de la resolucion: un rostro
    grande pero movido no debe ganarle a uno mediano y enfocado.
    """
    if recorte is None or recorte.size == 0:
        return 0.3
    gris = cv2.cvtColor(recorte, cv2.COLOR_BGR2GRAY) if recorte.ndim == 3 else recorte
    gris = cv2.resize(gris, (112, 112), interpolation=cv2.INTER_AREA)
    varianza = float(cv2.Laplacian(gris, cv2.CV_64F).var())
    return max(0.3, min(1.0, varianza / 120.0))


def plantilla_promedio(vistas: list[tuple[float, np.ndarray]], k: int) -> Optional[np.ndarray]:
    """Promedia los `k` embeddings de mejor calidad de una persona seguida.

    Antes de promediar se descartan las vistas que no se parecen a la mejor
    (similitud < 0.4): si el tracker confundio a dos personas en algun frame,
    ese embedding no debe contaminar el de la persona.
    """
    if not vistas:
        return None
    mejores = sorted(vistas, key=lambda v: v[0], reverse=True)[:max(1, k)]
    ref = mejores[0][1]
    usados = [e for _, e in mejores if float(np.dot(ref, e)) >= 0.4]
    media = np.mean(usados, axis=0).astype(np.float32)
    norma = float(np.linalg.norm(media))
    return media / norma if norma > 0 else ref


class FaceDetector(Detector):
    name = "faces"

    def __init__(self, cfg: EdgeConfig) -> None:
        self.cfg = cfg
        # Un rostro mas chico que esto da un embedding inservible: InsightFace
        # se entreno con recortes de 112x112, y por debajo de ~50 px de ancho
        # la identificacion se vuelve ruido. Mejor no reportar que reportar mal.
        self.MIN_ANCHO_ROSTRO = cfg.face_min_width
        self.embedder = FaceEmbedder.compartido(cfg)
        self.tracker = IoUTracker(iou_min=0.3, max_age=20, min_hits=3)
        self.snapshot_hd = SnapshotHD(cfg.source, canal=cfg.snapshot_hd_channel) \
            if cfg.snapshot_hd_enabled else None

        self._frame_idx = 0
        self._eventos_emitidos = 0
        self._descartados_pequenos = 0
        self._ms_inferencia = 0.0
        self._forma_frame: tuple[int, int] = (0, 0)
        self._hd_usados = 0

    # ----------------------------------------------------------------------

    def procesar(self, frame: FrameInfo) -> list[DetectionEvent]:
        self._frame_idx += 1
        self._forma_frame = frame.frame.shape[:2]

        t0 = time.perf_counter()
        rostros = self.embedder.detectar(frame.frame)
        self._ms_inferencia += (time.perf_counter() - t0) * 1000

        detecciones: list[Deteccion] = []
        datos_por_caja: list[Any] = []
        for r in rostros:
            x1, y1, x2, y2 = (float(v) for v in r.bbox)
            if (x2 - x1) < self.MIN_ANCHO_ROSTRO:
                self._descartados_pequenos += 1
                continue
            detecciones.append(Deteccion(bbox=(x1, y1, x2, y2),
                                         confidence=float(r.det_score), label="rostro"))
            datos_por_caja.append(r)

        tracks = self.tracker.update(detecciones, frame.ts)

        # Asocia cada track con el rostro cuya caja coincide. Se guardan las
        # mejores vistas (no solo la mejor) para promediarlas al cerrar, y el
        # recorte de la de mayor calidad como evidencia.
        k = max(1, self.cfg.face_template_size)
        for track in tracks:
            rostro = self._rostro_de(track, detecciones, datos_por_caja)
            if rostro is None:
                continue
            recorte = self._recortar(frame.frame, rostro.bbox)
            calidad = self._calidad(rostro, recorte)
            embedding = np.asarray(rostro.normed_embedding, dtype=np.float32)

            vistas: list = track.state.setdefault("vistas", [])
            if len(vistas) < k or calidad > min(v[0] for v in vistas):
                vistas.append((calidad, embedding))
                vistas.sort(key=lambda v: v[0], reverse=True)
                del vistas[k:]

            if calidad > track.state.get("calidad", 0.0):
                track.state["calidad"] = calidad
                track.state["recorte"] = recorte
                track.state["det_score"] = float(rostro.det_score)
                track.state["bbox_bajo"] = tuple(float(v) for v in rostro.bbox)

                # Se pide UNA sola vez por track, en la primera deteccion que
                # ya vale la pena mejorar -- no en cada frame que mejora la
                # calidad, que dispararia una peticion HTTP por frame. Se pide
                # temprano (la persona sigue en cuadro) porque para cuando el
                # track cierre y se arme el evento pueden haber pasado 2-3
                # segundos, tiempo de sobra para que ya se haya ido.
                if self.snapshot_hd is not None and "hd_future" not in track.state:
                    track.state["hd_future"] = self.snapshot_hd.pedir()

        eventos = []
        for track in self.tracker.recoger_expirados():
            evento = self._construir_evento(track)
            if evento is not None:
                eventos.append(evento)
        return eventos

    def vaciar(self) -> list[DetectionEvent]:
        eventos = []
        for track in self.tracker.cerrar():
            evento = self._construir_evento(track)
            if evento is not None:
                eventos.append(evento)
        return eventos

    # ----------------------------------------------------------------------

    @staticmethod
    def _rostro_de(track: Track, detecciones: list[Deteccion], datos: list):
        """Encuentra el rostro cuya caja corresponde a este track en este frame."""
        for det, rostro in zip(detecciones, datos):
            if abs(det.bbox[0] - track.bbox[0]) < 1 and abs(det.bbox[1] - track.bbox[1]) < 1:
                return rostro
        return None

    @staticmethod
    def _calidad(rostro, recorte: Optional[np.ndarray] = None) -> float:
        """Puntuacion de calidad del embedding.

        Tamano x confianza de deteccion x pose x nitidez. El tamano sigue
        siendo la base (la resolucion es lo que mas limita), pero un perfil o
        un rostro movido ya no le ganan a una vista frontal y enfocada.
        """
        x1, y1, x2, y2 = rostro.bbox
        lado = ((x2 - x1) * (y2 - y1)) ** 0.5
        pose = factor_pose(getattr(rostro, "kps", None))
        nitidez = factor_nitidez(recorte) if recorte is not None else 1.0
        return float(lado) * float(rostro.det_score) * pose * nitidez

    @staticmethod
    def _recortar(frame: np.ndarray, bbox) -> np.ndarray:
        alto, ancho = frame.shape[:2]
        x1, y1, x2, y2 = (int(v) for v in bbox)
        # Margen del 20%: un recorte pegado a la cara se ve mal en el dashboard
        # y le quita contexto al operador para reconocer a la persona.
        mx, my = int((x2 - x1) * 0.2), int((y2 - y1) * 0.2)
        x1, y1 = max(0, x1 - mx), max(0, y1 - my)
        x2, y2 = min(ancho, x2 + mx), min(alto, y2 + my)
        return frame[y1:y2, x1:x2].copy()

    def _mejorar_con_hd(self, track: Track) -> None:
        """Si ya llego la foto en alta resolucion pedida durante el track,
        reemplaza el embedding y el recorte por una version mas nitida.

        No vuelve a correr sobre el frame HD completo (3200x1800): busca solo
        en la region donde ya se sabe que estaba el rostro, escalada desde las
        coordenadas del frame de deteccion, con margen generoso porque la foto
        se pidio uno o varios frames antes de este cierre y la persona pudo
        moverse un poco. Si no aparece nadie ahi (se fue, o la foto tardo
        demasiado), se deja el recorte de baja resolucion tal cual: nunca es
        peor que lo que ya se tenia.
        """
        frame_hd = SnapshotHD.resultado_listo(track.state.get("hd_future"))
        bbox_bajo = track.state.get("bbox_bajo")
        if frame_hd is None or frame_hd.size == 0 or bbox_bajo is None:
            return

        x1, y1, x2, y2 = escalar_bbox(bbox_bajo, self._forma_frame, frame_hd.shape[:2], margen=0.6)
        if x2 <= x1 or y2 <= y1:
            return
        region = frame_hd[y1:y2, x1:x2]
        if region.size == 0:
            return

        rostros_hd = self.embedder.detectar(region)
        if not rostros_hd:
            return
        mejor = max(rostros_hd, key=lambda r: (r.bbox[2] - r.bbox[0]) * (r.bbox[3] - r.bbox[1]))
        if (mejor.bbox[2] - mejor.bbox[0]) < self.MIN_ANCHO_ROSTRO:
            return

        recorte = self._recortar(region, mejor.bbox)
        embedding = np.asarray(mejor.normed_embedding, dtype=np.float32)
        # La vista HD entra a la plantilla como una vista mas, con su propia
        # calidad (normalmente la mas alta: mas pixeles de rostro).
        track.state.setdefault("vistas", []).append((self._calidad(mejor, recorte), embedding))
        track.state["recorte"] = recorte
        track.state["det_score"] = float(mejor.det_score)
        self._hd_usados += 1

    def _construir_evento(self, track: Track) -> Optional[DetectionEvent]:
        self._mejorar_con_hd(track)
        embedding = plantilla_promedio(track.state.get("vistas", []),
                                       self.cfg.face_template_size)
        if embedding is None:
            return None

        evento = DetectionEvent(
            camera_id=self.cfg.camera_id,
            type=EventType.FACE,
            track_id=track.track_id,
            value="rostro",  # la identidad la resuelve la API con el embedding
            confidence=round(track.state.get("det_score", track.confidence), 4),
            bbox=BBox(x1=int(track.bbox[0]), y1=int(track.bbox[1]),
                      x2=int(track.bbox[2]), y2=int(track.bbox[3])),
            observations=track.hits,
            first_seen=_a_utc(track.first_seen),
            last_seen=_a_utc(track.last_seen),
            ts=_a_utc(track.last_seen),
            embedding=embedding.tolist(),
            meta={"calidad": round(track.state.get("calidad", 0.0), 1),
                  "vistas_promediadas": min(len(track.state.get("vistas", [])),
                                            self.cfg.face_template_size)},
        )

        recorte = track.state.get("recorte")
        if recorte is not None and recorte.size > 0:
            ruta = self.cfg.snapshot_dir / f"{evento.event_id}.jpg"
            cv2.imwrite(str(ruta), recorte)
            evento.snapshot_path = ruta.relative_to(BASE_DIR).as_posix()

        self._eventos_emitidos += 1
        # Se registra el track y la calidad, NUNCA el embedding: los logs se
        # copian, se comparten y se mandan en tickets.
        log.info("Rostro detectado (track=%d, %d frames, calidad %.0f)",
                 track.track_id, track.hits, track.state.get("calidad", 0))
        return evento

    def anotar(self, frame: np.ndarray) -> np.ndarray:
        for track in self.tracker.tracks_confirmados():
            x1, y1, x2, y2 = (int(v) for v in track.bbox)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 140, 0), 2)
            etiqueta = f"rostro #{track.track_id}"
            cv2.putText(frame, etiqueta, (x1, max(12, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 140, 0), 2)
        return frame

    def cerrar(self) -> None:
        if self.snapshot_hd is not None:
            self.snapshot_hd.cerrar()

    @property
    def stats(self) -> dict[str, Any]:
        n = max(1, self._frame_idx)
        return {
            "frames": self._frame_idx,
            "tracks_activos": self.tracker.activos,
            "eventos_emitidos": self._eventos_emitidos,
            "descartados_pequenos": self._descartados_pequenos,
            "ms_inferencia_promedio": round(self._ms_inferencia / n, 1),
            "provider": self.embedder.provider,
            "evidencia_hd_usada": self._hd_usados,
        }


def _a_utc(epoch: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc)
