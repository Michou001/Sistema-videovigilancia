"""Utilidades para los reportes CSV que se abren en Excel."""

from __future__ import annotations

from typing import Any


def celda(valor: Any) -> str:
    """Texto seguro para una celda de CSV.

    Excel ejecuta como formula cualquier celda que empiece con =, +, - o @.
    Un valor que viene de fuera (una lectura de OCR, el nombre que alguien le
    puso a una camara, un comentario) podria llevar una formula y ejecutarse
    en la maquina de quien abre el reporte ("inyeccion CSV"). Anteponer un
    apostrofo hace que Excel lo muestre como texto.
    """
    if valor is None:
        return ""
    texto = str(valor)
    if texto and texto[0] in "=+-@\t\r":
        return "'" + texto
    return texto
