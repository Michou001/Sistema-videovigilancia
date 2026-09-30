# Reentrenar los modelos con lo que ven TUS cámaras

Los modelos vienen entrenados con fotos de internet. Lo que más los mejora son
imágenes de **estas** cámaras, con **este** ángulo y **esta** luz, calificadas
por quien las revisa todos los días: los operadores.

El sistema cierra ese ciclo en cuatro pasos.

```
  detección ──► alerta ──► operador: "Atendida" / "Falso positivo"
      ▲                                   │
      │                                   ▼
  modelo nuevo ◄── yolo train ◄── tools/dataset_alertas.py
```

## 1. Activar la recolección (en el worker)

En el `.env` del worker:

```ini
DATASET_ENABLED=true
DATASET_TIPOS=weapon,anomaly,zone     # armas, movimiento/pose, zonas
RETENCION_DATASET_DIAS=30
```

Con esto, cada vez que un detector genera un evento, el worker guarda en
`data/entrenamiento/<camara>/` el **cuadro original** (sin cajas ni textos
dibujados: el modelo aprendería a buscar el recuadro) y la caja del objeto.
Si la API responde que ese evento no fue alerta, se borra al momento: nadie lo
va a revisar. Lo demás se borra solo a los 30 días.

Los cuadros **no salen de la máquina del worker**.

## 2. Que los operadores califiquen

Nada nuevo: en el dashboard, cada alerta se cierra con **Atender** (la
detección era correcta) o **Falso positivo** (no lo era). Ese clic es la
etiqueta. Mientras más alertas se califiquen, mejor el dataset.

## 3. Armar el dataset

En la máquina del worker:

```bash
python tools/dataset_alertas.py                  # todo
python tools/dataset_alertas.py --tipos weapon   # solo armas
```

Pregunta a la API el veredicto de cada alerta y genera un ZIP con formato YOLO:

| Veredicto | Qué entra |
|---|---|
| Atendida | La imagen con la caja del objeto (clase = `knife`, `persona_caida`, `persona`...) |
| Falso positivo | La imagen **sin** etiqueta: fondo. Le enseña al modelo que ahí no hay nada. Es lo que más baja las falsas alarmas. |
| Sin revisar | No entra |

## 4. Entrenar y poner el modelo nuevo

Con GPU (en la misma PC del worker o en otra):

```bash
unzip dataset-alertas-*.zip && cd dataset
yolo detect train data=data.yaml model=yolo11s.pt epochs=60 imgsz=640 patience=15
```

Recomendaciones:

- **No entrenes solo con esto.** Unos cientos de imágenes propias sobre un
  modelo genérico lo pueden hacer olvidar lo que ya sabía. Mezcla estas
  imágenes con las del dataset original del modelo (o parte de COCO para
  personas y vehículos).
- Para armas, el dataset propio tiene que tener el arma **en la mano, a la
  distancia real**: es exactamente lo que COCO no trae.
- El resultado queda en `runs/detect/train/weights/best.pt`. Cópialo a
  `models/` y apúntalo en el `.env` del worker (`WEAPON_MODEL=models/armas-v2.pt`).
- Compara una semana antes y una después: cuántas alertas y qué fracción
  terminó en "falso positivo" (el reporte CSV del Registro lo da).
- Opcional: expórtalo a TensorRT con `python tools/optimizar_modelos.py`.

## Placas

Las placas tienen su propio ciclo, con el OCR: cada lectura corregida por un
operador es un dato de entrenamiento. Ver [placas-mexicanas.md](placas-mexicanas.md)
y *Administración > Reentrenamiento* en el dashboard.

## Privacidad

Son imágenes de personas reales. El aviso de privacidad debe contemplar este
uso (mejorar el sistema), los archivos se guardan cifrados y se borran al
terminar de entrenar. Ver [privacidad.md](privacidad.md).
