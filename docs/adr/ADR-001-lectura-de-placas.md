# ADR-001: Detector y OCR de placas

**Estado:** Aceptada
**Fecha:** 2026-09-29
**Decide:** equipo GOSS IP

## Contexto

La lectura de placas es el módulo central del sistema: es lo que cruza contra
la lista negra y lo que genera las alertas críticas. La versión anterior usaba
un YOLOv5 entrenado con un dataset que ya no está disponible, más EasyOCR (un
OCR de texto general, no de placas).

Problemas medidos:

- **Ángulos de cámara.** Sobre fotos reales de placas mexicanas deformadas para
  simular la cámara de lado (35° y 50°), desde arriba, rotada, lejos y con poca
  luz, acertaba **20 de 35** lecturas. De lado o desde arriba bajaba a 2 de 5.
- **Latencia.** EasyOCR tardaba 30–120 ms por recorte, lo que obligaba a
  racionar el OCR (máximo 2 por frame, una lectura cada 3 frames).
- **Mantenimiento.** Sin el dataset original el modelo no se puede reentrenar
  ni mejorar, y el formato `.pt` (pickle) ejecuta código al cargarse.
- **Distancia.** Hacían falta 48 px de ancho de placa para leerla.

Restricciones: una GPU de 6 GB compartida con rostros y movimiento, inferencia
a 8 fps por cámara, instalación con `pip` en Windows sin compilar nada.

## Decisión

Sustituir detector y OCR por los modelos de **FastALPR** (licencia MIT):

- Detector **YOLOv9** de `open-image-models`, entrenado solo con placas
  (`yolo-v9-s-608-license-plate-end2end`, mAP50 0.966).
- OCR **fast-plate-ocr** (`cct-s-v2-global-model`), un transformer compacto
  entrenado con placas de muchos países.

Ambos corren en ONNX Runtime sobre la GPU, con el mismo `onnxruntime-gpu` que
ya usa InsightFace. Lo propio del sistema se conserva encima: tracking (un
evento por vehículo), consenso entre lecturas, corrección por posición según
el formato mexicano, deduplicación, evidencia HD y color del vehículo.

## Opciones consideradas

### A: Mantener YOLOv5 + EasyOCR

| Dimensión | Evaluación |
|---|---|
| Complejidad | Baja (ya existe) |
| Precisión con ángulo | 20/35 |
| Latencia de OCR | 30–120 ms |
| Mejora futura | Bloqueada: no existe el dataset |

**Pros:** nada que cambiar. **Contras:** precisión insuficiente fuera de la
vista frontal; OCR lento; el modelo no se puede reentrenar.

### B: Entrenar un YOLO11 propio + EasyOCR

| Dimensión | Evaluación |
|---|---|
| Complejidad | Media (dataset, 1–2 h de GPU por entrenamiento) |
| Precisión con ángulo | Depende del dataset; el OCR sigue siendo el cuello |
| Latencia de OCR | 30–120 ms |
| Mejora futura | Sí, con datos propios |

**Pros:** control total del detector. **Contras:** no resuelve el OCR, que es
donde falla la lectura con ángulo; hay que conseguir y etiquetar datos.

### C: FastALPR (YOLOv9 + OCR de placas) — elegida

| Dimensión | Evaluación |
|---|---|
| Complejidad | Baja: `pip install fast-alpr`, modelos ONNX que se descargan solos |
| Precisión con ángulo | **35/35** |
| Latencia | ~23 ms detección + **<1 ms** OCR por placa (RTX 4050 en batería) |
| Mejora futura | Sí: fast-plate-ocr trae su propio entrenamiento para afinar con placas mexicanas |

**Pros:** mejor en todas las condiciones medidas; OCR especializado en placas;
ONNX (no ejecuta código al cargar); licencia MIT.
**Contras:** el detector no reconoce placas "sueltas" sin vehículo (los videos
sintéticos de prueba dejaron de servir); dependencia de un proyecto de terceros.

### D: Servicio en la nube (Plate Recognizer y similares)

Descartada: cada lectura saldría de la red de las cámaras (datos personales,
LFPDPPP), con costo por consulta y dependencia de internet para operar.

## Análisis

El error principal estaba en el OCR, no en el detector: un OCR de texto
general parte la placa, lee el nombre del estado y se pierde con la
perspectiva. Un OCR entrenado solo con placas resuelve eso y, al costar menos
de 1 ms, permite leer cada vehículo muchas más veces (10 lecturas en lugar de
6, cada 2 frames en lugar de cada 3), lo que a su vez fortalece el consenso.

## Consecuencias

- **Más fácil:** leer placas de lado, desde arriba y de noche; alcance mayor
  (umbral de 48 → 36 px: con lente de 4 mm y main stream, de 11.3 a 15.1 m).
- **Más fácil:** el worker ya no depende de `torch.hub` ni del repositorio
  `yolov5/`, y la instalación pesa menos.
- **Más difícil:** las pruebas de extremo a extremo necesitan fotos o video
  reales, no rectángulos sintéticos.
- **A revisar:** afinar el OCR con placas mexicanas propias (fast-plate-ocr
  permite entrenar) cuando se junten suficientes capturas de la cámara real.

## Decisiones relacionadas (mismo cambio)

| Módulo | Decisión | Por qué |
|---|---|---|
| Rostros | Se mantiene InsightFace `buffalo_l` (ArcFace) con calidad por pose y nitidez y plantilla promedio | Sigue siendo referencia abierta en reconocimiento facial. **Ojo:** sus pesos son de uso no comercial; para un despliegue comercial hay que licenciarlos o cambiar de modelo |
| Movimiento | Se agrega detección de **persona caída** (de pie → tendida en < 2 s) sobre el mismo YOLO11 + ByteTrack | Es un evento que los centros de monitoreo atienden y no requiere otro modelo en la GPU |
| Armas | Sigue apagado | No hay un modelo abierto de armas con licencia y procedencia verificables; cargar un `.pt` de origen desconocido ejecuta código. Requiere entrenar uno propio |
| Plataforma | Folio de alerta, nota de atención con respuestas rápidas, reporte CSV, nombre y ubicación de cámara, color del vehículo en la alerta | Es la información con la que opera un C5: qué vehículo, dónde, quién atendió y qué se hizo |

## Acciones

1. [x] Integrar detector YOLOv9 y OCR CCT en `edge/detectors/plates.py`.
2. [x] Medir contra el modelo anterior con fotos reales y ángulos simulados.
3. [x] Recalibrar el alcance con `tools/calibrar_distancia.py`.
4. [ ] Validar en la cámara real apuntando a un carril (el encuadre actual es interior).
5. [ ] Juntar capturas de placas de la cámara real y afinar el OCR con ellas.
6. [ ] Entrenar un detector de armas propio antes de encender `ENABLE_WEAPONS`.
