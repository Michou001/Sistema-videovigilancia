# GOSS IP

**Plataforma de análisis inteligente de videovigilancia para la gestión
preventiva de incidentes en instalaciones institucionales.**

## El problema

Una institución con decenas de cámaras no tiene quién mire todas a la vez.
Instalar cámaras no garantiza que un evento relevante (un vehículo con reporte,
alguien en un área restringida a deshoras, una cámara que dejó de transmitir)
sea **visto, evaluado y canalizado** a tiempo al responsable. La mayor parte del
video solo se revisa después, cuando ya pasó algo.

## Qué propone GOSS IP

Analizar el video de las cámaras IP que la institución ya tiene, **en sus
propios equipos**, y convertir lo relevante en **alertas priorizadas y
verificables** para que una persona decida qué hacer:

1. las cámaras capturan video;
2. los modelos analizan los cuadros en un equipo local;
3. el sistema identifica un evento o una coincidencia;
4. se genera una **alerta preliminar** con foto, clip y folio;
5. el monitorista revisa la evidencia;
6. la clasifica: confirmada, falso aviso, indeterminada, duplicada o ensayo;
7. si corresponde, la **canaliza** al responsable institucional (queda en la bitácora);
8. el personal autorizado decide las acciones conforme a sus protocolos.

**La IA no decide.** No determina culpabilidad ni peligrosidad, no llama a la
policía y ninguna coincidencia facial o vehicular dispara por sí sola una
confrontación, detención o sanción. Una alerta es un aviso para revisión, no un
incidente confirmado.

**Para quién:** el personal de supervisión y seguridad de instituciones
educativas, edificios públicos y otras instalaciones con cámaras IP compatibles
y personal de monitoreo. **Caso de estudio:** un campus de la UAEMéx
([contexto y fuentes](docs/contexto-uaemex.md)). La universidad no ha
autorizado ni adoptado el proyecto; es el entorno de referencia para diseñarlo
y evaluarlo.

**Qué ofrece y qué no inventa.** La lectura de placas, la comparación facial y
la detección de objetos ya existen. La propuesta es **integrarlas** en una
plataforma modular y configurable: cámaras existentes, procesamiento local,
reglas por zona y horario, alertas centralizadas con evidencia, revisión humana
obligatoria, control de acceso por roles con verificación en dos pasos,
bitácora y retención configurable. Que esa integración sea más útil o más
económica que las alternativas comerciales es una **hipótesis por validar**
([comparación y métricas](docs/validacion-innovatics.md#14-comparación-con-soluciones-existentes)).

## Estado actual

**Prototipo funcional, con validación técnica parcial** (pruebas automatizadas,
ensayos en laboratorio y en el stand con dos cámaras Hikvision). **No** es un
piloto institucional autorizado ni un sistema listo para producción; lo que
falta para cada etapa está en
[docs/validacion-innovatics.md](docs/validacion-innovatics.md).

| Módulo | Estado | Evidencia y límites |
|---|---|---|
| Placas mexicanas (YOLOv9 + OCR + formatos NOM-001-SCT-2-2016) | Implementado, probado en ensayo | 35/35 lecturas en la prueba de ángulos ([ADR-001](docs/adr/ADR-001-lectura-de-placas.md)); en el stand, lecturas correctas a 1–2 m. Con lente de 2.8 mm y a más de ~3 m la placa queda muy pequeña: **requiere validación de campo** y, según la instalación, otro lente |
| Alertas, revisión humana, canalización y ficha de evidencia (SHA-256) | Implementado y probado | Ciclo completo cubierto por pruebas automatizadas; métricas de revisión en `/api/alerts/metricas` |
| Reglas por zona y horario (intrusión, cruce de línea, merodeo, conteo) | Implementado, pruebas automatizadas | Umbrales (p. ej. merodeo de 30–45 s) son parámetros de ensayo; falta medir falsos positivos en sitio |
| Cámaras Hikvision: RTSP, eventos ISAPI, reconexión, aviso de cámara caída | Implementado, probado con 2 cámaras reales | Reconexión cubierta por pruebas; comportamiento en red institucional por validar |
| Comparación facial (InsightFace, 512-d) | Implementado, **apagado por defecto** | Funciona en cámara real; el umbral no está calibrado con población real. Solo con autorización y referencias con fundamento ([privacidad](docs/privacidad.md)) |
| Movimiento súbito y caída estimada | **Experimental** | Señales para revisión; sin validación estadística |
| Pose: caída, manos arriba, posible agresión | **Experimental, apagado por defecto** | No apto para seguridad operativa hasta validarlo en campo |
| Armas blancas | **Deshabilitado** | El modelo generalista no detectó en prueba real |
| Clips de alertas y avisos (Telegram, correo, WhatsApp, webhook) | Implementado | Telegram probado con el bot del equipo; por defecto el aviso no lleva foto |
| Búsqueda por descripción, video WebRTC | Opcional, apagado por defecto | Probados en laboratorio |
| Seguridad: roles, verificación en dos pasos, HTTPS, bitácora, CI de seguridad | Implementado | [docs/seguridad-red.md](docs/seguridad-red.md) |
| Varias cámaras, PostgreSQL + Redis, Docker | Implementado | Medido con 1 y 4 cámaras en una RTX 4050 ([evidencias](docs/evidencias/README.md)); capacidad por nodo en producción sin certificar |

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

### Despliegue físico

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
banda, segmentación, redundancia, crecimiento por edificio o campus y ficha de
levantamiento por cámara:
**[docs/arquitectura-despliegue.md](docs/arquitectura-despliegue.md)**.

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
├── tests/               301 pruebas sin cámara ni GPU
├── docs/                Hikvision, despliegue, campus, privacidad, licencias,
│                        reentrenamiento, validación, evidencias medidas
├── demo/                Placa de prueba imprimible; videos del plan de contingencia
├── 2_documentacion_fase2/  Memoria técnica, portafolio de evidencias, bitácora del Bootcamp
└── 3_mercadotecnia/       Presentación, loop del logo para el stand y presskit
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

Dashboard en `http://localhost:8000`. La documentación interactiva de la API
(`/docs`) se activa con `API_DOCS=true` en el `.env`; en producción va apagada.

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
python -m edge.worker --carpeta camaras
```

---

## El dashboard

| Apartado | Para qué | Qué muestra |
|---|---|---|
| **Monitoreo** | Pantalla de guardia | Cámaras en vivo con las cajas dibujadas (MJPEG, o WebRTC con go2rtc); detecciones conforme entran |
| **Registro** | Pantalla de trabajo | Búsqueda de eventos por placa, tipo, estado, fechas o **descripción**; alertas por atender con su foto y **clip** |
| **Mapa** | Dónde está cada cámara | Cámaras en línea / sin señal; una alerta crítica parpadea en su punto |
| **Cámaras** | Solo administradores | Catálogo de cámaras: buscar, diagnosticar, recomendar instalación, dar de alta e iniciar su worker |
| **Administración** | Solo administradores | Usuarios y roles, bitácora, zonas, dataset de placas, notificaciones |

---

## Cruce contra el registro institucional de alertas y reglas

La "lista negra" es un **registro institucional de alertas**: placas o
personas que la institución responsable decidió vigilar, cada una con motivo,
fundamento, vigencia y autor en la bitácora. No es una clasificación automática
de personas peligrosas, y una coincidencia solo abre una alerta para revisión
humana. Quién puede dar de alta, con qué soporte y cuándo se borra:
[docs/privacidad.md](docs/privacidad.md#registro-institucional-de-alertas-lista-negra).
Para demostraciones se usan solo registros simulados o de participantes que lo
autorizaron.

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

InsightFace `buffalo_l` con embeddings de 512 dimensiones, **apagado por
defecto** (`ENABLE_FACES`). Requiere calibrar el umbral con población real y
fundamento para cada alta. De las personas que no coinciden con el registro no
se conserva ni el vector ni la foto (solo el evento: hora y cámara); al dar de
baja a alguien se borran su vector y su foto de referencia.

> `buffalo_l` tiene restricciones de uso: ver
> **[docs/licencias.md](docs/licencias.md)**.

---

## Movimiento, caídas y posturas

YOLO11 + tracking y YOLO11-pose permiten estimar movimiento súbito, caídas,
manos arriba y posible agresión. **Son experimentales**: señales para revisión
del operador, sin validación estadística en campo, y no aptas todavía para
seguridad operativa. La pose viene apagada (`ENABLE_POSE=false`); el
seguimiento de movimiento queda encendido porque lo usan las reglas por zona.

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
- verificación en dos pasos (TOTP: Google/Microsoft Authenticator), exigible por rol;
- el dashboard solo escucha en el propio equipo salvo que se abra con HTTPS;
- CI de seguridad: pip-audit, gitleaks y Trivy en cada push;
- cookie HttpOnly/SameSite para lectura;
- autorización para cambios;
- CSP estricta;
- HTTPS;
- go2rtc detrás de proxy;
- bitácora de auditoría;
- retención por capas.

Ver:
**[docs/seguridad-red.md](docs/seguridad-red.md)** (qué se expone en la red y
cómo se cierra), **[docs/privacidad.md](docs/privacidad.md)** y
**[docs/arquitectura-despliegue.md](docs/arquitectura-despliegue.md)**.

---

## Rendimiento y pruebas

```bash
python tests/correr_todas.py
```

**301 pruebas en 26 archivos**, sin cámara ni GPU: placas mexicanas, tracking,
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

Para la preparación de InnovaTICs no basta con CI: deben registrarse métricas
de campo de red, placa, pose, reglas, resiliencia, carga multicámara y
almacenamiento. Ver
**[docs/validacion-innovatics.md](docs/validacion-innovatics.md)**.

---

## Fase 2 del reto: herramientas para la demo

| Herramienta | Para qué |
|---|---|
| `python tools/respaldo.py [--con-evidencia] [--nube]` | Código (todas las ramas), `.env`, base de datos y secretos, con manifiesto SHA-256. `--nube` copia a OneDrive **sin** credenciales |
| `python tools/metricas.py --muestrear 300` | fps, ms por detector, latencia de la API, GPU, VRAM y CPU en vivo, a CSV |
| `python tools/metricas.py --resumen --desde AAAA-MM-DD` | Alertas, tiempo de atención, placas corregidas, caídas y recuperación, desde la base de datos |
| `python tools/placa_demo.py` | Placa de prueba imprimible (serie sin entidad asignada), validada con el detector y el OCR |
| `python tools/grabar_video.py --segundos 90` | Graba la cámara en el ensayo, para usar el video como cámara si en la sede falla la real |
| `python tools/marca/loop_logo.py 3_mercadotecnia/loop/GOSS_IP_Loop_Stand.mp4` | Loop del logo para el monitor del stand (12 s, 1080p, sin corte); la frase se cambia en el script |

Entregables de la Fase 2: memoria técnica, portafolio de evidencias y bitácora
del Bootcamp en [2_documentacion_fase2/](2_documentacion_fase2/); presentación y
loop del stand en [3_mercadotecnia/](3_mercadotecnia/)
(`loop/GOSS_IP_Loop_Stand.html` lo reproduce a pantalla completa sin internet).
Resultados medidos en [docs/evidencias/](docs/evidencias/README.md).

---

## Despliegue

Servidor 24/7, Docker, WebRTC, varias cámaras y checklist de producción:
**[docs/despliegue.md](docs/despliegue.md)**.

Diseño físico y red para campus:
**[docs/arquitectura-despliegue.md](docs/arquitectura-despliegue.md)**.
