"""Alta de camaras desde el dashboard, para el monitorista.

Hasta ahora encontrar la camara y armar su URL RTSP era trabajo de terminal
(ver tools/probe_camara.py). Este router expone la MISMA logica --no la
duplica-- como API, para que un monitorista sin conocimientos de linea de
comandos pueda buscar la camara en su red, probar las credenciales y guardar
el resultado, todo desde el navegador.

LIMITACION HONESTA: el sistema hoy corre UN worker por UNA camara (SOURCE en
.env, leido al arrancar). Guardar aqui escribe ese .env, pero el worker que ya
esta corriendo no lo relee solo -- hay que reiniciarlo. Dashboard multi-camara
en caliente es un proyecto aparte, no "una forma sencilla de anadir camaras".

Se restringe a Admin por dos motivos: escanea la red local (una accion que
vale la pena poder auditar) y prueba credenciales contra un dispositivo (igual
que dar de alta en la lista negra, tiene consecuencias si se usa mal).
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import cv2
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from api.config import BASE_DIR
from api.deps import Admin

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/camera-setup", tags=["configuracion de camara"])

ENV_PATH = BASE_DIR / ".env"


# --------------------------------------------------------------------------
# Descubrimiento en la red local
# --------------------------------------------------------------------------

@router.post("/descubrir")
def descubrir(admin: Admin) -> list[dict]:
    """Escanea la LAN /24 buscando dispositivos con puertos de camara abiertos.

    Es una funcion sincrona (no async def) A PROPOSITO: tarda ~20s recorriendo
    254 direcciones, y FastAPI corre los endpoints sincronos en un hilo aparte.
    Si esto fuera async y llamara directo al escaneo bloqueante, congelaria el
    event loop entero -- y con el, la vista en vivo de todos los que esten
    mirando el dashboard en ese momento.
    """
    from tools.probe_camara import PUERTOS_INTERES, descubrir as _descubrir

    encontrados = _descubrir()
    return [
        {
            "host": ip,
            "puertos": [{"puerto": p, "etiqueta": PUERTOS_INTERES[p]} for p in puertos],
        }
        for ip, puertos in encontrados
    ]


# --------------------------------------------------------------------------
# Prueba de conexion
# --------------------------------------------------------------------------

class ProbarCamaraIn(BaseModel):
    host: str = Field(min_length=3, max_length=100)
    user: str = Field(default="admin", max_length=64)
    password: str = Field(min_length=1, max_length=200)
    puerto: int = Field(default=554, ge=1, le=65535)


def _primera_ruta_funcional(host: str, user: str, password: str, puerto: int) -> dict | None:
    """Prueba las rutas conocidas EN ORDEN y se detiene en la primera que
    funcione, a diferencia de probar_camara() en tools/probe_camara.py, que
    las prueba todas para comparar y elegir la de menor resolucion.

    Ese comportamiento exhaustivo tiene sentido para una herramienta de
    diagnostico que se corre una vez y se espera. Aqui no: medido en este
    mismo proceso, una ruta que NO responde tarda ~30s en agotar el timeout de
    FFmpeg (mas de lo que promete el parametro `timeout` de probar_ruta, que
    ademas esta sin usar en esa funcion). Con 9 rutas posibles, probarlas
    todas puede pasar de los 4 minutos. Como la lista ya trae el sub-stream
    recomendado primero, detenerse en el primer exito casi siempre da el mismo
    resultado de todos modos -- y en el peor caso, uno peor pero en una
    fraccion del tiempo.
    """
    from tools.probe_camara import RUTAS_CANDIDATAS, construir_url, probar_ruta

    for ruta, _descripcion in RUTAS_CANDIDATAS:
        info = probar_ruta(construir_url(host, user, password, ruta, puerto))
        if info is not None:
            info["ruta"] = ruta
            return info
    return None


@router.post("/probar")
def probar(datos: ProbarCamaraIn, admin: Admin) -> dict:
    """Valida credenciales (un solo intento HTTP, para no gatillar el bloqueo
    de Hikvision tras varios fallidos) y despues prueba rutas RTSP conocidas
    hasta encontrar una que funcione. Devuelve una miniatura en base64 para
    que el monitorista confirme el encuadre ANTES de guardar -- sin eso,
    "guardar" es un acto de fe.
    """
    from tools.probe_camara import identificar

    if not identificar(datos.host, datos.user, datos.password):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "La camara rechazo usuario/contrasena. No se probaron las rutas de "
            "video para no arriesgar el bloqueo por intentos fallidos: Hikvision "
            "bloquea la IP tras ~5.",
        )

    resultado = _primera_ruta_funcional(datos.host, datos.user, datos.password, datos.puerto)
    if resultado is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Las credenciales son correctas pero ninguna ruta de video respondio. "
            "Revisa que RTSP este activado en la camara (Configuracion > Red > "
            "Avanzada > Protocolos) y que esta PC este en la misma red.",
        )

    ok, buf = cv2.imencode(".jpg", resultado["frame"], [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return {
        "ruta": resultado["ruta"],
        "ancho": resultado["ancho"],
        "alto": resultado["alto"],
        "fps": resultado["fps"],
        "preview_b64": base64.b64encode(buf).decode() if ok else None,
    }


# --------------------------------------------------------------------------
# Guardar en .env
# --------------------------------------------------------------------------

class GuardarCamaraIn(BaseModel):
    host: str = Field(min_length=3, max_length=100)
    user: str = Field(default="admin", max_length=64)
    password: str = Field(min_length=1, max_length=200)
    puerto: int = Field(default=554, ge=1, le=65535)
    ruta: str = Field(min_length=1, max_length=200, description="Ruta RTSP ya probada, ej. /Streaming/Channels/102")
    camera_id: str = Field(default="cam-01", max_length=64)


def _actualizar_env(cambios: dict[str, str]) -> None:
    """Reemplaza (o agrega) claves en .env, conservando todo lo demas tal cual.

    Mismo criterio que guardar_source_en_env() en tools/probe_camara.py: no se
    reescribe el archivo entero con un template, porque eso perderia los
    comentarios y el orden que ya tiene el operador ahi.
    """
    if not ENV_PATH.exists():
        ENV_PATH.write_text("", encoding="utf-8")

    lineas = ENV_PATH.read_text(encoding="utf-8").splitlines()
    pendientes = dict(cambios)

    for i, linea in enumerate(lineas):
        clave = linea.split("=", 1)[0].strip() if "=" in linea and not linea.strip().startswith("#") else None
        if clave in pendientes:
            lineas[i] = f"{clave}={pendientes.pop(clave)}"

    for clave, valor in pendientes.items():
        lineas.append(f"{clave}={valor}")

    ENV_PATH.write_text("\n".join(lineas) + "\n", encoding="utf-8")


@router.post("/guardar", status_code=status.HTTP_204_NO_CONTENT)
def guardar(datos: GuardarCamaraIn, admin: Admin) -> None:
    """Escribe SOURCE (y CAMERA_HOST/USER/PASSWORD, por si se vuelve a correr
    tools/probe_camara.py mas adelante) en el .env del worker.

    Exige haber probado la ruta primero (el frontend solo llama a esto despues
    de un /probar exitoso) para que nunca se guarde una camara que no se
    confirmo que funciona.
    """
    from urllib.parse import quote

    url = (
        f"rtsp://{quote(datos.user, safe='')}:{quote(datos.password, safe='')}"
        f"@{datos.host}:{datos.puerto}{datos.ruta}"
    )
    _actualizar_env({
        "CAMERA_HOST": datos.host,
        "CAMERA_USER": datos.user,
        "CAMERA_PASSWORD": datos.password,
        "CAMERA_ID": datos.camera_id,
        "SOURCE": url,
    })
    log.info("Camara '%s' (%s) guardada en .env por %s", datos.camera_id, datos.host, admin.username)
