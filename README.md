# GOSS IP — Sistema de Videovigilancia

Detección de **placas mexicanas**, **rostros**, **movimiento anómalo**,
**caídas y posturas**, **reglas por zona** y **sabotaje de cámaras** sobre
video en vivo de cámaras IP, con cruce contra lista negra, alertas en tiempo
real, clips de evidencia y avisos al celular, en una plataforma web.

> **Caso de despliegue institucional / InnovaTICs:** la propuesta para un campus
> universitario se documenta en
> **[docs/arquitectura-campus-uaemex.md](docs/arquitectura-campus-uaemex.md)**.
> El plan de pruebas y estabilización para la final está en
> **[docs/validacion-innovatics.md](docs/validacion-innovatics.md)**.

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
| Varias cámaras en un solo proceso con modelos compartidos | Probado con 2 Hikvision reales |
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

### Despliegue físico por campus

El software no exige que todas las cámaras estén conectadas al mismo router ni
que pertenezcan físicamente al mismo edificio. La propuesta institucional usa:

- cámaras IP por Ethernet;
- switches PoE cercanos a cada grupo de cámaras;
- Cat6 para tramos de cobre dentro de su límite de diseño;
- fibra entre edificios o zonas distantes;
- VLAN de videovigilancia separada de usuarios;
- uno o varios nodos GPU de borde según carga;
- API, base de datos y auditoría centralizadas.

Para alturas iniciales de cámara, tipos de punto, PoE, cálculo de ancho de
banda, segmentación, redundancia, crecimiento por campus y ficha de
levantamiento por cámara:
**[docs/arquitectura-campus-uaemex.md](docs/arquitectura-campus-uaemex.md)**.

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
│                        dataset_alertas, exportar_dataset, purgar_datos, ...
├── docker/              Imágenes, Caddyfile, go2rtc
├── tests/               233 pruebas sin cámara ni GPU
└── docs/                despliegue, campus, privacidad, licencias, validación
```

---

## Instalación

```bash
python -m venv venv
venv\Scripts\activate

pip install torch==2.13.0+cu126 torchvision==0.28.0+cu126 --index-url https://download.pytorch.org/whl/cu126
pip install -r requirements.txt -c constraints.txt
python -c "import torch; print(torch.cuda.is_available())"
```

Sin GPU NVIDIA se instala `pip install torch torchvision` y el sistema corre en
CPU, más lento.

```bash
copy .env.example .env
python tools/init_plataforma.py
```

Guía paso a paso para el equipo:
**[docs/GUIA_PRUEBA.md](docs/GUIA_PRUEBA.md)**.

Con Docker:
**[docs/despliegue.md](docs/despliegue.md#con-docker)**.

---

## Uso

```bash
iniciar_api.bat
iniciar_worker.bat
```

Dashboard en `http://localhost:8000`, documentación de la API en `/docs`.

Antes de cargar modelos:

```bash
python -m edge.worker --diagnostico
```

Cambiar fuente es configuración:

```bash
SOURCE=webcam:0
SOURCE=file:videos/prueba.mp4
SOURCE=rtsp://operador:pass@192.168.1.64:554/Streaming/Channels/102
```

Para varias cámaras:

```bash
python -m edge.worker --env .env --env .env.cam2 --env .env.cam3
python -m edge.worker --carpeta camaras
```

El worker comparte modelos entre cámaras y mantiene cada fuente en su propio
hilo.

---

## El dashboard

| Apartado | Para qué | Qué muestra |
|---|---|---|
| Monitoreo | Pantalla de guardia | cámaras en vivo, cajas y detecciones |
| Registro | Trabajo operativo | eventos, alertas, filtros, foto y clip |
| Mapa | Ubicación | cámaras en línea/sin señal y alertas |
| Administración | Gestión | usuarios, roles, bitácora, zonas y avisos |

---

## Cruce contra lista negra y reglas

| Detección | Estrategia | Resultado |
|---|---|---|
| Placa idéntica | Normalización + match exacto | critical |
| Placa con error de OCR | Corrección por formato mexicano | critical |
| Placa parecida | Distancia de edición ≤ 1 | warning |
| Rostro | Similitud coseno ≥ umbral | severidad del registro |
| Movimiento/caída/postura | Regla | warning |
| Zona | Regla + horario | severidad configurada |
| Sabotaje/pérdida de video | Evento de cámara | critical |
| Cámara sin imagen | Vigilante de API | warning |
| Arma confirmada | Regla | critical |

La coincidencia difusa nunca se presenta como hecho confirmado. Si la API se
cae, el worker conserva eventos en `data/spool/` y los reenvía.

---

## Lectura de placas

El flujo usa detección, tracking, OCR especializado, formatos mexicanos,
consenso por vehículo y cruce contra lista negra. Para instalación física, el
lente y la geometría del carril son tan importantes como el modelo.

Detalle:
**[docs/placas-mexicanas.md](docs/placas-mexicanas.md)**.

---

## Reconocimiento facial

InsightFace `buffalo_l` con embeddings de 512 dimensiones. Requiere
calibración de umbral y fundamento legal para altas. Los embeddings de personas
no registradas no se conservan.

> `buffalo_l` tiene restricciones de uso: ver
> **[docs/licencias.md](docs/licencias.md)**.

---

## Movimiento, caídas y posturas

YOLO11 + tracking y YOLO11-pose permiten estimar movimiento súbito, caídas,
manos arriba y posible agresión. Son señales para revisión del operador, no
sentencias automáticas.

---

## Zonas y reglas

Intrusión por horario, cruce de línea con sentido, merodeo y conteo se
configuran por cámara. Son especialmente útiles en accesos restringidos,
pasillos, patios y estacionamientos.

---

## Detección de armas

**`ENABLE_WEAPONS=false` por defecto.** El modelo generalista no alcanzó la
fiabilidad necesaria en prueba real; no debe venderse como función operativa
hasta contar con un modelo y dataset validados.

---

## Seguridad y privacidad

- bcrypt y JWT revocable;
- cookie HttpOnly/SameSite para lectura;
- autorización para cambios;
- CSP estricta;
- HTTPS;
- go2rtc detrás de proxy;
- bitácora de auditoría;
- retención por capas.

Ver:
**[docs/privacidad.md](docs/privacidad.md)** y
**[docs/arquitectura-campus-uaemex.md](docs/arquitectura-campus-uaemex.md)**.

---

## Rendimiento y pruebas

El proyecto contiene 233 pruebas automatizadas en 21 archivos y CI en Linux y
Windows, además de PostgreSQL, Redis y construcción de imagen Docker.

```bash
python tests/correr_todas.py
```

Para la preparación de InnovaTICs, no basta con CI: deben registrarse métricas
de campo de red, placa, pose, reglas, resiliencia, carga multicámara y
almacenamiento. Ver:
**[docs/validacion-innovatics.md](docs/validacion-innovatics.md)**.

---

## Despliegue

Servidor 24/7, Docker, WebRTC, varias cámaras y checklist de producción:
**[docs/despliegue.md](docs/despliegue.md)**.

Diseño físico y red para campus:
**[docs/arquitectura-campus-uaemex.md](docs/arquitectura-campus-uaemex.md)**.
