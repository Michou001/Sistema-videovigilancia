"""Estado y prueba de las notificaciones externas (Telegram, correo, WhatsApp,
webhook). La configuracion vive en el .env del servidor, no en la base de
datos: los tokens de los bots y las contrasenas de correo no deben poder
leerse ni cambiarse desde el navegador."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from typing import Literal

from api import notificaciones
from api.auditoria import registrar
from api.deps import Admin, SesionBD

router = APIRouter(prefix="/api/notificaciones", tags=["notificaciones"])


class EnsayoTelegram(BaseModel):
    tipo: Literal["plate", "face"]


def _notificador() -> notificaciones.Notificador:
    n = notificaciones.obtener()
    if n is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Notificaciones no inicializadas")
    return n


@router.get("")
def estado(_: Admin) -> dict:
    """Canales activos (sin secretos) y contadores de envio."""
    return _notificador().resumen()


@router.post("/prueba")
async def prueba(request: Request, session: SesionBD, operador: Admin) -> dict:
    """Manda un mensaje de prueba por TODOS los canales y devuelve el
    resultado de cada uno: es la forma de saber si el token o la contrasena
    estan bien sin esperar a que ocurra una alerta de verdad."""
    n = _notificador()
    if not n.activo:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "No hay canales configurados (ver NOTIFY_* en el .env del servidor)")
    resultados = await n.prueba()
    await run_in_threadpool(registrar, session, "notificaciones.prueba", usuario=operador.username,
                            detalle=resultados, request=request, confirmar=True)
    return {"resultados": resultados, "ok": all(v == "ok" for v in resultados.values())}


@router.post("/prueba-telegram")
async def prueba_telegram(datos: EnsayoTelegram, request: Request, session: SesionBD,
                          operador: Admin) -> dict:
    """Envia un ensayo de coincidencia solo al Telegram configurado."""
    n = _notificador()
    if not any(c.nombre == "telegram" for c in n.canales):
        raise HTTPException(status.HTTP_409_CONFLICT, "Telegram no esta configurado")
    resultados = await n.prueba_telegram(datos.tipo)
    await run_in_threadpool(registrar, session, "notificaciones.prueba_telegram",
                            usuario=operador.username,
                            detalle={"tipo": datos.tipo, "resultados": resultados},
                            request=request, confirmar=True)
    return {"resultados": resultados, "ok": resultados.get("telegram") == "ok"}
