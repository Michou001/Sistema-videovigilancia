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
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlmodel import col, select

from api.config import get_config
from api.deps import Admin, OperadorActual, SesionBD
from api.models import BlacklistFace, FechasEnUtc

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
    label: str = Form(..., min_length=2, max_length=120),
    reason: str = Form(..., min_length=3, max_length=300),
    legal_basis: str = Form(..., min_length=3, max_length=300),
    severity: str = Form("critical"),
    foto: UploadFile = File(...),
):
    if severity not in {"critical", "warning"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "severity debe ser 'critical' o 'warning'")

    datos = await foto.read()
    if len(datos) > MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            "La foto supera los 8 MB")

    imagen = cv2.imdecode(np.frombuffer(datos, np.uint8), cv2.IMREAD_COLOR)
    if imagen is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "No se pudo leer la imagen (formato no soportado)")

    vector = _embedder().embedding_de_foto(imagen)
    if vector is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "No se detecto ningun rostro en la foto. Se rechaza aqui a proposito: "
            "un registro sin rostro valido nunca coincidiria con nada y quedaria "
            "en la lista dando una falsa sensacion de cobertura.",
        )

    cfg = get_config()
    registro = BlacklistFace(
        label=label,
        vector=vector.tobytes(),
        dim=int(vector.shape[0]),
        reason=reason,
        legal_basis=legal_basis,
        severity=severity,
        created_by=admin.username,
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
    registro.photo_path = ruta.relative_to(cfg.snapshot_dir.parent.parent).as_posix()
    session.commit()
    session.refresh(registro)

    log.info("Rostro '%s' agregado a lista negra por %s (fundamento: %s)",
             label, admin.username, legal_basis)
    return registro


@router.delete("/{registro_id}", status_code=status.HTTP_204_NO_CONTENT)
def desactivar(registro_id: int, session: SesionBD, admin: Admin):
    """Baja logica: los eventos historicos siguen apuntando a este registro."""
    registro = session.get(BlacklistFace, registro_id)
    if registro is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese registro")
    registro.active = False
    session.commit()
    log.info("Rostro '%s' desactivado por %s", registro.label, admin.username)
