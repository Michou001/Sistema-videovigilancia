"""Modelos compartidos entre camaras del mismo proceso.

Con un proceso por camara, cada worker cargaba su propia copia de todo:
InsightFace, el detector y el OCR de placas, mas su propio contexto de CUDA
(varios cientos de MB por si solo). En una GPU de 6 GB cabian unas 3 camaras
y el limite era la VRAM, no el computo.

Con varias camaras en UN proceso (python -m edge.worker --env .env --env
.env.cam2), los modelos de onnxruntime se cargan una sola vez y todas las
camaras los usan: una sesion de onnxruntime admite llamadas concurrentes a
run(). Los YOLO de Ultralytics NO: su tracker guarda estado dentro del objeto
del modelo, asi que cada camara conserva el suyo (pesan unos MB).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Hashable

log = logging.getLogger(__name__)

_candado = threading.Lock()
_modelos: dict[Hashable, Any] = {}
_usos: dict[Hashable, int] = {}


def compartido(clave: Hashable, fabrica: Callable[[], Any]) -> Any:
    """Devuelve el modelo para `clave`, creandolo la primera vez.

    La clave debe incluir todo lo que cambia el modelo (nombre, umbral,
    proveedores): dos camaras con configuraciones distintas no comparten.
    """
    with _candado:
        if clave not in _modelos:
            _modelos[clave] = fabrica()
            _usos[clave] = 0
        else:
            log.info("Modelo compartido reutilizado: %s", clave[0] if isinstance(clave, tuple) else clave)
        _usos[clave] += 1
        return _modelos[clave]


def cargados() -> dict[str, int]:
    """Que modelos hay en memoria y cuantas camaras usan cada uno."""
    with _candado:
        return {str(k[0] if isinstance(k, tuple) else k): n for k, n in _usos.items()}


def olvidar_todos() -> None:
    """Solo para pruebas."""
    with _candado:
        _modelos.clear()
        _usos.clear()
