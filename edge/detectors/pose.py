"""Pose de las personas (YOLO11-pose): caidas, manos arriba y posible agresion.

El detector de movimiento mide la CAJA de cada persona. La caja no distingue
a alguien que se cae de alguien que se agacha a recoger algo (en los dos casos
se vuelve mas ancha que alta), ni ve los brazos. El esqueleto si:

  CAIDA           el torso (hombros -> cadera) pasa de vertical a horizontal
                  en menos de 2 s, Y la cadera baja hacia el piso. Agacharse
                  inclina el torso pero la cadera se queda donde estaba.
  MANOS ARRIBA    las dos munecas por encima de la cabeza, con el torso
                  erguido, sostenido MANOS_ARRIBA_S segundos: la postura de
                  un asalto. Estirarse dura menos.
  POSIBLE         munecas moviendose muy rapido (golpes, empujones) en varios
  AGRESION        frames seguidos, con otra persona pegada. Es la senal mas
                  debil de las tres: va como advertencia para que alguien mire.

Todo se normaliza por el largo del torso de cada persona, asi que los mismos
umbrales sirven cerca y lejos de la camara. Los puntos con poca confianza
(tapados, fuera de cuadro) no se usan.

Modelo: yolo11n-pose (Ultralytics) se descarga solo la primera vez (~6 MB).
Puede ser un .engine de TensorRT (tools/optimizar_modelos.py).
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

import cv2
import numpy as np

from edge.aceleracion import precision
from edge.config import BASE_DIR
from edge.detectors.base import Detector, caja
from edge.sources import FrameInfo
from shared.events import BBox, DetectionEvent, EventType

log = logging.getLogger(__name__)

# Puntos del esqueleto COCO
NARIZ, OJO_I, OJO_D, OREJA_I, OREJA_D = 0, 1, 2, 3, 4
HOMBRO_I, HOMBRO_D, CODO_I, CODO_D, MUNECA_I, MUNECA_D = 5, 6, 7, 8, 9, 10
CADERA_I, CADERA_D = 11, 12
HUESOS = [(5, 6), (5, 7), (7, 9), (6, 8), (8, 10), (5, 11), (6, 12), (11, 12), (11, 13), (13, 15),
          (12, 14), (14, 16), (0, 5), (0, 6)]

CONF_PUNTO = 0.4
ERGUIDO = 30.0            # grados del torso respecto a la vertical
TENDIDO = 60.0
FRAMES_TENDIDO = 3
SEGUNDOS_CAIDA = 2.0
BAJADA_CADERA = 0.25      # fraccion de la altura de pie que baja la cadera al caer
VEL_AGRESION = 6.0        # largos de torso por segundo de una muneca
FRAMES_AGRESION = 3
VENTANA_AGRESION = 5
ENFRIAMIENTO_S = 60.0     # por persona y tipo de evento


# --------------------------------------------------------------------------
# Geometria del esqueleto (funciones puras, se prueban sin modelo)
# --------------------------------------------------------------------------

def _promedio(kp: np.ndarray, conf: np.ndarray, indices: tuple[int, ...],
              minimo: float = CONF_PUNTO) -> Optional[tuple[float, float]]:
    buenos = [kp[i] for i in indices if conf[i] >= minimo]
    if not buenos:
        return None
    return float(np.mean([p[0] for p in buenos])), float(np.mean([p[1] for p in buenos]))


def torso(kp: np.ndarray, conf: np.ndarray) -> Optional[tuple[tuple[float, float], tuple[float, float], float]]:
    """(centro de hombros, centro de cadera, largo) o None si no se ven."""
    hombros = _promedio(kp, conf, (HOMBRO_I, HOMBRO_D))
    cadera = _promedio(kp, conf, (CADERA_I, CADERA_D))
    if hombros is None or cadera is None:
        return None
    largo = math.dist(hombros, cadera)
    if largo < 4:
        return None
    return hombros, cadera, largo


def angulo_torso(kp: np.ndarray, conf: np.ndarray) -> Optional[float]:
    """Grados respecto a la vertical: 0 de pie, 90 tendido, 180 de cabeza."""
    t = torso(kp, conf)
    if t is None:
        return None
    (hx, hy), (cx, cy), _ = t
    return math.degrees(math.atan2(abs(hx - cx), cy - hy))


def manos_arriba(kp: np.ndarray, conf: np.ndarray) -> bool:
    """Las dos munecas por encima de la cabeza, con el torso erguido."""
    t = torso(kp, conf)
    if t is None or conf[MUNECA_I] < CONF_PUNTO or conf[MUNECA_D] < CONF_PUNTO:
        return False
    (hx, hy), _, largo = t
    angulo = angulo_torso(kp, conf)
    if angulo is None or angulo > ERGUIDO:
        return False
    cabeza = _promedio(kp, conf, (NARIZ, OJO_I, OJO_D, OREJA_I, OREJA_D))
    tope = cabeza[1] if cabeza is not None else hy - 0.45 * largo
    # "Por encima" con margen: una mano a la altura de la frente no cuenta.
    limite = min(tope, hy - 0.5 * largo)
    return kp[MUNECA_I][1] < limite and kp[MUNECA_D][1] < limite


@dataclass
class Muestra:
    ts: float
    angulo: Optional[float]
    cadera_y: Optional[float]
    alto: float                         # alto de la caja
    munecas: tuple[Optional[tuple[float, float]], Optional[tuple[float, float]]]
    largo_torso: Optional[float]


def es_caida(historial: list[Muestra]) -> bool:
    """Torso tendido en los ultimos FRAMES_TENDIDO frames, erguido hace menos
    de SEGUNDOS_CAIDA, y la cadera bajo al menos BAJADA_CADERA de la altura
    que tenia de pie."""
    if len(historial) < FRAMES_TENDIDO + 1:
        return False
    recientes = historial[-FRAMES_TENDIDO:]
    if any(m.angulo is None or m.angulo < TENDIDO for m in recientes):
        return False
    primera = recientes[0]
    for m in reversed(historial[:-FRAMES_TENDIDO]):
        if m.angulo is None:
            continue
        if m.angulo <= ERGUIDO:
            if primera.ts - m.ts > SEGUNDOS_CAIDA:
                return False
            if m.cadera_y is None or recientes[-1].cadera_y is None:
                return False
            return recientes[-1].cadera_y - m.cadera_y >= BAJADA_CADERA * m.alto
        if m.angulo >= TENDIDO:
            return False          # ya estaba tendida: no es una caida nueva
    return False


def velocidad_munecas(previa: Muestra, actual: Muestra) -> float:
    """La muneca mas rapida, en largos de torso por segundo."""
    dt = actual.ts - previa.ts
    largo = actual.largo_torso or previa.largo_torso
    if dt <= 0 or not largo:
        return 0.0
    mejor = 0.0
    for a, b in zip(previa.munecas, actual.munecas):
        if a is not None and b is not None:
            mejor = max(mejor, math.dist(a, b) / largo / dt)
    return mejor


def cerca(caja_a, caja_b) -> bool:
    """Dos personas al alcance de un brazo: cajas que se tocan o centros a
    menos de 0.6 alturas."""
    ax1, ay1, ax2, ay2 = caja_a
    bx1, by1, bx2, by2 = caja_b
    if ax1 < bx2 and bx1 < ax2 and ay1 < by2 and by1 < ay2:
        return True
    alto = ((ay2 - ay1) + (by2 - by1)) / 2
    ca = ((ax1 + ax2) / 2, (ay1 + ay2) / 2)
    cb = ((bx1 + bx2) / 2, (by1 + by2) / 2)
    return math.dist(ca, cb) < 0.6 * alto


# --------------------------------------------------------------------------
# Detector
# --------------------------------------------------------------------------

@dataclass
class _Persona:
    historial: deque = field(default_factory=lambda: deque(maxlen=48))
    manos_desde: Optional[float] = None
    rapidos: deque = field(default_factory=lambda: deque(maxlen=VENTANA_AGRESION))
    ultimo: dict = field(default_factory=dict)       # tipo de evento -> ts
    caja: tuple = (0, 0, 0, 0)
    puntos: Optional[np.ndarray] = None
    confianzas: Optional[np.ndarray] = None
    visto: float = 0.0


class PoseDetector(Detector):
    name = "pose"

    def __init__(self, cfg, modelo=None) -> None:
        self.cfg = cfg
        self.device = cfg.resolve_device() if hasattr(cfg, "resolve_device") else "cpu"
        if modelo is None:
            from edge.aceleracion import cargar_yolo, usar_half

            t0 = time.perf_counter()
            modelo = cargar_yolo(cfg.pose_model, self.device)
            self.half = usar_half(cfg.yolo_half, self.device)
            log.info("Modelo de pose %s listo en %.1fs (device=%s, fp16=%s)", cfg.pose_model.name,
                     time.perf_counter() - t0, self.device, self.half)
        else:
            self.half = False
        self.model = modelo
        self.manos_s = float(getattr(cfg, "manos_arriba_s", 2.0))
        self.agresion = bool(getattr(cfg, "pose_agresion", True))
        self._personas: dict[int, _Persona] = {}
        self._frames = 0
        self._ms = 0.0
        self._conteo = {"persona_caida": 0, "manos_arriba": 0, "posible_agresion": 0}

    # ------------------------------------------------------------------

    def procesar(self, frame: FrameInfo) -> list[DetectionEvent]:
        self._frames += 1
        t0 = time.perf_counter()
        r = self.model.track(frame.frame, persist=True, verbose=False, conf=self.cfg.pose_conf,
                             imgsz=self.cfg.imgsz, device=self.device, tracker="bytetrack.yaml",
                             **precision(self.half))[0]
        self._ms += (time.perf_counter() - t0) * 1000
        ahora = frame.ts

        vistos: list[int] = []
        if r.boxes is not None and r.boxes.id is not None and r.keypoints is not None:
            cajas = r.boxes.xyxy.cpu().numpy()
            ids = r.boxes.id.cpu().numpy().astype(int)
            puntos = r.keypoints.xy.cpu().numpy()
            confs = (r.keypoints.conf.cpu().numpy() if r.keypoints.conf is not None
                     else np.ones(puntos.shape[:2]))
            for caja, tid, kp, kc in zip(cajas, ids, puntos, confs):
                tid = int(tid)
                vistos.append(tid)
                p = self._personas.setdefault(tid, _Persona())
                p.caja, p.puntos, p.confianzas, p.visto = tuple(float(v) for v in caja), kp, kc, ahora
                t = torso(kp, kc)
                munecas = tuple((float(kp[i][0]), float(kp[i][1])) if kc[i] >= CONF_PUNTO else None
                                for i in (MUNECA_I, MUNECA_D))
                muestra = Muestra(ts=ahora, angulo=angulo_torso(kp, kc),
                                  cadera_y=t[1][1] if t else None, alto=float(caja[3] - caja[1]),
                                  munecas=munecas, largo_torso=t[2] if t else None)
                if p.historial:
                    p.rapidos.append(velocidad_munecas(p.historial[-1], muestra) >= VEL_AGRESION)
                p.historial.append(muestra)
                if manos_arriba(kp, kc):
                    p.manos_desde = p.manos_desde or ahora
                else:
                    p.manos_desde = None

        eventos: list[DetectionEvent] = []
        for tid in vistos:
            p = self._personas[tid]
            if es_caida(list(p.historial)) and self._libre(p, "persona_caida", ahora):
                self._marcar(p, "persona_caida", ahora)
                eventos.append(self._evento(frame, tid, p, "persona_caida", 0.8, {
                    "metodo": "pose", "angulo_torso": round(p.historial[-1].angulo or 0, 1)}))
            if (p.manos_desde is not None and ahora - p.manos_desde >= self.manos_s
                    and self._libre(p, "manos_arriba", ahora)):
                self._marcar(p, "manos_arriba", ahora)
                eventos.append(self._evento(frame, tid, p, "manos_arriba", 0.75, {
                    "segundos": round(ahora - p.manos_desde, 1)}))
            if self.agresion and sum(p.rapidos) >= FRAMES_AGRESION:
                otro = next((o for o in vistos if o != tid and cerca(p.caja, self._personas[o].caja)), None)
                # Un solo aviso por pareja: el enfriamiento corre para los dos.
                if otro is not None and self._libre(p, "posible_agresion", ahora) \
                        and self._libre(self._personas[otro], "posible_agresion", ahora):
                    self._marcar(p, "posible_agresion", ahora)
                    self._marcar(self._personas[otro], "posible_agresion", ahora)
                    eventos.append(self._evento(frame, tid, p, "posible_agresion", 0.6, {
                        "con_track": otro, "frames_rapidos": f"{sum(p.rapidos)}/{len(p.rapidos)}"}))
                    p.rapidos.clear()

        for tid in [t for t, p in self._personas.items() if ahora - p.visto > 5.0]:
            del self._personas[tid]
        return eventos

    @staticmethod
    def _libre(p: _Persona, tipo: str, ahora: float) -> bool:
        return ahora - p.ultimo.get(tipo, -1e9) >= ENFRIAMIENTO_S

    @staticmethod
    def _marcar(p: _Persona, tipo: str, ahora: float) -> None:
        p.ultimo[tipo] = ahora

    def _evento(self, frame: FrameInfo, tid: int, p: _Persona, valor: str, confianza: float,
                meta: dict) -> DetectionEvent:
        from datetime import datetime, timezone

        x1, y1, x2, y2 = (int(v) for v in p.caja)
        evento = DetectionEvent(
            camera_id=self.cfg.camera_id, type=EventType.ANOMALY, track_id=tid, value=valor,
            confidence=confianza, bbox=BBox(x1=x1, y1=y1, x2=x2, y2=y2),
            observations=len(p.historial), ts=datetime.fromtimestamp(frame.ts, tz=timezone.utc), meta=meta)
        try:
            vista = self.anotar(frame.frame.copy(), solo=tid)
            cv2.rectangle(vista, (x1, y1), (x2, y2), (0, 0, 255), 3)
            ruta = self.cfg.snapshot_dir / f"{evento.event_id}.jpg"
            if cv2.imwrite(str(ruta), vista):
                try:
                    evento.snapshot_path = ruta.relative_to(BASE_DIR).as_posix()
                except ValueError:
                    evento.snapshot_path = ruta.as_posix()
        except Exception as e:  # noqa: BLE001
            log.debug("No se pudo guardar la evidencia de pose: %s", e)
        self._conteo[valor] += 1
        log.warning("[%s] %s (track=%d)", self.cfg.camera_id, valor.upper().replace("_", " "), tid)
        return evento

    # ------------------------------------------------------------------

    def anotar(self, frame: np.ndarray, solo: Optional[int] = None) -> np.ndarray:
        for tid, p in self._personas.items():
            if p.puntos is None or (solo is not None and tid != solo):
                continue
            for a, b in HUESOS:
                if p.confianzas[a] >= CONF_PUNTO and p.confianzas[b] >= CONF_PUNTO:
                    cv2.line(frame, (int(p.puntos[a][0]), int(p.puntos[a][1])),
                             (int(p.puntos[b][0]), int(p.puntos[b][1])), (255, 200, 0), 2)
        return frame

    def cajas(self) -> list[dict]:
        """El esqueleto de cada persona: puntos visibles (o null) para que el
        navegador dibuje los huesos."""
        salida = []
        for p in self._personas.values():
            if p.puntos is None:
                continue
            puntos =[[int(x), int(y)] if c >= CONF_PUNTO else None
                      for (x, y), c in zip(p.puntos.tolist(), p.confianzas.tolist())]
            salida.append(caja(p.caja, "", "#ffc800", "esqueleto", p=puntos))
        return salida

    def cerrar(self) -> None:
        try:
            del self.model
            if self.device == "cuda":
                import torch

                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    @property
    def stats(self) -> dict[str, Any]:
        return {"frames": self._frames, "tracks_activos": len(self._personas),
                "ms_inferencia_promedio": round(self._ms / max(1, self._frames), 1), **self._conteo}

    @property
    def resumen(self) -> str:
        s = self.stats
        return f"{s['tracks_activos']} esqueletos, {s['ms_inferencia_promedio']}ms"
