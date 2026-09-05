"""Confirmacion temporal: decide cuando una deteccion sostenida deja de ser ruido.

Nacio en el detector de armas (Fase 5) y se movio aqui porque el detector de
movimiento anomalo necesita exactamente la misma logica: llevar, por cada
objeto seguido, una ventana deslizante de los ultimos M frames marcando en
cuales se cumplio la condicion, y confirmar cuando acumula N aciertos.

Sin esto, cualquier detector que reaccione frame a frame (un reflejo, un salto
del tracker, una lectura aislada) generaria una alerta por evento aislado. La
defensa es exigir que la condicion se sostenga en el tiempo sobre el MISMO
track, no en frames sueltos.
"""

from __future__ import annotations

from collections import deque


class ConfirmacionTemporal:
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
