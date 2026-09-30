"""Como se redacta una alerta: titulo, detalle y el mensaje que va al dashboard.

Vive aparte de la ingesta porque hay mas de un camino que crea o actualiza
alertas: la ingesta del worker, la correccion de una lectura por el operador
y el re-escaneo del pasado al dar de alta una placa.
"""

from __future__ import annotations

from typing import Optional

from api.models import Alert
from shared.events import DetectionEvent, EventType, MatchKind, MatchResult, Severity

TITULOS = {
    EventType.PLATE: "Placa {valor} en lista negra",
    EventType.FACE: "Persona identificada: {valor}",
    EventType.WEAPON: "ARMA DETECTADA: {valor}",
    EventType.ANOMALY: "Movimiento súbito detectado",
}

TITULOS_POR_VALOR = {
    "persona_caida": "Posible persona caída",
    "manos_arriba": "Posible asalto: persona con las manos arriba",
}


def titulo(evento: DetectionEvent, resultado: MatchResult) -> str:
    """Titulo de la alerta tal como lo vera el operador.

    Una coincidencia DIFUSA nunca debe titularse como si fuera un hecho. Decir
    "Placa ABC-123 en lista negra" cuando en realidad se leyo "ABD-123" lleva a
    actuar contra el vehiculo equivocado: el operador ve el titulo y reacciona,
    no siempre lee el detalle. El titulo tiene que cargar la incertidumbre.
    """
    if resultado.match_kind == MatchKind.FUZZY:
        return f"Posible placa {resultado.matched_value} (se leyó {evento.value})"
    if resultado.titulo:
        return resultado.titulo
    if evento.value in TITULOS_POR_VALOR:
        return TITULOS_POR_VALOR[evento.value]
    etiqueta = resultado.matched_value or evento.value
    return TITULOS.get(evento.type, "Detección {valor}").format(valor=etiqueta)


def descripcion_vehiculo(meta: dict) -> str:
    """Lo primero que pregunta quien sale a buscar el vehiculo: color, tipo y
    de donde es la placa. "Vehículo gris (color aprox.) · Automóvil particular
    de Jalisco"."""
    partes = []
    if meta.get("color_vehiculo"):
        partes.append(f"Vehículo {meta['color_vehiculo']} (color aprox.)")
    tipo = meta.get("tipo_placa")
    if meta.get("pais") and meta["pais"] != "México":
        partes.append("Placa extranjera" if meta["pais"] == "Extranjera" else f"Placa de {meta['pais']}")
    elif tipo:
        partes.append(f"{tipo} de {meta['entidad']}" if meta.get("entidad") else tipo)
    return " · ".join(partes)


def detalle(evento: DetectionEvent, resultado: MatchResult) -> str:
    texto = resultado.reason or ""
    if evento.type == EventType.PLATE:
        extra = descripcion_vehiculo(evento.meta or {})
        if extra:
            texto = f"{texto} · {extra}".lstrip(" ·")
    return texto


def nueva_alerta(evento: DetectionEvent, resultado: MatchResult,
                 captura: Optional[str]) -> Optional[Alert]:
    """La alerta que corresponde a un evento, o None si no amerita."""
    if resultado.severity == Severity.INFO:
        return None
    return Alert(
        event_id=evento.event_id,
        camera_id=evento.camera_id,
        type=evento.type.value,
        severity=resultado.severity.value,
        title=titulo(evento, resultado),
        detail=detalle(evento, resultado),
        match_kind=resultado.match_kind.value,
        match_score=resultado.score,
        snapshot_path=captura,
    )


def mensaje(alerta: Alert, ts) -> dict:
    """Lo que se difunde por WebSocket. Se arma DESPUES del commit: el `id`
    de la alerta lo asigna la base de datos, y es lo que el dashboard necesita
    para los botones de "Atendida" y "Falso positivo"."""
    return {
        "id": alerta.id,
        "title": alerta.title,
        "detail": alerta.detail,
        "severity": alerta.severity,
        "type": alerta.type,
        "camera_id": alerta.camera_id,
        "event_id": alerta.event_id,
        "snapshot_path": alerta.snapshot_path,
        "clip_path": getattr(alerta, "clip_path", None),
        "match_kind": alerta.match_kind,
        "match_score": alerta.match_score,
        "status": alerta.status,
        "ts": ts,
    }
