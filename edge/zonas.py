"""Motor de reglas por zona del worker: intrusion, cruce de linea, merodeo y conteo.

Las zonas se dibujan en el dashboard y el worker las pide a la API (cada
ZONAS_REFRESCO_S, y solo recarga si cambiaron). Se guardan en disco, asi que
si el worker arranca sin API sigue aplicando las ultimas que conocia.

No corre ningun modelo: usa las personas y vehiculos que ya sigue el detector
de movimiento (ver Pista en edge/detectors/base.py). Por cada objeto y cada
zona lleva un pequeno estado:

  intrusion  N frames seguidos dentro (ZONAS_FRAMES_MIN) -> un evento. Se
             rearma si el objeto sale y pasa REARME_S fuera.
  merodeo    tiempo continuo dentro (se toleran huecos de TOLERANCIA_S, p.ej.
             alguien que pasa detras de un poste) >= segundos de la regla.
  linea      el objeto pasa de un lado de la linea al otro, atravesando el
             segmento, con una franja muerta alrededor para que alguien
             parado SOBRE la linea no cuente cruces por el temblor de la caja.
  conteo     igual que linea, pero es estadistica (la API nunca alerta).

El horario se revisa aqui tambien (una regla "de noche" no manda nada de dia)
y otra vez en la API con la hora del evento, que es la que decide.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from edge.config import BASE_DIR
from shared.events import BBox, DetectionEvent, EventType
from shared.zonas import (
    TIPOS_LINEA,
    VALOR_EVENTO,
    en_horario,
    lado,
    punto_en_poligono,
    segmentos_se_cruzan,
    sentido_de_cruce,
    zona_horaria,
)

log = logging.getLogger(__name__)

REARME_S = 30.0          # intrusion: fuera este tiempo y vuelve a contar
TOLERANCIA_S = 5.0       # merodeo: huecos permitidos dentro de la zona
ENFRIAMIENTO_LINEA = 1.5  # linea: segundos entre dos cruces del mismo objeto
OLVIDO_S = 10.0          # estado de un objeto que ya no se ve
COLORES = {"intrusion": (60, 60, 255), "linea": (0, 200, 255), "merodeo": (255, 120, 60),
           "conteo": (120, 220, 120)}


@dataclass
class Zona:
    id: int
    nombre: str
    tipo: str
    puntos: list[tuple[float, float]]
    clases: list[str] = field(default_factory=lambda: ["persona"])
    direccion: str = "ambas"
    segundos: Optional[int] = None
    horario: list[dict] = field(default_factory=list)

    @classmethod
    def desde(cls, d: dict) -> "Zona":
        return cls(id=int(d["id"]), nombre=str(d["nombre"]), tipo=str(d["tipo"]),
                   puntos=[(float(x), float(y)) for x, y in d["puntos"]],
                   clases=list(d.get("clases") or ["persona"]), direccion=d.get("direccion") or "ambas",
                   segundos=d.get("segundos"), horario=list(d.get("horario") or []))


@dataclass
class _Estado:
    visto: float = 0.0
    frames_dentro: int = 0
    alertado: bool = False
    fuera_desde: Optional[float] = None
    dentro_desde: Optional[float] = None
    ultimo_dentro: Optional[float] = None
    lado: int = 0
    punto: Optional[tuple[float, float]] = None
    ultimo_cruce: float = -1e9


class MotorZonas:
    """Complemento del worker (ver Camara.complementos en edge/worker.py)."""

    def __init__(self, cfg, *, frames_min: Optional[int] = None) -> None:
        self.cfg = cfg
        self.frames_min = max(1, int(frames_min or getattr(cfg, "zonas_frames_min", 3)))
        self.zonas: list[Zona] = []
        self.version: Optional[str] = None
        self._estados: dict[tuple[int, int], _Estado] = {}
        self._pendientes: list[DetectionEvent] = []
        self._candado = threading.Lock()
        self._tz = zona_horaria()
        self._forma = (0, 0)
        self.emitidos = 0
        self.cliente: Optional[ClienteZonas] = None

    # -- configuracion -----------------------------------------------------

    def actualizar(self, zonas: list[dict], version: Optional[str] = None) -> None:
        nuevas = []
        for d in zonas:
            try:
                nuevas.append(Zona.desde(d))
            except (KeyError, TypeError, ValueError) as e:
                log.warning("[%s] Zona ignorada por formato invalido: %s", self.cfg.camera_id, e)
        with self._candado:
            ids = {z.id for z in nuevas}
            # Una zona que cambio de forma empieza de cero: el estado viejo
            # (quien estaba dentro) ya no describe la zona nueva.
            previas = {z.id: z for z in self.zonas}
            cambiadas = {z.id for z in nuevas if previas.get(z.id) != z}
            self._estados = {k: v for k, v in self._estados.items() if k[0] in ids and k[0] not in cambiadas}
            self.zonas = nuevas
            self.version = version
        log.info("[%s] Zonas: %s", self.cfg.camera_id,
                 ", ".join(f"{z.nombre} ({z.tipo})" for z in nuevas) or "ninguna")

    # -- interfaz de complemento -------------------------------------------

    def al_frame(self, frame) -> None:
        pass

    def al_pistas(self, frame, pistas: list) -> None:
        alto, ancho = frame.frame.shape[:2]
        self._forma = (alto, ancho)
        ahora = frame.ts
        cuando = datetime.fromtimestamp(ahora, tz=timezone.utc)
        with self._candado:
            zonas = list(self.zonas)
        if not zonas:
            return
        eventos = []
        for zona in zonas:
            armada = en_horario(zona.horario, cuando, self._tz)
            for p in pistas:
                if p.clase not in zona.clases:
                    continue
                estado = self._estados.setdefault((zona.id, p.tid), _Estado())
                estado.visto = ahora
                evento = self._evaluar(zona, p, estado, ahora, ancho, alto, armada)
                if evento is not None:
                    eventos.append((zona, p, evento))
        self._olvidar(ahora)
        for zona, p, evento in eventos:
            if zona.tipo != "conteo":
                self._evidencia(frame.frame, zona, p, evento)
            with self._candado:
                self._pendientes.append(evento)
            self.emitidos += 1
            if zona.tipo != "conteo":
                log.warning("[%s] %s en '%s' (%s #%d)", self.cfg.camera_id, evento.value, zona.nombre,
                            p.clase, p.tid)

    def eventos(self) -> list[DetectionEvent]:
        with self._candado:
            pendientes, self._pendientes = self._pendientes, []
        return pendientes

    def estado(self) -> dict:
        return {"zonas": {"activas": len(self.zonas), "version": self.version, "eventos": self.emitidos}}

    def anotar(self, vista: np.ndarray) -> np.ndarray:
        """Dibuja las zonas en la vista en vivo y en la ventana de depuracion."""
        alto, ancho = vista.shape[:2]
        for z in list(self.zonas):
            color = COLORES.get(z.tipo, (200, 200, 200))
            pts = np.array([(int(x * ancho), int(y * alto)) for x, y in z.puntos], np.int32)
            if z.tipo in TIPOS_LINEA:
                cv2.line(vista, tuple(pts[0]), tuple(pts[1]), color, 2)
                _flecha_entrada(vista, pts[0], pts[1], color)
            else:
                capa = vista.copy()
                cv2.fillPoly(capa, [pts], color)
                cv2.addWeighted(capa, 0.15, vista, 0.85, 0, vista)
                cv2.polylines(vista, [pts], True, color, 2)
            cv2.putText(vista, z.nombre, (int(pts[0][0]) + 4, int(pts[0][1]) - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        return vista

    def cerrar(self) -> None:
        if self.cliente is not None:
            self.cliente.cerrar()

    # -- reglas ------------------------------------------------------------

    def _evaluar(self, zona: Zona, p, e: _Estado, ahora: float, ancho: int, alto: int,
                 armada: bool) -> Optional[DetectionEvent]:
        x1, y1, x2, y2 = p.bbox
        pie = ((x1 + x2) / 2, y2)                        # pixeles
        pie_n = (pie[0] / max(1, ancho), pie[1] / max(1, alto))

        if zona.tipo == "intrusion":
            dentro = punto_en_poligono(pie_n, zona.puntos)
            if dentro:
                e.frames_dentro += 1
                e.fuera_desde = None
                if e.frames_dentro >= self.frames_min and not e.alertado and armada:
                    e.alertado = True
                    return self._evento(zona, p, ahora, observaciones=e.frames_dentro)
            else:
                e.frames_dentro = 0
                if e.alertado:
                    e.fuera_desde = e.fuera_desde or ahora
                    if ahora - e.fuera_desde >= REARME_S:
                        e.alertado, e.fuera_desde = False, None
            return None

        if zona.tipo == "merodeo":
            dentro = punto_en_poligono(pie_n, zona.puntos)
            if dentro:
                if e.dentro_desde is None or (e.ultimo_dentro and ahora - e.ultimo_dentro > TOLERANCIA_S):
                    e.dentro_desde = ahora
                    e.alertado = False
                e.ultimo_dentro = ahora
                permanencia = ahora - e.dentro_desde
                if permanencia >= (zona.segundos or 60) and not e.alertado and armada:
                    e.alertado = True
                    return self._evento(zona, p, ahora, extra={"segundos": round(permanencia)})
            return None

        # linea / conteo, en pixeles para que la franja muerta sea pareja
        a = (zona.puntos[0][0] * ancho, zona.puntos[0][1] * alto)
        b = (zona.puntos[1][0] * ancho, zona.puntos[1][1] * alto)
        largo = max(1e-6, ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5)
        distancia = lado(pie, a, b) / largo
        margen = max(4.0, 0.012 * (ancho ** 2 + alto ** 2) ** 0.5)
        if abs(distancia) < margen:
            return None                                  # sobre la linea: no decide
        signo = 1 if distancia > 0 else -1
        if e.lado == 0:
            e.lado, e.punto = signo, pie
            return None
        if signo == e.lado:
            e.punto = pie
            return None
        antes, e.lado, previo, e.punto = e.lado, signo, e.punto, pie
        if previo is None or not segmentos_se_cruzan(previo, pie, a, b):
            return None                                  # paso por fuera de los extremos
        sentido = sentido_de_cruce(antes, signo)
        if ahora - e.ultimo_cruce < ENFRIAMIENTO_LINEA:
            return None
        e.ultimo_cruce = ahora
        if zona.direccion not in ("ambas", sentido) or not armada:
            return None
        return self._evento(zona, p, ahora, extra={"direccion": sentido})

    def _evento(self, zona: Zona, p, ahora: float, observaciones: int = 1,
                extra: Optional[dict] = None) -> DetectionEvent:
        x1, y1, x2, y2 = (int(v) for v in p.bbox)
        meta = {"zona_id": zona.id, "zona": zona.nombre, "clase": p.clase}
        if p.etiqueta:
            meta["etiqueta"] = p.etiqueta
        meta.update(extra or {})
        return DetectionEvent(
            camera_id=self.cfg.camera_id, type=EventType.ZONE, track_id=p.tid,
            value=VALOR_EVENTO[zona.tipo], confidence=round(min(1.0, max(0.0, p.conf)), 4),
            bbox=BBox(x1=x1, y1=y1, x2=x2, y2=y2), observations=max(1, observaciones),
            ts=datetime.fromtimestamp(ahora, tz=timezone.utc), meta=meta)

    def _evidencia(self, imagen: np.ndarray, zona: Zona, p, evento: DetectionEvent) -> None:
        try:
            vista = self.anotar(imagen.copy())
            x1, y1, x2, y2 = (int(v) for v in p.bbox)
            cv2.rectangle(vista, (x1, y1), (x2, y2), (0, 0, 255), 3)
            carpeta = Path(self.cfg.snapshot_dir)
            ruta = carpeta / f"{evento.event_id}.jpg"
            if cv2.imwrite(str(ruta), vista):
                try:
                    evento.snapshot_path = ruta.relative_to(BASE_DIR).as_posix()
                except ValueError:
                    evento.snapshot_path = ruta.as_posix()
        except Exception as e:  # noqa: BLE001 - sin foto, el evento sale igual
            log.debug("No se pudo guardar la evidencia de zona: %s", e)

    def _olvidar(self, ahora: float) -> None:
        viejos = [k for k, e in self._estados.items() if ahora - e.visto > OLVIDO_S]
        for k in viejos:
            del self._estados[k]


def _flecha_entrada(vista, a, b, color) -> None:
    """Flecha perpendicular al centro de la linea, hacia el lado de "entrada"
    (la derecha de quien camina de A a B), igual que en el editor."""
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    mx, my = (ax + bx) / 2, (ay + by) / 2
    dx, dy = bx - ax, by - ay
    largo = max(1.0, (dx * dx + dy * dy) ** 0.5)
    nx, ny = -dy / largo, dx / largo            # derecha en coordenadas de imagen
    punta = (int(mx + nx * 30), int(my + ny * 30))
    cv2.arrowedLine(vista, (int(mx), int(my)), punta, color, 2, tipLength=0.35)


# --------------------------------------------------------------------------
# Zonas desde la API
# --------------------------------------------------------------------------

class ClienteZonas:
    """Pregunta a la API las zonas de la camara y las pasa al motor."""

    def __init__(self, cfg, motor: MotorZonas, *, obtener: Optional[Callable[[], dict]] = None,
                 intervalo: Optional[float] = None, iniciar: bool = True) -> None:
        self.cfg = cfg
        self.motor = motor
        self.intervalo = float(intervalo or getattr(cfg, "zonas_refresco_s", 15.0))
        self.cache = BASE_DIR / "data" / "zonas" / f"{cfg.camera_id}.json"
        self._obtener = obtener or self._de_api
        self._parar = threading.Event()
        self._candado = threading.Lock()
        self.fallos = 0
        self._cargar_cache()
        self._hilo = threading.Thread(target=self._bucle, name=f"zonas-{cfg.camera_id}", daemon=True)
        if iniciar:
            self._hilo.start()

    def _de_api(self) -> dict:
        import httpx

        r = httpx.get(f"{self.cfg.api_url.rstrip('/')}/api/zonas/borde/{self.cfg.camera_id}",
                      headers={"X-API-Token": self.cfg.api_token}, timeout=10.0)
        r.raise_for_status()
        return r.json()

    def _cargar_cache(self) -> None:
        try:
            datos = json.loads(self.cache.read_text(encoding="utf-8"))
            self.motor.actualizar(datos.get("zonas", []), datos.get("version"))
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] Cache de zonas ilegible: %s", self.cfg.camera_id, e)

    def refrescar(self) -> bool:
        """Una consulta. True si cambiaron las zonas."""
        with self._candado:
            try:
                datos = self._obtener()
                self.fallos = 0
            except Exception as e:  # noqa: BLE001
                self.fallos += 1
                if self.fallos in (1, 20) or self.fallos % 200 == 0:
                    log.warning("[%s] No se pudieron consultar las zonas (%d): %s",
                                self.cfg.camera_id, self.fallos, e)
                return False
            if datos.get("version") == self.motor.version:
                return False
            self.motor.actualizar(datos.get("zonas", []), datos.get("version"))
            self._guardar_cache(datos)
            return True

    def _guardar_cache(self, datos: dict) -> None:
        # Archivo temporal + reemplazo: un corte de luz a media escritura no
        # deja una cache a medias que el siguiente arranque no pueda leer.
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            temporal = self.cache.with_suffix(".tmp")
            temporal.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
            os.replace(temporal, self.cache)
        except OSError as e:
            log.debug("No se pudo guardar la cache de zonas: %s", e)

    def _bucle(self) -> None:
        while not self._parar.is_set():
            self.refrescar()
            self._parar.wait(self.intervalo)

    def cerrar(self) -> None:
        self._parar.set()
        if self._hilo.is_alive():
            self._hilo.join(timeout=3)


def crear_motor_zonas(cfg) -> Optional[MotorZonas]:
    if not getattr(cfg, "enable_zonas", False):
        return None
    if not getattr(cfg, "enable_motion", False):
        log.warning("[%s] ENABLE_ZONAS requiere ENABLE_MOTION=true (usa sus personas y vehiculos): "
                    "las reglas de zona quedan apagadas", cfg.camera_id)
        return None
    motor = MotorZonas(cfg)
    if cfg.api_url and cfg.api_token:
        motor.cliente = ClienteZonas(cfg, motor)
    return motor
