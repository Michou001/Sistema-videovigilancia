"""Fechas: todo el sistema trabaja en UTC con zona explicita.

Por que existe: SQLModel 0.0.45 empezo a rechazar fechas SIN zona horaria al
escribir o comparar contra una columna de fecha ("Datetime values must have
timezone information"). El codigo quitaba la zona antes de comparar, porque
SQLite la pierde al guardar, y con una instalacion nueva la busqueda por
fechas del Registro respondia 500.

La regla desde entonces: cualquier fecha que entra al sistema (del worker, del
navegador, de la base de datos) pasa por `a_utc`. Una fecha sin zona se
interpreta como UTC, que es como se guardo; una con zona se convierte.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, overload


@overload
def a_utc(fecha: datetime) -> datetime: ...
@overload
def a_utc(fecha: None) -> None: ...


def a_utc(fecha: Optional[datetime]) -> Optional[datetime]:
    """La misma fecha, en UTC y con zona. None se queda en None."""
    if fecha is None:
        return None
    if fecha.tzinfo is None or fecha.utcoffset() is None:
        return fecha.replace(tzinfo=timezone.utc)
    return fecha.astimezone(timezone.utc)


def ahora_utc() -> datetime:
    return datetime.now(timezone.utc)
