"""Evidencia (fotos y clips) servida SOLO a usuarios con sesion.

Antes la carpeta data/snapshots se montaba como archivos estaticos publicos:
cualquiera que llegara al puerto 8000 con la URL de una foto la veia, sin
iniciar sesion. Son fotos de personas y vehiculos -- datos personales -- y las
URLs se filtraban por el historial del navegador y por los reportes CSV.

Ahora cada archivo pasa por aqui: se exige sesion (cabecera o cookie HttpOnly,
porque un <img> no manda cabeceras), el nombre se valida contra un patron
estricto y la ruta resuelta tiene que quedar dentro de la carpeta de evidencia.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from api.config import get_config
from api.deps import OperadorLectura

router = APIRouter(tags=["evidencia"])

_NOMBRE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,120}\.(jpg|jpeg|png|mp4|webm)")
_TIPOS = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
          "mp4": "video/mp4", "webm": "video/webm"}


def _carpetas() -> list[Path]:
    cfg = get_config()
    return [cfg.snapshot_dir, cfg.clips_dir]


def ruta_evidencia(nombre: str) -> Path | None:
    """Ruta del archivo de evidencia `nombre`, o None si no es valido o no existe."""
    if not _NOMBRE.fullmatch(nombre) or ".." in nombre:
        return None
    for carpeta in _carpetas():
        base = carpeta.resolve()
        ruta = (base / nombre).resolve()
        if ruta.parent != base:
            continue
        if ruta.is_file():
            return ruta
    return None


@router.get("/media/{nombre}")
def evidencia(nombre: str, _: OperadorLectura) -> FileResponse:
    ruta = ruta_evidencia(nombre)
    if ruta is None:
        # 404 tambien para nombres invalidos: no se distingue "no existe" de
        # "no te dejo verlo", para no dar pistas sobre la estructura de disco.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa evidencia")
    extension = ruta.suffix.lower().lstrip(".")
    return FileResponse(
        ruta,
        media_type=_TIPOS.get(extension, "application/octet-stream"),
        headers={
            # "private": ningun proxy intermedio debe guardar copia de una foto
            # de evidencia. El navegador del operador si, un rato.
            "Cache-Control": "private, max-age=3600",
        },
    )
