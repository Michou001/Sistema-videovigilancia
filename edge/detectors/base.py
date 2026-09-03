"""Interfaz comun de los detectores.

Los tres detectores (placas, rostros, armas) implementan este contrato. El
worker los trata igual y no sabe nada de sus modelos internos: agregar el de
armas en la Fase 5 sera anadir una linea a la lista de detectores.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from edge.sources import FrameInfo
from shared.events import DetectionEvent


class Detector(ABC):
    """Convierte frames en eventos.

    Regla importante: `procesar()` NO devuelve un evento por cada deteccion de
    cada frame. Devuelve eventos CONSOLIDADOS, es decir, un objeto que ya se
    termino de observar. La mayoria de las llamadas devuelven una lista vacia
    aunque haya detecciones en pantalla: el evento sale cuando el objeto se va,
    con la mejor evidencia acumulada de todos sus frames.
    """

    name: str = "detector"

    @abstractmethod
    def procesar(self, frame: FrameInfo) -> list[DetectionEvent]:
        """Procesa un frame. Devuelve los eventos que quedaron consolidados."""

    def vaciar(self) -> list[DetectionEvent]:
        """Cierra los objetos que siguen en pantalla y emite sus eventos.
        Se llama al detener el worker para no perder lo que estaba en curso."""
        return []

    def cerrar(self) -> None:
        """Libera modelos y memoria de GPU."""

    def anotar(self, frame):
        """Dibuja el estado actual sobre el frame, para la ventana de depuracion.

        Vive en el detector y no en el worker porque solo el detector sabe que
        vale la pena pintar. Devuelve el frame anotado.
        """
        return frame

    @property
    def stats(self) -> dict:
        """Metricas para el dashboard y para diagnosticar rendimiento."""
        return {}

    @property
    def resumen(self) -> str:
        """Linea corta para el reporte periodico del worker.

        Cada detector elige que numeros importan mirar en vivo. No es lo mismo
        depurar placas (cuantas lecturas de OCR van) que armas (cuanta evidencia
        lleva acumulada antes de confirmar).
        """
        s = self.stats
        return f"{s.get('tracks_activos', 0)} tracks, {s.get('ms_inferencia_promedio', 0)}ms"
