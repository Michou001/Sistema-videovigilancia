"""Recolector de datos de entrenamiento: el cuadro LIMPIO de cada alerta.

El ciclo de mejora de los modelos:

  1. Con DATASET_ENABLED=true, cuando un detector (arma, movimiento, pose,
     zona) genera un evento, el worker guarda el cuadro original -- sin cajas
     ni textos dibujados, que ensenarian al modelo a buscar el recuadro -- y
     la caja del objeto, en data/entrenamiento/<camara>/.
  2. Si la API responde que el evento NO fue alerta, se borra: solo las
     alertas las revisa un operador.
  3. Los operadores atienden las alertas en el dashboard: "Atendida" confirma
     la deteccion; "Falso positivo" la desmiente.
  4. `python tools/dataset_alertas.py` junta los cuadros con su veredicto y
     arma un dataset YOLO: las confirmadas con su caja, los falsos positivos
     como imagen de fondo (le ensenan al modelo que ahi NO hay nada). Con eso
     se afina el modelo (docs/reentrenamiento.md) y se vuelve a empezar.

Los cuadros se quedan en la maquina del worker (no viajan a la API) y se
borran solos a los RETENCION_DATASET_DIAS. Son imagenes de personas: el
aviso de privacidad debe contemplar este uso (docs/privacidad.md).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

import cv2

from edge.config import BASE_DIR

log = logging.getLogger(__name__)

CARPETA = BASE_DIR / "data" / "entrenamiento"


class RecolectorDataset:
    """Complemento del worker (ver Camara.complementos en edge/worker.py)."""

    def __init__(self, cfg, carpeta: Optional[Path] = None) -> None:
        self.cfg = cfg
        self.carpeta = (carpeta or CARPETA) / cfg.camera_id
        self.carpeta.mkdir(parents=True, exist_ok=True)
        self.tipos = {t.strip() for t in str(getattr(cfg, "dataset_tipos", "")).split(",") if t.strip()}
        self.dias = float(getattr(cfg, "dataset_dias", 30))
        self.guardados = 0
        self.descartados = 0
        self._ultima_limpieza = 0.0
        self.limpiar()

    # -- interfaz de complemento -------------------------------------------

    def al_frame(self, frame) -> None:
        ahora = time.time()
        if ahora - self._ultima_limpieza > 3600:
            self.limpiar()

    def eventos(self) -> list:
        return []

    def estado(self) -> dict:
        return {"dataset": {"guardados": self.guardados, "descartados": self.descartados}}

    def cerrar(self) -> None:
        pass

    def al_evento(self, evento, frame) -> None:
        if evento.type.value not in self.tipos or evento.bbox is None or frame is None:
            return
        imagen = frame.frame
        alto, ancho = imagen.shape[:2]
        base = self.carpeta / evento.event_id
        try:
            if not cv2.imwrite(str(base.with_suffix(".jpg")), imagen, [int(cv2.IMWRITE_JPEG_QUALITY), 92]):
                return
            meta = evento.meta or {}
            datos = {
                "event_id": evento.event_id, "camera_id": evento.camera_id, "tipo": evento.type.value,
                "valor": evento.value, "clase": meta.get("clase"), "ts": evento.ts.isoformat(),
                "ancho": ancho, "alto": alto,
                "bbox": [evento.bbox.x1, evento.bbox.y1, evento.bbox.x2, evento.bbox.y2],
                "confianza": evento.confidence,
            }
            base.with_suffix(".json").write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
            self.guardados += 1
        except Exception as e:  # noqa: BLE001 - el dataset nunca frena la deteccion
            log.debug("No se pudo guardar el cuadro de entrenamiento: %s", e)

    def al_responder(self, lote: dict, respuesta: dict) -> None:
        """Enganche de HttpSink: lo que no fue alerta no se va a revisar."""
        for m in respuesta.get("matches", []):
            if m.get("severity") == "info" and m.get("event_id"):
                if self._borrar(m["event_id"]):
                    self.descartados += 1

    # -- mantenimiento -----------------------------------------------------

    def _borrar(self, event_id: str) -> bool:
        borrado = False
        for ext in (".jpg", ".json"):
            ruta = self.carpeta / f"{event_id}{ext}"
            if ruta.exists():
                ruta.unlink(missing_ok=True)
                borrado = True
        return borrado

    def limpiar(self) -> int:
        """Borra lo que paso de RETENCION_DATASET_DIAS."""
        self._ultima_limpieza = time.time()
        limite = time.time() - self.dias * 86400
        n = 0
        for ruta in list(self.carpeta.glob("*.jpg")) + list(self.carpeta.glob("*.json")):
            try:
                if ruta.stat().st_mtime < limite:
                    ruta.unlink()
                    n += 1
            except OSError:
                pass
        return n


def crear_recolector(cfg, sink) -> Optional[RecolectorDataset]:
    if not getattr(cfg, "dataset_enabled", False):
        return None
    recolector = RecolectorDataset(cfg)
    http = getattr(sink, "http", None)
    if http is not None:
        anterior = http.al_responder

        def _encadenado(lote, respuesta):
            recolector.al_responder(lote, respuesta)
            if anterior is not None:
                anterior(lote, respuesta)

        http.al_responder = _encadenado
    log.info("[%s] Recolectando cuadros de alertas para reentrenar (%s, %d dias)", cfg.camera_id,
             ", ".join(sorted(recolector.tipos)), recolector.dias)
    return recolector
