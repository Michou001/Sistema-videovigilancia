"""Detector de movimiento anomalo: no clasifica OBJETOS, mide COMPORTAMIENTO.

Motivo del cambio de enfoque: un detector de armas por clasificacion de objeto
(ver weapons.py) depende de que el modelo reconozca la forma exacta del arma,
en la pose exacta en la que aparece. Un cuchillo apuntando hacia la camara en
penumbra, en la mano de alguien, no se parece a los cuchillos de foto de
producto con los que se entreno COCO -- y en pruebas reales, no lo detecto.

Este detector mide otra cosa, mas dificil de disfrazar: que tan rapido se
mueve una persona respecto a su propio tamano en pantalla. Un forcejeo, un
golpe, alguien corriendo hacia o desde algo, todos comparten una firma de
velocidad muy por encima de caminar normal, sin importar que traiga en la
mano ni en que angulo.

Es deteccion de COMPORTAMIENTO, no de identidad: no reemplaza a placas ni
rostros, y como cualquier senal de comportamiento va a tener falsos positivos
razonables (alguien corriendo para alcanzar un camion). Por eso el evento sale
como WARNING y no CRITICAL (ver api/matching.py): amerita que el operador
mire, no una alarma automatica.

SOBRE EL MODELO: usa YOLO11 estandar filtrado a la clase 'person' de COCO.
A diferencia de un cuchillo, una persona de pie es uno de los casos donde COCO
rinde mejor -- no hace falta un modelo afinado para esta parte.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from concurrent.futures import Future
from typing import Any, Optional

import cv2
import numpy as np

from edge.config import BASE_DIR, EdgeConfig
from edge.detectors.base import Detector
from edge.detectors.confirmacion import ConfirmacionTemporal
from edge.snapshot_hd import SnapshotHD, escalar_bbox
from edge.sources import FrameInfo
from shared.events import BBox, DetectionEvent, EventType

log = logging.getLogger(__name__)

CLASE_PERSONA = 0  # id de 'person' en COCO

VALOR_EVENTO = "movimiento_subito"


class MotionAnomalyDetector(Detector):
    name = "motion"

    def __init__(self, cfg: EdgeConfig) -> None:
        self.cfg = cfg
        self.device = cfg.resolve_device()

        from ultralytics import YOLO

        # Mismo archivo que weapons.py: es el modelo YOLO11-small estandar de
        # Ultralytics, no algo especifico de este detector. Cargarlo dos veces
        # (si algun dia se reactivan ambos detectores) duplica VRAM; no es un
        # problema hoy porque ENABLE_WEAPONS esta apagado por defecto.
        ruta = BASE_DIR / "models" / "yolo11s.pt"
        t0 = time.perf_counter()
        self.model = YOLO(str(ruta))
        self.model.to(self.device)
        log.info("Modelo de movimiento listo en %.1fs (device=%s)",
                 time.perf_counter() - t0, self.device)

        self.confirmador = ConfirmacionTemporal(
            cfg.motion_confirm_hits, cfg.motion_confirm_window
        )
        self.snapshot_hd = SnapshotHD(cfg.source, canal=cfg.snapshot_hd_channel) \
            if cfg.snapshot_hd_enabled else None

        # Cuantas posiciones recientes conservar por track para medir
        # velocidad. Comparar frames consecutivos amplifica el ruido normal
        # del tracker (el centro de la caja tiembla unos pixeles aunque la
        # persona este quieta); comparar contra ~1 segundo atras promedia ese
        # ruido y de paso es justo la escala de tiempo de un movimiento
        # "subito".
        self._ventana_posiciones = max(3, round(cfg.infer_fps) + 1)
        self._posiciones: dict[int, deque[tuple[float, float, float, float]]] = {}
        self._ultima_deteccion: dict[int, dict] = {}
        self._hd_futures: dict[int, Future] = {}

        self._frame_idx = 0
        self._eventos_emitidos = 0
        self._ms_inferencia = 0.0
        self._forma_frame: tuple[int, int] = (0, 0)
        self._hd_usados = 0

    # ----------------------------------------------------------------------

    def procesar(self, frame: FrameInfo) -> list[DetectionEvent]:
        self._frame_idx += 1
        self._forma_frame = frame.frame.shape[:2]

        t0 = time.perf_counter()
        resultados = self.model.track(
            frame.frame,
            persist=True,
            verbose=False,
            conf=self.cfg.motion_conf,
            imgsz=self.cfg.imgsz,
            classes=[CLASE_PERSONA],
            device=self.device,
            tracker="bytetrack.yaml",
        )
        self._ms_inferencia += (time.perf_counter() - t0) * 1000

        vistos_ahora: set[int] = set()
        r = resultados[0]
        if r.boxes is not None and r.boxes.id is not None:
            for caja, tid in zip(
                r.boxes.xyxy.cpu().numpy(),
                r.boxes.id.cpu().numpy().astype(int),
            ):
                tid = int(tid)
                vistos_ahora.add(tid)
                x1, y1, x2, y2 = (float(v) for v in caja)
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                alto = max(1.0, y2 - y1)

                historial = self._posiciones.setdefault(
                    tid, deque(maxlen=self._ventana_posiciones)
                )
                historial.append((frame.ts, cx, cy, alto))
                velocidad = self._velocidad(historial)

                self._ultima_deteccion[tid] = {
                    "bbox": (x1, y1, x2, y2),
                    "velocidad": velocidad,
                    "ts": frame.ts,
                }
                supera_umbral = velocidad >= self.cfg.motion_speed_threshold
                self.confirmador.marcar(tid, supera_umbral)

                # Se pide en cuanto empieza a acumular evidencia (no al
                # confirmarse): la confirmacion tarda MOTION_CONFIRM_WINDOW
                # frames (~0.6s a 8 fps), tiempo parecido a lo que tarda la
                # foto HD en llegar -- para cuando el evento se emite, el
                # future probablemente ya esta listo.
                if supera_umbral and self.snapshot_hd is not None and tid not in self._hd_futures:
                    self._hd_futures[tid] = self.snapshot_hd.pedir()

        # Igual que en weapons.py: un track que desaparece del frame cuenta
        # como fallo, y si la ventana entera queda vacia, se olvida.
        for tid in self.confirmador.tracks():
            if tid not in vistos_ahora:
                self.confirmador.marcar(tid, False)
                if self.confirmador.perdido(tid):
                    self.confirmador.olvidar(tid)
                    self._posiciones.pop(tid, None)
                    self._ultima_deteccion.pop(tid, None)
                    self._hd_futures.pop(tid, None)

        eventos = []
        for tid in vistos_ahora:
            if self.confirmador.ya_alertado(tid):
                continue
            if self.confirmador.confirmado(tid):
                evento = self._construir_evento(tid, frame)
                if evento is not None:
                    eventos.append(evento)
                    self.confirmador.registrar_alerta(tid)
        return eventos

    def _velocidad(self, historial: deque) -> float:
        """Velocidad en 'alturas de cuerpo por segundo' entre el extremo mas
        viejo y el mas nuevo de la ventana. Normalizar por la altura de la
        caja (que se aproxima a la distancia a la camara) es lo que hace que
        el umbral sirva igual de cerca que de lejos."""
        if len(historial) < 2:
            return 0.0
        t0, x0, y0, h0 = historial[0]
        t1, x1, y1, h1 = historial[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0.0
        distancia = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        alto_promedio = (h0 + h1) / 2
        return (distancia / alto_promedio) / dt

    def vaciar(self) -> list[DetectionEvent]:
        # Igual que armas: la alerta ya salio en cuanto se confirmo, no hay
        # nada pendiente que emitir al cerrar.
        return []

    # ----------------------------------------------------------------------

    def _frame_evidencia(
        self, tid: int, frame: FrameInfo, bbox: tuple[int, int, int, int]
    ) -> tuple[np.ndarray, int, int, int, int]:
        """Devuelve el frame donde dibujar la evidencia, y el bbox ya en las
        coordenadas de ESE frame.

        Si ya llego la foto HD pedida cuando este track empezo a acumular
        evidencia, se usa esa (mas nitida para el operador) con el bbox
        escalado. Si no -- todavia no llega, la fuente no es una Hikvision, o
        el track nunca supero el umbral hasta este ultimo frame -- se usa el
        frame normal tal cual, sin perder nada respecto al comportamiento
        anterior.
        """
        frame_hd = SnapshotHD.resultado_listo(self._hd_futures.get(tid))
        if frame_hd is None or frame_hd.size == 0:
            return frame.frame.copy(), *bbox

        x1, y1, x2, y2 = escalar_bbox(bbox, self._forma_frame, frame_hd.shape[:2], margen=0.0)
        self._hd_usados += 1
        return frame_hd.copy(), x1, y1, x2, y2

    def _construir_evento(self, tid: int, frame: FrameInfo) -> Optional[DetectionEvent]:
        datos = self._ultima_deteccion.get(tid)
        if datos is None:
            return None

        aciertos, total = self.confirmador.progreso(tid)
        x1, y1, x2, y2 = (int(v) for v in datos["bbox"])
        velocidad = datos["velocidad"]

        # No es una probabilidad de clasificacion (no hay clase que clasificar
        # aqui): se usa como una lectura acotada de "cuanto se paso del
        # umbral", para que el operador tenga una senal de magnitud.
        confianza = min(1.0, velocidad / (self.cfg.motion_speed_threshold * 2))

        evento = DetectionEvent(
            camera_id=self.cfg.camera_id,
            type=EventType.ANOMALY,
            track_id=tid,
            value=VALOR_EVENTO,
            confidence=round(confianza, 4),
            bbox=BBox(x1=x1, y1=y1, x2=x2, y2=y2),
            observations=aciertos,
            ts=_a_utc(datos["ts"]),
            meta={
                "velocidad_alturas_por_s": round(velocidad, 2),
                "umbral": self.cfg.motion_speed_threshold,
                "confirmacion": f"{aciertos}/{total} frames",
            },
        )

        vista, ex1, ey1, ex2, ey2 = self._frame_evidencia(tid, frame, (x1, y1, x2, y2))
        cv2.rectangle(vista, (ex1, ey1), (ex2, ey2), (0, 140, 255), 3)
        cv2.putText(vista, f"MOVIMIENTO SUBITO {velocidad:.1f}x", (ex1, max(20, ey1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 140, 255), 2)
        ruta = self.cfg.snapshot_dir / f"{evento.event_id}.jpg"
        cv2.imwrite(str(ruta), vista)
        evento.snapshot_path = ruta.relative_to(BASE_DIR).as_posix()

        self._eventos_emitidos += 1
        log.warning("MOVIMIENTO SUBITO: %.1f alturas/s (umbral %.1f, %d/%d frames, track=%d)",
                    velocidad, self.cfg.motion_speed_threshold, aciertos, total, tid)
        return evento

    def anotar(self, frame: np.ndarray) -> np.ndarray:
        for tid, datos in self._ultima_deteccion.items():
            x1, y1, x2, y2 = (int(v) for v in datos["bbox"])
            confirmado = self.confirmador.ya_alertado(tid)
            color = (0, 140, 255) if confirmado else (200, 200, 200)
            aciertos, _ = self.confirmador.progreso(tid)
            etiqueta = f"#{tid} {datos['velocidad']:.1f}x [{aciertos}/{self.cfg.motion_confirm_hits}]"
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            cv2.putText(frame, etiqueta, (x1, max(12, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        return frame

    def cerrar(self) -> None:
        if self.snapshot_hd is not None:
            self.snapshot_hd.cerrar()
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
            "tracks_activos": self.confirmador.activos,
            "evidencia_hd_usada": self._hd_usados,
            "eventos_emitidos": self._eventos_emitidos,
            "descartados_sin_confirmar": self.confirmador.descartados_sin_confirmar,
            "ms_inferencia_promedio": round(self._ms_inferencia / n, 1),
        }

    @property
    def resumen(self) -> str:
        s = self.stats
        return f"{s['tracks_activos']} personas, {s['ms_inferencia_promedio']}ms"


def _a_utc(epoch: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc)
