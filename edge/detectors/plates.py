"""Detector de placas: YOLOv9 para localizar + OCR especializado + tracking.

Modelos (ambos ONNX, licencia MIT, se descargan solos la primera vez):

  DETECTOR  open-image-models, YOLOv9 end-to-end entrenado solo con placas
            (mAP50 0.966 en su version "s"). Sustituye al YOLOv5 anterior.
  OCR       fast-plate-ocr, transformer compacto (CCT) entrenado con placas de
            muchos paises. Lee el recorte completo en menos de 1 ms; EasyOCR,
            que era un OCR generico de texto, tardaba 30-120 ms por recorte.

Medido sobre fotos reales de placas mexicanas deformadas para simular el
angulo de la camara (de lado 35-50 grados, desde arriba, rotada, lejos, poca
luz): 35 de 35 lecturas correctas, contra 20 de 35 del YOLOv5 + EasyOCR.

Encima de los modelos queda lo que es propio del sistema:

  - un evento por vehiculo (tracking), no una fila por frame;
  - varias lecturas por vehiculo y consenso entre ellas;
  - correccion por posicion segun el formato de placa mexicana;
  - foto HD del canal principal como evidencia;
  - color aproximado del vehiculo, para que la alerta diga "sedan gris".
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import cv2
import numpy as np

from edge.config import BASE_DIR, EdgeConfig
from edge.detectors.base import Detector
from edge.snapshot_hd import SnapshotHD, escalar_bbox
from edge.sources import FrameInfo
from edge.tracking import Deteccion, IoUTracker, Track
from shared.events import BBox, DetectionEvent, EventType
from shared.plates import (
    elegir_mejor_lectura,
    es_placa_valida,
    formatear,
    lecturas_de_ocr,
    normalizar,
)

log = logging.getLogger(__name__)


def color_vehiculo(frame: np.ndarray, bbox: tuple[float, float, float, float]) -> Optional[str]:
    """Color dominante de la carroceria alrededor de la placa, en palabras.

    Se mide la carroceria a los LADOS de la placa, a su misma altura: ahi casi
    siempre hay defensa o cajuela pintada. Encima de la placa puede quedar el
    vidrio polarizado o la parrilla, que dan el color equivocado. Es una
    aproximacion (un reflejo o una sombra la cambian), por eso va como "color
    aprox." y nunca decide una alerta: le sirve al operador para ubicar el
    vehiculo en el video.
    """
    alto, ancho = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    w, h = x2 - x1, y2 - y1
    if w <= 0 or h <= 0:
        return None
    ry1, ry2 = int(max(0, y1 - h * 0.5)), int(min(alto, y2))
    franjas = [frame[ry1:ry2, int(max(0, x1 - w * 0.9)):int(max(0, x1 - w * 0.1))],
               frame[ry1:ry2, int(min(ancho, x2 + w * 0.1)):int(min(ancho, x2 + w * 0.9))]]
    pixeles = [f.reshape(-1, 3) for f in franjas if f.size >= 3 * 16]
    if not pixeles:
        return None

    hsv = cv2.cvtColor(np.concatenate(pixeles)[None, :, :], cv2.COLOR_BGR2HSV)[0].astype(np.float32)
    matiz, sat, val = np.median(hsv[:, 0]), np.median(hsv[:, 1]), np.median(hsv[:, 2])
    if val < 50:
        return "negro"
    if sat < 50:
        return "blanco/plata" if val > 185 else "gris"
    # OpenCV usa matiz de 0 a 180.
    if matiz < 8 or matiz >= 165:
        return "rojo"
    if matiz < 20:
        return "naranja/cafe" if val > 120 else "cafe"
    if matiz < 35:
        return "amarillo"
    if matiz < 85:
        return "verde"
    if matiz < 130:
        return "azul"
    return "morado"


class PlateDetector(Detector):
    name = "plates"

    # --- Presupuesto de OCR --------------------------------------------------
    # El OCR nuevo cuesta menos de 1 ms, asi que ya no hay que racionarlo como
    # con EasyOCR: se lee cada track en frames alternos y se juntan mas
    # lecturas para el consenso.
    OCR_CADA_N_FRAMES = 2       # por track, no global
    MAX_LECTURAS_POR_TRACK = 10  # pasadas de OCR por vehiculo
    MAX_OCR_POR_FRAME = 6        # techo aunque haya muchas placas visibles

    # Un recorte mas chico que esto no tiene resolucion para leerse.
    MIN_ANCHO_RECORTE = 30
    MIN_ALTO_RECORTE = 10

    def __init__(self, cfg: EdgeConfig) -> None:
        self.cfg = cfg
        self.device = cfg.resolve_device()

        # Las DLL de CUDA que usa onnxruntime vienen con torch (ver faces.py).
        from edge.detectors.faces import configurar_onnx_gpu

        configurar_onnx_gpu()
        from fast_plate_ocr import LicensePlateRecognizer
        from open_image_models import LicensePlateDetector

        proveedores = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                       if self.device == "cuda" else ["CPUExecutionProvider"])

        t0 = time.perf_counter()
        self.detector = LicensePlateDetector(
            detection_model=cfg.plate_detector_model,
            conf_thresh=cfg.plate_conf,
            providers=proveedores,
        )
        self.ocr = LicensePlateRecognizer(cfg.plate_ocr_model, providers=proveedores)
        self._ocr_color = self.ocr.config.image_color_mode
        log.info("Placas listas en %.1fs (%s + %s, %s)", time.perf_counter() - t0,
                 cfg.plate_detector_model, cfg.plate_ocr_model, proveedores[0])

        self.tracker = IoUTracker(iou_min=0.25, max_age=16, min_hits=3)
        self.snapshot_hd = SnapshotHD(cfg.source, canal=cfg.snapshot_hd_channel)             if cfg.snapshot_hd_enabled else None

        self._frame_idx = 0
        self._ocr_ejecutados = 0
        self._eventos_emitidos = 0
        self._duplicados_suprimidos = 0
        self._ms_inferencia = 0.0
        self._ms_ocr = 0.0
        self._emitidas: dict[str, float] = {}  # placa normalizada -> ts del ultimo evento
        self._forma_frame: tuple[int, int] = (0, 0)
        self._hd_usados = 0

    def _purgar_emitidas(self, ahora: float) -> None:
        """Evita que el diccionario de deduplicacion crezca sin limite en un
        worker que corre semanas seguidas."""
        if len(self._emitidas) < 500:
            return
        limite = ahora - self.cfg.plate_dedupe_s
        self._emitidas = {k: v for k, v in self._emitidas.items() if v >= limite}

    # ----------------------------------------------------------------------

    def procesar(self, frame: FrameInfo) -> list[DetectionEvent]:
        self._frame_idx += 1
        alto, ancho = frame.frame.shape[:2]
        self._forma_frame = (alto, ancho)

        # 1. Deteccion
        t0 = time.perf_counter()
        resultados = self.detector.predict(frame.frame)
        self._ms_inferencia += (time.perf_counter() - t0) * 1000

        detecciones = [
            Deteccion(
                bbox=(float(r.bounding_box.x1), float(r.bounding_box.y1),
                      float(r.bounding_box.x2), float(r.bounding_box.y2)),
                confidence=float(r.confidence),
                label="placa",
            )
            for r in resultados
        ]

        # 2. Tracking
        tracks = self.tracker.update(detecciones, frame.ts)

        # 3. OCR sobre los tracks que lo ameritan
        presupuesto = self.MAX_OCR_POR_FRAME
        for track in tracks:
            if presupuesto <= 0:
                break
            if self._debe_leer(track):
                if self._leer_placa(track, frame.frame, ancho, alto):
                    presupuesto -= 1

        # 4. Emitir eventos de los tracks que ya se fueron
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

    # -- OCR ---------------------------------------------------------------

    def _debe_leer(self, track: Track) -> bool:
        """Decide si vale la pena gastar OCR en este track ahora."""
        # Se cuentan pasadas de OCR, no lecturas: una pasada puede dejar varias
        # candidatas (fragmentos unidos, correcciones) y eso no es evidencia
        # nueva sobre la placa.
        if track.state.get("ocr_hechos", 0) >= self.MAX_LECTURAS_POR_TRACK:
            return False
        # Reparte en el tiempo, y desfasa por track_id para que dos placas
        # simultaneas no pidan OCR en el mismo frame.
        return (self._frame_idx + track.track_id) % self.OCR_CADA_N_FRAMES == 0

    def _leer_placa(self, track: Track, frame: np.ndarray, ancho: int, alto: int) -> bool:
        """Recorta la placa y le pasa OCR. Devuelve True si se ejecuto."""
        x1, y1, x2, y2 = (int(v) for v in track.bbox)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(ancho, x2), min(alto, y2)

        if (x2 - x1) < self.MIN_ANCHO_RECORTE or (y2 - y1) < self.MIN_ALTO_RECORTE:
            return False

        recorte = frame[y1:y2, x1:x2]
        if recorte.size == 0:
            return False

        # Se pide UNA vez por track, en el primer intento de OCR -- temprano,
        # mientras el vehiculo sigue en cuadro. Es solo para la EVIDENCIA que
        # ve el operador; el OCR sigue leyendo del recorte normal, esto no le
        # cambia nada a la precision de lectura.
        if self.snapshot_hd is not None and "hd_future" not in track.state:
            track.state["hd_future"] = self.snapshot_hd.pedir()

        t0 = time.perf_counter()
        try:
            texto, conf = self._leer(recorte)
        except Exception as e:  # noqa: BLE001 - un OCR fallido no debe tumbar el worker
            log.warning("OCR fallo en track %d: %s", track.track_id, e)
            return False
        self._ms_ocr += (time.perf_counter() - t0) * 1000
        self._ocr_ejecutados += 1
        track.state["ocr_hechos"] = track.state.get("ocr_hechos", 0) + 1

        # El OCR devuelve la placa completa en un solo texto; la caja es la del
        # recorte entero. Pasa por la misma correccion por posicion y el mismo
        # filtro de confianza que cualquier lectura.
        alto_r, ancho_r = recorte.shape[:2]
        caja = [[0, 0], [ancho_r, 0], [ancho_r, alto_r], [0, alto_r]]
        lecturas: list = track.state.setdefault("lecturas", [])
        for texto, conf in lecturas_de_ocr([(caja, texto, conf)], min_conf=self.cfg.ocr_conf):
            lecturas.append((texto, conf))
            # Guarda el recorte de la lectura mas confiable como evidencia
            if conf > track.state.get("mejor_conf", 0.0):
                track.state["mejor_conf"] = conf
                track.state["recorte"] = recorte.copy()
                track.state["bbox_bajo"] = (float(x1), float(y1), float(x2), float(y2))
                track.state["color"] = color_vehiculo(frame, (x1, y1, x2, y2))
        return True

    def _leer(self, recorte: np.ndarray) -> tuple[str, float]:
        """Texto de la placa y su confianza (el promedio de la de cada caracter,
        asi un solo caracter dudoso baja la confianza de toda la lectura)."""
        if self._ocr_color == "grayscale":
            entrada = cv2.cvtColor(recorte, cv2.COLOR_BGR2GRAY)
        elif self._ocr_color == "rgb":
            entrada = cv2.cvtColor(recorte, cv2.COLOR_BGR2RGB)
        else:
            entrada = recorte
        pred = self.ocr.run_one(entrada, return_confidence=True)
        texto = (pred.plate or "").replace("_", "")
        probs = np.asarray(pred.char_probs if pred.char_probs is not None else [0.0], dtype=np.float32)
        probs = probs[: max(1, len(texto))]
        return texto, float(probs.mean()) if probs.size else 0.0

    # -- Construccion del evento -------------------------------------------

    def _recorte_hd_o_normal(self, track: Track) -> Optional[np.ndarray]:
        """Si ya llego la foto HD pedida durante el track, recorta la misma
        region (escalada) de ahi en vez de usar el recorte de baja resolucion.

        A diferencia de rostros, aqui no hace falta volver a correr el
        detector: ya se sabe donde estaba la placa (el bbox del momento de
        mejor lectura), asi que es un recorte geometrico simple. No toca el
        texto ya leido -- el OCR ya corrio sobre el recorte normal -- esto
        solo mejora que tan clara se ve la evidencia guardada.
        """
        frame_hd = SnapshotHD.resultado_listo(track.state.get("hd_future"))
        bbox_bajo = track.state.get("bbox_bajo")
        if frame_hd is None or frame_hd.size == 0 or bbox_bajo is None:
            return track.state.get("recorte")

        x1, y1, x2, y2 = escalar_bbox(bbox_bajo, self._forma_frame, frame_hd.shape[:2], margen=0.3)
        if x2 <= x1 or y2 <= y1:
            return track.state.get("recorte")
        recorte_hd = frame_hd[y1:y2, x1:x2]
        if recorte_hd.size == 0:
            return track.state.get("recorte")

        self._hd_usados += 1
        return recorte_hd

    def _construir_evento(self, track: Track) -> Optional[DetectionEvent]:
        """Consolida un track terminado en un unico evento."""
        lecturas: list[tuple[str, float]] = track.state.get("lecturas", [])
        if not lecturas:
            # Se detecto una placa pero nunca se pudo leer: demasiado lejos,
            # borrosa o de lado. No se emite evento porque un evento sin texto
            # no sirve para cruzar la lista negra.
            log.debug("Track %d descartado: sin lecturas de OCR", track.track_id)
            return None

        elegida = elegir_mejor_lectura(lecturas)
        if elegida is None:
            return None
        texto, conf_ocr = elegida

        valida, limpio, formato = es_placa_valida(texto)
        if not valida:
            log.debug("Track %d: '%s' no tiene formato de placa", track.track_id, texto)
            return None

        # Red de seguridad contra duplicados por track roto (ver plate_dedupe_s).
        # Se compara la forma NORMALIZADA para que dos lecturas del mismo coche
        # con errores distintos de OCR ("ABC-123" y "A8C-I23") cuenten como una.
        clave = normalizar(limpio)
        if self.cfg.plate_dedupe_s > 0:
            previo = self._emitidas.get(clave)
            if previo is not None and (track.last_seen - previo) < self.cfg.plate_dedupe_s:
                self._duplicados_suprimidos += 1
                log.info("Placa %s suprimida: duplicado a %.0fs del evento anterior "
                         "(track %d, el tracking se habia roto)",
                         formatear(limpio), track.last_seen - previo, track.track_id)
                return None
        self._emitidas[clave] = track.last_seen
        self._purgar_emitidas(track.last_seen)

        evento = DetectionEvent(
            camera_id=self.cfg.camera_id,
            type=EventType.PLATE,
            track_id=track.track_id,
            value=formatear(limpio),
            confidence=round(conf_ocr, 4),
            bbox=BBox(x1=int(track.bbox[0]), y1=int(track.bbox[1]),
                      x2=int(track.bbox[2]), y2=int(track.bbox[3])),
            observations=track.hits,
            first_seen=_a_utc(track.first_seen),
            last_seen=_a_utc(track.last_seen),
            ts=_a_utc(track.last_seen),
            meta={
                "formato": formato,
                "conf_deteccion": round(track.confidence, 4),
                "lecturas_ocr": [[t, round(c, 3)] for t, c in lecturas],
                "texto_crudo": texto,
                "color_vehiculo": track.state.get("color"),
            },
        )

        recorte = self._recorte_hd_o_normal(track)
        if recorte is not None:
            ruta = self.cfg.snapshot_dir / f"{evento.event_id}.jpg"
            cv2.imwrite(str(ruta), recorte)
            # .as_posix() y no str(): en Windows str() da "data\snapshots\x.jpg",
            # y esa ruta va a terminar siendo una URL en el dashboard. Las URLs
            # usan "/" en todas las plataformas.
            evento.snapshot_path = ruta.relative_to(BASE_DIR).as_posix()

        self._eventos_emitidos += 1
        log.info("Placa %s (conf %.2f, %d frames, %d lecturas) track=%d",
                 evento.value, conf_ocr, track.hits, len(lecturas), track.track_id)
        return evento

    # ----------------------------------------------------------------------

    def anotar(self, frame: np.ndarray) -> np.ndarray:
        """Dibuja los tracks activos y su lectura acumulada."""
        for track in self.tracker.tracks_confirmados():
            x1, y1, x2, y2 = (int(v) for v in track.bbox)
            lecturas = track.state.get("lecturas", [])
            mejor = elegir_mejor_lectura(lecturas) if lecturas else None

            # Verde si ya se leyo, ambar si aun se esta intentando.
            color = (0, 220, 0) if mejor else (0, 180, 255)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

            etiqueta = f"#{track.track_id}"
            if mejor:
                etiqueta += f" {formatear(mejor[0])} ({mejor[1]:.2f})"
            etiqueta += f" [{len(lecturas)}]"

            (tw, th), _ = cv2.getTextSize(etiqueta, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
            cv2.rectangle(frame, (x1, max(0, y1 - th - 8)), (x1 + tw + 6, y1), color, -1)
            cv2.putText(frame, etiqueta, (x1 + 3, max(12, y1 - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 2)
        return frame

    def cerrar(self) -> None:
        if self.snapshot_hd is not None:
            self.snapshot_hd.cerrar()
        # Las sesiones de onnxruntime liberan su memoria al destruirse.
        self.detector = None
        self.ocr = None

    @property
    def stats(self) -> dict[str, Any]:
        n = max(1, self._frame_idx)
        return {
            "frames": self._frame_idx,
            "tracks_activos": self.tracker.activos,
            "ocr_ejecutados": self._ocr_ejecutados,
            "eventos_emitidos": self._eventos_emitidos,
            "duplicados_suprimidos": self._duplicados_suprimidos,
            "ms_inferencia_promedio": round(self._ms_inferencia / n, 1),
            "ms_ocr_promedio": round(self._ms_ocr / max(1, self._ocr_ejecutados), 1),
            "evidencia_hd_usada": self._hd_usados,
        }


def _a_utc(epoch: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc)
