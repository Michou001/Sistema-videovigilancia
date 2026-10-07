"""Dataset para reentrenar el OCR de placas con placas mexicanas reales.

El OCR (fast-plate-ocr) se entreno con placas de muchos paises. Lo que mas lo
mejora para este sistema son placas MEXICANAS de ESTAS camaras, con su
angulo, su luz y su compresion. Este modulo junta esas imagenes con su
texto correcto en el formato que espera el entrenamiento de fast-plate-ocr:

    anotaciones.csv     image_path,plate_text,plate_region
    imagenes/<id>.jpg   recorte justo de la placa

Fuentes de verdad, de mas a menos confiable:
  1. Lecturas que un operador CORRIGIO (Event.corregido): el texto lo puso
     una persona mirando la foto.
  2. Opcional: lecturas automaticas muy seguras (confianza alta y muchos
     frames de consenso). Sirven para tener volumen, pero pueden llevar
     errores: el LEEME lo advierte.

Son fotos de placas de vehiculos: datos personales. El archivo exportado
queda fuera de la politica de retencion del sistema, bajo responsabilidad de
quien lo descarga (queda en la bitacora).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Optional

import cv2
from sqlmodel import Session, col, select

from api.config import BASE_DIR
from api.models import Event
from shared.fechas import a_utc
from shared.plates import limpiar

log = logging.getLogger(__name__)

# Nombre del pais como lo usa el OCR (sus etiquetas de region van en ingles).
PAIS_A_REGION = {"México": "Mexico", "Estados Unidos": "United States", "Canadá": "Canada"}

LEEME = """Dataset de placas exportado por GOSS IP
=======================================

Contenido
  anotaciones.csv    image_path, plate_text, plate_region (formato fast-plate-ocr)
  imagenes/          recorte de cada placa
  negativas/         recortes revisados como NO placa; nunca entran a anotaciones.csv
  negativas.jsonl    trazabilidad de esos falsos positivos

Origen de cada fila (columna 'origen' de fuentes.csv)
  corregida   un operador corrigio la lectura mirando la foto: verdad de campo
  confirmada  un operador reviso la imagen y confirmo el texto sin cambiarlo
  automatica  lectura del sistema con confianza >= {min_conf} y >= {min_frames} frames
              de consenso. Revisa una muestra antes de entrenar: puede tener errores.

Como afinar el OCR con estas placas
  pip install "fast-plate-ocr[train]"
  fast_plate_ocr train --model-config-file <modelo>.yaml --plate-config-file <placas>.yaml \\
      --annotations anotaciones.csv --val-annotations <validacion>.csv --epochs 50
  Separa por sesion/camara/objeto antes de entrenar; no repartas cuadros del
  mismo objeto entre entrenamiento y validacion. Deduplica capturas similares.
  Las negativas son recortes para revisar el detector, no etiquetas OCR vacias
  ni escenas completas YOLO: requieren preparar y validar ese entrenamiento.
  La exportacion NO reentrena ni modifica el modelo desplegado.
  Exporta el modelo a ONNX y ponlo en
  PLATE_OCR del .env del worker. Detalle en docs/placas-mexicanas.md.

Privacidad
  Son placas de vehiculos reales (datos personales). Este archivo ya no lo
  purga el sistema: guardalo cifrado y borralo al terminar de entrenar.
"""


@dataclass
class Resumen:
    corregidas: int = 0
    automaticas: int = 0
    sin_foto: int = 0
    negativas: int = 0
    omitidas: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.corregidas + self.automaticas


def recorte_de_placa(evento: Event) -> Optional[bytes]:
    """JPEG del recorte justo de la placa, a partir de la foto de evidencia."""
    if not evento.snapshot_path:
        return None
    ruta = (BASE_DIR / evento.snapshot_path).resolve()
    if not ruta.is_relative_to(BASE_DIR.resolve()) or not ruta.is_file():
        return None
    imagen = cv2.imread(str(ruta), cv2.IMREAD_COLOR)
    if imagen is None or imagen.size == 0:
        return None
    alto, ancho = imagen.shape[:2]
    caja = (evento.meta or {}).get("placa_en_evidencia") or [0.0, 0.0, 1.0, 1.0]
    try:
        x1, y1, x2, y2 = (float(v) for v in caja)
    except (TypeError, ValueError):
        x1, y1, x2, y2 = 0.0, 0.0, 1.0, 1.0
    px1, py1 = max(0, int(x1 * ancho)), max(0, int(y1 * alto))
    px2, py2 = min(ancho, int(round(x2 * ancho))), min(alto, int(round(y2 * alto)))
    if px2 - px1 < 8 or py2 - py1 < 4:
        return None
    ok, buf = cv2.imencode(".jpg", imagen[py1:py2, px1:px2], [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    return buf.tobytes() if ok else None


def exportar(session: Session, salida: BinaryIO, *, incluir_automaticas: bool = False,
             min_conf: float = 0.9, min_frames: int = 5, desde=None) -> Resumen:
    """Escribe el dataset como ZIP en `salida`."""
    resumen = Resumen()
    consulta = select(Event).where(Event.type == "plate", col(Event.snapshot_path).is_not(None))
    if desde is not None:
        consulta = consulta.where(col(Event.ts) >= a_utc(desde))
    # La revision vive en meta_json; filtrar en Python es portable entre
    # SQLite y PostgreSQL e incluye las confirmaciones sin editar el texto.
    eventos = session.exec(consulta.order_by(col(Event.ts))).all()

    anotaciones = io.StringIO()
    fuentes = io.StringIO()
    esc = csv.writer(anotaciones)
    esc.writerow(["image_path", "plate_text", "plate_region"])
    esc_fuentes = csv.writer(fuentes)
    esc_fuentes.writerow(["image_path", "origen", "camara", "fecha_utc", "lectura_original"])

    with zipfile.ZipFile(salida, "w", compression=zipfile.ZIP_DEFLATED) as z:
        negativas = []
        for ev in eventos:
            meta = ev.meta or {}
            revision = meta.get("revision_placa")
            if revision == "no_es_placa":
                jpeg = recorte_de_placa(ev)
                if jpeg is not None:
                    nombre = f"negativas/{Path(ev.snapshot_path).stem}.jpg"
                    z.writestr(nombre, jpeg)
                    negativas.append({"imagen": nombre, "evento": ev.event_id,
                                      "clase": "no_es_placa", "revisado_por": meta.get("revisado_por"),
                                      "revisado_en": meta.get("revisado_en"), "camara": ev.camera_id})
                    resumen.negativas += 1
                else:
                    resumen.sin_foto += 1
                continue
            if not (ev.corregido or revision == "confirmada" or
                    (incluir_automaticas and ev.confidence >= min_conf and (ev.observations or 0) >= min_frames)):
                continue
            texto = limpiar(ev.value)
            if not (2 <= len(texto) <= 10):
                resumen.omitidas.append(ev.event_id)
                continue
            jpeg = recorte_de_placa(ev)
            if jpeg is None:
                resumen.sin_foto += 1
                continue
            nombre = f"imagenes/{Path(ev.snapshot_path).stem}.jpg"
            z.writestr(nombre, jpeg)
            meta = ev.meta or {}
            region = PAIS_A_REGION.get(meta.get("pais") or "México", "Unknown")
            esc.writerow([nombre, texto, region])
            origen = "corregida" if ev.corregido else "confirmada" if revision == "confirmada" else "automatica"
            esc_fuentes.writerow([nombre, origen, ev.camera_id, a_utc(ev.ts).isoformat(),
                                  meta.get("lectura_original", "")])
            if ev.corregido or revision == "confirmada":
                resumen.corregidas += 1
            else:
                resumen.automaticas += 1
        z.writestr("anotaciones.csv", anotaciones.getvalue())
        z.writestr("fuentes.csv", fuentes.getvalue())
        z.writestr("negativas.jsonl", "\n".join(json.dumps(n, ensure_ascii=False) for n in negativas))
        z.writestr("LEEME.txt", LEEME.format(min_conf=min_conf, min_frames=min_frames))
    log.info("Dataset de placas: %d corregidas, %d automaticas, %d sin foto",
             resumen.corregidas, resumen.automaticas, resumen.sin_foto)
    return resumen
