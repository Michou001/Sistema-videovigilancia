"""Consulta y exportacion de la bitacora de auditoria (solo administradores)."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel
from sqlmodel import col, select

from api.auditoria import ACCIONES, registrar
from api.deps import Admin, SesionBD
from api.exportacion import celda
from api.models import AuditLog, FechasEnUtc
from shared.fechas import a_utc

router = APIRouter(prefix="/api/audit", tags=["auditoria"])


class EntradaLeida(FechasEnUtc, BaseModel):
    id: int
    ts: datetime
    usuario: Optional[str]
    accion: str
    objetivo: Optional[str]
    detalle: Optional[str]
    ip: Optional[str]


def _consulta(usuario, accion, desde, hasta):
    consulta = select(AuditLog)
    if usuario:
        consulta = consulta.where(AuditLog.usuario == usuario)
    if accion:
        consulta = consulta.where(col(AuditLog.accion).startswith(accion))
    if desde:
        consulta = consulta.where(col(AuditLog.ts) >= a_utc(desde))
    if hasta:
        consulta = consulta.where(col(AuditLog.ts) <= a_utc(hasta))
    return consulta.order_by(col(AuditLog.ts).desc(), col(AuditLog.id).desc())


@router.get("/acciones")
def acciones(_: Admin) -> dict[str, str]:
    """Nombres legibles de cada accion, para el filtro del dashboard."""
    return ACCIONES


@router.get("", response_model=list[EntradaLeida])
def listar(
    session: SesionBD,
    _: Admin,
    usuario: Optional[str] = Query(None, max_length=64),
    accion: Optional[str] = Query(None, max_length=64),
    desde: Optional[datetime] = None,
    hasta: Optional[datetime] = None,
    limite: int = Query(200, ge=1, le=2000),
):
    return session.exec(_consulta(usuario, accion, desde, hasta).limit(limite)).all()


@router.get("/export.csv")
def exportar(
    session: SesionBD,
    admin: Admin,
    request: Request,
    usuario: Optional[str] = Query(None, max_length=64),
    accion: Optional[str] = Query(None, max_length=64),
    desde: Optional[datetime] = None,
    hasta: Optional[datetime] = None,
):
    filas = session.exec(_consulta(usuario, accion, desde, hasta).limit(50_000)).all()
    salida = io.StringIO()
    escritor = csv.writer(salida)
    escritor.writerow(["fecha_hora_local", "usuario", "accion", "descripcion", "objetivo",
                       "detalle", "ip"])
    for e in filas:
        escritor.writerow([f"{a_utc(e.ts).astimezone():%Y-%m-%d %H:%M:%S}", e.usuario or "",
                           e.accion, ACCIONES.get(e.accion, ""), celda(e.objetivo),
                           celda(e.detalle), e.ip or ""])
    registrar(session, "auditoria.exportacion", usuario=admin.username,
              detalle={"filas": len(filas)}, request=request, confirmar=True)
    nombre = f"bitacora-{datetime.now():%Y%m%d-%H%M}.csv"
    return Response("\ufeff" + salida.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"'})
