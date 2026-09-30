"""Destinos de eventos.

Los eventos van a consola, a un archivo JSONL diario (respaldo local) y, si hay
API_TOKEN, a la plataforma web por HTTP.

El diseno con buffer en disco no es opcional: si la plataforma web se cae, el
worker NO debe morir ni perder detecciones. Escribe a disco y reintenta luego.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from shared.events import DetectionEvent

log = logging.getLogger(__name__)


class Sink(ABC):
    @abstractmethod
    def enviar(self, evento: DetectionEvent) -> None: ...

    def cerrar(self) -> None: ...


class ConsoleSink(Sink):
    """Imprime el evento legible. Para desarrollo."""

    def enviar(self, evento: DetectionEvent) -> None:
        hora = evento.ts.astimezone().strftime("%H:%M:%S")
        print(
            f"  [{hora}] {evento.type.value.upper():6} {evento.value:12} "
            f"conf={evento.confidence:.2f}  track={evento.track_id}  "
            f"frames={evento.observations}"
        )


class JsonlSink(Sink):
    """Una linea JSON por evento, un archivo por dia.

    Formato a prueba de cortes: si el proceso muere a media escritura solo se
    pierde la ultima linea, no el archivo. El nombre se decide en cada
    escritura y no al arrancar: un worker que corre semanas escribia todo en
    el archivo del dia en que se inicio.
    """

    def __init__(self, carpeta: Path, prefijo: str = "eventos") -> None:
        self.carpeta = carpeta
        self.prefijo = prefijo
        self.carpeta.mkdir(parents=True, exist_ok=True)
        self._fecha: Optional[str] = None
        self._f = None

    def _archivo(self):
        hoy = datetime.now().strftime("%Y%m%d")
        if hoy != self._fecha:
            if self._f is not None:
                self._f.close()
            self._f = (self.carpeta / f"{self.prefijo}-{hoy}.jsonl").open("a", encoding="utf-8")
            self._fecha = hoy
        return self._f

    def enviar(self, evento: DetectionEvent) -> None:
        datos = evento.model_dump(mode="json")
        # El embedding facial no se escribe en claro a disco: son datos
        # biometricos sensibles y este archivo es de depuracion.
        datos.pop("embedding", None)
        datos.pop("snapshot_b64", None)
        f = self._archivo()
        f.write(json.dumps(datos, ensure_ascii=False, default=str) + "\n")
        f.flush()

    def cerrar(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None


class MultiSink(Sink):
    """Manda el evento a varios destinos. Que uno falle no detiene a los demas."""

    def __init__(self, *sinks: Sink) -> None:
        self.sinks = list(sinks)

    @property
    def http(self) -> Optional["HttpSink"]:
        """El destino que habla con la API, si lo hay."""
        return next((s for s in self.sinks if isinstance(s, HttpSink)), None)

    def enviar(self, evento: DetectionEvent) -> None:
        for s in self.sinks:
            try:
                s.enviar(evento)
            except Exception as e:  # noqa: BLE001
                log.error("Fallo el destino %s: %s", type(s).__name__, e)

    def cerrar(self) -> None:
        for s in self.sinks:
            try:
                s.cerrar()
            except Exception:  # noqa: BLE001
                pass


class HttpSink(Sink):
    """Envia eventos a la API en un hilo aparte, por lotes, con respaldo en disco.

    `enviar()` solo deja el evento en una cola y vuelve de inmediato. Antes el
    POST se hacia dentro del bucle de deteccion: con la API caida cada evento
    esperaba el timeout completo (5 s) y el worker dejaba de procesar video
    justo cuando mas eventos se estaban generando.

    Si un envio falla, el lote se escribe en `spool/` y se reintenta con
    espera creciente, tambien cuando no llegan eventos nuevos. Un reinicio del
    servidor web no cuesta ni una sola deteccion.
    """

    MAX_POR_LOTE = 20
    MAX_BYTES_LOTE = 8 * 1024 * 1024   # con fotos en base64, no mas de ~8 MB por POST
    ESPERA_MAX = 30.0

    def __init__(self, base_url: str, token: str, spool_dir: Path, *,
                 camera_id: str, adjuntar_fotos: bool = False,
                 base_fotos: Optional[Path] = None, timeout: float = 10.0) -> None:
        import httpx

        self.url = base_url.rstrip("/") + "/api/events"
        self.camera_id = camera_id
        self.spool_dir = spool_dir
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.rechazados_dir = spool_dir / "rechazados"
        self.adjuntar_fotos = adjuntar_fotos
        self.base_fotos = base_fotos
        self._cliente = httpx.Client(
            timeout=timeout,
            headers={"X-API-Token": token, "Content-Type": "application/json"},
        )

        self._cola: queue.Queue[DetectionEvent] = queue.Queue(maxsize=5000)
        self._parar = threading.Event()
        self._fallos = 0
        self._espera = 1.0
        self._proximo_intento = 0.0
        self.enviados = 0
        self.al_responder = None
        """Funcion opcional (lote, respuesta) que se llama tras cada envio
        aceptado. La usa el grabador de clips para saber que eventos se
        volvieron alerta: esa decision la toma la API, no el borde."""
        self.en_spool = len(list(self.spool_dir.glob("*.json")))

        self._hilo = threading.Thread(target=self._bucle, name="envio-eventos", daemon=True)
        self._hilo.start()

    # -- interfaz ----------------------------------------------------------

    def enviar(self, evento: DetectionEvent) -> None:
        try:
            self._cola.put_nowait(evento)
        except queue.Full:
            # La API lleva mucho sin responder y la cola en memoria se lleno:
            # directo a disco. Nunca se descarta un evento.
            self._encolar(self._lote([self._con_foto(evento)]))

    def cerrar(self) -> None:
        self._parar.set()
        self._hilo.join(timeout=15.0)
        # Lo que no se alcanzo a mandar queda en disco para el siguiente arranque.
        pendientes = []
        while True:
            try:
                pendientes.append(self._con_foto(self._cola.get_nowait()))
            except queue.Empty:
                break
        if pendientes:
            self._encolar(self._lote(pendientes))
        self._cliente.close()

    @property
    def stats(self) -> dict:
        return {"enviados": self.enviados, "en_cola": self._cola.qsize(),
                "en_spool": self.en_spool, "fallos_seguidos": self._fallos}

    # -- hilo de envio -----------------------------------------------------

    def _bucle(self) -> None:
        while not self._parar.is_set() or not self._cola.empty():
            eventos = self._tomar_lote()

            if time.monotonic() < self._proximo_intento:
                # API caida y todavia en espera: lo nuevo se guarda sin
                # intentar la red, para no bloquear la cola con timeouts.
                if eventos:
                    self._encolar(self._lote(eventos))
                if self._parar.is_set():
                    break
                continue

            if eventos:
                lote = self._lote(eventos)
                if not self._publicar(lote):
                    self._encolar(lote)
                    continue
            if self.en_spool:
                self._reintentar_pendientes()

    def _tomar_lote(self) -> list[DetectionEvent]:
        try:
            primero = self._cola.get(timeout=1.0)
        except queue.Empty:
            return []
        eventos = [self._con_foto(primero)]
        tamano = len(eventos[0].snapshot_b64 or "")
        # Espera breve para juntar los eventos que salen casi a la vez (varios
        # tracks que cierran en el mismo frame) en una sola peticion.
        limite = time.monotonic() + 0.25
        while len(eventos) < self.MAX_POR_LOTE and tamano < self.MAX_BYTES_LOTE:
            restante = limite - time.monotonic()
            if restante <= 0:
                break
            try:
                ev = self._con_foto(self._cola.get(timeout=restante))
            except queue.Empty:
                break
            eventos.append(ev)
            tamano += len(ev.snapshot_b64 or "")
        return eventos

    def _con_foto(self, evento: DetectionEvent) -> DetectionEvent:
        """Adjunta la captura en base64 cuando la API corre en otra maquina y
        no puede leerla de este disco."""
        if (not self.adjuntar_fotos or evento.snapshot_b64 or not evento.snapshot_path
                or self.base_fotos is None):
            return evento
        try:
            datos = (self.base_fotos / evento.snapshot_path).read_bytes()
            evento.snapshot_b64 = base64.b64encode(datos).decode("ascii")
        except OSError as e:
            log.debug("No se pudo adjuntar %s: %s", evento.snapshot_path, e)
        return evento

    def _lote(self, eventos: list[DetectionEvent]) -> dict:
        return {
            "camera_id": self.camera_id,
            "sent_at": datetime.now().astimezone().isoformat(),
            "events": [e.model_dump(mode="json") for e in eventos],
        }

    def _publicar(self, lote: dict) -> bool:
        """True si el lote ya no necesita reintento (aceptado o descartado)."""
        try:
            r = self._cliente.post(self.url, json=lote)
        except Exception as e:  # noqa: BLE001
            self._registrar_fallo(f"{type(e).__name__}: {e}")
            return False

        if r.status_code == 401:
            # Token mal configurado. Los eventos se conservan en el spool: en
            # cuanto se corrija API_TOKEN y se reinicie, llegan todos.
            self._registrar_fallo("la API rechazo el token de ingesta (401); "
                                  "revisa API_TOKEN en el .env del worker")
            return False
        if 400 <= r.status_code < 500:
            # Un lote que la API rechaza por formato nunca va a entrar. Se
            # aparta para revisarlo en vez de bloquear la cola para siempre.
            self._apartar(lote, f"{r.status_code}: {r.text[:300]}")
            return True
        if r.status_code >= 500:
            self._registrar_fallo(f"HTTP {r.status_code}")
            return False

        try:
            respuesta = r.json()
        except ValueError:
            respuesta = {}
        for m in respuesta.get("matches", []):
            if m.get("severity") != "info":
                log.warning("  >> %s: %s", m["severity"].upper(), m.get("reason"))

        # La API acepta el lote aunque traiga eventos mal formados, y dice
        # cuales rechazo. Esos se apartan para revisarlos: reintentarlos no
        # sirve (van a fallar igual) y perderlos en silencio tampoco.
        rechazados = respuesta.get("rejected") or []
        if rechazados:
            ids = {r.get("event_id") for r in rechazados}
            malos = [e for e in lote.get("events", []) if e.get("event_id") in ids]
            motivo = "; ".join(str(r.get("motivo")) for r in rechazados[:3])
            self._apartar({**lote, "events": malos or rechazados}, f"rechazados: {motivo}")
        if self.al_responder is not None:
            try:
                self.al_responder(lote, respuesta)
            except Exception as e:  # noqa: BLE001 - un observador no tumba el envio
                log.debug("El observador de respuestas fallo: %s", e)

        if self._fallos:
            log.info("API disponible de nuevo tras %d intentos fallidos", self._fallos)
        self._fallos = 0
        self._espera = 1.0
        self._proximo_intento = 0.0
        self.enviados += len(lote.get("events", []))
        return True

    def _registrar_fallo(self, motivo: str) -> None:
        self._fallos += 1
        if self._fallos in (1, 10) or self._fallos % 50 == 0:
            log.error("No se pudo enviar a la API (%d fallos seguidos): %s",
                      self._fallos, motivo)
        self._proximo_intento = time.monotonic() + self._espera
        self._espera = min(self._espera * 2, self.ESPERA_MAX)

    def _encolar(self, lote: dict) -> None:
        marca = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        archivo = self.spool_dir / f"{marca}.json"
        try:
            archivo.write_text(json.dumps(lote, ensure_ascii=False, default=str),
                               encoding="utf-8")
            self.en_spool += 1
        except OSError as e:
            log.error("No se pudo escribir el spool (%s): se pierde un lote de %d eventos",
                      e, len(lote.get("events", [])))

    def _apartar(self, lote: dict, motivo: str) -> None:
        self.rechazados_dir.mkdir(parents=True, exist_ok=True)
        marca = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        lote = {**lote, "_motivo_rechazo": motivo}
        (self.rechazados_dir / f"{marca}.json").write_text(
            json.dumps(lote, ensure_ascii=False, default=str), encoding="utf-8")
        log.error("La API rechazo un lote de %d eventos (%s). Se aparto en %s",
                  len(lote.get("events", [])), motivo, self.rechazados_dir)

    def _reintentar_pendientes(self) -> None:
        pendientes = sorted(self.spool_dir.glob("*.json"))
        self.en_spool = len(pendientes)
        if not pendientes:
            return
        log.info("Reenviando %d lotes pendientes...", len(pendientes))
        # Por tandas: entre tanda y tanda el hilo vuelve a atender eventos nuevos.
        for archivo in pendientes[:50]:
            if self._parar.is_set() and not self._cola.empty():
                break
            try:
                lote = json.loads(archivo.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - archivo corrupto: no reintentar eternamente
                archivo.unlink(missing_ok=True)
                self.en_spool -= 1
                continue
            if not self._publicar(lote):
                break  # sigue caida: no gastar tiempo en el resto
            archivo.unlink(missing_ok=True)
            self.en_spool -= 1


def _api_es_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", ""}


def adoptar_spool_legado(raiz: Path, destino: Path, camera_id: str) -> int:
    """Mueve a la carpeta de esta camara los lotes pendientes que versiones
    anteriores dejaban sueltos en data/spool/. Solo los de ESTA camara: con
    varias camaras en un proceso, cada una reenvia lo suyo."""
    movidos = 0
    if raiz == destino or not raiz.is_dir():
        return 0
    for archivo in sorted(raiz.glob("*.json")):
        try:
            if json.loads(archivo.read_text(encoding="utf-8")).get("camera_id") != camera_id:
                continue
            destino.mkdir(parents=True, exist_ok=True)
            archivo.replace(destino / archivo.name)
            movidos += 1
        except (OSError, ValueError):
            continue
    if movidos:
        log.info("%d lotes pendientes de una version anterior pasan a %s", movidos, destino)
    return movidos


def crear_sink(cfg) -> Sink:
    """Destino por defecto del worker.

    Todo lo que se escribe a disco va por camara (spool y JSONL): con varias
    camaras en un proceso, dos hilos no deben escribir el mismo archivo ni
    reenviar cada uno los pendientes del otro.
    """
    from edge.config import BASE_DIR

    sinks: list[Sink] = [
        ConsoleSink(),
        JsonlSink(cfg.offline_dir.parent, prefijo=f"eventos-{cfg.camera_id}"),
    ]
    if cfg.api_url and cfg.api_token:
        modo = str(cfg.send_snapshot_b64).lower()
        adjuntar = (not _api_es_local(cfg.api_url)) if modo == "auto" else modo == "true"
        spool = cfg.offline_dir / cfg.camera_id
        adoptar_spool_legado(cfg.offline_dir, spool, cfg.camera_id)
        sinks.append(HttpSink(cfg.api_url, cfg.api_token, spool,
                              camera_id=cfg.camera_id, adjuntar_fotos=adjuntar,
                              base_fotos=BASE_DIR))
        log.info("Enviando eventos a %s%s", cfg.api_url,
                 " (con fotos adjuntas)" if adjuntar else "")
    else:
        log.warning("Sin API_TOKEN en el .env: los eventos NO se envian a la "
                    "plataforma web, solo a consola y JSONL.")
    return MultiSink(*sinks)
