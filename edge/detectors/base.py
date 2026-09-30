"""Interfaz comun de los detectores.

Los tres detectores (placas, rostros, armas) implementan este contrato. El
worker los trata igual y no sabe nada de sus modelos internos: agregar el de
armas en la Fase 5 sera anadir una linea a la lista de detectores.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from edge.sources import FrameInfo
from shared.events import DetectionEvent


@dataclass
class Pista:
    """Un objeto seguido en el frame actual, para quien quiera razonar sobre
    el (reglas de zona). Evita que cada pieza corra su propio detector."""

    tid: int
    clase: str                               # "persona" | "vehiculo"
    bbox: tuple[float, float, float, float]  # pixeles del frame procesado
    conf: float = 1.0
    etiqueta: str = ""                       # car, truck, bus, motorcycle...


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

    def pistas(self) -> list[Pista]:
        """Objetos seguidos en el ULTIMO frame procesado. Solo los publica el
        detector que sigue personas y vehiculos (movimiento)."""
        return []

    def cajas(self) -> list[dict]:
        """Lo que hay que dibujar ahora, como DATOS (para que el navegador lo
        pinte sobre el video de go2rtc). Mismo contenido que `anotar()`."""
        return [caja(p.bbox, f"{NOMBRES_OBJETO.get(p.etiqueta, p.clase)} #{p.tid}",
                     "#9aa4b1" if p.clase == "persona" else "#38bdf8", p.clase)
                for p in self.pistas()]


NOMBRES_OBJETO = {"person": "persona", "car": "auto", "truck": "camión", "bus": "autobús",
                  "motorcycle": "moto"}


def caja(bbox, texto: str, color: str, tipo: str, **extra) -> dict:
    """Una caja para el navegador: coordenadas del frame procesado."""
    return {"b": [int(round(v)) for v in bbox], "t": texto, "c": color, "k": tipo, **extra}

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
