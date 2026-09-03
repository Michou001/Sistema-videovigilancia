# Sistema de Videovigilancia Inteligente

Detección de **placas vehiculares**, **rostros** y **armas** sobre video en vivo,
con cruce contra lista negra y alertas en tiempo real en una plataforma web.

> **Estado:** Fase 0 y Fase 1 completadas. La captura de video funciona de punta
> a punta; los detectores se conectan en las fases 2, 4 y 5. Ver [Roadmap](#roadmap).

---

## Arquitectura

Tres procesos con responsabilidades separadas, unidos por un contrato de evento:

```
   [Cámara Hikvision / webcam / archivo]
                  │
                  ▼
   ┌──────────────────────────────┐
   │  edge/    worker de borde    │   GPU: lee frames, corre modelos,
   │           (este repo)        │        trackea objetos
   └──────────────┬───────────────┘
                  │  POST /api/events   (shared/events.py)
                  ▼
   ┌──────────────────────────────┐
   │  api/     FastAPI            │   guarda, cruza LISTA NEGRA,
   │                              │   decide severidad
   └──────────────┬───────────────┘
                  │  WebSocket /ws/alerts
                  ▼
   ┌──────────────────────────────┐
   │  web/     dashboard          │   video, timeline, alertas
   └──────────────────────────────┘
```

**Regla de diseño:** el borde no decide qué es una alerta. Solo reporta *"vi
esto, con esta confianza"*. El cruce contra la lista negra y la severidad los
decide la API, que es quien tiene la base de datos.

### Estructura

```
sistema-videovigilancia/
├── shared/              Contrato compartido entre borde y API
│   ├── events.py        DetectionEvent, Severity, MatchResult
│   └── plates.py        Normalización de placas tolerante a errores de OCR
├── edge/                Worker de inferencia
│   ├── config.py        Configuración por variables de entorno
│   ├── sources.py       webcam | archivo | RTSP con la misma interfaz
│   ├── worker.py        Loop principal
│   └── detectors/       placas (F2), rostros (F4), armas (F5)
├── api/                 Plataforma web
│   ├── main.py          App FastAPI + WebSocket de alertas
│   ├── models.py        Esquema de base de datos
│   ├── matching.py      Cruce contra lista negra (el corazón del sistema)
│   ├── security.py      bcrypt + JWT + token de ingesta
│   └── routers/         auth, events, blacklist, alerts
├── web/                 Dashboard (HTML + JS, sin build)
├── tools/
│   ├── probe_camara.py     Encuentra y prueba la cámara IP
│   └── init_plataforma.py  Crea BD, usuario admin y token del worker
├── docs/
│   └── camara-hikvision.md
├── models/              Pesos de los modelos
└── legacy/              Scripts originales (referencia para la Fase 2)
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

Luego copia la configuración y ajústala:

```bash
copy .env.example .env
```

---

## Uso

Verifica que tu fuente de video funciona, antes de cargar modelo alguno:

```bash
python -m edge.worker --diagnostico
```

Reporta fps reales, latencia, frames descartados y reconexiones. Si aquí los
números están mal, ningún modelo lo va a arreglar.

Para conectar la cámara Hikvision, ver **[docs/camara-hikvision.md](docs/camara-hikvision.md)**:

```bash
python tools/probe_camara.py --descubrir
python tools/probe_camara.py --host 192.168.1.64 --user operador --password TU_PASS
```

Cambiar de fuente es solo editar `SOURCE` en el `.env` — nunca el código:

```bash
SOURCE=webcam:0
SOURCE=file:videos/prueba.mp4
SOURCE=rtsp://operador:pass@192.168.1.64:554/Streaming/Channels/102
```

### Detección

```bash
python -m edge.worker                          # corre indefinido
python -m edge.worker --ventana                # con ventana de depuración
python -m edge.worker --limitar --segundos 60  # se detiene a los 60 s
python -m edge.worker --source file:videos/prueba.mp4
```

Los eventos salen por consola y se escriben en `data/eventos-YYYYMMDD.jsonl`.
En la Fase 3 se agrega el envío HTTP a la API.

Un evento se emite **cuando el objeto sale de escena**, no en cada frame. Es
normal ver detecciones en la ventana durante segundos antes de que aparezca su
evento: el sistema está acumulando lecturas para elegir la mejor.

### Plataforma web

Primera vez (crea la base de datos, el usuario admin y el token del worker):

```bash
python tools/init_plataforma.py
```

Luego, en dos terminales:

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

```bash
python -m edge.worker
```

Dashboard en `http://localhost:8000`, documentación de la API en `/docs`.

**Cómo funciona el cruce contra lista negra.** El borde no decide qué es una
alerta: solo reporta *"vi esto, con esta confianza"*. La API decide, y por eso
la lista negra se puede cambiar sin redesplegar los workers.

| Detección | Estrategia | Resultado |
|---|---|---|
| Placa idéntica | Normalización + match exacto | `critical` |
| Placa con error de OCR (`A8C-I23`) | Colapso de caracteres ambiguos | `critical` — misma placa |
| Placa parecida (`ABD-123`) | Distancia de edición ≤ 1 | `warning` — *posible* coincidencia |
| Placa sin coincidencia | — | `info`, sin alerta |
| Arma | Regla fija, sin lista negra | `critical` |

Una coincidencia difusa **nunca** se titula como un hecho: la alerta dice
"Posible placa ABC-123 (se leyó ABD-123)". El operador reacciona al título, no
siempre lee el detalle, y actuar contra el vehículo equivocado es el error caro.

**Si la plataforma se cae, el worker no pierde nada.** Los eventos se escriben
en `data/spool/` y se reenvían solos cuando la API vuelve (verificado).

### Detección de armas

El problema de esta fase **no es detectar, es no gritar en falso.** Un detector
ingenuo alerta con cualquier celular, botella o desarmador que agarre alguien.
Y una alerta falsa de arma manda a una persona a responder a una emergencia
inexistente — a la tercera vez, alguien apaga el sistema.

La defensa es la **confirmación temporal**: un arma solo alerta si se sostiene
en `WEAPON_CONFIRM_HITS` de los últimos `WEAPON_CONFIRM_WINDOW` frames del
mismo objeto seguido (por defecto 4 de 6).

| Secuencia de frames | ¿Alerta? | Por qué |
|---|---|---|
| `XXXX` | Sí | Detección sostenida = arma real |
| `X.....X.....X` | No | Destellos aislados: mala inferencia |
| `X.X.X.X.X.` | No | Parpadeo: objeto ambiguo, típico celular |
| `XX.XX` | Sí | Se tapó un frame, sigue siendo un arma |

Otra diferencia con placas y rostros: esos emiten su evento **cuando el objeto
sale de escena**, para acumular la mejor evidencia. Un arma se emite **en cuanto
se confirma** — esperar a que la persona se vaya para avisar que traía un arma
no tiene sentido.

#### Para detectar armas de fuego

COCO no tiene clase de pistola. Hace falta afinar un modelo:

1. Consigue un dataset etiquetado (Roboflow Universe tiene varios de
   *pistol/handgun*), en formato YOLO.
2. `yolo detect train model=yolo11s.pt data=tu_dataset.yaml epochs=100 imgsz=640`
3. Copia los pesos a `models/weapons.pt`. El detector lo usa automáticamente y
   asume que todas sus clases son armas.

> ⚠️ **No descargues pesos `.pt` de repositorios desconocidos.** Son archivos
> pickle y **ejecutan código arbitrario al cargarse**. Entrena el tuyo o usa
> fuentes oficiales.

### Pruebas

```bash
python tests/test_tracking.py       # tracker: 8 pruebas
python tests/test_confirmacion.py   # anti falsos positivos de armas: 10 pruebas
```

Vale la pena correrlas antes de tocar `edge/tracking.py`. La prueba
`test_objeto_rapido` cubre un fallo que los videos de prueba no detectaban: con
una placa moviéndose ~3 px por frame todo parecía correcto, pero un coche a
velocidad real se desplaza más que el ancho de su propia placa entre frames y el
tracking se rompía en cada uno, sin emitir un solo evento.

---

## Roadmap

| Fase | Contenido | Estado |
|---|---|---|
| 0 | CUDA, estructura, contrato de evento, esquema de BD | ✅ Hecho |
| 1 | Capa de fuente de video + herramienta de cámara | ✅ Hecho |
| 2 | Placas: módulo, tracking, agregación por track | ✅ Hecho |
| 3 | Plataforma web + lista negra + alerta end-to-end | ✅ Hecho |
| 4 | Rostros con InsightFace + lista negra biométrica | ✅ Hecho¹ |
| 5 | Armas con YOLO11 + confirmación temporal | ✅ Hecho² |
| 6 | Retención, privacidad, despliegue 24/7 | ✅ Hecho |

> ² **Solo armas blancas por ahora.** Funciona con `knife`, `scissors` y
> `baseball bat` de COCO, sin entrenar nada. **COCO no tiene armas de fuego**:
> para pistolas hace falta un modelo afinado (ver abajo). La lógica de
> confirmación temporal está probada (10/10).
>
> ¹ **Pendiente de validar con rostros reales.** La lógica de coincidencia
> biométrica está verificada con vectores sintéticos (vector idéntico → 100%,
> vector distinto → sin coincidencia) y la detección corre en GPU, pero el
> umbral `FACE_MATCH_THRESHOLD=0.50` **no se ha calibrado contra personas
> reales**, porque no ha habido un rostro frente a la cámara. Hasta hacerlo, no
> se sabe la tasa real de falsos positivos y negativos.

### Pendiente de validar en campo

Lo que sigue **no está probado con sujetos reales** y es lo primero que hay que
hacer antes de confiar en el sistema:

| Qué | Cómo |
|---|---|
| Umbral facial | Darse de alta con foto y verificar reconocimiento; ajustar `FACE_MATCH_THRESHOLD` |
| Detección de armas | Mostrar un cuchillo a la cámara y medir falsos positivos con un celular en la mano |
| Lectura de placas | Apuntar la cámara a la calle o cochera — el encuadre actual es interior |

### Reconocimiento facial

Modelo **InsightFace `buffalo_l`** (ArcFace, embeddings de 512-d) sobre
onnxruntime-GPU. Se cargan solo los módulos de detección y reconocimiento —
sin landmarks ni edad/género, que no se usan y ocupan VRAM compartida con el
modelo de placas.

Un rostro de menos de **50 px de ancho se descarta**: por debajo de eso el
embedding es ruido, y un falso positivo biométrico señala a una persona
equivocada. De cada persona seguida se conserva el embedding de **mejor
calidad** (tamaño × confianza), no el primero ni el último.

Dar de alta a alguien exige declarar un **fundamento legal**. No es burocracia:
es lo que permite auditar meses después por qué esa persona está siendo
vigilada.

### Cómo se evitan los eventos duplicados

Son **dos mecanismos en capas**, y el orden importa:

1. **Tracking (principal).** `edge/tracking.py` asocia detecciones entre frames
   por IoU sobre la posición *predicha* con velocidad. Un vehículo = un
   `track_id` = un evento, con la mejor de todas sus lecturas de OCR elegida
   por consenso.
2. **Deduplicación por valor (red de seguridad).** El tracking no es infalible:
   en pruebas se midió al detector perdiendo una placa durante **72 frames
   seguidos** por el ángulo. Cuando eso pasa nace un track nuevo y saldría un
   segundo evento del mismo coche. `PLATE_DEDUPE_S` lo suprime.

Esto reemplaza al `COOLDOWN_SEGUNDOS = 15` del código original, que era el
*único* mecanismo y se aplicaba a ciegas.

### Decisión resuelta: formato del modelo

El `.pt` está en formato YOLOv5 y **Ultralytics no puede cargarlo**. Se
verificó que `torch.hub` con el repo `yolov5/` local **sí lo carga** y corre a
7.5 ms/frame en GPU, así que **no hizo falta exportar a ONNX ni reentrenar**.

Consecuencia: el tracker de Ultralytics (ByteTrack) no está disponible para
placas, de ahí el `IoUTracker` propio. Los detectores de rostros y armas sí
usarán modelos de Ultralytics y su tracker integrado.

### Rendimiento medido

RTX 4050 Laptop (6 GB), 1280×720:

| | ms/frame |
|---|---|
| YOLO11n, CPU | 48.6 |
| YOLO11n, GPU | 9.0 |
| Placas (YOLOv5s), GPU, archivo | 10.1 |
| Placas, GPU, RTSP en vivo | 15-31 |
| EasyOCR por recorte | 30-57 |
| **Rostros (InsightFace), GPU** | **12-42** |
| Rostros, CPU | 77 |

**Los tres detectores simultáneos** sobre la cámara en vivo (Hikvision
1280×720 @ 20 fps, RTX 4050 6 GB):

| Detector | ms/frame |
|---|---|
| Placas | 10.5 |
| Rostros | 21.4 |
| Armas | 12.0 |
| **Total** | **43.9** |

**7.82 fps de 8.0 objetivo (98%)**, **1.64 GB de 6 GB** de VRAM, 0 reconexiones
y 0 falsos positivos en 358 frames. Queda margen de sobra para más cámaras o
modelos más grandes.

Dos trampas encontradas midiendo, ambas silenciosas:

1. **onnxruntime-gpu 1.23+ está compilado contra CUDA 13**, y PyTorch cu126
   trae CUDA 12. No lanza error: cae a CPU y el reconocimiento pasa de 12 a
   77 ms sin avisar. Por eso `requirements.txt` fija `onnxruntime-gpu==1.22.0`.
2. **El limitador de fps recalculaba el objetivo desde el momento del yield.**
   Como solo se puede entregar un frame cuando llega uno de la cámara, ese
   sobrante se arrastraba y el periodo real se redondeaba al alza: daba 5.5 fps
   con objetivo 8. Corregido con planificación acumulativa → 7.2 fps.

---

## Alcance de lectura de placas (medido)

El sistema necesita que la placa mida **≥48 píxeles de ancho** para leerla —
medido sobre placas mexicanas reales con este modelo y este OCR. Detecta desde
25 px, pero detectar no sirve de nada si no se puede leer.

Traducido a distancia, con placa de frente:

| Lente | Sub 1280px | **Main 3200px** |
|---|---|---|
| 2.8 mm (gran angular) | 3.1 m | **7.7 m** |
| 4 mm (estándar) | 4.5 m | **11.3 m** |
| 6 mm (teleobjetivo) | 8.0 m | **20.0 m** |

En ángulo de 45° multiplica por 0.7. Y **48 px es el mejor caso**: en las
imágenes de prueba la dispersión fue grande, así que para instalar conviene
usar el doble de margen (~100 px), lo que corta las distancias a la mitad.

Reproducible con:

```bash
python tools/calibrar_distancia.py
```

No hace falta cinta métrica: reduce fotos reales de placas para simular
distancia. Tres conclusiones prácticas: **usa el main stream** para placas, **el
lente manda más que el modelo**, y **apunta la cámara al carril**, no al
estacionamiento entero desde una esquina.

## Privacidad

Los embeddings faciales son **datos personales sensibles** bajo la LFPDPPP.

**Decisión de diseño central: los embeddings de personas que NO están en la
lista negra no se guardan nunca.** Se calculan en memoria, se comparan y se
descartan. Guardarlos convertiría el sistema en una base de datos biométrica de
todo el que pase frente a la cámara, sin finalidad que lo justifique.

Retención por capas, con purga automática cada 24 h:

| Dato | Plazo |
|---|---|
| Fotos de eventos sin coincidencia | 7 días |
| Eventos sin coincidencia (solo texto) | 30 días |
| Alertas y su evidencia | 1 año |

Para responder *"¿pasó el coche ABC-123 el martes?"* basta el texto; la foto no
aporta y sí es un dato personal de alguien que no hizo nada.

```bash
python tools/purgar_datos.py --simular
```

Ver **[docs/privacidad.md](docs/privacidad.md)** para las obligaciones legales
al instalarlo, incluyendo el caso de menores de edad en un entorno escolar.

## Despliegue

Para pasarlo a un servidor 24/7 (systemd, varias cámaras, checklist de
producción): **[docs/despliegue.md](docs/despliegue.md)**.

El código corre igual en Windows y Linux; los parches específicos de Windows
están condicionados por plataforma.
