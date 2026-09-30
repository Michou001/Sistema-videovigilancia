"""Busqueda en lenguaje natural sobre las capturas: "camioneta blanca",
"persona con mochila roja", "moto en la banqueta".

COMO FUNCIONA
  Un modelo de vision y lenguaje (SigLIP multilingue, via open_clip) convierte
  cada captura en un vector, y la frase del operador en otro vector del mismo
  espacio. Buscar es ordenar las capturas por parecido con la frase. El modelo
  entiende espanol: no hace falta escribir en ingles.

  - Un indexador en segundo plano calcula el vector de cada evento con foto
    (en lotes, en un hilo, sin frenar la ingesta).
  - Los vectores viven en la base (tabla semantic_embeddings) y en una matriz
    en memoria para que buscar entre decenas de miles tarde milisegundos.
  - Solo hay vector mientras haya foto: la retencion borra los dos juntos.

ACTIVARLO (opcional, pesado): SEMANTIC_SEARCH=true en el .env de la API y
    pip install open-clip-torch transformers sentencepiece
La primera vez descarga el modelo (~1.5 GB). Con GPU, la API la usa.

PRIVACIDAD: buscar "hombre con playera roja" es buscar personas por su
apariencia. Cada busqueda queda en la bitacora con su texto.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

import numpy as np
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, delete, select

from api.config import BASE_DIR
from api.models import Event, SemanticEmbedding

log = logging.getLogger(__name__)


def activada() -> bool:
    return (os.getenv("SEMANTIC_SEARCH", "false") or "").lower() in {"1", "true", "si", "yes"}


class Codificador(Protocol):
    nombre: str
    dim: int

    def imagenes(self, imagenes: list[np.ndarray]) -> np.ndarray: ...
    def texto(self, frase: str) -> np.ndarray: ...


class CodificadorOpenClip:
    """SigLIP / CLIP de open_clip. Vectores normalizados (norma 1)."""

    def __init__(self, modelo: Optional[str] = None, pesos: Optional[str] = None,
                 device: Optional[str] = None) -> None:
        import open_clip
        import torch

        self.modelo_nombre = modelo or os.getenv("SEMANTIC_MODEL", "ViT-B-16-SigLIP-i18n-256")
        self.pesos = pesos or os.getenv("SEMANTIC_PRETRAINED", "webli")
        pedido = (device or os.getenv("SEMANTIC_DEVICE", "auto")).lower()
        self.device = ("cuda" if torch.cuda.is_available() else "cpu") if pedido == "auto" else pedido
        self.nombre = f"{self.modelo_nombre}/{self.pesos}"
        t0 = time.perf_counter()
        self._modelo, _, self._prep = open_clip.create_model_and_transforms(
            self.modelo_nombre, pretrained=self.pesos, device=self.device)
        self._modelo.eval()
        self._tok = open_clip.get_tokenizer(self.modelo_nombre)
        self._torch = torch
        with torch.no_grad():
            self.dim = int(self._modelo.encode_text(self._tok(["prueba"]).to(self.device)).shape[-1])
        log.info("Busqueda semantica: %s listo en %.0f s (%s, %d dimensiones)", self.nombre,
                 time.perf_counter() - t0, self.device, self.dim)

    def imagenes(self, imagenes: list[np.ndarray]) -> np.ndarray:
        from PIL import Image

        torch = self._torch
        lote = torch.stack([self._prep(Image.fromarray(img[:, :, ::-1])) for img in imagenes]).to(self.device)
        with torch.no_grad():
            v = self._modelo.encode_image(lote).float()
            v = v / v.norm(dim=-1, keepdim=True)
        return v.cpu().numpy()

    def texto(self, frase: str) -> np.ndarray:
        torch = self._torch
        with torch.no_grad():
            v = self._modelo.encode_text(self._tok([frase]).to(self.device)).float()
            v = v / v.norm(dim=-1, keepdim=True)
        return v.cpu().numpy()[0]


# --------------------------------------------------------------------------
# Indice
# --------------------------------------------------------------------------

@dataclass
class Resultado:
    event_id: str
    similitud: float


class IndiceSemantico:
    LOTE = 16

    def __init__(self, engine, fabrica=None, min_similitud: Optional[float] = None) -> None:
        self.engine = engine
        self._fabrica = fabrica or CodificadorOpenClip
        self.codificador: Optional[Codificador] = None
        self.estado = "desactivado"
        self.error: Optional[str] = None
        self.min_similitud = float(min_similitud if min_similitud is not None
                                   else os.getenv("SEMANTIC_MIN_SIMILITUD", "0.04"))
        self._candado = threading.Lock()
        self._ids = np.zeros(0, dtype=np.int64)          # id de fila en la tabla
        self._eventos: list[str] = []
        self._matriz = np.zeros((0, 0), dtype=np.float32)
        self._ultimo_id = 0
        self._reconstruida = 0.0
        self.indexados_sesion = 0

    # -- modelo ------------------------------------------------------------

    def cargar(self) -> None:
        """Carga el modelo (lento: se llama en un hilo al arrancar)."""
        self.estado = "cargando"
        try:
            self.codificador = self._fabrica()
            self.estado = "listo"
        except Exception as e:  # noqa: BLE001
            self.estado = "error"
            self.error = f"{type(e).__name__}: {e}"[:300]
            log.error("No se pudo cargar el modelo de busqueda semantica: %s", self.error)

    def cargar_en_segundo_plano(self) -> None:
        threading.Thread(target=self.cargar, name="semantica-modelo", daemon=True).start()

    @property
    def listo(self) -> bool:
        return self.estado == "listo" and self.codificador is not None

    # -- indexado ----------------------------------------------------------

    def _pendientes(self, s: Session, limite: int) -> list[Event]:
        ya = select(SemanticEmbedding.event_id)
        return list(s.exec(
            select(Event).where(col(Event.snapshot_path).is_not(None),
                                col(Event.event_id).not_in(ya))
            .order_by(col(Event.ts).desc()).limit(limite)
        ).all())

    def pendientes(self) -> int:
        from sqlmodel import func

        with Session(self.engine) as s:
            ya = select(SemanticEmbedding.event_id)
            return s.exec(select(func.count()).select_from(Event).where(
                col(Event.snapshot_path).is_not(None), col(Event.event_id).not_in(ya))).one()

    def indexar(self, maximo: int = 64) -> int:
        """Calcula los vectores de hasta `maximo` eventos con foto y sin
        vector. Devuelve cuantos guardo."""
        if not self.listo:
            return 0
        import cv2

        guardados = 0
        with Session(self.engine) as s:
            eventos = self._pendientes(s, maximo)
            for i in range(0, len(eventos), self.LOTE):
                lote = eventos[i:i + self.LOTE]
                imagenes, validos = [], []
                for ev in lote:
                    ruta = (BASE_DIR / ev.snapshot_path).resolve()
                    img = cv2.imread(str(ruta)) if str(ruta).startswith(str(BASE_DIR.resolve())) else None
                    if img is None or img.size == 0:
                        # Foto ya borrada o ilegible: se marca con un vector
                        # vacio para no reintentarla en cada vuelta.
                        s.add(SemanticEmbedding(event_id=ev.event_id, vector=b"", dim=0,
                                                modelo=self.codificador.nombre))
                        continue
                    imagenes.append(img)
                    validos.append(ev)
                if imagenes:
                    vectores = self.codificador.imagenes(imagenes).astype(np.float16)
                    for ev, v in zip(validos, vectores):
                        s.add(SemanticEmbedding(event_id=ev.event_id, vector=v.tobytes(), dim=int(v.shape[0]),
                                                modelo=self.codificador.nombre))
                        guardados += 1
                try:
                    s.commit()
                except IntegrityError:
                    # Otro proceso de la API indexo lo mismo al mismo tiempo.
                    s.rollback()
        self.indexados_sesion += guardados
        return guardados

    # -- busqueda ----------------------------------------------------------

    def _sincronizar(self) -> None:
        """Trae a memoria los vectores nuevos; cada 10 min reconstruye todo
        para soltar los que borro la retencion."""
        with self._candado:
            completo = time.monotonic() - self._reconstruida > 600
            desde = 0 if completo else self._ultimo_id
            with Session(self.engine) as s:
                filas = s.exec(select(SemanticEmbedding.id, SemanticEmbedding.event_id, SemanticEmbedding.vector,
                                      SemanticEmbedding.dim)
                               .where(SemanticEmbedding.id > desde, SemanticEmbedding.dim > 0)
                               .order_by(SemanticEmbedding.id)).all()
            if completo:
                self._ids = np.zeros(0, dtype=np.int64)
                self._eventos = []
                self._matriz = np.zeros((0, 0), dtype=np.float32)
                self._reconstruida = time.monotonic()
                self._ultimo_id = 0
            if not filas:
                return
            dim = filas[0][3]
            nuevos = np.stack([np.frombuffer(f[2], dtype=np.float16).astype(np.float32) for f in filas
                               if f[3] == dim])
            ids = np.array([f[0] for f in filas if f[3] == dim], dtype=np.int64)
            eventos = [f[1] for f in filas if f[3] == dim]
            if self._matriz.size and self._matriz.shape[1] != dim:
                # Cambio de modelo: lo viejo no es comparable.
                self._matriz = np.zeros((0, dim), dtype=np.float32)
                self._ids, self._eventos = np.zeros(0, dtype=np.int64), []
            self._matriz = nuevos if not self._matriz.size else np.vstack([self._matriz, nuevos])
            self._ids = np.concatenate([self._ids, ids])
            self._eventos += eventos
            self._ultimo_id = int(max(self._ultimo_id, ids.max()))

    def buscar(self, frase: str, candidatos: Optional[set[str]] = None, limite: int = 30) -> list[Resultado]:
        if not self.listo:
            raise RuntimeError(self.estado)
        self._sincronizar()
        with self._candado:
            if not self._matriz.size:
                return []
            consulta = self.codificador.texto(frase).astype(np.float32)
            if consulta.shape[0] != self._matriz.shape[1]:
                return []
            similitud = self._matriz @ consulta
            orden = np.argsort(-similitud)
            resultados = []
            for i in orden:
                s = float(similitud[i])
                if s < self.min_similitud:
                    break
                ev = self._eventos[i]
                if candidatos is not None and ev not in candidatos:
                    continue
                resultados.append(Resultado(ev, round(s, 4)))
                if len(resultados) >= limite:
                    break
            return resultados

    def resumen(self) -> dict:
        return {"estado": self.estado, "modelo": self.codificador.nombre if self.codificador else None,
                "error": self.error, "en_memoria": int(self._matriz.shape[0]) if self._matriz.size else 0}


def borrar_de_eventos(session: Session, event_ids: list[str]) -> None:
    """La retencion llama esto antes de borrar la foto o el evento."""
    for i in range(0, len(event_ids), 500):
        session.exec(delete(SemanticEmbedding).where(col(SemanticEmbedding.event_id).in_(event_ids[i:i + 500])))


indice: Optional[IndiceSemantico] = None


async def indexar_periodicamente(idx: IndiceSemantico, obtener_canal=None, intervalo: float = 20.0) -> None:
    """Bucle de fondo de la API: indexa lo nuevo cada `intervalo` segundos.
    Con varios procesos (Redis), uno solo trabaja en cada vuelta."""
    import asyncio

    while True:
        try:
            await asyncio.sleep(intervalo)
            if not idx.listo:
                continue
            canal = obtener_canal() if obtener_canal else None
            if canal is not None and not await canal.tomar_turno("semantica", int(intervalo) + 5):
                continue
            n = await asyncio.to_thread(idx.indexar, 64)
            if n:
                log.info("Busqueda semantica: %d capturas nuevas indexadas", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.error("Fallo el indexador semantico: %s", e)


def ventana_por_defecto(dias: int = 30) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=dias)
