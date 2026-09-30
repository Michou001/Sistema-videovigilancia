# GOSS IP — Sistema de Videovigilancia

Detección de **placas vehiculares**, **rostros** y **movimiento anómalo** sobre
video en vivo de cámaras IP, con cruce contra lista negra y alertas en tiempo
real en una plataforma web.

| Módulo | Estado |
|---|---|
| Placas (YOLOv5 + EasyOCR) | Funcionando en cámara real |
| Rostros (InsightFace, ArcFace 512-d) | Funcionando en cámara real; umbral pendiente de calibrar con personas |
| Movimiento anómalo (YOLO11 + ByteTrack) | Funcionando; umbral pendiente de validar con un incidente real |
| Armas blancas (YOLO11-COCO) | Apagado por defecto: no detectó en prueba real (ver abajo) |
| Plataforma web, lista negra, retención de datos | Funcionando |
| Varias cámaras a la vez | Probado con 2 Hikvision reales |

---

## Arquitectura

Tres procesos con responsabilidades separadas, unidos por un contrato de evento:

```
   [Cámara Hikvision / webcam / archivo]
                  │
                  ▼
   ┌──────────────────────────────┐
   │  edge/    worker de borde    │   GPU: lee frames, corre modelos,
   │           (uno por cámara)   │        sigue objetos entre frames
   └──────────────┬───────────────┘
                  │  POST /api/events  +  latido de salud cada 15 s
                  ▼
   ┌──────────────────────────────┐
   │  api/     FastAPI + SQLite   │   guarda, cruza LISTA NEGRA,
   │                              │   decide severidad
   └──────────────┬───────────────┘
                  │  WebSocket /ws/alerts  +  MJPEG /api/preview
                  ▼
   ┌──────────────────────────────┐
   │  web/     dashboard          │   Monitoreo: cámaras en vivo
   │                              │   Registro:  búsqueda y alertas
   └──────────────────────────────┘
```

**Regla de diseño:** el borde no decide qué es una alerta. Solo reporta *"vi
esto, con esta confianza"*. El cruce contra la lista negra y la severidad los
decide la API, que es quien tiene la base de datos; por eso la lista negra se
cambia sin tocar los workers.

El video en vivo sube del worker a la API como JPEG y baja al navegador como
MJPEG. No va directo de la cámara al navegador porque ningún navegador
reproduce RTSP, la cámara suele estar en una red privada, y el operador
necesita ver **las cajas dibujadas**, que solo existen en el worker.

### Estructura

```
├── shared/              Contrato compartido entre borde y API
│   ├── events.py        DetectionEvent, Severity, MatchResult
│   └── plates.py        Lectura, corrección y cruce de placas
├── edge/                Worker de inferencia
│   ├── config.py        Configuración por archivo de entorno
│   ├── sources.py       webcam | archivo | RTSP con la misma interfaz
│   ├── worker.py        Bucle principal
│   ├── sink.py          Envío por lotes con respaldo en disco
│   ├── heartbeat.py     Latido de salud hacia la API
│   ├── preview.py       Video anotado para el dashboard
│   ├── snapshot_hd.py   Foto del canal principal como evidencia
│   ├── tracking.py      Tracker IoU con predicción de velocidad
│   └── detectors/       placas, rostros, movimiento, armas
├── api/                 Plataforma web
│   ├── main.py          App FastAPI + WebSocket de alertas
│   ├── models.py        Esquema de base de datos
│   ├── matching.py      Cruce contra lista negra
│   ├── retroactive.py   Re-escaneo del pasado al dar de alta
│   ├── retention.py     Purga automática de datos personales
│   └── routers/         auth, eventos, alertas, lista negra, cámaras, video
├── web/                 Dashboard (HTML + JS, sin compilación)
├── tools/               init_plataforma, probe_camara, purgar_datos, calibrar_distancia
├── tests/               Pruebas sin cámara ni GPU
├── docs/                Hikvision, despliegue, privacidad, guía de prueba
└── models/              Pesos de los modelos
```

---

## Instalación

```bash
python -m venv venv
venv\Scripts\activate

# 1. torch CON CUDA -- va aparte, NO desde requirements.txt
pip install torch==2.13.0+cu126 torchvision==0.28.0+cu126 --index-url https://download.pytorch.org/whl/cu126

# 2. el resto
pip install -r requirements.txt

# 3. verificar que la GPU se usa (debe decir True)
python -c "import torch; print(torch.cuda.is_available())"
```

Sin GPU NVIDIA se instala `pip install torch torchvision` y el sistema corre en
CPU, más lento.

```bash
copy .env.example .env
python tools/init_plataforma.py      # crea BD, usuario admin y token del worker
```

`init_plataforma.py` imprime una sola vez la contraseña del admin y la línea
`API_TOKEN=...` que va en el `.env`. Guía paso a paso para el equipo:
**[docs/GUIA_PRUEBA.md](docs/GUIA_PRUEBA.md)**.

---

## Uso

```bash
iniciar_api.bat                 # o: uvicorn api.main:app --host 0.0.0.0 --port 8000
iniciar_worker.bat              # o: python -m edge.worker
```

Dashboard en `http://localhost:8000`, documentación de la API en `/docs`.

Antes de cargar modelos conviene verificar la fuente de video:

```bash
python -m edge.worker --diagnostico
```

Reporta fps reales, latencia, frames descartados y reconexiones. Si aquí los
números están mal, ningún modelo lo va a arreglar.

Cambiar de fuente es solo editar `SOURCE` en el `.env`:

```bash
SOURCE=webcam:0
SOURCE=file:videos/prueba.mp4
SOURCE=rtsp://operador:pass@192.168.1.64:554/Streaming/Channels/102
```

Para encontrar la cámara y su URL RTSP: botón **Cámaras** del dashboard, o
`python tools/probe_camara.py --descubrir` (ver
**[docs/camara-hikvision.md](docs/camara-hikvision.md)**).

### Varias cámaras

Un worker por cámara, cada uno con su archivo de entorno:

```bash
iniciar_worker.bat                      # cam-01, lee .env
iniciar_worker.bat --env .env.cam2      # cam-02, lee .env.cam2
```

El alta desde el dashboard crea ese archivo solo (copiando el token y los
detectores del `.env`) y responde con el comando para arrancar el worker.

### El dashboard

| Apartado | Para qué | Qué muestra |
|---|---|---|
| **Monitoreo** | Pantalla de guardia | Cámaras en vivo con las cajas dibujadas, fps de análisis y reconexiones; detecciones conforme entran |
| **Registro** | Pantalla de trabajo | Búsqueda de eventos por placa, tipo, estado y fechas; alertas por atender o descartar |

- La búsqueda ignora guiones y espacios: `abc123` encuentra `ABC-123`. Sirve
  para responder *"¿pasó el coche ABC-123 el martes?"*.
- Toda la evidencia se amplía con un clic (la foto puede ser la captura HD de
  3200 px).
- Una alerta crítica muestra un banner y suena; un aviso aparece en una
  esquina y se retira solo.
- La vista en vivo no cuesta nada mientras nadie mira: al salir de Monitoreo
  o con la pestaña en segundo plano, el worker deja de codificar y subir video.

### Cruce contra lista negra

| Detección | Estrategia | Resultado |
|---|---|---|
| Placa idéntica | Normalización + match exacto | `critical` |
| Placa con error de OCR (`A8C-I23`) | Colapso de caracteres ambiguos | `critical` — misma placa |
| Placa parecida (`ABD-123`) | Distancia de edición ≤ 1 | `warning` — *posible* coincidencia |
| Rostro | Similitud coseno ≥ `FACE_MATCH_THRESHOLD` | severidad del registro |
| Movimiento súbito | Regla, sin lista negra | `warning` |
| Arma confirmada | Regla, sin lista negra | `critical` |

Una coincidencia difusa **nunca** se titula como un hecho: la alerta dice
"Posible placa ABC-123 (se leyó ABD-123)". Cada registro puede tener
**vigencia**; al vencer deja de generar alertas.

Al dar de alta una placa o una persona, el sistema **revisa el pasado**: si ya
había pasado frente a una cámara (30 días de lecturas de placa, 7 días de fotos
de rostro), genera la alerta de inmediato.

**Si la plataforma se cae, el worker no pierde nada.** El envío corre en su
propio hilo: la detección no se frena esperando a la red. Lo que no se pudo
enviar va a `data/spool/` y se reenvía solo, con espera creciente, cuando la
API vuelve.

---

## Lectura de placas

1. **Detección** con YOLOv5 (pesos propios en `models/plates_yolov5.pt`).
2. **Seguimiento**: un vehículo = un `track_id` = un evento, emitido cuando
   sale de escena con la mejor evidencia acumulada.
3. **OCR** con EasyOCR restringido a mayúsculas y dígitos, sobre el recorte
   ampliado y con contraste ecualizado (CLAHE).
4. **Unión de fragmentos**: EasyOCR suele partir la placa (`ABC` + `123`) y
   leer el nombre del estado. Se descartan las leyendas y se unen los trozos
   de la línea principal de izquierda a derecha.
5. **Corrección por posición**: cada formato de placa mexicana dice qué
   posiciones son letras y cuáles dígitos. `A8C-I23` se corrige a `ABC-123`
   (una lectura corregida pesa un poco menos que una limpia).
6. **Consenso** entre hasta 6 lecturas del mismo vehículo, ponderado por
   confianza, más un voto carácter por carácter que recupera la placa aunque
   ningún frame la haya leído completa.

Los duplicados se evitan en dos capas: el tracking (principal) y una ventana
por valor de placa, `PLATE_DEDUPE_S`, para cuando el tracking se rompe.

### Alcance medido

Hace falta que la placa mida **≥ 48 px de ancho** para leerla (medido sobre
placas mexicanas reales con este modelo y este OCR). Con placa de frente:

| Lente | Sub-stream 1280 px | **Main 3200 px** |
|---|---|---|
| 2.8 mm (gran angular) | 3.1 m | **7.7 m** |
| 4 mm (estándar) | 4.5 m | **11.3 m** |
| 6 mm (teleobjetivo) | 8.0 m | **20.0 m** |

En ángulo de 45° multiplica por 0.7, y para instalar conviene el doble de
margen (~100 px). Reproducible con `python tools/calibrar_distancia.py`.
Conclusiones prácticas: **el lente manda más que el modelo** y **la cámara va
apuntada al carril**, no al estacionamiento entero.

---

## Reconocimiento facial

Modelo **InsightFace `buffalo_l`** (ArcFace, embeddings de 512-d) sobre
onnxruntime-GPU, solo con los módulos de detección y reconocimiento.

- Un rostro de menos de **50 px de ancho se descarta**: por debajo el embedding
  es ruido, y un falso positivo biométrico señala a una persona equivocada.
- La **calidad** de cada vista combina tamaño, confianza, **pose** (a partir
  de los 5 puntos faciales: un perfil vale mucho menos) y **nitidez**
  (varianza del laplaciano).
- El embedding del evento es el **promedio de las 3 mejores vistas** de la
  persona seguida, descartando las que no se parecen a la mejor (por si el
  tracker confundió a dos personas). Es más estable que una sola vista.
- Si la cámara es una Hikvision, el rostro se vuelve a calcular sobre la foto
  del canal principal (3200×1800).
- La foto de alta se valida: se rechaza si el rostro es muy chico o no está de
  frente, con el motivo explicado.

Dar de alta a alguien exige declarar un **fundamento legal**: es lo que permite
auditar meses después por qué esa persona está siendo vigilada.

---

## Movimiento anómalo

En vez de clasificar el OBJETO (un cuchillo en la pose exacta que aprendió el
modelo), mide el COMPORTAMIENTO: qué tan rápido se mueve una persona respecto a
su propio tamaño en pantalla. Un forcejeo, un golpe o alguien corriendo
comparten una firma de velocidad muy por encima de caminar normal.

YOLO11 filtrado a la clase `person` con ByteTrack. Por cada persona se mide el
desplazamiento de su centro en ~1 s, normalizado por la altura de su caja.

| Variable | Qué hace |
|---|---|
| `MOTION_SPEED_THRESHOLD` | Alturas de cuerpo por segundo que cuentan como súbitas. Caminar ronda 0.8–1.2; por defecto 2.5 |
| `MOTION_CONFIRM_HITS` / `_WINDOW` | Confirmación temporal: 3 de 5 lecturas sobre el umbral |
| `MOTION_COOLDOWN_S` | Tras alertar, la misma persona puede volver a alertar pasado este tiempo |

Los saltos que no puede hacer una persona (el tracker cambió de identidad
entre dos personas cercanas, o una oclusión cortó la caja) se descartan en vez
de contarse como velocidad. Sale como `warning`, no `critical`: amerita que el
operador mire, no una alarma automática.

---

## Detección de armas

**`ENABLE_WEAPONS=false` por defecto.** Probado contra la cámara real con un
cuchillo en mano, en pose y luz realistas: YOLO11-COCO no lo detectó. COCO
reconoce `knife`, `scissors` y `baseball bat` en foto de producto, y **no
tiene clase de arma de fuego**. El código queda listo para un modelo afinado:

1. Dataset etiquetado de *pistol/handgun* en formato YOLO (Roboflow Universe).
2. `yolo detect train model=yolo11s.pt data=tu_dataset.yaml epochs=100 imgsz=640`
3. Copiar los pesos a `models/weapons.pt`; el detector los usa solo.

> No descargues pesos `.pt` de repositorios desconocidos: son archivos pickle
> y **ejecutan código arbitrario al cargarse**.

La defensa contra falsos positivos es la **confirmación temporal**: un arma
solo alerta si se sostiene en 4 de los últimos 6 frames del mismo objeto
seguido. Un destello aislado o un celular que parpadea no pasa.

---

## Privacidad

Los embeddings faciales son **datos personales sensibles** bajo la LFPDPPP.
**Los embeddings de personas que NO están en la lista negra no se guardan
nunca**: se calculan en memoria, se comparan y se descartan.

Retención por capas, con purga automática cada 24 h:

| Dato | Plazo |
|---|---|
| Fotos de eventos sin coincidencia | 7 días |
| Eventos sin coincidencia (solo texto) | 30 días |
| Alertas y su evidencia | 1 año |

```bash
python tools/purgar_datos.py --simular
```

Obligaciones al instalarlo, incluido el caso de un entorno escolar:
**[docs/privacidad.md](docs/privacidad.md)**.

---

## Rendimiento medido

RTX 4050 Laptop (6 GB), Hikvision 1280×720 @ 20 fps, los tres detectores a la
vez:

| Detector | ms/frame |
|---|---|
| Placas | 10.5 |
| Rostros | 21.4 |
| Armas | 12.0 |
| **Total** | **43.9** |

**7.8 fps de 8 objetivo, 1.6 GB de 6 GB de VRAM**, 0 reconexiones. En batería
la laptop baja la GPU a modo de ahorro y los tiempos suben varias veces: para
operar conviene conectada a la corriente.

Dos trampas encontradas midiendo, ambas silenciosas:

1. **onnxruntime-gpu 1.23+ está compilado contra CUDA 13** y PyTorch cu126 trae
   CUDA 12. No lanza error: cae a CPU y el reconocimiento pasa de 12 a 77 ms.
   Por eso `requirements.txt` fija `onnxruntime-gpu==1.22.0`.
2. **El limitador de fps recalculaba el objetivo desde el momento del yield**
   y daba 5.5 fps con objetivo 8. Corregido con planificación acumulativa.

---

## Pruebas

```bash
python tests/correr_todas.py
```

Nueve archivos, sin cámara ni GPU: tracking, confirmación temporal, lectura y
cruce de placas, calidad de rostros, filtro de saltos del tracker, envío con la
API caída, resiliencia de la fuente, vista en vivo, fechas y la API completa
contra una base de datos temporal.

---

## Despliegue

Para pasarlo a un servidor 24/7 (servicios, varias cámaras, checklist de
producción): **[docs/despliegue.md](docs/despliegue.md)**. El código corre
igual en Windows y Linux.
