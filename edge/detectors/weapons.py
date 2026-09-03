"""Detector de armas con confirmacion temporal.

EL PROBLEMA CENTRAL DE ESTA FASE NO ES DETECTAR, ES NO GRITAR EN FALSO.

Un detector de armas ingenuo alerta con cualquier celular, control remoto,
desarmador o botella que agarre alguien. Y una alerta falsa de arma no es un
inconveniente: manda a una persona a responder a una emergencia inexistente.
Si eso pasa tres veces, el sistema se apaga y ya no sirve para nada.

La defensa es la CONFIRMACION TEMPORAL: un arma solo alerta si se sostiene en
N de los ultimos M frames del MISMO objeto seguido. Un reflejo o una mala
inferencia aislada no pasa el filtro; un cuchillo en la mano de alguien, si.

DIFERENCIA IMPORTANTE con placas y rostros: esos emiten su evento cuando el
objeto SALE de escena, porque hay tiempo de sobra y conviene acumular la mejor
evidencia. Un arma se emite EN CUANTO se confirma. Esperar a que la persona se
vaya para avisar que traia un arma no tiene ningun sentido.

--------------------------------------------------------------------------
SOBRE EL MODELO

Por defecto usa YOLO11 con COCO, que trae `knife`, `scissors` y `baseball bat`.
Eso cubre ARMAS BLANCAS y funciona hoy sin entrenar nada.

COCO NO TIENE ARMAS DE FUEGO. Para pistolas hace falta un modelo afinado; la
ruta se configura con WEAPON_MODEL y este detector lo usa automaticamente.
Ver la seccion de la Fase 5 en el README para el procedimiento de entrenamiento.

NO descargues pesos .pt de repositorios desconocidos: son archivos pickle y
ejecutan codigo arbitrario al cargarse. Entrena el tuyo o usa fuentes oficiales.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Optional

import cv2
import numpy as np

from edge.config import BASE_DIR, EdgeConfig
from edge.detectors.base import Detector
from edge.sources import FrameInfo
from shared.events import BBox, DetectionEvent, EventType

log = logging.getLogger(__name__)

# Clases de COCO que se consideran arma, con su nombre en espanol.
ARMAS_COCO = {
    "knife": "cuchillo",
    "scissors": "tijeras",
    "baseball bat": "bate",
}

# Traduccion para modelos afinados. Se amplia al entrenar clases nuevas.
NOMBRES_ES = {
    **ARMAS_COCO,
    "pistol": "pistola",
    "gun": "arma de fuego",
    "handgun": "pistola",
    "rifle": "rifle",
    "weapon": "arma",
}


class ConfirmacionTemporal:
    """Decide cuando una deteccion sostenida deja de ser ruido.

    Lleva, por cada objeto seguido, una ventana deslizante de los ultimos M
    frames marcando en cuales se le vio. El objeto se considera confirmado
    cuando acumula N aciertos dentro de esa ventana.

    Vive aparte del detector para poder probarse sin cargar ningun modelo:
    es la pieza de la que depende que el sistema no genere alarmas falsas, y
    esa merece pruebas propias.
    """

    def __init__(self, aciertos: int, ventana: int) -> None:
        if aciertos > ventana:
            raise ValueError("aciertos no puede ser mayor que la ventana")
        self.aciertos = aciertos
        self.ventana = ventana
        self._historial: dict[int, deque[bool]] = {}
        self._confirmados: set[int] = set()
        self.descartados_sin_confirmar = 0

    def marcar(self, tid: int, visto: bool) -> None:
        self._historial.setdefault(tid, deque(maxlen=self.ventana)).append(visto)

    def confirmado(self, tid: int) -> bool:
        h = self._historial.get(tid)
        return h is not None and sum(h) >= self.aciertos

    def ya_alertado(self, tid: int) -> bool:
        return tid in self._confirmados

    def registrar_alerta(self, tid: int) -> None:
        self._confirmados.add(tid)

    def perdido(self, tid: int) -> bool:
        """True si la ventana completa no tiene ni una aparicion."""
        h = self._historial.get(tid)
        return h is not None and len(h) == self.ventana and not any(h)

    def olvidar(self, tid: int) -> None:
        if self._historial.pop(tid, None) is not None and tid not in self._confirmados:
            self.descartados_sin_confirmar += 1
        self._confirmados.discard(tid)

    def progreso(self, tid: int) -> tuple[int, int]:
        h = self._historial.get(tid, ())
        return sum(h), len(h)

    @property
    def activos(self) -> int:
        return len(self._historial)

    def tracks(self) -> list[int]:
        return list(self._historial)


class WeaponDetector(Detector):
    name = "weapons"

    def __init__(self, cfg: EdgeConfig) -> None:
        self.cfg = cfg
        self.device = cfg.resolve_device()

        from ultralytics import YOLO

        if cfg.weapon_model.exists():
            ruta = cfg.weapon_model
            self.personalizado = True
            log.info("Cargando modelo de armas afinado: %s", ruta.name)
        else:
            ruta = BASE_DIR / "models" / "yolo11n.pt"
            self.personalizado = False
            log.warning(
                "No existe %s: usando YOLO11-COCO, que solo detecta ARMAS BLANCAS "
                "(cuchillo, tijeras, bate). Para armas de fuego hace falta un "
                "modelo afinado; ver la Fase 5 del README.", cfg.weapon_model.name
            )

        t0 = time.perf_counter()
        self.model = YOLO(str(ruta))
        self.model.to(self.device)
        log.info("Modelo de armas listo en %.1fs (device=%s)",
                 time.perf_counter() - t0, self.device)

        # Que ids de clase cuentan como arma en ESTE modelo.
        if self.personalizado:
            # En un modelo afinado se asume que todas sus clases son armas:
            # se entreno especificamente para eso.
            self.clases_arma = set(self.model.names)
        else:
            self.clases_arma = {i for i, n in self.model.names.items() if n in ARMAS_COCO}
        log.info("Clases consideradas arma: %s",
                 sorted(self.model.names[i] for i in self.clases_arma))

        self.confirmador = ConfirmacionTemporal(
            cfg.weapon_confirm_hits, cfg.weapon_confirm_window
        )
        self._ultima_deteccion: dict[int, dict] = {}
        self._frame_idx = 0
        self._eventos_emitidos = 0
        self._ms_inferencia = 0.0

    # ----------------------------------------------------------------------

    def procesar(self, frame: FrameInfo) -> list[DetectionEvent]:
        self._frame_idx += 1

        t0 = time.perf_counter()
        # .track() da track_id gratis: este modelo SI es de Ultralytics, asi que
        # trae ByteTrack integrado y no hace falta el IoUTracker propio.
        resultados = self.model.track(
            frame.frame,
            persist=True,
            verbose=False,
            conf=self.cfg.weapon_conf,
            classes=sorted(self.clases_arma) or None,
            device=self.device,
            tracker="bytetrack.yaml",
        )
        self._ms_inferencia += (time.perf_counter() - t0) * 1000

        vistos_ahora: set[int] = set()
        r = resultados[0]
        if r.boxes is not None and r.boxes.id is not None:
            for caja, tid, conf, cls in zip(
                r.boxes.xyxy.cpu().numpy(),
                r.boxes.id.cpu().numpy().astype(int),
                r.boxes.conf.cpu().numpy(),
                r.boxes.cls.cpu().numpy().astype(int),
            ):
                tid = int(tid)
                vistos_ahora.add(tid)
                etiqueta = self.model.names[int(cls)]
                self._ultima_deteccion[tid] = {
                    "bbox": tuple(float(v) for v in caja),
                    "conf": float(conf),
                    "etiqueta": etiqueta,
                    "ts": frame.ts,
                }
                self.confirmador.marcar(tid, True)

        # Los tracks que existian pero no se vieron en este frame suman un fallo.
        # Esto es lo que hace que una deteccion parpadeante nunca se confirme.
        for tid in self.confirmador.tracks():
            if tid not in vistos_ahora:
                self.confirmador.marcar(tid, False)
                if self.confirmador.perdido(tid):
                    # Ventana entera sin verlo: el objeto se fue.
                    self.confirmador.olvidar(tid)
                    self._ultima_deteccion.pop(tid, None)

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

    # ----------------------------------------------------------------------

    def _construir_evento(self, tid: int, frame: FrameInfo) -> Optional[DetectionEvent]:
        datos = self._ultima_deteccion.get(tid)
        if datos is None:
            return None

        aciertos, total = self.confirmador.progreso(tid)
        etiqueta_es = NOMBRES_ES.get(datos["etiqueta"], datos["etiqueta"])
        x1, y1, x2, y2 = (int(v) for v in datos["bbox"])

        evento = DetectionEvent(
            camera_id=self.cfg.camera_id,
            type=EventType.WEAPON,
            track_id=tid,
            value=etiqueta_es,
            confidence=round(datos["conf"], 4),
            bbox=BBox(x1=x1, y1=y1, x2=x2, y2=y2),
            observations=aciertos,
            ts=_a_utc(datos["ts"]),
            meta={
                "clase_modelo": datos["etiqueta"],
                "confirmacion": f"{aciertos}/{total} frames",
                "modelo": "afinado" if self.personalizado else "coco",
            },
        )

        # La evidencia de un arma incluye contexto, no solo el objeto: al
        # operador le sirve ver quien la lleva y donde esta. Se guarda el frame
        # completo con la caja marcada, no el recorte.
        vista = frame.frame.copy()
        cv2.rectangle(vista, (x1, y1), (x2, y2), (0, 0, 255), 3)
        cv2.putText(vista, etiqueta_es.upper(), (x1, max(20, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        ruta = self.cfg.snapshot_dir / f"{evento.event_id}.jpg"
        cv2.imwrite(str(ruta), vista)
        evento.snapshot_path = ruta.relative_to(BASE_DIR).as_posix()

        self._eventos_emitidos += 1
        log.warning("ARMA CONFIRMADA: %s (conf %.2f, %d/%d frames, track=%d)",
                    etiqueta_es, datos["conf"], aciertos, total, tid)
        return evento

    def vaciar(self) -> list[DetectionEvent]:
        # Un arma ya se alerto en su momento; no hay nada pendiente que emitir
        # al cerrar. Lo contrario de placas y rostros.
        return []

    def anotar(self, frame: np.ndarray) -> np.ndarray:
        for tid, datos in self._ultima_deteccion.items():
            x1, y1, x2, y2 = (int(v) for v in datos["bbox"])
            confirmado = self.confirmador.ya_alertado(tid)
            # Rojo si ya se confirmo, amarillo mientras acumula evidencia: deja
            # ver el filtro trabajando en vez de que parezca que no detecta.
            color = (0, 0, 255) if confirmado else (0, 200, 255)
            aciertos, _ = self.confirmador.progreso(tid)
            etiqueta = (f"{NOMBRES_ES.get(datos['etiqueta'], datos['etiqueta'])} "
                        f"{aciertos}/{self.cfg.weapon_confirm_hits}")
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            cv2.putText(frame, etiqueta, (x1, max(12, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
        return frame

    @property
    def stats(self) -> dict[str, Any]:
        n = max(1, self._frame_idx)
        return {
            "frames": self._frame_idx,
            "tracks_activos": self.confirmador.activos,
            "eventos_emitidos": self._eventos_emitidos,
            "descartados_sin_confirmar": self.confirmador.descartados_sin_confirmar,
            "ms_inferencia_promedio": round(self._ms_inferencia / n, 1),
            "modelo": "afinado" if self.personalizado else "coco (solo armas blancas)",
        }


def _a_utc(epoch: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc)
