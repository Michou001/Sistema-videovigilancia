"""Detector de placas: YOLOv5 para localizar + EasyOCR para leer + tracking.

Portado desde legacy/main_camara.py. Los cambios de fondo respecto al original:

  ANTES                                  AHORA
  ---------------------------------      ---------------------------------
  Una fila en CSV por cada lectura        Un evento por vehiculo
  COOLDOWN_SEGUNDOS=15 para no repetir    track_id: se sabe que es el mismo coche
  Se guarda la PRIMERA lectura valida     Se elige la MEJOR de todas por consenso
  OCR cada 3 frames, global               Presupuesto de OCR por track
  cv2.imshow + CSV dentro del detector    Devuelve eventos; no sabe de pantallas

El cambio importante es el ultimo del bloque de arriba: la primera lectura que
pasa el regex casi nunca es la mejor. El coche se acerca, la placa se ve mejor,
y esa lectura tardia es la buena. Acumular todas las lecturas del mismo track y
decidir al final sube la precision sin costar nada.
"""

from __future__ import annotations

import logging
import pathlib
import time
import warnings
from typing import Any, Optional

# El repo yolov5/ esta congelado en una version que usa APIs de torch ya
# deprecadas (torch.cuda.amp.autocast). Emite un FutureWarning POR CADA
# inferencia, lo que inunda la consola y esconde los eventos reales. No es un
# error y no hay nada que arreglar de nuestro lado: el repo es de terceros.
warnings.filterwarnings("ignore", category=FutureWarning, module="yolov5.*")
warnings.filterwarnings("ignore", message=r".*torch\.cuda\.amp\.autocast.*")

# Los pesos se entrenaron en Linux y guardan rutas PosixPath. Sin este parche,
# torch.load truena en Windows al deserializar. Debe ir ANTES de importar torch.
pathlib.PosixPath = pathlib.WindowsPath

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from edge.config import BASE_DIR, EdgeConfig  # noqa: E402
from edge.detectors.base import Detector  # noqa: E402
from edge.sources import FrameInfo  # noqa: E402
from edge.tracking import Deteccion, IoUTracker, Track  # noqa: E402
from shared.events import BBox, DetectionEvent, EventType  # noqa: E402
from shared.plates import (  # noqa: E402
    elegir_mejor_lectura,
    es_placa_valida,
    formatear,
    normalizar,
)

log = logging.getLogger(__name__)


class PlateDetector(Detector):
    name = "plates"

    # --- Presupuesto de OCR --------------------------------------------------
    # EasyOCR cuesta 20-60 ms por recorte. Con varias placas en pantalla, hacer
    # OCR de todas en cada frame arruina los fps. Estos tres limites reparten
    # ese presupuesto donde rinde:
    OCR_CADA_N_FRAMES = 3      # por track, no global
    MAX_LECTURAS_POR_TRACK = 6  # tras 6 lecturas, ya se sabe lo que dice
    MAX_OCR_POR_FRAME = 2       # techo duro aunque haya 10 placas visibles

    # Un recorte mas chico que esto no tiene resolucion para leerse.
    MIN_ANCHO_RECORTE = 40
    MIN_ALTO_RECORTE = 15

    def __init__(self, cfg: EdgeConfig) -> None:
        self.cfg = cfg
        self.device = cfg.resolve_device()

        import torch

        log.info("Cargando modelo de placas (%s)...", cfg.plate_model.name)
        t0 = time.perf_counter()
        if not cfg.plate_model.exists():
            raise FileNotFoundError(f"No existe el modelo: {cfg.plate_model}")

        # source='local' usa el repo yolov5/ del proyecto, sin salir a internet.
        self.model = torch.hub.load(
            str(BASE_DIR / "yolov5"),
            "custom",
            path=str(cfg.plate_model),
            source="local",
            force_reload=False,
            verbose=False,
        )
        self.model.conf = cfg.plate_conf
        self.model.to(self.device)
        log.info("Modelo listo en %.1fs (device=%s)", time.perf_counter() - t0, self.device)

        log.info("Inicializando EasyOCR...")
        import easyocr

        self.reader = easyocr.Reader(["es", "en"], gpu=(self.device == "cuda"), verbose=False)

        self.tracker = IoUTracker(iou_min=0.25, max_age=16, min_hits=3)
        self._frame_idx = 0
        self._ocr_ejecutados = 0
        self._eventos_emitidos = 0
        self._duplicados_suprimidos = 0
        self._ms_inferencia = 0.0
        self._ms_ocr = 0.0
        self._emitidas: dict[str, float] = {}  # placa normalizada -> ts del ultimo evento

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

        # 1. Deteccion
        t0 = time.perf_counter()
        rgb = cv2.cvtColor(frame.frame, cv2.COLOR_BGR2RGB)
        resultados = self.model(rgb, size=self.cfg.imgsz)
        self._ms_inferencia += (time.perf_counter() - t0) * 1000

        # results.xyxy[0] -> tensor [N, 6]: x1, y1, x2, y2, conf, clase.
        # Se usa el tensor y no .pandas() porque construir el DataFrame cuesta
        # mas que la inferencia misma cuando hay pocas cajas.
        detecciones = [
            Deteccion(
                bbox=(float(f[0]), float(f[1]), float(f[2]), float(f[3])),
                confidence=float(f[4]),
                label="placa",
            )
            for f in resultados.xyxy[0].cpu().numpy()
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
        lecturas: list = track.state.setdefault("lecturas", [])
        if len(lecturas) >= self.MAX_LECTURAS_POR_TRACK:
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

        t0 = time.perf_counter()
        try:
            resultados = self.reader.readtext(self._preparar(recorte))
        except Exception as e:  # noqa: BLE001 - un OCR fallido no debe tumbar el worker
            log.warning("OCR fallo en track %d: %s", track.track_id, e)
            return False
        self._ms_ocr += (time.perf_counter() - t0) * 1000
        self._ocr_ejecutados += 1

        lecturas: list = track.state.setdefault("lecturas", [])
        for _, texto, conf in resultados:
            if conf >= self.cfg.ocr_conf:
                lecturas.append((texto, float(conf)))
                # Guarda el recorte de la lectura mas confiable como evidencia
                mejor = track.state.get("mejor_conf", 0.0)
                if conf > mejor:
                    track.state["mejor_conf"] = float(conf)
                    track.state["recorte"] = recorte.copy()
        return True

    def _preparar(self, recorte: np.ndarray) -> np.ndarray:
        """Acondiciona el recorte antes del OCR.

        El escalado es lo que mas rinde: EasyOCR se degrada mucho con texto de
        menos de ~30 px de alto, y una placa a media distancia cae facil por
        debajo. Ampliar con interpolacion cubica antes de leer sube bastante la
        tasa de acierto y cuesta menos de 1 ms.
        """
        alto = recorte.shape[0]
        if alto < 64:
            escala = min(4.0, 64 / max(alto, 1))
            recorte = cv2.resize(recorte, None, fx=escala, fy=escala,
                                 interpolation=cv2.INTER_CUBIC)
        # Ecualizacion de contraste sobre el canal de luminancia: normaliza
        # placas quemadas por el sol o en sombra sin alterar el color.
        lab = cv2.cvtColor(recorte, cv2.COLOR_BGR2LAB)
        lab[:, :, 0] = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(lab[:, :, 0])
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

    # -- Construccion del evento -------------------------------------------

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
            },
        )

        recorte = track.state.get("recorte")
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
        try:
            import torch

            del self.model
            if self.device == "cuda":
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

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
        }


def _a_utc(epoch: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc)
