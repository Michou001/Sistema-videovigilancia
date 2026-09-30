"""Zonas y reglas por camara: alta, edicion, baja, lo que recibe el worker y
el conteo de las lineas de conteo.

Solo el administrador define reglas: una zona mal puesta genera alertas falsas
toda la noche (o, peor, ninguna). Cada cambio queda en la bitacora.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi import Path as PathParam
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlmodel import col, select

from api.auditoria import registrar
from api.deps import Admin, OperadorActual, SesionBD, verificar_worker
from api.hub import hub
from api.models import Camera, Event, FechasEnUtc, Zone
from api.zonas import zonas_de_camara
from shared.events import PATRON_CAMARA
from shared.fechas import a_utc, ahora_utc
from shared.zonas import (
    CLASES,
    DIRECCIONES,
    TIPOS,
    describir_horario,
    en_horario,
    proximo_cambio,
    validar_horario,
    validar_puntos,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/zonas", tags=["zonas"])


class ZonaEntrada(BaseModel):
    camera_id: str = Field(pattern=PATRON_CAMARA)
    nombre: str = Field(min_length=2, max_length=80)
    tipo: str
    puntos: list[list[float]] = Field(max_length=24)
    clases: list[str] = Field(default_factory=lambda: ["persona"])
    direccion: str = "ambas"
    segundos: Optional[int] = Field(default=None, ge=5, le=3600)
    horario: list[dict[str, Any]] = Field(default_factory=list, max_length=14)
    severidad: str = "warning"
    activa: bool = True

    @field_validator("nombre")
    @classmethod
    def _nombre(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 2:
            raise ValueError("el nombre es muy corto")
        return v

    @field_validator("tipo")
    @classmethod
    def _tipo(cls, v: str) -> str:
        if v not in TIPOS:
            raise ValueError(f"tipo debe ser uno de: {', '.join(TIPOS)}")
        return v

    @field_validator("clases")
    @classmethod
    def _clases(cls, v: list[str]) -> list[str]:
        v = sorted(set(v))
        if not v or any(c not in CLASES for c in v):
            raise ValueError(f"clases: al menos una de {', '.join(CLASES)}")
        return v

    @field_validator("direccion")
    @classmethod
    def _direccion(cls, v: str) -> str:
        if v not in DIRECCIONES:
            raise ValueError(f"dirección debe ser una de: {', '.join(DIRECCIONES)}")
        return v

    @field_validator("severidad")
    @classmethod
    def _severidad(cls, v: str) -> str:
        if v not in ("warning", "critical"):
            raise ValueError("severidad debe ser 'warning' o 'critical'")
        return v

    @field_validator("horario")
    @classmethod
    def _horario(cls, v: list[dict]) -> list[dict]:
        return validar_horario(v)

    @model_validator(mode="after")
    def _segun_tipo(self) -> "ZonaEntrada":
        self.puntos = validar_puntos(self.tipo, self.puntos)
        if self.tipo == "merodeo":
            self.segundos = self.segundos or 60
        else:
            self.segundos = None
        if self.tipo not in ("linea", "conteo"):
            self.direccion = "ambas"
        return self


class ZonaCambios(BaseModel):
    nombre: Optional[str] = None
    puntos: Optional[list[list[float]]] = None
    clases: Optional[list[str]] = None
    direccion: Optional[str] = None
    segundos: Optional[int] = None
    horario: Optional[list[dict[str, Any]]] = None
    severidad: Optional[str] = None
    activa: Optional[bool] = None


class ZonaLeida(FechasEnUtc, BaseModel):
    id: int
    camera_id: str
    nombre: str
    tipo: str
    puntos: list[list[float]]
    clases: list[str]
    direccion: str
    segundos: Optional[int]
    horario: list[dict]
    horario_texto: str
    severidad: str
    activa: bool
    armada: bool
    cambia_en: Optional[datetime] = None
    creada_por: Optional[str]
    created_at: datetime
    updated_at: datetime


def _leida(z: Zone, ahora: Optional[datetime] = None) -> ZonaLeida:
    ahora = ahora or ahora_utc()
    return ZonaLeida(
        id=z.id, camera_id=z.camera_id, nombre=z.nombre, tipo=z.tipo, puntos=z.puntos,
        clases=z.lista_clases, direccion=z.direccion, segundos=z.segundos, horario=z.horario,
        horario_texto=describir_horario(z.horario), severidad=z.severidad, activa=z.activa,
        armada=z.activa and z.tipo != "conteo" and en_horario(z.horario, ahora),
        cambia_en=proximo_cambio(z.horario, ahora) if z.activa else None,
        creada_por=z.creada_por, created_at=z.created_at, updated_at=z.updated_at,
    )


def _errores(e: ValidationError) -> str:
    return "; ".join(str(err.get("msg", "")).removeprefix("Value error, ") for err in e.errors()[:3])


def _aplicar(zona: Zone, datos: ZonaEntrada) -> None:
    zona.camera_id = datos.camera_id
    zona.nombre = datos.nombre
    zona.tipo = datos.tipo
    zona.puntos_json = json.dumps(datos.puntos)
    zona.clases = ",".join(datos.clases)
    zona.direccion = datos.direccion
    zona.segundos = datos.segundos
    zona.horario_json = json.dumps(datos.horario, ensure_ascii=False) if datos.horario else None
    zona.severidad = datos.severidad
    zona.activa = datos.activa
    zona.updated_at = ahora_utc()


def _resumen(zona: Zone) -> dict:
    return {"tipo": zona.tipo, "clases": zona.clases, "direccion": zona.direccion,
            "segundos": zona.segundos, "horario": describir_horario(zona.horario),
            "severidad": zona.severidad, "activa": zona.activa, "puntos": zona.puntos}


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

@router.get("", response_model=list[ZonaLeida])
def listar(session: SesionBD, _: OperadorActual,
           camera_id: Annotated[Optional[str], Query(pattern=PATRON_CAMARA)] = None):
    consulta = select(Zone).order_by(Zone.camera_id, Zone.id)
    if camera_id:
        consulta = consulta.where(Zone.camera_id == camera_id)
    ahora = ahora_utc()
    return [_leida(z, ahora) for z in session.exec(consulta).all()]


@router.post("", response_model=ZonaLeida, status_code=status.HTTP_201_CREATED)
async def crear(datos: ZonaEntrada, session: SesionBD, admin: Admin, request: Request):
    if session.exec(select(Camera).where(Camera.camera_id == datos.camera_id)).first() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa cámara")
    zona = Zone(camera_id=datos.camera_id, nombre=datos.nombre, tipo=datos.tipo, puntos_json="[]",
                creada_por=admin.username)
    _aplicar(zona, datos)
    session.add(zona)
    session.flush()
    registrar(session, "zonas.alta", usuario=admin.username, objetivo=f"{zona.camera_id}: {zona.nombre}",
              detalle=_resumen(zona), request=request)
    session.commit()
    session.refresh(zona)
    log.info("Zona '%s' (%s) creada en %s por %s", zona.nombre, zona.tipo, zona.camera_id, admin.username)
    await hub.difundir("zonas", {"camera_id": zona.camera_id})
    return _leida(zona)


@router.patch("/{zona_id}", response_model=ZonaLeida)
async def editar(zona_id: int, cambios: ZonaCambios, session: SesionBD, admin: Admin, request: Request):
    zona = session.get(Zone, zona_id)
    if zona is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa zona")
    actual = {"camera_id": zona.camera_id, "nombre": zona.nombre, "tipo": zona.tipo,
              "puntos": zona.puntos, "clases": zona.lista_clases, "direccion": zona.direccion,
              "segundos": zona.segundos, "horario": zona.horario, "severidad": zona.severidad,
              "activa": zona.activa}
    actual.update(cambios.model_dump(exclude_unset=True))
    try:
        datos = ZonaEntrada.model_validate(actual)
    except ValidationError as e:
        raise HTTPException(422, _errores(e)) from None
    antes = _resumen(zona)
    nombre_antes = zona.nombre
    _aplicar(zona, datos)
    despues = _resumen(zona)
    diferencias = {k: {"antes": antes[k], "despues": despues[k]} for k in despues if antes[k] != despues[k]}
    if zona.nombre != nombre_antes:
        diferencias["nombre"] = {"antes": nombre_antes, "despues": zona.nombre}
    if not diferencias:
        # Nada cambio: ni bitacora ni aviso a los dashboards.
        session.rollback()
        session.refresh(zona)
        return _leida(zona)
    registrar(session, "zonas.edicion", usuario=admin.username, objetivo=f"{zona.camera_id}: {nombre_antes}",
              detalle=diferencias, request=request)
    session.commit()
    session.refresh(zona)
    await hub.difundir("zonas", {"camera_id": zona.camera_id})
    return _leida(zona)


@router.delete("/{zona_id}", status_code=status.HTTP_204_NO_CONTENT)
async def borrar(zona_id: int, session: SesionBD, admin: Admin, request: Request) -> None:
    zona = session.get(Zone, zona_id)
    if zona is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa zona")
    camera_id = zona.camera_id
    registrar(session, "zonas.baja", usuario=admin.username, objetivo=f"{zona.camera_id}: {zona.nombre}",
              detalle=_resumen(zona), request=request)
    # Los eventos que la mencionan se quedan: son historia. Su alerta ya dice
    # el nombre de la zona en el titulo.
    session.delete(zona)
    session.commit()
    await hub.difundir("zonas", {"camera_id": camera_id})


@router.get("/{zona_id}/conteo")
def conteo(zona_id: int, session: SesionBD, _: OperadorActual,
           desde: Optional[datetime] = None, hasta: Optional[datetime] = None) -> dict:
    """Cuantos objetos cruzaron una linea de conteo, por sentido, clase y hora.
    Por defecto, las ultimas 24 h."""
    zona = session.get(Zone, zona_id)
    if zona is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa zona")
    hasta = a_utc(hasta) if hasta else ahora_utc()
    desde = a_utc(desde) if desde else hasta - timedelta(hours=24)
    if hasta - desde > timedelta(days=31):
        raise HTTPException(422, "El rango máximo es de 31 días")
    filas = session.exec(
        select(Event.ts, Event.meta_json).where(
            Event.camera_id == zona.camera_id, Event.type == "zone", Event.value == "conteo",
            col(Event.ts) >= desde, col(Event.ts) <= hasta,
        )
    ).all()
    total, por_sentido, por_clase, por_hora = 0, {}, {}, {}
    for ts, meta_json in filas:
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except ValueError:
            continue
        if meta.get("zona_id") != zona.id:
            continue
        total += 1
        sentido = meta.get("direccion") or "?"
        clase = meta.get("clase") or "?"
        por_sentido[sentido] = por_sentido.get(sentido, 0) + 1
        por_clase[clase] = por_clase.get(clase, 0) + 1
        hora = a_utc(ts).replace(minute=0, second=0, microsecond=0).isoformat()
        por_hora[hora] = por_hora.get(hora, 0) + 1
    return {"zona_id": zona.id, "nombre": zona.nombre, "desde": desde, "hasta": hasta, "total": total,
            "por_sentido": por_sentido, "por_clase": por_clase,
            "por_hora": [{"hora": h, "n": n} for h, n in sorted(por_hora.items())]}


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------

@router.get("/borde/{camera_id}", dependencies=[Depends(verificar_worker)])
def para_worker(camera_id: Annotated[str, PathParam(pattern=PATRON_CAMARA)], session: SesionBD) -> dict:
    """Zonas activas de la camara, para el motor de reglas del worker. La
    version cambia solo cuando cambia alguna zona: el worker pregunta cada
    pocos segundos y solo recarga si hace falta."""
    version, zonas = zonas_de_camara(session, camera_id)
    return {"version": version, "zonas": zonas,
            "servidor": datetime.now(timezone.utc).isoformat()}
