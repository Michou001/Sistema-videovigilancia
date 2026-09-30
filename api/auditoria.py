"""Bitacora de auditoria: registrar quien hizo que.

Se llama desde los endpoints que tocan datos personales o la configuracion
(lista negra, alertas, reportes, usuarios, camaras). El registro va en la
MISMA sesion de base de datos que el cambio que describe: si el cambio se
confirma, su registro tambien; si falla, no queda una entrada de algo que no
paso.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import Request
from sqlmodel import Session

from api.models import AuditLog

log = logging.getLogger(__name__)

# Acciones que se registran. Tenerlas en un solo lugar permite filtrar la
# bitacora desde el dashboard con nombres legibles.
ACCIONES = {
    "sesion.login": "Inicio de sesión",
    "sesion.login_fallido": "Intento de sesión fallido",
    "sesion.logout": "Cierre de sesión",
    "sesion.cambio_password": "Cambio de contraseña",
    "usuarios.alta": "Alta de usuario",
    "usuarios.edicion": "Edición de usuario",
    "lista_negra.alta_placa": "Alta de placa en lista negra",
    "lista_negra.reactivacion_placa": "Reactivación de placa",
    "lista_negra.baja_placa": "Baja de placa",
    "lista_negra.alta_rostro": "Alta de persona en lista negra",
    "lista_negra.baja_rostro": "Baja de persona",
    "alertas.atendida": "Alerta atendida",
    "alertas.descartada": "Alerta descartada",
    "eventos.correccion": "Corrección de lectura",
    "reportes.csv": "Exportación de reporte",
    "reportes.dataset": "Exportación de dataset",
    "camaras.edicion": "Edición de cámara",
    "camaras.descubrir": "Búsqueda de cámaras en la red",
    "camaras.probar": "Prueba de conexión a cámara",
    "camaras.guardar": "Alta de cámara",
    "zonas.alta": "Alta de zona",
    "zonas.edicion": "Edición de zona",
    "zonas.baja": "Baja de zona",
    "auditoria.exportacion": "Exportación de la bitácora",
    "notificaciones.prueba": "Prueba de notificaciones",
    "busqueda.semantica": "Búsqueda por descripción",
}


def ip_de(request: Optional[Request]) -> Optional[str]:
    if request is None or request.client is None:
        return None
    return request.client.host


def registrar(
    session: Session,
    accion: str,
    *,
    usuario: Optional[str] = None,
    objetivo: Optional[str] = None,
    detalle: Any = None,
    request: Optional[Request] = None,
    confirmar: bool = False,
) -> AuditLog:
    """Agrega una entrada a la bitacora.

    `confirmar=True` hace commit de inmediato: para acciones que no cambian
    nada mas (un login fallido, una exportacion). En el resto se deja que el
    endpoint confirme junto con su propio cambio.
    """
    if accion not in ACCIONES:
        # Un nombre de accion mal escrito no debe tumbar el endpoint, pero si
        # quedar visible para corregirlo.
        log.warning("Accion de auditoria no catalogada: %s", accion)
    if detalle is not None and not isinstance(detalle, str):
        detalle = json.dumps(detalle, ensure_ascii=False, default=str)
    entrada = AuditLog(
        usuario=usuario,
        accion=accion,
        objetivo=(objetivo or None) and str(objetivo)[:200],
        detalle=(detalle or None) and detalle[:2000],
        ip=ip_de(request),
    )
    session.add(entrada)
    if confirmar:
        session.commit()
    return entrada
