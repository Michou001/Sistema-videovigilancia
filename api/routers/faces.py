"""Lista negra biometrica: alta y baja de personas por rostro.

Este es el endpoint mas delicado del sistema. Dar de alta a alguien aqui hace
que el sistema lo senale automaticamente cada vez que aparezca ante una camara.

Por eso, ademas de exigir rol admin, cada alta obliga a declarar un
`legal_basis`: el fundamento por el que esa persona esta siendo vigilada. No es
burocracia -- es el registro que permite auditar el sistema y responder "por
que esta esta persona aqui" meses despues.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import cv2
import numpy as np
from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlmodel import col, select

from api.auditoria import registrar
from api.config import BASE_DIR, get_config, ruta_guardable
from api.deps import Admin, OperadorActual, SesionBD
from api.matching import lista_negra
from api.models import BlacklistFace, FechasEnUtc
from api.retroactive import reescanear_rostro
from shared.fechas import a_utc, ahora_utc

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/blacklist/faces", tags=["lista negra"])

MAX_BYTES = 8 * 1024 * 1024


class RostroLeido(FechasEnUtc, BaseModel):
    id: int
    label: str
    reason: str
    legal_basis: Optional[str]
    severity: str
    photo_path: Optional[str]
    active: bool
    created_by: Optional[str]
    created_at: datetime


def _embedder():
    """Carga el modelo bajo demanda.

    No se carga al arrancar la API porque ocupa VRAM y la mayoria de los
    reinicios del servidor no dan de alta a nadie. La primera alta paga ~2 s.
    """
    from edge.config import load_config
    from edge.detectors.faces import FaceEmbedder

    return FaceEmbedder.compartido(load_config())


@router.get("", response_model=list[RostroLeido])
def listar(session: SesionBD, _: OperadorActual, incluir_inactivos: bool = False):
    consulta = select(BlacklistFace)
    if not incluir_inactivos:
        consulta = consulta.where(BlacklistFace.active)
    return session.exec(consulta.order_by(col(BlacklistFace.created_at).desc())).all()


@router.post("", response_model=RostroLeido, status_code=status.HTTP_201_CREATED)
async def agregar(
    session: SesionBD,
    admin: Admin,
    tareas: BackgroundTasks,
    request: Request,
    label: str = Form(..., min_length=2, max_length=120),
    reason: str = Form(..., min_length=3, max_length=300),
    legal_basis: str = Form(..., min_length=3, max_length=300),
    severity: str = Form("critical"),
    foto: UploadFile = File(...),
    expires_at: Optional[datetime] = Form(
        None, description="Vigencia del registro (ISO 8601). Vencido, deja de alertar."),
):
    if severity not in {"critical", "warning"}:
        raise HTTPException(422,
                            "severity debe ser 'critical' o 'warning'")
    expires_at = a_utc(expires_at)
    if expires_at is not None and expires_at <= ahora_utc():
        raise HTTPException(422, "La vigencia debe ser una fecha futura")

    # Se lee como maximo un byte mas del limite: un archivo enorme no llega
    # completo a memoria para luego rechazarlo.
    datos = await foto.read(MAX_BYTES + 1)
    if len(datos) > MAX_BYTES:
        raise HTTPException(413,
                            "La foto supera los 8 MB")

    imagen = cv2.imdecode(np.frombuffer(datos, np.uint8), cv2.IMREAD_COLOR)
    if imagen is None:
        raise HTTPException(422,
                            "No se pudo leer la imagen (formato no soportado)")
    if max(imagen.shape[:2]) > 10000:
        raise HTTPException(422, "La imagen es demasiado grande (máximo 10 000 px por lado)")

    # Cargar el modelo (la primera vez) y correrlo tarda segundos: en el pool
    # de hilos, no en el bucle de eventos, o el video en vivo y el WebSocket
    # de todos los dashboards se congelan mientras tanto.
    vector, motivo = await run_in_threadpool(lambda: _embedder().analizar_foto_alta(imagen))
    if vector is None:
        # Se rechaza aqui a proposito: una referencia sin rostro valido nunca
        # coincidiria con nada y daria una falsa sensacion de cobertura.
        raise HTTPException(422, motivo)

    cfg = get_config()
    registro = BlacklistFace(
        label=label,
        vector=vector.tobytes(),
        dim=int(vector.shape[0]),
        reason=reason,
        legal_basis=legal_basis,
        severity=severity,
        created_by=admin.username,
        expires_at=expires_at,
    )
    session.add(registro)
    session.commit()
    session.refresh(registro)

    # La foto de referencia se guarda con el id del registro, fuera de la
    # carpeta de evidencia de eventos: tienen politicas de retencion distintas.
    carpeta = cfg.snapshot_dir.parent / "referencias"
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / f"rostro-{registro.id}.jpg"
    cv2.imwrite(str(ruta), imagen)
    registro.photo_path = ruta_guardable(ruta)
    # El fundamento legal va a la bitacora: es lo que se pide en una auditoria.
    registrar(session, "lista_negra.alta_rostro", usuario=admin.username,
              objetivo=f"{label} (#{registro.id})",
              detalle={"motivo": reason, "fundamento": legal_basis, "severidad": severity},
              request=request)
    session.commit()
    session.refresh(registro)
    lista_negra.invalidar()

    log.info("Rostro '%s' agregado a lista negra por %s (fundamento: %s)",
             label, admin.username, legal_basis)
    # En segundo plano: revisa las fotos de rostros ya guardadas (7 dias) por
    # si esta persona ya habia pasado antes de hoy. No recalcula nada si no
    # hace falta -- ver api/retroactive.py sobre por que esto no se guarda
    # como base de datos biometrica permanente.
    tareas.add_task(reescanear_rostro, registro)
    return registro


@router.delete("/{registro_id}", status_code=status.HTTP_204_NO_CONTENT)
def desactivar(registro_id: int, session: SesionBD, admin: Admin, request: Request):
    """Baja: se BORRAN el vector biometrico y la foto de referencia.

    Queda la fila (etiqueta, motivo, fundamento, quien la dio de alta) porque
    los eventos y alertas historicos la referencian y la bitacora debe poder
    explicar por que hubo una coincidencia. Lo que identifica a la persona -- su
    rostro -- no se conserva una vez que deja de tener fundamento.
    """
    registro = session.get(BlacklistFace, registro_id)
    if registro is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese registro")
    registro.active = False
    registro.vector = bytes(4 * (registro.dim or 512))   # ceros: ya no compara con nada
    if registro.photo_path:
        foto = (BASE_DIR / registro.photo_path).resolve()
        carpeta = (get_config().snapshot_dir.parent / "referencias").resolve()
        if foto.parent == carpeta:
            foto.unlink(missing_ok=True)
        registro.photo_path = None
    registrar(session, "lista_negra.baja_rostro", usuario=admin.username,
              objetivo=f"{registro.label} (#{registro.id})",
              detalle={"datos_biometricos": "eliminados"}, request=request)
    session.commit()
    lista_negra.invalidar()
    log.info("Rostro '%s' desactivado por %s", registro.label, admin.username)
