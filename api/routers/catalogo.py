"""Catalogo de camaras en el dashboard: descubrir, diagnosticar, recomendar y dar de alta.

    GET  /api/cameras/discover         equipos en la LAN, con marca/modelo si se pueden saber
    POST /api/cameras/probe            diagnostico con credenciales (o de una fuente manual)
    POST /api/cameras/recommend        usos recomendados segun altura, distancia y lente
    POST /api/cameras/register         alta: archivo de entorno del worker + ficha en la BD
    GET  /api/cameras/catalog          camaras registradas con su ficha, funciones y familias
    POST /api/cameras/{id}/worker      inicia el worker de esa camara en este equipo
    DELETE /api/cameras/{id}/worker    lo detiene (solo si lo inicio el dashboard)

La logica de red vive en api/catalogo_camaras.py y la de instalacion en
shared/instalacion.py; aqui solo se valida, se audita y se guarda. Todo es
para administradores: escanea la red y prueba credenciales contra equipos.

Los endpoints que tocan la red son sincronos (def, no async def) a proposito:
FastAPI los corre en un hilo aparte y un escaneo de ~5 s no congela la vista
en vivo de los demas.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, status
from fastapi import Path as PathParam
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlmodel import Session, select

from api import catalogo_camaras as catalogo
from api.auditoria import registrar
from api.config import BASE_DIR
from api.database import engine
from api.deps import Admin, SesionBD
from api.models import Camera
from api.routers.camera_setup import ENV_PATH, _actualizar_env, _leer_env, _sin_control, archivo_env_para
from shared.events import PATRON_CAMARA
from shared.instalacion import DETECTORES, FUNCIONES, LENTES_HFOV, DatosInvalidos, recomendar

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cameras", tags=["catalogo de camaras"])

_HOST_VALIDO = re.compile(r"[A-Za-z0-9.\-:\[\]]{3,100}")
_RUTA_VALIDA = re.compile(r"/[A-Za-z0-9_\-./?=&%:,;~+]{0,239}")
_ID_VALIDO = re.compile(r"[A-Za-z0-9_\-]{2,64}")
CARPETAS_DEMO = ("demo", "videos")
EXTENSIONES_VIDEO = {".mp4", ".mkv", ".avi", ".mov", ".webm"}


# --------------------------------------------------------------------------
# Camaras ya registradas: en la BD y en los archivos de entorno
# --------------------------------------------------------------------------

def archivos_env() -> list[Path]:
    """`.env` y los `.env.<camara>` (sin respaldos ni el ejemplo)."""
    salida = [ENV_PATH] if ENV_PATH.exists() else []
    for f in sorted(BASE_DIR.glob(".env.*")):
        if f.name == ".env.example" or f.suffix in {".bak", ".example"} or "bak" in f.name:
            continue
        salida.append(f)
    return salida


def _host_de(valores: dict[str, str]) -> Optional[str]:
    fuente = valores.get("SOURCE", "")
    if fuente.startswith(("rtsp://", "rtsps://")):
        return urlparse(fuente).hostname
    return valores.get("CAMERA_HOST") or None


def camaras_en_entorno() -> dict[str, dict]:
    """camera_id -> {archivo, host, fuente_tipo} segun los archivos de entorno."""
    salida = {}
    for f in archivos_env():
        v = _leer_env(f)
        cid = v.get("CAMERA_ID") or ("cam-01" if f == ENV_PATH else None)
        if not cid or cid in salida:
            continue
        fuente = v.get("SOURCE", "")
        tipo = ("rtsp" if fuente.startswith(("rtsp://", "rtsps://")) else
                "webcam" if fuente.startswith("webcam:") or fuente.isdigit() else
                "archivo" if fuente else "sin fuente")
        salida[cid] = {"archivo": f, "host": _host_de(v), "tipo": tipo}
    return salida


def siguiente_id(usados: set[str]) -> str:
    n = 1
    while f"cam-{n:02d}" in usados:
        n += 1
    return f"cam-{n:02d}"


def _ids_en_uso(session: Session) -> set[str]:
    return {c.camera_id for c in session.exec(select(Camera)).all()} | set(camaras_en_entorno())


def _auditar(accion: str, usuario: str, request: Request, **kwargs) -> None:
    try:
        with Session(engine) as s:
            registrar(s, accion, usuario=usuario, request=request, confirmar=True, **kwargs)
    except Exception as e:  # noqa: BLE001 - la auditoria no debe tumbar la operacion
        log.error("No se pudo registrar %s en la bitacora: %s", accion, e)


# --------------------------------------------------------------------------
# Descubrir
# --------------------------------------------------------------------------

@router.get("/discover")
def descubrir(admin: Admin, request: Request) -> dict:
    registradas = {d["host"]: cid for cid, d in camaras_en_entorno().items() if d["host"]}
    try:
        resultado = catalogo.descubrir(registradas=registradas)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(422, str(e)) from None
    with Session(engine) as s:
        resultado["id_sugerido"] = siguiente_id(_ids_en_uso(s))
    camaras = sum(1 for d in resultado["dispositivos"] if d["es_camara"])
    _auditar("camaras.descubrir", admin.username, request,
             detalle={"subred": resultado["subred"], "equipos": len(resultado["dispositivos"]),
                      "camaras": camaras})
    return resultado


# --------------------------------------------------------------------------
# Diagnosticar
# --------------------------------------------------------------------------

def validar_fuente_manual(fuente: str) -> str:
    """webcam:N, file:<video dentro de demo/ o videos/> o una URL rtsp:// de la LAN."""
    fuente = fuente.strip()
    if re.fullmatch(r"webcam:\d{1,2}", fuente):
        return fuente
    if fuente.startswith("file:"):
        relativa = Path(fuente[5:].strip().replace("\\", "/"))
        destino = (BASE_DIR / relativa).resolve()
        permitidas = [(BASE_DIR / c).resolve() for c in CARPETAS_DEMO]
        if not any(destino.is_relative_to(p) for p in permitidas):
            raise ValueError("El video debe estar dentro de las carpetas demo/ o videos/ del proyecto.")
        if destino.suffix.lower() not in EXTENSIONES_VIDEO or not destino.is_file():
            raise ValueError("No encontré ese video (mp4, mkv, avi, mov o webm).")
        return "file:" + destino.relative_to(BASE_DIR.resolve()).as_posix()
    if fuente.startswith(("rtsp://", "rtsps://")):
        u = urlparse(fuente)
        if not u.hostname or not catalogo.es_direccion_local(u.hostname):
            raise ValueError("La cámara debe estar en la red local.")
        if any(ord(c) < 33 for c in fuente):
            raise ValueError("La URL no puede llevar espacios ni saltos de línea.")
        return fuente
    raise ValueError("Fuente no válida: usa rtsp://…, webcam:0 o file:demo/video.mp4")


class DiagnosticoIn(BaseModel):
    modo: Literal["red", "manual"] = "red"
    host: Optional[str] = Field(default=None, max_length=100)
    user: str = Field(default="admin", max_length=64)
    password: str = Field(default="", max_length=200)
    puerto_rtsp: int = Field(default=554, ge=1, le=65535)
    puerto_http: int = Field(default=80, ge=1, le=65535)
    onvif_xaddr: Optional[str] = Field(default=None, max_length=300)
    fuente: Optional[str] = Field(default=None, max_length=400)

    @field_validator("user", "password")
    @classmethod
    def _texto(cls, v: str) -> str:
        return _sin_control(v)

    @model_validator(mode="after")
    def _segun_modo(self):
        if self.modo == "red":
            if not self.host or not _HOST_VALIDO.fullmatch(self.host.strip()):
                raise ValueError("IP o nombre de host no válido")
            self.host = self.host.strip()
            if not self.password:
                raise ValueError("Falta la contraseña de la cámara")
        else:
            if not self.fuente:
                raise ValueError("Indica la fuente: rtsp://…, webcam:0 o file:demo/video.mp4")
            self.fuente = validar_fuente_manual(self.fuente)
        return self


@router.post("/probe")
def diagnosticar(datos: DiagnosticoIn, admin: Admin, request: Request) -> dict:
    if datos.modo == "red":
        if not catalogo.es_direccion_local(datos.host):
            raise HTTPException(422, "La cámara debe estar en la red local (IP privada).")
        resultado = catalogo.diagnosticar(datos.host, datos.user, datos.password,
                                          datos.puerto_rtsp, datos.puerto_http, datos.onvif_xaddr)
        objetivo = datos.host
    else:
        resultado = catalogo.diagnosticar_manual(ruta_para_abrir(datos.fuente))
        resultado["fuente"] = datos.fuente
        objetivo = catalogo_fuente_censurada(datos.fuente)

    registradas = camaras_en_entorno()
    resultado["registrada"] = next((cid for cid, d in registradas.items()
                                    if d["host"] and d["host"] == resultado.get("host")), None)
    with Session(engine) as s:
        resultado["id_sugerido"] = resultado["registrada"] or siguiente_id(_ids_en_uso(s))
    # La contrasena no va a la bitacora; el resultado si (sin la miniatura).
    _auditar("camaras.diagnosticar", admin.username, request, objetivo=objetivo,
             detalle={"estado": resultado["estado"], "credenciales": resultado.get("credenciales"),
                      "modelo": (resultado["dispositivo"].get("modelo") or {}).get("valor")})
    return resultado


def ruta_para_abrir(fuente: str) -> str:
    """Los videos se guardan relativos al proyecto (el .env sirve en otra
    computadora), pero se abren con su ruta completa: la API puede correr
    desde otra carpeta."""
    if fuente.startswith("file:"):
        return "file:" + str(BASE_DIR / fuente[5:])
    return fuente


def catalogo_fuente_censurada(fuente: str) -> str:
    from tools.probe_camara import ocultar_password

    return ocultar_password(fuente)


# --------------------------------------------------------------------------
# Recomendar
# --------------------------------------------------------------------------

class RecomendacionIn(BaseModel):
    funcion: str = Field(max_length=32)
    altura_m: float
    distancia_m: float
    ancho_px: int
    lente_mm: Optional[float] = None
    hfov_grados: Optional[float] = None
    angulo_horizontal: float = 0.0
    ancho_px_principal: Optional[int] = Field(default=None, ge=160, le=8192)
    codec: Optional[str] = Field(default=None, max_length=20)


@router.post("/recommend")
def recomendar_instalacion(datos: RecomendacionIn, _: Admin) -> dict:
    try:
        return recomendar(**datos.model_dump())
    except DatosInvalidos as e:
        raise HTTPException(422, str(e)) from None


# --------------------------------------------------------------------------
# Alta
# --------------------------------------------------------------------------

class DatoIn(BaseModel):
    valor: str = Field(max_length=120)
    fuente: str = Field(max_length=40)


class PerfilIn(BaseModel):
    clave: Optional[str] = Field(default=None, max_length=64)
    nombre: Optional[str] = Field(default=None, max_length=64)
    codec: Optional[str] = Field(default=None, max_length=20)
    ancho: Optional[int] = Field(default=None, ge=0, le=16384)
    alto: Optional[int] = Field(default=None, ge=0, le=16384)
    fps: Optional[float] = Field(default=None, ge=0, le=500)
    fuente: Optional[str] = Field(default=None, max_length=20)


class RecomendacionResumen(BaseModel):
    dori: Optional[str] = Field(default=None, max_length=40)
    altura_m: Optional[float] = None
    distancia_m: Optional[float] = None
    hfov_grados: Optional[float] = None
    px_por_metro: Optional[float] = None
    placa_px: Optional[float] = None
    rostro_px: Optional[float] = None
    angulo_vertical: Optional[float] = None


class FichaIn(BaseModel):
    estado: Literal["identificada", "parcial", "manual"] = "manual"
    fabricante: Optional[DatoIn] = None
    modelo: Optional[DatoIn] = None
    firmware: Optional[DatoIn] = None
    serie: Optional[DatoIn] = None
    mac: Optional[DatoIn] = None
    familia: Optional[str] = Field(default=None, max_length=40)
    familia_nombre: Optional[str] = Field(default=None, max_length=80)
    perfil: Optional[PerfilIn] = None
    recomendacion: Optional[RecomendacionResumen] = None


class AltaIn(BaseModel):
    camera_id: str = Field(max_length=64)
    nombre: str = Field(min_length=1, max_length=80)
    ubicacion: Optional[str] = Field(default=None, max_length=160)
    funcion: Optional[str] = Field(default=None, max_length=32)
    detectores: dict[str, bool] = Field(default_factory=dict)
    modo: Literal["red", "manual"] = "red"
    # Modo red
    host: Optional[str] = Field(default=None, max_length=100)
    user: str = Field(default="admin", max_length=64)
    password: str = Field(default="", max_length=200)
    puerto: int = Field(default=554, ge=1, le=65535)
    ruta: Optional[str] = Field(default=None, max_length=240)
    canal_principal: Optional[str] = Field(default=None, max_length=8)
    isapi: bool = False
    # Modo manual
    fuente: Optional[str] = Field(default=None, max_length=400)
    ficha: FichaIn = Field(default_factory=FichaIn)

    @field_validator("camera_id")
    @classmethod
    def _id(cls, v: str) -> str:
        v = v.strip()
        if not _ID_VALIDO.fullmatch(v):
            raise ValueError("el identificador solo admite letras, números, '-' y '_' (2 a 64)")
        return v

    @field_validator("user", "password", "nombre")
    @classmethod
    def _texto(cls, v: str) -> str:
        return _sin_control(v)

    @field_validator("funcion")
    @classmethod
    def _funcion(cls, v: Optional[str]) -> Optional[str]:
        if v and v not in FUNCIONES:
            raise ValueError("función desconocida")
        return v or None

    @field_validator("detectores")
    @classmethod
    def _detectores(cls, v: dict[str, bool]) -> dict[str, bool]:
        desconocidos = set(v) - set(DETECTORES)
        if desconocidos:
            raise ValueError(f"detectores desconocidos: {', '.join(sorted(desconocidos))}")
        return v

    @model_validator(mode="after")
    def _segun_modo(self):
        if self.modo == "red":
            if not self.host or not _HOST_VALIDO.fullmatch(self.host.strip()):
                raise ValueError("IP o nombre de host no válido")
            if not self.password:
                raise ValueError("Falta la contraseña de la cámara")
            if not self.ruta or not _RUTA_VALIDA.fullmatch(self.ruta):
                raise ValueError("Ruta RTSP no válida: elige un stream del diagnóstico")
            if self.canal_principal and not self.canal_principal.isdigit():
                raise ValueError("Canal principal no válido")
            self.host = self.host.strip()
        else:
            if not self.fuente:
                raise ValueError("Falta la fuente de video")
            self.fuente = validar_fuente_manual(self.fuente)
        return self


def _cambios_env(datos: AltaIn) -> dict[str, str]:
    from urllib.parse import quote

    cambios = {"CAMERA_ID": datos.camera_id}
    if datos.modo == "red":
        cambios["SOURCE"] = (f"rtsp://{quote(datos.user, safe='')}:{quote(datos.password, safe='')}"
                             f"@{datos.host}:{datos.puerto}{datos.ruta}")
        cambios.update(CAMERA_HOST=datos.host, CAMERA_USER=datos.user, CAMERA_PASSWORD=datos.password)
        # Eventos y foto HD por ISAPI solo en Hikvision; en otras marcas serian
        # peticiones que siempre fallan.
        cambios["ISAPI"] = "auto" if datos.isapi else "false"
        cambios["SNAPSHOT_HD_ENABLED"] = "true" if datos.isapi and datos.canal_principal else "false"
        if datos.isapi and datos.canal_principal:
            cambios["SNAPSHOT_HD_CHANNEL"] = datos.canal_principal
    else:
        cambios["SOURCE"] = datos.fuente
        # Un archivo copiado de .env traeria la contrasena de otra camara.
        cambios.update(CAMERA_HOST="", CAMERA_USER="", CAMERA_PASSWORD="", ISAPI="false",
                       SNAPSHOT_HD_ENABLED="false")
        es_video = datos.fuente.startswith("file:")
        # Un video de demostracion se comporta como camara: a velocidad real y en bucle.
        cambios["SOURCE_LOOP"] = "true" if es_video else "false"
        cambios["SOURCE_REALTIME"] = "true" if es_video else "false"
    for clave in DETECTORES:
        if clave in datos.detectores:
            cambios[clave] = "true" if datos.detectores[clave] else "false"
    cambios["ENABLE_WEAPONS"] = "false"
    return cambios


@router.post("/register", status_code=status.HTTP_201_CREATED)
def dar_de_alta(datos: AltaIn, admin: Admin, request: Request, session: SesionBD) -> dict:
    if datos.modo == "red" and not catalogo.es_direccion_local(datos.host):
        raise HTTPException(422, "La cámara debe estar en la red local (IP privada).")

    destino = archivo_env_para(datos.camera_id)
    _actualizar_env(destino, _cambios_env(datos))

    camara = session.exec(select(Camera).where(Camera.camera_id == datos.camera_id)).first()
    if camara is None:
        camara = Camera(camera_id=datos.camera_id, name=datos.nombre)
        session.add(camara)
    camara.name = datos.nombre.strip()
    camara.location = (datos.ubicacion or "").strip() or None
    camara.funcion = datos.funcion
    camara.ficha_json = datos.ficha.model_dump_json(exclude_none=True)
    camara.enabled = True
    registrar(session, "camaras.alta", usuario=admin.username, objetivo=datos.camera_id, request=request,
              detalle={"archivo": destino.name, "modo": datos.modo, "host": datos.host,
                       "funcion": datos.funcion, "estado": datos.ficha.estado,
                       "detectores": {k: v for k, v in datos.detectores.items() if v}})
    session.commit()
    log.info("Camara '%s' dada de alta en %s por %s", datos.camera_id, destino.name, admin.username)

    comando = "iniciar_worker.bat" if destino == ENV_PATH else f"iniciar_worker.bat --env {destino.name}"
    return {"camera_id": datos.camera_id, "archivo": destino.name, "comando": comando,
            "puede_iniciar": puede_iniciar_workers()}


# --------------------------------------------------------------------------
# Catalogo de las registradas
# --------------------------------------------------------------------------

def _ficha(camara: Camera) -> dict:
    try:
        return json.loads(camara.ficha_json) if camara.ficha_json else {}
    except ValueError:
        return {}


@router.get("/catalog")
def catalogo_registradas(_: Admin, session: SesionBD) -> dict:
    entorno = camaras_en_entorno()
    ahora = datetime.now(timezone.utc)
    filas = []
    vistas = set()
    for c in session.exec(select(Camera).order_by(Camera.camera_id)).all():
        vistas.add(c.camera_id)
        visto = c.last_heartbeat
        if visto is not None and visto.tzinfo is None:
            visto = visto.replace(tzinfo=timezone.utc)
        segundos = (ahora - visto).total_seconds() if visto else None
        try:
            salud = json.loads(c.status_json) if c.status_json else {}
        except ValueError:
            salud = {}
        env = entorno.get(c.camera_id)
        filas.append({
            "camera_id": c.camera_id, "name": c.name, "location": c.location,
            "funcion": c.funcion,
            "funcion_nombre": FUNCIONES[c.funcion].nombre if c.funcion in FUNCIONES else None,
            "ficha": _ficha(c),
            "online": segundos is not None and segundos < 60 and salud.get("connected", True) is not False,
            "segundos_sin_senal": round(segundos) if segundos is not None else None,
            "fps": salud.get("fps_procesados"),
            "archivo": env["archivo"].name if env else None,
            "host": env["host"] if env else None,
            "tipo_fuente": env["tipo"] if env else None,
            "worker_local": _worker_vivo(c.camera_id),
        })
    for cid, env in entorno.items():
        if cid not in vistas:
            filas.append({"camera_id": cid, "name": cid, "location": None, "funcion": None,
                          "funcion_nombre": None, "ficha": {}, "online": False,
                          "segundos_sin_senal": None, "fps": None, "archivo": env["archivo"].name,
                          "host": env["host"], "tipo_fuente": env["tipo"],
                          "worker_local": _worker_vivo(cid)})
    return {
        "camaras": filas,
        "funciones": [{"clave": f.clave, "nombre": f.nombre, "nota": f.nota,
                       "detectores": {d: bool(f.detectores.get(d)) for d in DETECTORES}}
                      for f in FUNCIONES.values()],
        "lentes": [{"mm": mm, "hfov": h} for mm, h in LENTES_HFOV.items()],
        "familias": [{k: v for k, v in f.items() if k != "patron"} for f in catalogo.FAMILIAS],
        "puede_iniciar": puede_iniciar_workers(),
        "videos_demo": videos_demo(),
    }


def videos_demo() -> list[str]:
    salida = []
    for carpeta in CARPETAS_DEMO:
        base = BASE_DIR / carpeta
        if base.is_dir():
            salida += [f"file:{p.relative_to(BASE_DIR).as_posix()}" for p in sorted(base.rglob("*"))
                       if p.suffix.lower() in EXTENSIONES_VIDEO and p.is_file()]
    return salida[:50]


# --------------------------------------------------------------------------
# Iniciar / detener el worker de una camara desde el dashboard
# --------------------------------------------------------------------------

_procesos: dict[str, subprocess.Popen] = {}
_candado = threading.Lock()


def puede_iniciar_workers() -> bool:
    """Solo cuando la API corre en el mismo equipo que los workers (la laptop
    de la demo, un servidor sin contenedores). En Docker los workers son
    servicios aparte y la API no debe lanzar procesos."""
    valor = os.getenv("PERMITIR_INICIAR_WORKER", "auto").strip().lower()
    if valor in ("0", "false", "no", "off"):
        return False
    if valor in ("1", "true", "si", "yes", "on"):
        return True
    return not Path("/.dockerenv").exists()


def _worker_vivo(camera_id: str) -> bool:
    p = _procesos.get(camera_id)
    return p is not None and p.poll() is None


IdCamara = Annotated[str, PathParam(pattern=PATRON_CAMARA)]


@router.post("/{camera_id}/worker")
def iniciar_worker(camera_id: IdCamara, admin: Admin, request: Request, session: SesionBD) -> dict:
    if not puede_iniciar_workers():
        raise HTTPException(403, "En este equipo los workers se inician aparte (servicio o Docker).")
    env = camaras_en_entorno().get(camera_id)
    if env is None:
        raise HTTPException(404, "Esa cámara no tiene archivo de configuración: dala de alta primero.")
    camara = session.exec(select(Camera).where(Camera.camera_id == camera_id)).first()
    if camara and camara.last_heartbeat:
        visto = camara.last_heartbeat
        if visto.tzinfo is None:
            visto = visto.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - visto).total_seconds() < 45:
            raise HTTPException(409, "Esa cámara ya está reportando: su worker ya corre.")

    with _candado:
        if _worker_vivo(camera_id):
            raise HTTPException(409, "El worker de esa cámara ya se está iniciando.")
        comando = [sys.executable, "-m", "edge.worker"]
        if env["archivo"] != ENV_PATH:
            comando += ["--env", env["archivo"].name]
        opciones: dict = {"cwd": str(BASE_DIR)}
        if os.name == "nt":
            # Ventana propia: el equipo ve el log del worker, como con el .bat.
            opciones["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        else:
            logs = BASE_DIR / "data" / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            salida = open(logs / f"worker-{camera_id}.log", "ab")  # noqa: SIM115 - vive con el proceso
            opciones.update(stdout=salida, stderr=subprocess.STDOUT, start_new_session=True)
        _procesos[camera_id] = subprocess.Popen(comando, **opciones)  # noqa: S603 - comando fijo

    _auditar("camaras.worker_iniciar", admin.username, request, objetivo=camera_id,
             detalle={"archivo": env["archivo"].name, "pid": _procesos[camera_id].pid})
    log.info("Worker de %s iniciado desde el dashboard por %s (pid %d)",
             camera_id, admin.username, _procesos[camera_id].pid)
    return {"camera_id": camera_id, "pid": _procesos[camera_id].pid,
            "mensaje": "Worker iniciado: la cámara aparece en Monitoreo en cuanto cargue los modelos "
                       "(unos segundos)."}


@router.delete("/{camera_id}/worker")
def detener_worker(camera_id: IdCamara, admin: Admin, request: Request) -> dict:
    with _candado:
        proceso = _procesos.get(camera_id)
        if proceso is None or proceso.poll() is not None:
            raise HTTPException(404, "Ese worker no lo inició el dashboard (o ya terminó): "
                                     "ciérralo desde su ventana.")
        proceso.terminate()
        try:
            proceso.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proceso.kill()
        _procesos.pop(camera_id, None)
    _auditar("camaras.worker_detener", admin.username, request, objetivo=camera_id)
    log.info("Worker de %s detenido desde el dashboard por %s", camera_id, admin.username)
    return {"camera_id": camera_id, "detenido": True}
