# GOSS IP — Sistema de Videovigilancia

Detección de **placas mexicanas**, **rostros**, **movimiento anómalo**,
**caídas y posturas**, **reglas por zona** y **sabotaje de cámaras** sobre
video en vivo de cámaras IP, con cruce contra lista negra, alertas en tiempo
real, clips de evidencia y avisos al celular, en una plataforma web.

| Módulo | Estado |
|---|---|
| Placas mexicanas (YOLOv9 + OCR de placas + formatos NOM-001-SCT-2-2016) | Funcionando; 35/35 lecturas en pruebas de ángulo ([ADR-001](docs/adr/ADR-001-lectura-de-placas.md), [placas mexicanas](docs/placas-mexicanas.md)) |
| Rostros (InsightFace, ArcFace 512-d) | Funcionando en cámara real; umbral pendiente de calibrar con personas |
| Movimiento anómalo (YOLO11 + ByteTrack) | Funcionando; umbrales pendientes de validar con un incidente real |
| Pose (YOLO11-pose): caída, manos arriba, posible agresión | Probado con el modelo real; pendiente de validar en campo |
| Zonas y reglas: intrusión con horario, cruce de línea, merodeo, conteo | Funcionando, con editor en el dashboard |
| Eventos de la cámara Hikvision (sabotaje, pérdida de video) y cámaras caídas | Funcionando |
| Clips de video de las alertas y avisos por Telegram / correo / WhatsApp / webhook | Funcionando |
| Búsqueda por descripción ("camioneta blanca") | Opcional; probado con el modelo real en español |
| Video en vivo por WebRTC (go2rtc) con cajas dibujadas en el navegador | Opcional; MJPEG como respaldo automático |
| Armas blancas (YOLO11-COCO) | Apagado por defecto: no detectó en prueba real (ver abajo) |
| Varias cámaras en un solo proceso con modelos compartidos | Probado con 2 Hikvision reales; con más de 2, un proceso por cámara rinde 67 % más ([medición](docs/evidencias/README.md)) |
| **Catálogo de cámaras**: buscar (ONVIF, puertos, MAC), diagnosticar (ISAPI/ONVIF/RTSP), recomendar instalación y dar de alta desde el navegador | Funcionando; probado con una cámara simulada en las pruebas, pendiente con la Hikvision del stand |
| PostgreSQL + Redis (varios procesos de API), Docker, HTTPS | Funcionando |

---

## Arquitectura

Procesos con responsabilidades separadas, unidos por un contrato de evento
([shared/events.py](shared/events.py)):

```
   [Cámaras Hikvision / webcam / archivo]──────────────┐ RTSP
                  │ RTSP                                ▼
                  ▼                              ┌─────────────┐
   ┌──────────────────────────────────┐          │   go2rtc    │ video WebRTC
   │ edge/  worker de borde (GPU)     │          │  (opcional) │ sin recodificar
   │  todas las cámaras en 1 proceso, │          └──────┬──────┘
   │  modelos compartidos; zonas,     │                 │
   │  clips, eventos ISAPI            │                 │
   └──────────────┬───────────────────┘                 │
                  │ POST /api/events, latido, clips,    │
                  │ cajas en vivo                       │
                  ▼                                     │
   ┌──────────────────────────────────┐                 │
   │ api/  FastAPI (1..N procesos)    │─► Telegram, correo, WhatsApp, webhook
   │  PostgreSQL/SQLite + Redis       │                 │
   │  cruza LISTA NEGRA y REGLAS,     │                 │
   │  decide severidad                │                 │
   └──────────────┬───────────────────┘                 │
                  │ WebSocket, SSE, MJPEG               │
                  ▼                                     ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ web/  dashboard: Monitoreo · Registro · Mapa · Administración │
   └─────────────────────────────────────────────────────────────┘
```

**Regla de diseño:** el borde no decide qué es una alerta. Solo reporta *"vi
esto, con esta confianza"* (o *"alguien cruzó la línea X"*). El cruce contra la
lista negra, el horario de las reglas y la severidad los decide la API, que es
quien tiene la base de datos; por eso la lista negra y las reglas se cambian
sin tocar los workers.

### Estructura

```
├── shared/              Contrato compartido entre borde y API
│   ├── events.py        DetectionEvent, Severity, MatchResult
│   ├── plates.py        Formatos de placas mexicanas, corrección y cruce
│   ├── zonas.py         Geometría y horarios de las reglas por zona
│   └── fechas.py        Todo en UTC y con zona
├── edge/                Worker de inferencia
│   ├── worker.py        Bucle principal, varias cámaras por proceso
│   ├── detectors/       placas, rostros, movimiento, pose, armas
│   ├── zonas.py         Motor de reglas por zona
│   ├── clips.py         Clips de video de las alertas
│   ├── isapi.py         Eventos de la cámara Hikvision (sabotaje, pérdida de video)
│   ├── dataset.py       Cuadros de alertas para reentrenar
│   ├── sources.py       webcam | archivo | RTSP | GStreamer, con decodificación por GPU
│   ├── aceleracion.py   FP16, TensorRT, NVDEC
│   ├── sink.py          Envío por lotes con respaldo en disco
│   └── preview.py       Video y cajas para el dashboard
├── api/                 Plataforma web
│   ├── main.py          App FastAPI, WebSocket, tareas de fondo
│   ├── matching.py      Cruce contra lista negra y reglas
│   ├── zonas.py         Severidad de las reglas según su horario
│   ├── notificaciones.py  Telegram, correo, WhatsApp, webhook
│   ├── camaras_caidas.py  Vigilante de cámaras sin señal
│   ├── semantica.py     Búsqueda por descripción
│   ├── retention.py     Purga automática de datos personales
│   ├── migraciones/     Alembic: el esquema se actualiza solo
│   └── routers/         auth, usuarios, bitácora, eventos, alertas, zonas, ...
├── web/                 Dashboard (HTML + JS, sin compilación ni CDN)
├── tools/               init_plataforma, probe_camara, optimizar_modelos, go2rtc_config,
│                        dataset_alertas, exportar_dataset, purgar_datos, respaldo,
│                        metricas, placa_demo, grabar_video, ...
├── docker/              Imágenes, Caddyfile, go2rtc
├── tests/               267 pruebas sin cámara ni GPU
├── docs/                Hikvision, despliegue, privacidad, licencias, reentrenamiento,
│                        evidencias medidas (docs/evidencias)
├── demo/                Placa de prueba imprimible; videos del plan de contingencia
└── entrega/             Presentación y memoria técnica de la Fase 2
```

---

## Instalación

```bash
python -m venv venv
venv\Scripts\activate

# 1. torch CON CUDA -- va aparte, NO desde requirements.txt
pip install torch==2.13.0+cu126 torchvision==0.28.0+cu126 --index-url https://download.pytorch.org/whl/cu126

# 2. el resto, con las versiones probadas
pip install -r requirements.txt -c constraints.txt

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

**Con Docker** (equipo dedicado con GPU NVIDIA): `docker-compose.yml` levanta
todo, con HTTPS, PostgreSQL, Redis y go2rtc. Ver
**[docs/despliegue.md](docs/despliegue.md#con-docker)**.

---

## Uso

```bash
iniciar_api.bat                 # o: python -m api
iniciar_worker.bat              # o: python -m edge.worker
```

Dashboard en `http://localhost:8000`, documentación de la API en `/docs`.

Antes de cargar modelos conviene verificar la fuente de video:

```bash
python -m edge.worker --diagnostico
```

Reporta fps reales, latencia, frames descartados, reconexiones y uso de CPU. Si
aquí los números están mal, ningún modelo lo va a arreglar.

Cambiar de fuente es solo editar `SOURCE` en el `.env`:

```bash
SOURCE=webcam:0
SOURCE=file:videos/prueba.mp4
SOURCE=rtsp://operador:pass@192.168.1.64:554/Streaming/Channels/102
```

Para encontrar la cámara y su URL RTSP: apartado **Cámaras** del dashboard, o
`python tools/probe_camara.py --descubrir` (ver
**[docs/camara-hikvision.md](docs/camara-hikvision.md)**).

### Catálogo de cámaras

Apartado **Cámaras** del dashboard (solo administradores), en cuatro pasos:

1. **Buscar** en todas las redes del equipo, sin mandar contraseñas: ONVIF
   WS-Discovery, puertos de cámara, huella ISAPI/Dahua y fabricante por
   prefijo MAC. Los teléfonos y laptops (MAC aleatoria, WSD de Windows) se
   separan como "otro equipo".
2. **Diagnosticar** con la contraseña, que se prueba **una sola vez**:
   marca, modelo, firmware y serie (ISAPI u ONVIF), cada stream con codec,
   resolución y fps, confirmado con RTSP DESCRIBE; vista previa y aviso si el
   stream va en H.265 (sin WebRTC garantizado).
3. **Recomendar** según función, altura, distancia y lente: px por metro,
   px de placa y de rostro, ángulo vertical, alcance y nivel DORI
   (IEC 62676-4), usos posibles, detectores sugeridos y lo que hay que
   validar en sitio ([shared/instalacion.py](shared/instalacion.py)).
4. **Dar de alta**: escribe el `.env` de la cámara y guarda su ficha (sin
   credenciales). El worker se **inicia y detiene desde el navegador**
   cuando API y workers corren en el mismo equipo (`PERMITIR_INICIAR_WORKER`).

Cada dato dice su fuente (la cámara, el instalador o una regla de GOSS) y
una cámara se reconoce por serie o MAC aunque el router le cambie la IP.
También acepta altas manuales: URL RTSP, webcam o un video de `demo/videos`
que hace de cámara (en bucle y a velocidad real, `SOURCE_LOOP` y
`SOURCE_REALTIME`).

### Varias cámaras

Todas en **un solo proceso**: los modelos se cargan una vez y los comparten.
Cada cámara corre en su hilo; si una se cae, las demás siguen.

Medido en una RTX 4050: con 4 cámaras en un proceso la GPU queda al 34 % y el
total es de 11 cuadros/s (el límite es el GIL de Python); con un proceso por
cámara, 18 cuadros/s y la GPU al 77 %. Con más de 2 cámaras conviene un
proceso por cámara, que es lo que hace el catálogo
([docs/evidencias](docs/evidencias/README.md)).

```bash
python -m edge.worker --env .env --env .env.cam2 --env .env.cam3
python -m edge.worker --carpeta camaras          # todos los camaras/*.env
```

El alta desde el dashboard crea el archivo de cada cámara (copiando el token y
los detectores del `.env`).

### El dashboard

| Apartado | Para qué | Qué muestra |
|---|---|---|
| **Monitoreo** | Pantalla de guardia | Cámaras en vivo con las cajas dibujadas (MJPEG, o WebRTC con go2rtc); detecciones conforme entran |
| **Registro** | Pantalla de trabajo | Búsqueda de eventos por placa, tipo, estado, fechas o **descripción**; alertas por atender con su foto y **clip** |
| **Mapa** | Dónde está cada cámara | Cámaras en línea / sin señal; una alerta crítica parpadea en su punto |
| **Cámaras** | Solo administradores | Catálogo de cámaras: buscar, diagnosticar, recomendar instalación, dar de alta e iniciar su worker |
| **Administración** | Solo administradores | Usuarios y roles, bitácora, dataset de placas, notificaciones |

- Tres roles: **Consulta** mira, **Operador** además atiende alertas y
  exporta, **Administrador** además gestiona lista negra, cámaras, zonas y
  usuarios. Todo cambio queda en la **bitácora de auditoría**.
- La búsqueda de placas ignora guiones y espacios: `abc123` encuentra
  `ABC-123-A`. Una lectura mal hecha se **corrige** desde la fila (lápiz): se
  vuelve a cruzar con la lista negra y queda como dato para reentrenar el OCR.
- Cada alerta tiene **folio** (`ALR-000123`), foto ampliable, **clip de video**
  de los segundos antes y después, y se cierra con una **nota de atención**.
- **Zonas y reglas** se dibujan sobre el video de cada cámara (icono de zona
  en su recuadro).
- Una alerta crítica muestra un banner y suena, y llega al celular del
  responsable (Telegram, correo, WhatsApp o webhook).
- La vista en vivo no cuesta nada mientras nadie mira: al salir de Monitoreo
  o con la pestaña en segundo plano, el worker deja de codificar y subir video.

### Cruce contra lista negra y reglas

| Detección | Estrategia | Resultado |
|---|---|---|
| Placa idéntica | Normalización + match exacto | `critical` |
| Placa con error de OCR (`A8C-I23`) | Corrección por formato mexicano | `critical` — misma placa |
| Placa parecida (`ABD-123`) | Distancia de edición ≤ 1 | `warning` — *posible* coincidencia |
| Rostro | Similitud coseno ≥ `FACE_MATCH_THRESHOLD` | severidad del registro |
| Movimiento súbito, caída, manos arriba, posible agresión | Regla | `warning` |
| Zona: intrusión, cruce de línea, merodeo | Regla **dentro de su horario** | la que elija el administrador |
| Sabotaje / pérdida de video de la cámara | Evento ISAPI de la cámara | `critical` |
| Cámara sin imagen más de `NOTIFY_CAMARA_CAIDA_S` | Vigilante de la API | `warning` + aviso al recuperarse |
| Arma confirmada | Regla | `critical` |

Una coincidencia difusa **nunca** se titula como un hecho: la alerta dice
"Posible placa ABC-123 (se leyó ABD-123)". Cada registro puede tener
**vigencia**. Al dar de alta una placa o una persona, el sistema **revisa el
pasado** y genera la alerta si ya había pasado frente a una cámara.

**Si la plataforma se cae, el worker no pierde nada.** Lo que no se pudo
enviar va a `data/spool/` y se reenvía solo cuando la API vuelve.

---

## Lectura de placas

1. **Detección** con YOLOv9 entrenado solo con placas
   (`open-image-models`, ONNX, mAP50 0.966).
2. **Seguimiento**: un vehículo = un `track_id` = un evento, emitido cuando
   sale de escena con la mejor evidencia acumulada.
3. **OCR** con `fast-plate-ocr` (un transformer entrenado con placas, no un
   OCR de texto general): menos de 1 ms por lectura, hasta 10 por vehículo.
4. **Formatos mexicanos** (NOM-001-SCT-2-2016 y anteriores): cada formato dice
   qué posiciones son letras y cuáles dígitos, así que `A8C-I23` se corrige a
   `ABC-123`. Sin `I`, `Ñ`, `O` ni `Q` donde la norma no las usa. La alerta dice
   el **tipo de placa y la entidad** ("Automóvil particular de Jalisco").
5. **Placas extranjeras** (Texas, California...): el OCR reconoce el país y no
   se fuerzan al formato mexicano.
6. **Consenso** entre las lecturas del mismo vehículo y **color aproximado**
   del vehículo para que la alerta diga qué buscar.

Detalle y cómo reentrenar el OCR con placas propias:
**[docs/placas-mexicanas.md](docs/placas-mexicanas.md)**.

| Condición | YOLOv5 + EasyOCR (anterior) | YOLOv9 + OCR de placas |
|---|---|---|
| De frente | 4/5 | 5/5 |
| De lado 35° / 50° | 2/5 / 2/5 | 5/5 / 5/5 |
| Desde arriba | 2/5 | 5/5 |
| Rotada 15° | 3/5 | 5/5 |
| Lejos | 3/5 | 5/5 |
| Poca luz | 4/5 | 5/5 |
| Tiempo por foto | 50–280 ms | 22–29 ms |

### Alcance medido

Hace falta que la placa mida **≥ 36 px de ancho** para leerla. Con placa de
frente:

| Lente | Sub-stream 1280 px | **Main 3200 px** |
|---|---|---|
| 2.8 mm (gran angular) | 4.1 m | **10.2 m** |
| 4 mm (estándar) | 6.0 m | **15.1 m** |
| 6 mm (teleobjetivo) | 10.6 m | **26.6 m** |

En ángulo de 45° multiplica por 0.7, y para instalar conviene el doble de
margen (~100 px). Reproducible con `python tools/calibrar_distancia.py`.
Conclusiones prácticas: **el lente manda más que el modelo** y **la cámara va
apuntada al carril**, no al estacionamiento entero.

---

## Reconocimiento facial

Modelo **InsightFace `buffalo_l`** (ArcFace, embeddings de 512-d) sobre
onnxruntime-GPU. Un rostro de menos de **50 px** se descarta; la calidad de
cada vista combina tamaño, confianza, **pose** y **nitidez**; el embedding del
evento promedia las 3 mejores vistas de la persona seguida y, con Hikvision,
se recalcula sobre la foto del canal principal.

Dar de alta a alguien exige declarar un **fundamento legal**. Los embeddings
de quien **no** está en la lista negra no se guardan nunca.

> `buffalo_l` es de **uso no comercial**: ver [docs/licencias.md](docs/licencias.md).

---

## Movimiento, caídas y posturas

**Movimiento súbito.** Mide qué tan rápido se mueve una persona respecto a su
propio tamaño en pantalla (alturas de cuerpo por segundo): un forcejeo, un
golpe o alguien corriendo comparten una firma de velocidad muy por encima de
caminar. Confirmación temporal (3 de 5 lecturas) y saltos imposibles del
tracker descartados.

**Pose (YOLO11-pose, `ENABLE_POSE`).** El esqueleto distingue lo que la caja no:

| Evento | Criterio |
|---|---|
| **Persona caída** | El torso pasa de vertical a horizontal en < 2 s **y la cadera baja** hacia el piso. Agacharse o acostarse despacio no cuentan. |
| **Manos arriba** | Las dos muñecas sobre la cabeza, con el torso erguido, sostenido `MANOS_ARRIBA_S` segundos: postura de asalto. |
| **Posible agresión** | Muñecas muy rápidas en varios frames seguidos junto a otra persona. Estimación: amerita mirar. |

Todo sale como `warning`: el operador decide, no una alarma automática.

---

## Zonas y reglas

Se dibujan en el dashboard sobre el video de cada cámara (solo
administradores) y el worker las aplica sobre las personas y vehículos que ya
sigue, sin correr otro modelo:

| Regla | Ejemplo |
|---|---|
| **Intrusión** con horario | Alguien en el patio de 22:00 a 06:00 |
| **Cruce de línea** con sentido | Un vehículo que entra por la salida |
| **Merodeo** | Alguien más de 90 s junto al cajero |
| **Conteo** | Vehículos que entran y salen por hora (estadística, sin alerta) |

El pie de la persona (no el centro de la caja) decide si está dentro; una
franja alrededor de las líneas evita contar cruces cuando alguien se queda
parado sobre ellas. Los horarios son hora local del sitio (`ZONA_HORARIA`) y
la API los juzga con la hora del **evento**.

## La cámara misma

- **Eventos ISAPI de Hikvision** (`ISAPI=auto`): lente tapado, cámara movida o
  desenfocada, pérdida de video y la analítica propia de la cámara. Ver
  [docs/camara-hikvision.md](docs/camara-hikvision.md).
- **Cámara caída**: la API avisa si una cámara lleva más de
  `NOTIFY_CAMARA_CAIDA_S` sin imagen (worker caído, red cortada, cámara que no
  entrega video), y cuando se recupera. El aviso sale una sola vez aunque la
  API corra en varios procesos.

## Clips y avisos al celular

- **Clips**: el worker guarda en memoria los últimos segundos; cuando la API
  responde que un evento fue alerta, arma un clip de `CLIP_PRE_S` antes y
  `CLIP_POST_S` después (H.264 con PyAV, o VP8) y lo sube. "Ver clip" aparece
  en la alerta sin recargar.
- **Notificaciones** (`NOTIFY_*`): Telegram, correo, WhatsApp (Twilio) y
  webhook firmado. Solo críticas por defecto, sin repetidos, con tope por
  minuto y **sin foto** salvo que el aviso de privacidad lo contemple. Se
  prueban desde *Administración > Notificaciones*.

## Búsqueda por descripción (opcional)

Con `SEMANTIC_SEARCH=true`, un modelo de visión y lenguaje multilingüe
(SigLIP) permite buscar en las capturas **en español**: "camioneta blanca",
"persona con mochila roja". Cada búsqueda queda en la bitácora y los vectores
se borran junto con su foto.

---

## Detección de armas

**`ENABLE_WEAPONS=false` por defecto.** Probado contra la cámara real con un
cuchillo en mano: YOLO11-COCO no lo detectó (COCO no tiene arma de fuego y
reconoce cuchillos de foto de producto). El código queda listo para un modelo
afinado (`models/weapons.pt`), con confirmación temporal de 4 de 6 frames.

El camino para tenerlo: activar `DATASET_ENABLED`, que los operadores
califiquen las alertas y armar el dataset con `tools/dataset_alertas.py`
(**[docs/reentrenamiento.md](docs/reentrenamiento.md)**).

> No descargues pesos `.pt` de repositorios desconocidos: son archivos pickle
> y **ejecutan código arbitrario al cargarse**.

---

## Seguridad

- Contraseñas con bcrypt; sesión JWT con **revocación** (cambiar la
  contraseña o el rol cierra las sesiones abiertas); límite de intentos de
  login compartido entre procesos.
- La evidencia (fotos, clips, video en vivo) solo se sirve con sesión; en el
  navegador la sesión viaja en una cookie `HttpOnly` y `SameSite=Strict`, y
  las acciones que cambian algo exigen la cabecera `Authorization` (CSRF).
- Política de seguridad de contenido estricta: sin JavaScript en línea ni de
  CDN (las librerías van dentro del proyecto).
- **HTTPS** con `tools/generar_certificado.py` o con Caddy (Docker).
- go2rtc solo es alcanzable a través del proxy, con sesión y solo para
  negociar el video.
- **Bitácora de auditoría** de todo cambio y de toda búsqueda por
  descripción.

## Privacidad

Retención por capas, con purga automática cada 24 h:

| Dato | Plazo |
|---|---|
| Fotos de eventos sin coincidencia | 7 días |
| Eventos sin coincidencia (solo texto) | 30 días |
| Clips de video de alertas | 90 días |
| Alertas y su evidencia | 1 año |
| Embeddings de quien no está en la lista negra | nunca se guardan |

```bash
python tools/purgar_datos.py --simular
```

Obligaciones al instalarlo, notificaciones, clips y búsqueda:
**[docs/privacidad.md](docs/privacidad.md)**.

## Licencias

Casi todo es MIT/BSD/Apache, **salvo tres piezas** que deciden si el sistema
se puede comercializar tal cual: **Ultralytics YOLO11 (AGPL-3.0)**, los pesos
de rostros **`buffalo_l` (no comercial)** y FFmpeg/x264 de los clips H.264
(GPL). Alternativas en **[docs/licencias.md](docs/licencias.md)**.

---

## Rendimiento medido

RTX 4050 Laptop (6 GB), Hikvision 1280×720 @ 20 fps, placas + rostros + armas
a la vez:

| Detector | ms/frame |
|---|---|
| Placas | 10.5 |
| Rostros | 21.4 |
| Armas | 12.0 |
| **Total** | **43.9** |

**7.8 fps de 8 objetivo, 1.6 GB de 6 GB de VRAM**, 0 reconexiones.

Para ir más rápido: `python tools/optimizar_modelos.py` mide FP16 y exporta
los YOLO a **TensorRT**; `ORT_TENSORRT=true` hace lo mismo con placas y
rostros; `HW_DECODE=auto` decodifica el video en la GPU; y varias cámaras en
un proceso comparten los modelos.

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

**267 pruebas en 23 archivos**, sin cámara ni GPU: placas mexicanas, tracking,
confirmación temporal, rostros, pose, zonas y horarios, ISAPI, clips,
notificaciones (servicios simulados), cámaras caídas, búsqueda, reentrenamiento,
WebRTC, seguridad, retención, catálogo de cámaras (con una cámara simulada
que responde ISAPI y RTSP) y la API completa contra una base temporal. Las
pruebas usan su propia carpeta de evidencia: nunca tocan `data/`.
Contra PostgreSQL y Redis reales:

```bash
PRUEBAS_POSTGRES="postgresql+psycopg://postgres@localhost:5432/postgres" \
PRUEBAS_REDIS=redis://localhost:6379/15 python tests/correr_todas.py
```

La integración continua corre todo en Linux y Windows (Python 3.11 y 3.12),
contra PostgreSQL 16 y Redis 7, y construye la imagen Docker de la API.

---

## Fase 2 del reto: herramientas para la demo

| Herramienta | Para qué |
|---|---|
| `python tools/respaldo.py [--con-evidencia] [--nube]` | Código (todas las ramas), `.env`, base de datos y secretos, con manifiesto SHA-256. `--nube` copia a OneDrive **sin** credenciales |
| `python tools/metricas.py --muestrear 300` | fps, ms por detector, latencia de la API, GPU, VRAM y CPU en vivo, a CSV |
| `python tools/metricas.py --resumen --desde AAAA-MM-DD` | Alertas, tiempo de atención, placas corregidas, caídas y recuperación, desde la base de datos |
| `python tools/placa_demo.py` | Placa de prueba imprimible (serie sin entidad asignada), validada con el detector y el OCR |
| `python tools/grabar_video.py --segundos 90` | Graba la cámara en el ensayo, para usar el video como cámara si en la sede falla la real |

Entregables en [entrega/](entrega/) (presentación y memoria técnica) y
resultados medidos en [docs/evidencias/](docs/evidencias/README.md).

---

## Despliegue

Servidor 24/7, servicios, Docker, WebRTC, varias cámaras y checklist de
producción: **[docs/despliegue.md](docs/despliegue.md)**. El código corre
igual en Windows y Linux.
