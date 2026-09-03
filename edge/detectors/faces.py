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
from typing import Any, Optional

import cv2
import numpy as np

from edge.config import BASE_DIR, EdgeConfig
from edge.detectors.base import Detector
from edge.sources import FrameInfo
from edge.tracking import Deteccion, IoUTracker, Track
from shared.events import BBox, DetectionEvent, EventType

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
                 det_size: int = 640) -> None:
        configurar_onnx_gpu()
        from insightface.app import FaceAnalysis

        proveedores = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                       if device == "cuda" else ["CPUExecutionProvider"])

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
        if device == "cuda" and real != "CUDAExecutionProvider":
            log.warning(
                "InsightFace cayo a CPU pese a pedir GPU (6x mas lento). "
                "Causa habitual: onnxruntime-gpu compilado para otra version "
                "de CUDA que la de torch. Ver configurar_onnx_gpu()."
            )
        self.provider = real

    @classmethod
    def compartido(cls, cfg: EdgeConfig) -> "FaceEmbedder":
        """Instancia unica: cargar el modelo dos veces duplicaria el uso de VRAM."""
        if cls._instancia is None:
            cls._instancia = cls(cfg.face_model, cfg.resolve_device(), cfg.imgsz)
        return cls._instancia

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


class FaceDetector(Detector):
    name = "faces"

    # Un rostro mas chico que esto da un embedding inservible: no hay
    # suficientes pixeles para los rasgos. InsightFace se entreno con recortes
    # de 112x112, y por debajo de ~50 px de ancho la identificacion se vuelve
    # ruido. Es preferible no reportar a reportar mal: un falso positivo
    # biometrico senala a una persona equivocada.
    MIN_ANCHO_ROSTRO = 50

    def __init__(self, cfg: EdgeConfig) -> None:
        self.cfg = cfg
        self.embedder = FaceEmbedder.compartido(cfg)
        self.tracker = IoUTracker(iou_min=0.3, max_age=20, min_hits=3)

        self._frame_idx = 0
        self._eventos_emitidos = 0
        self._descartados_pequenos = 0
        self._ms_inferencia = 0.0

    # ----------------------------------------------------------------------

    def procesar(self, frame: FrameInfo) -> list[DetectionEvent]:
        self._frame_idx += 1

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

        # Asocia cada track con el rostro cuya caja coincide, y se queda con el
        # embedding de MEJOR CALIDAD visto hasta ahora para ese track.
        for track in tracks:
            rostro = self._rostro_de(track, detecciones, datos_por_caja)
            if rostro is None:
                continue
            calidad = self._calidad(rostro)
            if calidad > track.state.get("calidad", 0.0):
                track.state["calidad"] = calidad
                track.state["embedding"] = np.asarray(rostro.normed_embedding,
                                                      dtype=np.float32)
                track.state["recorte"] = self._recortar(frame.frame, rostro.bbox)
                track.state["det_score"] = float(rostro.det_score)

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
    def _calidad(rostro) -> float:
        """Puntuacion de calidad del embedding.

        Combina tamano y confianza de deteccion. El tamano pesa mas porque un
        rostro grande y algo borroso da mejor embedding que uno nitido de 40 px:
        la resolucion es lo que limita.
        """
        x1, y1, x2, y2 = rostro.bbox
        lado = ((x2 - x1) * (y2 - y1)) ** 0.5
        return float(lado) * float(rostro.det_score)

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

    def _construir_evento(self, track: Track) -> Optional[DetectionEvent]:
        embedding = track.state.get("embedding")
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
            meta={"calidad": round(track.state.get("calidad", 0.0), 1)},
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
        }


def _a_utc(epoch: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc)
