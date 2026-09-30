"""Endpoints de la vista en vivo.

Tres piezas: el worker sube frames, el dashboard pregunta que camaras hay, y
el <img> del navegador consume el MJPEG.

Cada una tiene su autenticacion y no son la misma: subir video es cosa de una
maquina (token de ingesta), verlo es cosa de una persona (JWT de sesion).
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import StreamingResponse

from api.deps import OperadorActual, OperadorLectura, verificar_worker
from api.preview import MAX_BYTES_FRAME, clave_pistas, obtener_preview
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
            413,
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


# --------------------------------------------------------------------------
# Video por WebRTC (go2rtc) con las cajas dibujadas en el navegador
# --------------------------------------------------------------------------
#
# Con GO2RTC_URL, el navegador toma el video directo de go2rtc por WebRTC: sin
# recodificar, a resolucion completa y con menos de medio segundo de retraso.
# Pero ese video no trae las cajas de los detectores. El worker las manda aqui
# como JSON (unos cientos de bytes, no un JPEG) y el navegador las dibuja
# encima. Mismo principio que el MJPEG: solo se mandan mientras alguien mira.

MAX_BYTES_PISTAS = 64 * 1024


def url_go2rtc() -> str:
    """Donde encuentra el NAVEGADOR a go2rtc. Lo recomendado es detras del
    mismo Caddy que la API (/go2rtc), protegido con la sesion del dashboard
    (docker/Caddyfile): go2rtc por si solo no pide contrasena."""
    return (os.getenv("GO2RTC_URL", "") or "").rstrip("/")


@router.get("/config")
async def configuracion(_: OperadorActual) -> dict:
    base = url_go2rtc()
    return {"modo": "webrtc" if base else "mjpeg", "go2rtc": base or None}


@router.post("/{camera_id}/pistas", dependencies=[Depends(verificar_worker)])
async def publicar_pistas(request: Request, camera_id: str = Path(pattern=PATRON_CAMARA)) -> dict:
    cuerpo = await request.body()
    if len(cuerpo) > MAX_BYTES_PISTAS:
        raise HTTPException(413, "Demasiadas cajas")
    try:
        datos = json.loads(cuerpo)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El cuerpo no es JSON") from None
    if not isinstance(datos, dict) or not isinstance(datos.get("objetos"), list):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Falta la lista 'objetos'")
    # Se vuelve a serializar en una linea: asi viaja tal cual como un evento
    # SSE, y nada raro que haya mandado el cliente llega al navegador.
    limpio = json.dumps({"ancho": datos.get("ancho"), "alto": datos.get("alto"), "ts": datos.get("ts"),
                         "objetos": datos["objetos"][:200]}, ensure_ascii=False,
                        separators=(",", ":")).encode()
    espectadores = await obtener_preview().apublicar(clave_pistas(camera_id), limpio)
    return {"ok": True, "espectadores": espectadores}


@router.get("/{camera_id}/pistas.sse")
async def flujo_pistas(_: OperadorLectura, camera_id: str = Path(pattern=PATRON_CAMARA)) -> StreamingResponse:
    """Las cajas de la camara conforme cambian (Server-Sent Events). El
    EventSource del navegador se reconecta solo si el flujo se corta."""

    async def eventos():
        interno = obtener_preview().flujo_crudo(clave_pistas(camera_id))
        try:
            yield b"retry: 3000\n\n"
            async for datos in interno:
                yield b"data: " + datos + b"\n\n"
        finally:
            await interno.aclose()

    return StreamingResponse(eventos(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
