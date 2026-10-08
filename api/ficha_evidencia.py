"""Ficha de evidencia de una alerta: lo que se entrega a una autoridad.

Un CSV dice "esta placa paso a tal hora"; para una denuncia hace falta mas:
la foto, el clip, en que camara y donde esta, que hizo el monitorista y a
quien se paso el caso. Todo va en un ZIP:

  ficha.html      imprimible (o "Guardar como PDF" desde el navegador)
  foto.jpg        la captura de la alerta, si sigue en disco
  clip.mp4        el video antes y despues del hecho, si lo hay
  SHA256SUMS.txt  huella de cada archivo, en el formato de `sha256sum -c`

La huella permite demostrar despues que lo entregado no se altero: la misma
lista queda en la bitacora (alertas.ficha_evidencia) con quien la descargo.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from api.auditoria import ACCIONES
from api.models import Alert, AuditLog, Camera, Event
from api.routers.media import ruta_evidencia
from shared.zonas import zona_horaria

DESTINOS = {
    "proteccion_universitaria": "Protección Universitaria",
    "911_c5": "911 / C5 Edomex",
    "c4_municipal": "C4 municipal",
    "fiscalia": "Fiscalía (denuncia)",
    "otro": "Otra instancia",
}

TIPOS_ALERTA = {"plate": "Placa", "face": "Rostro", "weapon": "Arma", "anomaly": "Movimiento",
                "zone": "Zona / regla", "camera": "Cámara"}
ESTADOS = {"new": "Sin atender", "acknowledged": "Atendida", "dismissed": "Descartada"}


def leer_canalizaciones(texto: Optional[str]) -> list[dict]:
    if not texto:
        return []
    try:
        datos = json.loads(texto)
    except ValueError:
        return []
    return datos if isinstance(datos, list) else []


def _local(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, str):
        try:
            valor = datetime.fromisoformat(valor)
        except ValueError:
            return valor
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return f"{valor.astimezone(zona_horaria()):%Y-%m-%d %H:%M:%S}"


def _sha256(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def _fila(etiqueta: str, valor) -> str:
    if valor in (None, ""):
        return ""
    return f"<tr><th>{html.escape(etiqueta)}</th><td>{html.escape(str(valor))}</td></tr>"


def armar_ficha(alerta: Alert, evento: Optional[Event], camara: Optional[Camera],
                bitacora: Sequence[AuditLog], generado_por: str) -> tuple[bytes, dict[str, str]]:
    """Devuelve (bytes del ZIP, {archivo: sha256})."""
    folio = f"ALR-{alerta.id:06d}"
    archivos: dict[str, bytes] = {}

    for nombre_zip, ruta in (("foto", alerta.snapshot_path), ("clip", alerta.clip_path)):
        if not ruta:
            continue
        en_disco = ruta_evidencia(Path(ruta).name)
        if en_disco is not None:
            archivos[f"{nombre_zip}{en_disco.suffix.lower()}"] = en_disco.read_bytes()

    huellas = {nombre: _sha256(datos) for nombre, datos in archivos.items()}
    sitio = os.getenv("SITIO_NOMBRE", "").strip()
    meta = evento.meta if evento is not None else {}

    datos_alerta = "".join([
        _fila("Folio", folio),
        _fila("Tipo", TIPOS_ALERTA.get(alerta.type, alerta.type)),
        _fila("Severidad", alerta.severity),
        _fila("Hecho", alerta.title),
        _fila("Detalle", alerta.detail),
        _fila("Fecha y hora del hecho", _local(evento.ts if evento is not None else alerta.created_at)),
        _fila("Registrada en el sistema", _local(alerta.created_at)),
        _fila("Valor leído", evento.value if evento is not None else None),
        _fila("Confianza del modelo", f"{evento.confidence:.2f}" if evento is not None else None),
        _fila("Coincidencia", alerta.match_kind if alerta.match_kind != "none" else None),
        _fila("Color del vehículo", meta.get("color_vehiculo")),
    ])
    datos_camara = "".join([
        _fila("Cámara", f"{camara.name} ({camara.camera_id})" if camara else alerta.camera_id),
        _fila("Ubicación", camara.location if camara else None),
        _fila("Coordenadas", f"{camara.lat:.6f}, {camara.lon:.6f}"
              if camara and camara.lat is not None and camara.lon is not None else None),
    ])
    atencion = "".join([
        _fila("Estado", ESTADOS.get(alerta.status, alerta.status)),
        _fila("Atendió", alerta.acknowledged_by),
        _fila("Hora de atención", _local(alerta.acknowledged_at)),
        _fila("Motivo de descarte", alerta.dismissed_reason),
        _fila("Nota del monitorista", alerta.notes),
    ])
    canalizaciones = leer_canalizaciones(alerta.canalizaciones_json)
    filas_canal = "".join(
        f"<tr><td>{html.escape(_local(c.get('ts')))}</td>"
        f"<td>{html.escape(DESTINOS.get(c.get('destino'), str(c.get('destino'))))}</td>"
        f"<td>{html.escape(c.get('referencia') or '')}</td>"
        f"<td>{html.escape(c.get('nota') or '')}</td>"
        f"<td>{html.escape(c.get('por') or '')}</td></tr>"
        for c in canalizaciones
    ) or '<tr><td colspan="5">Sin canalizar</td></tr>'
    filas_bitacora = "".join(
        f"<tr><td>{html.escape(_local(b.ts))}</td><td>{html.escape(b.usuario or 'sistema')}</td>"
        f"<td>{html.escape(ACCIONES.get(b.accion, b.accion))}</td></tr>"
        for b in bitacora
    ) or '<tr><td colspan="3">Sin movimientos</td></tr>'
    filas_huellas = "".join(
        f"<tr><td>{html.escape(n)}</td><td class='hash'>{h}</td></tr>" for n, h in huellas.items()
    ) or '<tr><td colspan="2">La foto y el clip ya no están en disco (retención)</td></tr>'
    foto = next((n for n in archivos if n.startswith("foto")), None)
    clip = next((n for n in archivos if n.startswith("clip")), None)

    pagina = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<title>Ficha de evidencia {folio}</title>
<style>
body{{font-family:system-ui,Segoe UI,Arial,sans-serif;color:#111;max-width:820px;margin:24px auto;padding:0 16px}}
h1{{font-size:20px;margin:0}} h2{{font-size:15px;margin:22px 0 6px;border-bottom:1px solid #999}}
.sub{{color:#555;font-size:13px}} table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{text-align:left;vertical-align:top;padding:4px 6px;border-bottom:1px solid #ddd}}
th{{width:34%;color:#333}} .hash{{font-family:Consolas,monospace;font-size:11px;word-break:break-all}}
img{{max-width:100%;border:1px solid #ccc;margin-top:6px}} .aviso{{font-size:12px;color:#444;margin-top:24px}}
</style></head><body>
<h1>Ficha de evidencia {folio}</h1>
<div class="sub">GOSS IP{f" · {html.escape(sitio)}" if sitio else ""} · generada el {_local(datetime.now(timezone.utc))} por {html.escape(generado_por)}</div>
<h2>Hecho</h2><table>{datos_alerta}</table>
<h2>Lugar</h2><table>{datos_camara}</table>
<h2>Atención</h2><table>{atencion}</table>
<h2>Canalización</h2>
<table><tr><th>Fecha</th><th>Instancia</th><th>Folio externo</th><th>Nota</th><th>Quién</th></tr>{filas_canal}</table>
<h2>Evidencia</h2>
{f'<img src="{foto}" alt="Captura de la alerta">' if foto else '<p>Sin foto.</p>'}
{f'<p>Clip de video: <b>{clip}</b> (incluido en este paquete).</p>' if clip else ''}
<h2>Huellas SHA-256</h2>
<table><tr><th>Archivo</th><th>SHA-256</th></tr>{filas_huellas}</table>
<h2>Bitácora del folio</h2>
<table><tr><th>Fecha</th><th>Usuario</th><th>Acción</th></tr>{filas_bitacora}</table>
<p class="aviso">Generada automáticamente. La detección la hace un modelo de visión y la verificó
un operador humano; no constituye por sí misma una identificación. Contiene datos personales:
entregar solo a la autoridad competente, conforme al aviso de privacidad del sitio. Para comprobar
que los archivos no cambiaron: <code>sha256sum -c SHA256SUMS.txt</code>.</p>
</body></html>
"""
    sumas = "".join(f"{h}  {n}\n" for n, h in huellas.items())

    salida = io.BytesIO()
    with zipfile.ZipFile(salida, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ficha.html", pagina)
        for nombre, datos in archivos.items():
            # Foto y clip ya vienen comprimidos: guardarlos tal cual es mas rapido.
            z.writestr(zipfile.ZipInfo(nombre, date_time=datetime.now().timetuple()[:6]), datos,
                       compress_type=zipfile.ZIP_STORED)
        z.writestr("SHA256SUMS.txt", sumas)
    return salida.getvalue(), huellas
