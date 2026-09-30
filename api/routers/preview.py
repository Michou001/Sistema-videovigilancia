"""Endpoints de la vista en vivo.

Tres piezas: el worker sube frames, el dashboard pregunta que camaras hay, y
el <img> del navegador consume el MJPEG.

Cada una tiene su autenticacion y no son la misma: subir video es cosa de una
maquina (token de ingesta), verlo es cosa de una persona (JWT de sesion).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import StreamingResponse

from api.deps import OperadorActual, OperadorLectura, verificar_worker
from api.preview import MAX_BYTES_FRAME, obtener_preview
from shared.events import PATRON_CAMARA

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/preview", tags=["vista en vivo"])

# Los dos primeros bytes de todo JPEG (marcador SOI). Se valida para no
# repartir a los dashboards lo que sea que un cliente mal configurado suba.
JPEG_SOI = b"\xff\xd8"


@router.post("/{camera_id}", dependencies=[Depends(verificar_worker)])
async def publicar_frame(request: Request,
                         camera_id: str = Path(pattern=PATRON_CAMARA)) -> dict:
    """El worker sube el ultimo frame anotado, en crudo (image/jpeg).

    Va como cuerpo binario y no como base64 dentro de un JSON porque base64
    infla un 33% el trafico de algo que se manda varias veces por segundo.

    Devuelve `espectadores`: cuantos dashboards estan mirando esta camara. El
    worker deja de codificar y subir cuando es 0. Esa cifra es la razon de que
    el preview no cueste nada cuando nadie tiene el dashboard abierto.
    """
    cuerpo = await request.body()

    if not cuerpo:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Frame vacio")
    if len(cuerpo) > MAX_BYTES_FRAME:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Frame de {len(cuerpo)} bytes; el maximo es {MAX_BYTES_FRAME}. "
            "Baja PREVIEW_WIDTH o PREVIEW_QUALITY en el .env del worker.",
        )
    if not cuerpo.startswith(JPEG_SOI):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El cuerpo no es un JPEG")

    espectadores = await obtener_preview().apublicar(camera_id, cuerpo)
    return {"ok": True, "espectadores": espectadores}


@router.get("/camaras")
async def camaras_en_vivo(_: OperadorActual) -> list[dict]:
    """Camaras que estan mandando video en este momento.

    El dashboard consulta esto cada pocos segundos para montar y desmontar los
    recuadros. Es distinto de /api/stats: alli una camara esta "online" si
    manda eventos, aqui solo si manda VIDEO. Una camara puede estar detectando
    perfectamente y no aparecer aqui porque el preview esta apagado en su .env.
    """
    return await obtener_preview().acamaras()


@router.get("/{camera_id}/live.mjpg")
async def flujo_en_vivo(_: OperadorLectura,
                        camera_id: str = Path(pattern=PATRON_CAMARA)) -> StreamingResponse:
    """Flujo MJPEG de una camara.

    Un <img> no puede mandar la cabecera Authorization, asi que la sesion se
    toma de la cookie HttpOnly que pone el login (ver api/security.py). Antes
    iba como ?token= en la URL y quedaba en el historial del navegador.
    """

    return StreamingResponse(
        obtener_preview().flujo_mjpeg(camera_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            # Sin esto un proxy intermedio puede intentar almacenar el flujo y
            # el operador acaba viendo imagenes viejas, que en videovigilancia
            # es peor que no ver nada.
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",  # nginx: no acumular antes de enviar
        },
    )
