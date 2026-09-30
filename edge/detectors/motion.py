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

from edge.aceleracion import precision
from edge.config import BASE_DIR, EdgeConfig
from edge.detectors.base import Detector, Pista
from edge.detectors.confirmacion import ConfirmacionTemporal
from edge.snapshot_hd import SnapshotHD, escalar_bbox
from edge.sources import FrameInfo
from shared.events import BBox, DetectionEvent, EventType

log = logging.getLogger(__name__)

CLASE_PERSONA = 0  # id de 'person' en COCO
# Vehiculos de COCO, para las reglas de zona (conteo de autos, intrusion de
# un vehiculo en el acceso peatonal). Salen del MISMO paso del modelo: no
# cuesta otra inferencia.
CLASES_VEHICULO = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}

VALOR_EVENTO = "movimiento_subito"
VALOR_CAIDA = "persona_caida"

# Proporcion alto/ancho de la caja de una persona. De pie ronda 2.5-3; tendida
# en el piso baja de 1. Se exige pasar de claramente de pie a claramente
# tendida, para que agacharse o sentarse (que quedan en medio) no cuenten.
DE_PIE = 1.4
TENDIDA = 0.9
FRAMES_TENDIDA = 3          # sostenido: un frame raro de la caja no es una caida
SEGUNDOS_TRANSICION = 2.0   # caerse es subito; acostarse despacio no alerta

# Velocidad (alturas de cuerpo por segundo) entre dos muestras consecutivas
# por encima de la cual el salto no es de la persona sino del tracker: cambio
# de identidad entre dos personas cercanas, o una caja que se corto por una
# oclusion. Nadie se desplaza diez veces su estatura en un segundo.
SALTO_IMPOSIBLE = 10.0

# Cambio de altura de la caja entre muestras consecutivas que indica lo mismo:
# la caja dejo de corresponder a la misma silueta completa. Es holgado a
# proposito: agacharse o caer cambia la altura, pero no a la mitad en 1/8 s.
RAZON_ALTURA_MAX = 2.0


class MotionAnomalyDetector(Detector):
    name = "motion"

    def __init__(self, cfg: EdgeConfig) -> None:
        self.cfg = cfg
        self.device = cfg.resolve_device()

        from edge.aceleracion import cargar_yolo, usar_half

        # Por defecto el YOLO11-small estandar de Ultralytics (MOTION_MODEL).
        # Puede ser un .engine de TensorRT exportado con
        # tools/optimizar_modelos.py: misma deteccion, bastante mas rapida.
        ruta = cfg.motion_model
        t0 = time.perf_counter()
        self.model = cargar_yolo(ruta, self.device)
        self.half = usar_half(cfg.yolo_half, self.device)
        log.info("Modelo de movimiento %s listo en %.1fs (device=%s, fp16=%s)",
                 ruta.name, time.perf_counter() - t0, self.device, self.half)

        self.clases = [CLASE_PERSONA] + (sorted(CLASES_VEHICULO) if getattr(cfg, "enable_zonas", False) else [])
        # Con el detector de pose activo, la caida se juzga por el angulo del
        # torso (mucho mas fiable que la forma de la caja): aqui ya no.
        self.caida_por_caja = not getattr(cfg, "enable_pose", False)
        self._pistas: list[Pista] = []

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
        self._alertado_en: dict[int, float] = {}
        # Proporcion alto/ancho reciente de cada persona, para detectar caidas.
        self._posturas: dict[int, deque[tuple[float, float]]] = {}
        self._caida_en: dict[int, float] = {}
        self._caidas_emitidas = 0

        self._frame_idx = 0
        self._eventos_emitidos = 0
        self._saltos_descartados = 0
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
            classes=self.clases,
            device=self.device,
            tracker="bytetrack.yaml",
            **precision(self.half),
        )
        self._ms_inferencia += (time.perf_counter() - t0) * 1000

        vistos_ahora: set[int] = set()
        self._pistas = []
        r = resultados[0]
        if r.boxes is not None and r.boxes.id is not None:
            n = len(r.boxes.id)
            clases = (r.boxes.cls.cpu().numpy().astype(int) if getattr(r.boxes, "cls", None) is not None
                      else np.zeros(n, dtype=int))
            confianzas = (r.boxes.conf.cpu().numpy() if getattr(r.boxes, "conf", None) is not None
                          else np.ones(n))
            for caja, tid, clase, conf in zip(
                r.boxes.xyxy.cpu().numpy(),
                r.boxes.id.cpu().numpy().astype(int),
                clases, confianzas,
            ):
                tid = int(tid)
                x1, y1, x2, y2 = (float(v) for v in caja)
                if int(clase) != CLASE_PERSONA:
                    self._pistas.append(Pista(tid, "vehiculo", (x1, y1, x2, y2), float(conf),
                                              CLASES_VEHICULO.get(int(clase), "")))
                    continue
                self._pistas.append(Pista(tid, "persona", (x1, y1, x2, y2), float(conf), "person"))
                vistos_ahora.add(tid)
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                alto = max(1.0, y2 - y1)

                historial = self._posiciones.setdefault(
                    tid, deque(maxlen=self._ventana_posiciones)
                )
                if historial and self._salto_del_tracker(historial[-1], (frame.ts, cx, cy, alto)):
                    # El historial ya no describe a esta silueta: se empieza
                    # de nuevo en vez de medir una "velocidad" que no existio.
                    historial.clear()
                    self._saltos_descartados += 1
                historial.append((frame.ts, cx, cy, alto))
                velocidad = self._velocidad(historial)

                posturas = self._posturas.setdefault(
                    tid, deque(maxlen=max(8, round(self.cfg.infer_fps * 4)))
                )
                posturas.append((frame.ts, alto / max(1.0, x2 - x1)))

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
                    self._alertado_en.pop(tid, None)
                    self._posturas.pop(tid, None)
                    self._caida_en.pop(tid, None)

        eventos = []
        for tid in (vistos_ahora if self.caida_por_caja else ()):
            ultima = self._caida_en.get(tid)
            if ultima is not None and frame.ts - ultima < self.cfg.motion_cooldown_s:
                continue
            if es_caida(list(self._posturas.get(tid, ()))):
                evento = self._construir_evento(tid, frame, caida=True)
                if evento is not None:
                    eventos.append(evento)
                    self._caida_en[tid] = frame.ts

        for tid in vistos_ahora:
            if self.confirmador.ya_alertado(tid):
                # Pasado el enfriamiento y con la persona ya calmada, se
                # rearma: un segundo incidente de la misma persona tambien
                # debe avisar.
                calmado = self.confirmador.progreso(tid)[0] == 0
                if calmado and frame.ts - self._alertado_en.get(tid, frame.ts) >= self.cfg.motion_cooldown_s:
                    self.confirmador.rearmar(tid)
                    self._hd_futures.pop(tid, None)
                continue
            if self.confirmador.confirmado(tid):
                evento = self._construir_evento(tid, frame)
                if evento is not None:
                    eventos.append(evento)
                    self.confirmador.registrar_alerta(tid)
                    self._alertado_en[tid] = frame.ts
        return eventos

    def pistas(self) -> list[Pista]:
        return list(self._pistas)

    @staticmethod
    def _salto_del_tracker(previa: tuple, actual: tuple) -> bool:
        t0, x0, y0, h0 = previa
        t1, x1, y1, h1 = actual
        razon = max(h0, h1) / max(1.0, min(h0, h1))
        if razon > RAZON_ALTURA_MAX:
            return True
        dt = t1 - t0
        if dt <= 0:
            return False
        distancia = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        return (distancia / ((h0 + h1) / 2)) / dt > SALTO_IMPOSIBLE

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

    def _construir_evento(self, tid: int, frame: FrameInfo,
                          caida: bool = False) -> Optional[DetectionEvent]:
        datos = self._ultima_deteccion.get(tid)
        if datos is None:
            return None

        aciertos, total = self.confirmador.progreso(tid)
        x1, y1, x2, y2 = (int(v) for v in datos["bbox"])
        velocidad = datos["velocidad"]

        if caida:
            proporcion = (y2 - y1) / max(1, x2 - x1)
            valor, confianza = VALOR_CAIDA, 0.8
            observaciones = FRAMES_TENDIDA
            meta = {"proporcion_alto_ancho": round(proporcion, 2),
                    "velocidad_alturas_por_s": round(velocidad, 2)}
            texto = "POSIBLE PERSONA CAIDA"
        else:
            # No es una probabilidad de clasificacion (no hay clase que
            # clasificar aqui): se usa como una lectura acotada de "cuanto se
            # paso del umbral", para que el operador tenga una senal de magnitud.
            valor = VALOR_EVENTO
            confianza = min(1.0, velocidad / (self.cfg.motion_speed_threshold * 2))
            observaciones = aciertos
            meta = {"velocidad_alturas_por_s": round(velocidad, 2),
                    "umbral": self.cfg.motion_speed_threshold,
                    "confirmacion": f"{aciertos}/{total} frames"}
            texto = f"MOVIMIENTO SUBITO {velocidad:.1f} alt/s"

        evento = DetectionEvent(
            camera_id=self.cfg.camera_id,
            type=EventType.ANOMALY,
            track_id=tid,
            value=valor,
            confidence=round(confianza, 4),
            bbox=BBox(x1=x1, y1=y1, x2=x2, y2=y2),
            observations=max(1, observaciones),
            ts=_a_utc(datos["ts"]),
            meta=meta,
        )

        vista, ex1, ey1, ex2, ey2 = self._frame_evidencia(tid, frame, (x1, y1, x2, y2))
        cv2.rectangle(vista, (ex1, ey1), (ex2, ey2), (0, 140, 255), 3)
        cv2.putText(vista, texto, (ex1, max(20, ey1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 140, 255), 2)
        ruta = self.cfg.snapshot_dir / f"{evento.event_id}.jpg"
        cv2.imwrite(str(ruta), vista)
        evento.snapshot_path = ruta.relative_to(BASE_DIR).as_posix()

        self._eventos_emitidos += 1
        if caida:
            self._caidas_emitidas += 1
            log.warning("POSIBLE PERSONA CAIDA (track=%d)", tid)
        else:
            log.warning("MOVIMIENTO SUBITO: %.1f alturas/s (umbral %.1f, %d/%d frames, track=%d)",
                        velocidad, self.cfg.motion_speed_threshold, aciertos, total, tid)
        return evento

    def anotar(self, frame: np.ndarray) -> np.ndarray:
        for tid, datos in self._ultima_deteccion.items():
            x1, y1, x2, y2 = (int(v) for v in datos["bbox"])
            confirmado = self.confirmador.ya_alertado(tid)
            color = (0, 140, 255) if confirmado else (200, 200, 200)
            aciertos, _ = self.confirmador.progreso(tid)
            etiqueta = (f"#{tid} {datos['velocidad']:.1f} alt/s "
                        f"[{aciertos}/{self.cfg.motion_confirm_hits}]")
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
            "saltos_de_tracker_descartados": self._saltos_descartados,
            "caidas": self._caidas_emitidas,
            "ms_inferencia_promedio": round(self._ms_inferencia / n, 1),
        }

    @property
    def resumen(self) -> str:
        s = self.stats
        return f"{s['tracks_activos']} personas, {s['ms_inferencia_promedio']}ms"


def es_caida(posturas: list[tuple[float, float]]) -> bool:
    """Si la secuencia (ts, alto/ancho) de una persona muestra una caida.

    Hace falta que los ultimos FRAMES_TENDIDA frames la muestren tendida y que
    poco antes (SEGUNDOS_TRANSICION) haya estado claramente de pie. Asi no
    alerta alguien que ya estaba acostado cuando empezo a verse, ni alguien que
    se acuesta despacio en una banca.
    """
    if len(posturas) < FRAMES_TENDIDA + 1:
        return False
    recientes = posturas[-FRAMES_TENDIDA:]
    if any(r > TENDIDA for _, r in recientes):
        return False
    primera_tendida = recientes[0][0]
    for ts, r in reversed(posturas[:-FRAMES_TENDIDA]):
        if r >= DE_PIE:
            return primera_tendida - ts <= SEGUNDOS_TRANSICION
        if r > TENDIDA:
            continue   # en transicion: se sigue buscando cuando estaba de pie
        return False   # ya estaba tendida antes: no es una caida nueva
    return False


def _a_utc(epoch: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc)
