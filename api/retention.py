"""Retencion y purga de datos.

EL PRINCIPIO: guardar lo minimo durante el minimo tiempo.

No es solo higiene tecnica. La LFPDPPP obliga a que los datos personales se
conserven solo mientras sean necesarios para la finalidad que justifico
recogerlos. Un sistema de vigilancia que acumula indefinidamente todo lo que ve
es un problema legal, y ademas un blanco: lo que no guardas no te lo pueden
robar.

LAS CAPAS
---------
No es "guardar todo" ni "borrar todo". Cada tipo de dato tiene su plazo, segun
para que sirve:

  Alertas y sus eventos      1 ano    Son evidencia de un incidente.
  Fotos de eventos normales  7 dias   Sirven para verificar un caso reciente.
  Eventos normales (texto)   30 dias  Permiten responder "paso este coche?"
                                      cuando un robo se reporta despues.
  Embeddings faciales        NUNCA    Dato biometrico sensible. Si no coincide
                             (si no   con la lista negra, no hay ninguna razon
                             coincide) para conservarlo.

El caso de los eventos normales de placa es el interesante: se conserva el
TEXTO pero se borra la FOTO a los 7 dias. Para responder "el coche ABC-123 paso
por aqui el martes" basta una linea de 50 bytes; la foto de ese coche no aporta
nada y si es un dato personal de alguien que no hizo nada.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import Session, col, delete, select

from api.config import get_config
from api.models import Alert, Event, FaceEmbedding

log = logging.getLogger(__name__)


class Politica:
    """Plazos de retencion, en dias. Configurables por variable de entorno."""

    def __init__(self) -> None:
        import os

        def dias(nombre: str, default: int) -> int:
            try:
                return max(0, int(os.getenv(nombre, default)))
            except (TypeError, ValueError):
                return default

        self.fotos_eventos = dias("RETENCION_FOTOS_DIAS", 7)
        self.eventos_info = dias("RETENCION_EVENTOS_DIAS", 30)
        self.alertas = dias("RETENCION_ALERTAS_DIAS", 365)
        self.embeddings = dias("RETENCION_EMBEDDINGS_DIAS", 7)
        """Plazo maximo de un embedding que SI coincidio. Los que no coinciden
        no llegan a guardarse (ver guardar_embedding en routers/events.py)."""

    def resumen(self) -> str:
        return (f"fotos {self.fotos_eventos}d | eventos {self.eventos_info}d | "
                f"alertas {self.alertas}d | embeddings {self.embeddings}d")


def _antes_de(dias: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=dias)


def _borrar_archivo(ruta_rel: str | None, base: Path) -> bool:
    if not ruta_rel:
        return False
    try:
        archivo = base / ruta_rel
        # Comprobacion de contencion: una ruta manipulada con ".." podria
        # apuntar fuera del proyecto y esta funcion borra archivos.
        archivo = archivo.resolve()
        if not str(archivo).startswith(str(base.resolve())):
            log.error("Ruta de snapshot fuera del proyecto, no se borra: %s", ruta_rel)
            return False
        if archivo.is_file():
            archivo.unlink()
            return True
    except Exception as e:  # noqa: BLE001
        log.debug("No se pudo borrar %s: %s", ruta_rel, e)
    return False


def purgar(session: Session, politica: Politica | None = None,
           simular: bool = False) -> dict[str, int]:
    """Aplica la politica de retencion. Devuelve cuanto se elimino.

    `simular=True` cuenta sin borrar nada: util para revisar el impacto antes
    de dejarlo corriendo solo.
    """
    politica = politica or Politica()
    cfg = get_config()
    base = cfg.snapshot_dir.parent.parent  # raiz del proyecto
    cuenta = {"fotos": 0, "eventos": 0, "alertas": 0, "embeddings": 0, "huerfanas": 0}

    # 1. Embeddings faciales caducados -------------------------------------
    viejos = session.exec(
        select(FaceEmbedding).where(col(FaceEmbedding.created_at) < _antes_de(politica.embeddings))
    ).all()
    cuenta["embeddings"] = len(viejos)
    if not simular:
        for e in viejos:
            session.delete(e)

    # 2. Fotos de eventos sin coincidencia ---------------------------------
    # Se borra el archivo pero SE CONSERVA la fila: el texto de la placa sigue
    # sirviendo para responder consultas, la foto ya no.
    con_foto = session.exec(
        select(Event).where(
            Event.severity == "info",
            col(Event.snapshot_path).is_not(None),
            col(Event.ts) < _antes_de(politica.fotos_eventos),
        )
    ).all()
    for ev in con_foto:
        if simular or _borrar_archivo(ev.snapshot_path, base):
            cuenta["fotos"] += 1
        if not simular:
            ev.snapshot_path = None

    # 3. Eventos sin coincidencia, ya caducados ----------------------------
    antiguos = session.exec(
        select(Event).where(
            Event.severity == "info",
            col(Event.ts) < _antes_de(politica.eventos_info),
        )
    ).all()
    cuenta["eventos"] = len(antiguos)
    if not simular:
        for ev in antiguos:
            _borrar_archivo(ev.snapshot_path, base)
            session.exec(delete(FaceEmbedding).where(FaceEmbedding.event_id == ev.event_id))
            session.delete(ev)

    # 4. Alertas y sus eventos, ya caducados -------------------------------
    alertas = session.exec(
        select(Alert).where(col(Alert.created_at) < _antes_de(politica.alertas))
    ).all()
    cuenta["alertas"] = len(alertas)
    if not simular:
        for a in alertas:
            _borrar_archivo(a.snapshot_path, base)
            ev = session.exec(select(Event).where(Event.event_id == a.event_id)).first()
            if ev is not None:
                session.exec(delete(FaceEmbedding).where(FaceEmbedding.event_id == ev.event_id))
                session.delete(ev)
            session.delete(a)

    if not simular:
        session.commit()

    # 5. Archivos huerfanos -------------------------------------------------
    # Si un borrado fallo a medias en algun momento, quedan fotos en disco sin
    # fila que las referencie. Nadie las va a mirar nunca y siguen siendo datos
    # personales, asi que se limpian.
    cuenta["huerfanas"] = _limpiar_huerfanas(session, cfg.snapshot_dir, base, simular)
    return cuenta


def _limpiar_huerfanas(session: Session, carpeta: Path, base: Path, simular: bool) -> int:
    if not carpeta.is_dir():
        return 0

    # session.exec() con UNA sola columna devuelve los valores directos, no
    # tuplas de un elemento (a diferencia de seleccionar varias columnas).
    referenciadas = {
        Path(p).name for p in session.exec(
            select(Event.snapshot_path).where(col(Event.snapshot_path).is_not(None))
        ).all() if p
    }
    referenciadas |= {
        Path(p).name for p in session.exec(
            select(Alert.snapshot_path).where(col(Alert.snapshot_path).is_not(None))
        ).all() if p
    }

    # Las capturas de diagnostico de herramientas no son evidencia de ningun
    # evento y no deben borrarse por "huerfanas".
    protegidas = {"prueba_camara.jpg", "rostro_camara.jpg"}

    n = 0
    for archivo in carpeta.glob("*.jpg"):
        if archivo.name in referenciadas or archivo.name in protegidas:
            continue
        n += 1
        if not simular:
            try:
                archivo.unlink()
            except OSError:
                n -= 1
    return n


def formatear(cuenta: dict[str, int], simular: bool) -> str:
    verbo = "se eliminarian" if simular else "eliminados"
    return (f"{verbo}: {cuenta['embeddings']} embeddings, {cuenta['fotos']} fotos, "
            f"{cuenta['eventos']} eventos, {cuenta['alertas']} alertas, "
            f"{cuenta['huerfanas']} archivos huerfanos")
