"""Destinos de eventos.

En la Fase 2 los eventos van a consola y a un archivo JSONL, que es suficiente
para verificar que el detector funciona. En la Fase 3 se agrega el destino HTTP
hacia la API, y el JSONL se queda como respaldo local.

El diseno con buffer en disco no es opcional: si la plataforma web se cae, el
worker NO debe morir ni perder detecciones. Escribe a disco y reintenta luego.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

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
    """Una linea JSON por evento. Formato a prueba de cortes: si el proceso
    muere a media escritura solo se pierde la ultima linea, no el archivo."""

    def __init__(self, ruta: Path) -> None:
        self.ruta = ruta
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.ruta.open("a", encoding="utf-8")

    def enviar(self, evento: DetectionEvent) -> None:
        datos = evento.model_dump(mode="json")
        # El embedding facial no se escribe en claro a disco: son datos
        # biometricos sensibles y este archivo es de depuracion.
        datos.pop("embedding", None)
        datos.pop("snapshot_b64", None)
        self._f.write(json.dumps(datos, ensure_ascii=False, default=str) + "\n")
        self._f.flush()

    def cerrar(self) -> None:
        self._f.close()


class MultiSink(Sink):
    """Manda el evento a varios destinos. Que uno falle no detiene a los demas."""

    def __init__(self, *sinks: Sink) -> None:
        self.sinks = list(sinks)

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
    """Envia eventos a la API, con respaldo en disco si no responde.

    El worker NO debe morir ni perder detecciones porque la plataforma web este
    caida: si falla el envio, el evento se escribe en `spool/` y se reintenta
    en el siguiente envio exitoso. Un reinicio del servidor web no debe costar
    ni una sola placa.
    """

    def __init__(self, base_url: str, token: str, spool_dir: Path,
                 timeout: float = 5.0) -> None:
        import httpx

        self.url = base_url.rstrip("/") + "/api/events"
        self.spool_dir = spool_dir
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self._cliente = httpx.Client(
            timeout=timeout,
            headers={"X-API-Token": token, "Content-Type": "application/json"},
        )
        self._fallos = 0

    def enviar(self, evento: DetectionEvent) -> None:
        lote = {
            "camera_id": evento.camera_id,
            "sent_at": datetime.now().astimezone().isoformat(),
            "events": [evento.model_dump(mode="json")],
        }
        if self._publicar(lote):
            self._reintentar_pendientes()
        else:
            self._encolar(lote)

    def _publicar(self, lote: dict) -> bool:
        try:
            r = self._cliente.post(self.url, json=lote)
            if r.status_code == 401:
                # Credencial mal configurada: reintentar no lo va a arreglar y
                # llenaria el spool de basura. Se avisa fuerte y se descarta.
                log.error("La API rechazo el token de ingesta (401). "
                          "Revisa API_TOKEN en el .env del worker.")
                return True
            r.raise_for_status()
            respuesta = r.json()
            for m in respuesta.get("matches", []):
                if m.get("severity") != "info":
                    log.warning("  >> %s: %s", m["severity"].upper(), m.get("reason"))
            self._fallos = 0
            return True
        except Exception as e:  # noqa: BLE001
            self._fallos += 1
            if self._fallos in (1, 10) or self._fallos % 50 == 0:
                log.error("No se pudo enviar a la API (%d fallos): %s", self._fallos, e)
            return False

    def _encolar(self, lote: dict) -> None:
        marca = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        archivo = self.spool_dir / f"{marca}.json"
        archivo.write_text(json.dumps(lote, ensure_ascii=False, default=str),
                           encoding="utf-8")

    def _reintentar_pendientes(self) -> None:
        pendientes = sorted(self.spool_dir.glob("*.json"))
        if not pendientes:
            return
        log.info("Reenviando %d eventos pendientes...", len(pendientes))
        for archivo in pendientes[:100]:  # por tandas, para no bloquear la captura
            try:
                lote = json.loads(archivo.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - archivo corrupto: no reintentar eternamente
                archivo.unlink(missing_ok=True)
                continue
            if self._publicar(lote):
                archivo.unlink(missing_ok=True)
            else:
                break  # sigue caida: no gastar tiempo en el resto

    def cerrar(self) -> None:
        self._cliente.close()


def crear_sink(cfg) -> Sink:
    """Destino por defecto del worker."""
    marca = datetime.now().strftime("%Y%m%d")
    sinks: list[Sink] = [
        ConsoleSink(),
        JsonlSink(cfg.offline_dir.parent / f"eventos-{marca}.jsonl"),
    ]
    if cfg.api_url and cfg.api_token:
        sinks.append(HttpSink(cfg.api_url, cfg.api_token, cfg.offline_dir))
        log.info("Enviando eventos a %s", cfg.api_url)
    else:
        log.warning("Sin API_TOKEN en el .env: los eventos NO se envian a la "
                    "plataforma web, solo a consola y JSONL.")
    return MultiSink(*sinks)
