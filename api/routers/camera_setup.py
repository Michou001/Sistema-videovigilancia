"""Alta de camaras desde el dashboard, para el monitorista.

Encontrar la camara y armar su URL RTSP era trabajo de terminal (ver
tools/probe_camara.py). Este router expone la MISMA logica --no la duplica--
como API, para que un monitorista sin conocimientos de linea de comandos pueda
buscar la camara en su red, probar las credenciales y guardar el resultado,
todo desde el navegador.

Cada camara corre en su propio worker con su propio archivo de entorno: la
primera en `.env`, las demas en `.env.<camera_id>`. Guardar aqui escribe ese
archivo; el worker lo lee al arrancar, asi que una camara nueva se levanta con
`iniciar_worker.bat --env .env.<camera_id>` (la respuesta trae el comando).

Se restringe a Admin por dos motivos: escanea la red local (una accion que
vale la pena poder auditar) y prueba credenciales contra un dispositivo (igual
que dar de alta en la lista negra, tiene consecuencias si se usa mal).
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path

import cv2
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from api.auditoria import registrar
from api.config import BASE_DIR
from api.database import engine
from api.deps import Admin

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/camera-setup", tags=["configuracion de camara"])

ENV_PATH = BASE_DIR / ".env"

_HOST_VALIDO = re.compile(r"[A-Za-z0-9.\-:\[\]]{3,100}")
_CAMARA_VALIDA = re.compile(r"[A-Za-z0-9_\-]{2,64}")
_RUTA_VALIDA = re.compile(r"/[A-Za-z0-9_\-./?=&]{0,199}")


def _sin_control(valor: str) -> str:
    """Los valores terminan como lineas del .env: un salto de linea en una
    contrasena escribiria una variable extra en el archivo."""
    if any(ord(c) < 32 for c in valor):
        raise ValueError("no puede contener saltos de línea ni caracteres de control")
    return valor


# --------------------------------------------------------------------------
# Descubrimiento en la red local
# --------------------------------------------------------------------------

@router.post("/descubrir")
def descubrir(admin: Admin, request: Request) -> list[dict]:
    """Escanea la LAN /24 buscando dispositivos con puertos de camara abiertos.

    Es una funcion sincrona (no async def) A PROPOSITO: tarda ~20s recorriendo
    254 direcciones, y FastAPI corre los endpoints sincronos en un hilo aparte.
    Si esto fuera async y llamara directo al escaneo bloqueante, congelaria el
    event loop entero -- y con el, la vista en vivo de todos los que esten
    mirando el dashboard en ese momento.
    """
    from tools.probe_camara import PUERTOS_INTERES, descubrir as _descubrir

    encontrados = _descubrir()
    _auditar("camaras.descubrir", admin.username, request, detalle={"encontrados": len(encontrados)})
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

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip()
        if not _HOST_VALIDO.fullmatch(v):
            raise ValueError("IP o nombre de host no válido")
        return v

    @field_validator("user", "password")
    @classmethod
    def _texto(cls, v: str) -> str:
        return _sin_control(v)


def _primera_ruta_funcional(host: str, user: str, password: str, puerto: int) -> dict | None:
    """Prueba las rutas conocidas EN ORDEN y se detiene en la primera que
    funcione, a diferencia de probar_camara() en tools/probe_camara.py, que
    las prueba todas para comparar y elegir la de menor resolucion.

    Ese comportamiento exhaustivo tiene sentido para una herramienta de
    diagnostico que se corre una vez y se espera. Aqui el monitorista esta
    mirando un boton de "Probando...": como la lista ya trae el sub-stream
    recomendado primero, detenerse en el primer exito casi siempre da el mismo
    resultado en una fraccion del tiempo.
    """
    from tools.probe_camara import RUTAS_CANDIDATAS, construir_url, probar_ruta

    for ruta, _descripcion in RUTAS_CANDIDATAS:
        info = probar_ruta(construir_url(host, user, password, ruta, puerto), timeout=6.0)
        if info is not None:
            info["ruta"] = ruta
            return info
    return None


@router.post("/probar")
def probar(datos: ProbarCamaraIn, admin: Admin, request: Request) -> dict:
    """Valida credenciales (un solo intento HTTP, para no gatillar el bloqueo
    de Hikvision tras varios fallidos) y despues prueba rutas RTSP conocidas
    hasta encontrar una que funcione. Devuelve una miniatura en base64 para
    que el monitorista confirme el encuadre ANTES de guardar -- sin eso,
    "guardar" es un acto de fe.
    """
    from tools.probe_camara import identificar

    ok_credenciales = identificar(datos.host, datos.user, datos.password)
    _auditar("camaras.probar", admin.username, request, objetivo=datos.host,
             detalle={"credenciales_validas": bool(ok_credenciales)})
    if not ok_credenciales:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "La cámara rechazó usuario/contraseña. No se probaron las rutas de "
            "video para no arriesgar el bloqueo por intentos fallidos: Hikvision "
            "bloquea la IP tras ~5.",
        )

    resultado = _primera_ruta_funcional(datos.host, datos.user, datos.password, datos.puerto)
    if resultado is None:
        raise HTTPException(
            422,
            "Las credenciales son correctas pero ninguna ruta de video respondió. "
            "Revisa que RTSP esté activado en la cámara (Configuración > Red > "
            "Avanzada > Protocolos) y que esta PC esté en la misma red.",
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

class GuardarCamaraIn(ProbarCamaraIn):
    ruta: str = Field(min_length=1, max_length=200, description="Ruta RTSP ya probada, ej. /Streaming/Channels/102")
    camera_id: str = Field(default="cam-01", max_length=64)

    @field_validator("ruta")
    @classmethod
    def _ruta(cls, v: str) -> str:
        if not _RUTA_VALIDA.fullmatch(v):
            raise ValueError("ruta RTSP no válida")
        return v

    @field_validator("camera_id")
    @classmethod
    def _camara(cls, v: str) -> str:
        v = v.strip()
        if not _CAMARA_VALIDA.fullmatch(v):
            raise ValueError("el identificador solo admite letras, números, '-' y '_'")
        return v


def _leer_env(ruta: Path) -> dict[str, str]:
    from edge.config import parse_env_line

    valores = {}
    if ruta.exists():
        for linea in ruta.read_text(encoding="utf-8-sig").splitlines():
            par = parse_env_line(linea)
            if par is not None:
                valores[par[0]] = par[1]
    return valores


def archivo_env_para(camera_id: str) -> Path:
    """Archivo de entorno del worker de esta camara.

    `.env` si es la camara que ya vive ahi (o si todavia no hay ninguna). Si
    no, el `.env.*` que ya tenga ese CAMERA_ID, o uno nuevo `.env.<camera_id>`.
    Antes siempre se escribia `.env`: dar de alta la segunda camara
    reemplazaba la configuracion de la primera.
    """
    principal = _leer_env(ENV_PATH)
    if not principal or principal.get("CAMERA_ID", camera_id) == camera_id \
            or not principal.get("SOURCE", "").startswith(("rtsp://", "rtsps://")):
        return ENV_PATH
    for otro in sorted(BASE_DIR.glob(".env.*")):
        if otro.name == ".env.example" or otro.suffix in {".bak", ".example"}:
            continue
        if _leer_env(otro).get("CAMERA_ID") == camera_id:
            return otro
    return BASE_DIR / f".env.{camera_id}"


def _actualizar_env(ruta: Path, cambios: dict[str, str]) -> None:
    """Reemplaza (o agrega) claves en el archivo, conservando todo lo demas.

    Mismo criterio que guardar_source_en_env() en tools/probe_camara.py: no se
    reescribe el archivo entero con un template, porque eso perderia los
    comentarios y el orden que ya tiene el operador ahi. Un archivo nuevo de
    otra camara parte de una copia de `.env`: hereda API_URL, API_TOKEN y los
    detectores activos, que son los mismos para todas.
    """
    if not ruta.exists():
        base = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
        ruta.write_text(base, encoding="utf-8")

    lineas = ruta.read_text(encoding="utf-8").splitlines()
    pendientes = dict(cambios)

    for i, linea in enumerate(lineas):
        clave = linea.split("=", 1)[0].strip() if "=" in linea and not linea.strip().startswith("#") else None
        if clave in pendientes:
            lineas[i] = f"{clave}={pendientes.pop(clave)}"

    for clave, valor in pendientes.items():
        lineas.append(f"{clave}={valor}")

    ruta.write_text("\n".join(lineas) + "\n", encoding="utf-8")


@router.post("/guardar")
def guardar(datos: GuardarCamaraIn, admin: Admin, request: Request) -> dict:
    """Escribe SOURCE (y CAMERA_HOST/USER/PASSWORD, por si se vuelve a correr
    tools/probe_camara.py mas adelante) en el archivo de entorno de la camara.

    Exige haber probado la ruta primero (el frontend solo llama a esto despues
    de un /probar exitoso) para que nunca se guarde una camara que no se
    confirmo que funciona.
    """
    from urllib.parse import quote

    url = (
        f"rtsp://{quote(datos.user, safe='')}:{quote(datos.password, safe='')}"
        f"@{datos.host}:{datos.puerto}{datos.ruta}"
    )
    destino = archivo_env_para(datos.camera_id)
    _actualizar_env(destino, {
        "CAMERA_HOST": datos.host,
        "CAMERA_USER": datos.user,
        "CAMERA_PASSWORD": datos.password,
        "CAMERA_ID": datos.camera_id,
        "SOURCE": url,
    })
    log.info("Camara '%s' (%s) guardada en %s por %s",
             datos.camera_id, datos.host, destino.name, admin.username)
    # La contrasena de la camara NO va a la bitacora.
    _auditar("camaras.guardar", admin.username, request, objetivo=datos.camera_id,
             detalle={"host": datos.host, "ruta": datos.ruta, "archivo": destino.name})
    comando = "iniciar_worker.bat" if destino == ENV_PATH else f"iniciar_worker.bat --env {destino.name}"
    return {"archivo": destino.name, "comando": comando}


def _auditar(accion: str, usuario: str, request: Request, **kwargs) -> None:
    """Estos endpoints no usan sesion de BD (son sincronos y largos); la
    bitacora abre la suya."""
    from sqlmodel import Session

    try:
        with Session(engine) as session:
            registrar(session, accion, usuario=usuario, request=request, confirmar=True, **kwargs)
    except Exception as e:  # noqa: BLE001 - la auditoria no debe tumbar la operacion
        log.error("No se pudo registrar %s en la bitacora: %s", accion, e)
