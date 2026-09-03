"""Tracker por IoU con prediccion de velocidad.

Por que existe: el modelo de placas se carga con torch.hub (formato YOLOv5), y
por ahi no viene el tracker integrado de Ultralytics. Sin tracking no hay forma
de decir "este es el mismo coche que el frame anterior", y sin eso el sistema
escribe una fila por frame -- el problema que el script original tapaba con un
COOLDOWN_SEGUNDOS de 15.

Por que no un ByteTrack completo: para placas sobre una camara fija, la
asociacion por IoU con prediccion lineal es suficiente y no agrega
dependencias. Los rostros y las armas (fases 4 y 5) usaran modelos de
Ultralytics, que ya traen ByteTrack integrado; ese es el caso donde la
diferencia se nota, porque hay oclusiones entre personas.

La pieza que si es indispensable aqui es la PREDICCION DE VELOCIDAD. A 8 fps
pasan 125 ms entre frames: un coche a 30 km/h avanza ~1 m, que a 720p puede
ser mas que el ancho de la caja de la placa. Sin predecir, el IoU da 0 y el
track se rompe en cada frame.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Optional

Caja = tuple[float, float, float, float]  # x1, y1, x2, y2


@dataclass
class Deteccion:
    """Una deteccion cruda del modelo, antes de asociarse a un track."""

    bbox: Caja
    confidence: float
    label: str = ""


@dataclass
class Track:
    """Un objeto seguido a lo largo de varios frames."""

    track_id: int
    bbox: Caja
    confidence: float
    first_seen: float
    last_seen: float

    hits: int = 1           # frames en los que se le vio
    age: int = 0            # frames consecutivos SIN verlo
    velocity: tuple[float, float] = (0.0, 0.0)  # px/frame del centro

    state: dict[str, Any] = field(default_factory=dict)
    """Espacio libre para que cada detector acumule lo suyo: lecturas de OCR
    en placas, historial de confirmacion en armas, embeddings en rostros."""

    @property
    def center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.bbox
        return (x1 + x2) / 2, (y1 + y2) / 2

    @property
    def area(self) -> float:
        x1, y1, x2, y2 = self.bbox
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)

    def predict(self) -> Caja:
        """Posicion esperada en el siguiente frame, extrapolando la velocidad."""
        vx, vy = self.velocity
        x1, y1, x2, y2 = self.bbox
        return (x1 + vx, y1 + vy, x2 + vx, y2 + vy)


def iou(a: Caja, b: Caja) -> float:
    """Interseccion sobre union de dos cajas."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b

    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter <= 0:
        return 0.0

    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


class IoUTracker:
    """Asocia detecciones entre frames y les asigna un track_id estable.

    Parametros que importan:

      iou_min    Umbral de solape para considerar que dos cajas son el mismo
                 objeto. 0.25 es permisivo a proposito: mejor mantener el track
                 de un coche que se mueve rapido, que partirlo en dos.

      max_age    Frames que un track sobrevive sin ser visto. A 8 fps, 16
                 frames son ~2 segundos: aguanta que la placa se pierda
                 momentaneamente por un reflejo o un cambio de angulo.

      min_hits   Frames antes de considerar el track confirmado. Filtra
                 detecciones parpadeantes de un solo frame, que casi siempre
                 son falsos positivos.

      dist_max_factor
                 Radio de busqueda de la asociacion por distancia, en multiplos
                 del tamano caracteristico de la caja. 2.0 permite que el objeto
                 se desplace hasta el doble de su propio tamano entre frames.

      razon_area_max
                 Cambio de tamano tolerado al asociar por distancia. 2.5 cubre
                 a un coche acercandose; mas alla empieza a emparejar cajas que
                 no tienen nada que ver.
    """

    def __init__(
        self,
        *,
        iou_min: float = 0.25,
        max_age: int = 16,
        min_hits: int = 3,
        dist_max_factor: float = 2.0,
        razon_area_max: float = 2.5,
    ) -> None:
        self.iou_min = iou_min
        self.max_age = max_age
        self.min_hits = min_hits
        self.dist_max_factor = dist_max_factor
        self.razon_area_max = razon_area_max

        self._tracks: dict[int, Track] = {}
        self._contador = itertools.count(1)
        self._expirados: list[Track] = []

    # -- API ---------------------------------------------------------------

    def update(self, detecciones: list[Deteccion], ts: float) -> list[Track]:
        """Procesa las detecciones de un frame. Devuelve los tracks confirmados.

        Los tracks que llevan mas de `max_age` frames sin verse se retiran y
        quedan disponibles en `recoger_expirados()`. Ese es el momento correcto
        para emitir el evento: ya se vio todo lo que se iba a ver del objeto.
        """
        self._expirados = []

        # 1. Asociacion voraz por IoU sobre la posicion PREDICHA.
        #    Voraz y no Hungaro a proposito: con pocas cajas por frame (rara vez
        #    mas de 5 placas) la diferencia de calidad es nula y el codigo se
        #    entiende de un vistazo.
        pares: list[tuple[float, int, int]] = []
        ids = list(self._tracks)
        for ti, tid in enumerate(ids):
            predicha = self._tracks[tid].predict()
            for di, det in enumerate(detecciones):
                solape = iou(predicha, det.bbox)
                if solape >= self.iou_min:
                    pares.append((solape, ti, di))

        pares.sort(reverse=True)
        tracks_usados: set[int] = set()
        dets_usadas: set[int] = set()

        for solape, ti, di in pares:
            if ti in tracks_usados or di in dets_usadas:
                continue
            tracks_usados.add(ti)
            dets_usadas.add(di)
            self._actualizar_track(self._tracks[ids[ti]], detecciones[di], ts)

        # 1b. Segunda pasada: asociacion por DISTANCIA para lo que quedo suelto.
        #
        # Sin esto, un objeto que se desplaza mas que su propio tamano entre dos
        # frames nunca llega a trackearse: al crear el track la velocidad es 0,
        # asi que en su PRIMER movimiento la prediccion no ayuda, el IoU da 0 y
        # nace un track nuevo en cada frame. Un coche a velocidad real cae justo
        # en ese caso, que es el escenario principal de este sistema.
        #
        # Se exige ademas que las cajas sean de tamano parecido, para no
        # emparejar la placa de un coche con una caja cualquiera que pase cerca.
        candidatos: list[tuple[float, int, int]] = []
        for ti, tid in enumerate(ids):
            if ti in tracks_usados:
                continue
            track = self._tracks[tid]
            px1, py1, px2, py2 = track.predict()
            pcx, pcy = (px1 + px2) / 2, (py1 + py2) / 2
            escala = max(1.0, ((px2 - px1) * (py2 - py1)) ** 0.5)

            for di, det in enumerate(detecciones):
                if di in dets_usadas:
                    continue
                dx1, dy1, dx2, dy2 = det.bbox
                dcx, dcy = (dx1 + dx2) / 2, (dy1 + dy2) / 2
                distancia = ((pcx - dcx) ** 2 + (pcy - dcy) ** 2) ** 0.5
                if distancia > self.dist_max_factor * escala:
                    continue

                area_det = max(1.0, (dx2 - dx1) * (dy2 - dy1))
                razon = area_det / max(1.0, (px2 - px1) * (py2 - py1))
                if not (1 / self.razon_area_max <= razon <= self.razon_area_max):
                    continue
                candidatos.append((distancia, ti, di))

        candidatos.sort()  # la distancia mas corta primero
        for _, ti, di in candidatos:
            if ti in tracks_usados or di in dets_usadas:
                continue
            tracks_usados.add(ti)
            dets_usadas.add(di)
            self._actualizar_track(self._tracks[ids[ti]], detecciones[di], ts)

        # 2. Detecciones sin asignar -> tracks nuevos
        for di, det in enumerate(detecciones):
            if di not in dets_usadas:
                self._crear_track(det, ts)

        # 3. Envejecer los tracks no vistos y retirar los que se pasaron
        for ti, tid in enumerate(ids):
            if ti not in tracks_usados:
                track = self._tracks[tid]
                track.age += 1
                if track.age > self.max_age:
                    self._expirados.append(self._tracks.pop(tid))

        return self.tracks_confirmados()

    def recoger_expirados(self) -> list[Track]:
        """Tracks retirados en la ultima llamada a update().

        Solo devuelve los que alcanzaron `min_hits`: un track de 1-2 frames que
        desaparece era ruido, no un objeto real, y emitir un evento por el
        seria un falso positivo.
        """
        confirmados = [t for t in self._expirados if t.hits >= self.min_hits]
        self._expirados = []
        return confirmados

    def cerrar(self) -> list[Track]:
        """Retira TODOS los tracks vivos. Se llama al detener el worker para no
        perder los eventos de objetos que seguian en pantalla."""
        vivos = [t for t in self._tracks.values() if t.hits >= self.min_hits]
        self._tracks.clear()
        return vivos

    def tracks_confirmados(self) -> list[Track]:
        return [t for t in self._tracks.values() if t.hits >= self.min_hits and t.age == 0]

    @property
    def activos(self) -> int:
        return len(self._tracks)

    # -- interno -----------------------------------------------------------

    def _crear_track(self, det: Deteccion, ts: float) -> Track:
        tid = next(self._contador)
        track = Track(
            track_id=tid,
            bbox=det.bbox,
            confidence=det.confidence,
            first_seen=ts,
            last_seen=ts,
        )
        self._tracks[tid] = track
        return track

    def _actualizar_track(self, track: Track, det: Deteccion, ts: float) -> None:
        cx_ant, cy_ant = track.center
        track.bbox = det.bbox
        cx, cy = track.center

        # Velocidad suavizada: 70% la anterior, 30% la nueva. Sin el suavizado,
        # un solo frame con la caja mal ajustada manda la prediccion lejos y
        # rompe el track en el frame siguiente.
        vx_ant, vy_ant = track.velocity
        track.velocity = (
            0.7 * vx_ant + 0.3 * (cx - cx_ant),
            0.7 * vy_ant + 0.3 * (cy - cy_ant),
        )

        track.confidence = max(track.confidence, det.confidence)
        track.last_seen = ts
        track.hits += 1
        track.age = 0
