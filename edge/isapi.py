"""Eventos de la propia camara Hikvision (ISAPI alertStream).

La camara sabe cosas que el video no dice: que alguien le tapo el lente
(sabotaje), que perdio la senal de video, o que su propia analitica vio a
alguien cruzar una linea. Esos avisos salen por un flujo HTTP que la camara
mantiene abierto:

    GET http://<camara>/ISAPI/Event/notification/alertStream   (Digest)

Es un multipart sin fin de bloques XML <EventNotificationAlert>. Aqui se lee
en un hilo aparte, se traduce a eventos de tipo "camera" y el worker los manda
a la API como cualquier otro evento (la severidad la decide la API, ver
EVENTOS_CAMARA en api/matching.py).

Detalles de Hikvision que importan:
  - Cada ~10 s manda videoloss con estado "inactive": es su latido, no un
    evento. Solo cuentan los "active".
  - Mientras la condicion dura, repite el aviso cada segundo: se agrupan los
    de un mismo tipo en ENFRIAMIENTO_S.
  - Un NVR manda los eventos de TODOS sus canales: se filtra por el canal de
    la camara (el de la URL RTSP: Channels/102 -> canal 1).

Con ISAPI=auto se intenta solo si SOURCE es rtsp:// con usuario; si la camara
no tiene ese servicio (404, otra marca) se deja de intentar sin ruido.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional
from urllib.parse import unquote, urlparse

import cv2

from edge.config import BASE_DIR
from shared.events import DetectionEvent, EventType

log = logging.getLogger(__name__)

# tipo de Hikvision (en minusculas) -> valor del evento de camara
MAPA = {
    "shelteralarm": "sabotaje",          # "video tampering": lente tapado
    "tamperdetection": "sabotaje",
    "scenechangedetection": "sabotaje",  # la movieron de lugar
    "defocus": "sabotaje",               # la desenfocaron
    "videoloss": "perdida_video",
    "linedetection": "deteccion_linea",
    "fielddetection": "intrusion_camara",
    "regionentrance": "intrusion_camara",
    "vmd": "movimiento_camara",
}
ENFRIAMIENTO_S = 30.0
FIN_BLOQUE = b"</EventNotificationAlert>"
MAX_BUFFER = 256 * 1024


def _campo(xml: str, nombre: str) -> Optional[str]:
    m = re.search(rf"<{nombre}>\s*([^<]*?)\s*</{nombre}>", xml)
    return m.group(1) if m else None


def parsear_alerta(xml: str) -> dict:
    """Los campos que importan de un <EventNotificationAlert>. Con
    expresiones regulares y no con un parser XML: el contenido viene de un
    dispositivo de la red y no hace falta mas que leer cuatro etiquetas."""
    canal = _campo(xml, "channelID") or _campo(xml, "dynChannelID")
    return {
        "tipo": (_campo(xml, "eventType") or "").strip().lower(),
        "estado": (_campo(xml, "eventState") or "").strip().lower(),
        "canal": int(canal) if canal and canal.isdigit() else None,
        "descripcion": _campo(xml, "eventDescription") or "",
        "fecha": _campo(xml, "dateTime"),
    }


def canal_de_fuente(source: str) -> Optional[int]:
    """rtsp://.../Streaming/Channels/102 -> 1 (los dos ultimos digitos son
    el sub-flujo). None si la URL no lo dice."""
    m = re.search(r"/channels/(\d+)", source, re.IGNORECASE)
    if not m or len(m.group(1)) < 3:
        return None
    return int(m.group(1)[:-2])


class EventosCamara:
    """Complemento del worker: produce eventos 'camera' desde el alertStream."""

    def __init__(self, cfg, *, host: str, usuario: str, clave: str, canal: Optional[int],
                 permitidos: Iterable[str], abrir: Optional[Callable] = None,
                 iniciar: bool = True, reloj: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        esquema = "https" if getattr(cfg, "isapi_https", False) else "http"
        puerto = int(getattr(cfg, "isapi_puerto", 80) or 80)
        defecto = 443 if esquema == "https" else 80
        self.url = f"{esquema}://{host}{'' if puerto == defecto else f':{puerto}'}" \
                   "/ISAPI/Event/notification/alertStream"
        self.usuario, self.clave, self.canal = usuario, clave, canal
        self.permitidos = {p.strip() for p in permitidos if p.strip()}
        self._abrir = abrir or self._abrir_http
        self._reloj = reloj
        self._buffer = b""
        self._pendientes: list[DetectionEvent] = []
        self._ultimo: dict[str, float] = {}
        self._candado = threading.Lock()
        self._parar = threading.Event()
        self._frame = None
        self.conectado = False
        self.recibidos = 0
        self.emitidos = 0
        self.ultimo_error: Optional[str] = None
        self._hilo = None
        if iniciar:
            self._hilo = threading.Thread(target=self._bucle, name=f"isapi-{cfg.camera_id}", daemon=True)
            self._hilo.start()

    # -- interfaz de complemento -------------------------------------------

    def al_frame(self, frame) -> None:
        # Solo la referencia: si llega un sabotaje, la evidencia es el
        # ultimo cuadro (el lente tapado, la imagen negra).
        self._frame = frame.frame

    def eventos(self) -> list[DetectionEvent]:
        with self._candado:
            pendientes, self._pendientes = self._pendientes, []
        return pendientes

    def estado(self) -> dict:
        return {"isapi": {"conectado": self.conectado, "avisos": self.recibidos,
                          "eventos": self.emitidos, "ultimo_error": self.ultimo_error}}

    def cerrar(self) -> None:
        self._parar.set()
        if self._hilo is not None:
            self._hilo.join(timeout=3)

    # -- lectura -----------------------------------------------------------

    def alimentar(self, datos: bytes) -> int:
        """Agrega bytes del flujo; procesa los bloques completos. Devuelve
        cuantos eventos nuevos genero."""
        self._buffer += datos
        nuevos = 0
        while True:
            fin = self._buffer.find(FIN_BLOQUE)
            if fin < 0:
                break
            inicio = self._buffer.rfind(b"<EventNotificationAlert", 0, fin)
            bloque = self._buffer[inicio if inicio >= 0 else 0:fin + len(FIN_BLOQUE)]
            self._buffer = self._buffer[fin + len(FIN_BLOQUE):]
            if self._procesar(parsear_alerta(bloque.decode("utf-8", "replace"))):
                nuevos += 1
        if len(self._buffer) > MAX_BUFFER:
            # Algo que no es un alertStream: no se deja crecer la memoria.
            self._buffer = self._buffer[-4096:]
        return nuevos

    def _procesar(self, alerta: dict) -> bool:
        self.recibidos += 1
        if alerta["estado"] != "active":
            return False                  # latido de Hikvision o fin de la condicion
        valor = MAPA.get(alerta["tipo"])
        if valor is None or valor not in self.permitidos:
            return False
        if self.canal is not None and alerta["canal"] is not None and alerta["canal"] != self.canal:
            return False
        ahora = self._reloj()
        if ahora - self._ultimo.get(valor, -1e9) < ENFRIAMIENTO_S:
            return False
        self._ultimo[valor] = ahora
        evento = DetectionEvent(
            camera_id=self.cfg.camera_id, type=EventType.CAMERA, value=valor, confidence=1.0,
            ts=datetime.fromtimestamp(ahora, tz=timezone.utc),
            meta={"origen": "isapi", "tipo_hikvision": alerta["tipo"], "canal": alerta["canal"],
                  "detalle": alerta["descripcion"][:120] or None})
        self._evidencia(evento)
        with self._candado:
            self._pendientes.append(evento)
        self.emitidos += 1
        log.warning("[%s] La cámara reporta %s (%s)", self.cfg.camera_id, valor, alerta["tipo"])
        return True

    def _evidencia(self, evento: DetectionEvent) -> None:
        frame = self._frame
        if frame is None:
            return
        try:
            ruta = self.cfg.snapshot_dir / f"{evento.event_id}.jpg"
            if cv2.imwrite(str(ruta), frame):
                try:
                    evento.snapshot_path = ruta.relative_to(BASE_DIR).as_posix()
                except ValueError:
                    evento.snapshot_path = ruta.as_posix()
        except Exception as e:  # noqa: BLE001
            log.debug("Sin evidencia para el evento ISAPI: %s", e)

    def _abrir_http(self):
        """Generador de trozos del alertStream. Lanza PermissionError (401),
        LookupError (404: la camara no tiene el servicio) o errores de red."""
        import httpx

        # Las camaras traen certificado autofirmado: con ISAPI_HTTPS no hay
        # CA contra la cual validarlo. Digest ya protege la contrasena en HTTP.
        with httpx.Client(auth=httpx.DigestAuth(self.usuario, self.clave), verify=False,
                          timeout=httpx.Timeout(10.0, read=90.0)) as cliente:
            with cliente.stream("GET", self.url) as r:
                if r.status_code in (401, 403):
                    raise PermissionError(f"la cámara rechazó las credenciales ({r.status_code})")
                if r.status_code == 404:
                    raise LookupError("la cámara no tiene ISAPI alertStream")
                r.raise_for_status()
                self.conectado = True
                yield from r.iter_bytes()

    def _bucle(self) -> None:
        espera = 5.0
        while not self._parar.is_set():
            try:
                for trozo in self._abrir():
                    if self._parar.is_set():
                        return
                    self.alimentar(trozo)
                    espera = 5.0
                self.ultimo_error = "el flujo se cerró"
            except LookupError as e:
                self.ultimo_error = str(e)
                log.info("[%s] %s: sin eventos de la cámara (ISAPI=false para no intentar)",
                         self.cfg.camera_id, e)
                return
            except PermissionError as e:
                self.ultimo_error = str(e)
                log.error("[%s] ISAPI: %s. Revisa el usuario de SOURCE (necesita permiso de "
                          "notificaciones).", self.cfg.camera_id, e)
                espera = 300.0
            except Exception as e:  # noqa: BLE001 - la camara se reinicia, la red parpadea
                self.ultimo_error = f"{type(e).__name__}: {e}"[:200]
                log.debug("[%s] alertStream cortado: %s", self.cfg.camera_id, e)
            finally:
                self.conectado = False
                self._buffer = b""
            self._parar.wait(espera)
            espera = min(60.0, espera * 2)


def crear_eventos_camara(cfg) -> Optional[EventosCamara]:
    modo = (getattr(cfg, "isapi", "auto") or "auto").lower()
    if modo in ("false", "0", "no", "off"):
        return None
    fuente = str(getattr(cfg, "source", ""))
    partes = urlparse(fuente) if fuente.startswith(("rtsp://", "rtsps://")) else None
    if partes is None or not partes.hostname or not partes.username:
        if modo == "true":
            log.warning("[%s] ISAPI=true pero SOURCE no es rtsp:// con usuario y contraseña",
                        cfg.camera_id)
        return None
    return EventosCamara(cfg, host=partes.hostname, usuario=unquote(partes.username),
                         clave=unquote(partes.password or ""), canal=canal_de_fuente(fuente),
                         permitidos=str(getattr(cfg, "isapi_eventos", "")).split(","))
