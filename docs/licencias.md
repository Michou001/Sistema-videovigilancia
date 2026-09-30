# Licencias de los modelos y librerías

Casi todo lo que usa el sistema es software libre con licencias permisivas
(MIT, BSD, Apache). **Tres piezas no lo son**, y deciden si el sistema se
puede vender o dar como servicio tal cual está:

| Pieza | Licencia | Qué implica |
|---|---|---|
| **Ultralytics / YOLO11** (movimiento, pose, armas, zonas) | **AGPL-3.0** | Uso interno: sin problema. Venderlo, instalarlo a clientes o darlo como servicio obliga a publicar el código fuente de TODO el sistema bajo AGPL, **o** a comprar la [licencia Enterprise de Ultralytics](https://www.ultralytics.com/license). |
| **InsightFace `buffalo_l`** (rostros) | **Solo uso no comercial** (los pesos; el código es MIT) | Para uso comercial hay que licenciarlo con InsightFace o cambiar de modelo (ver abajo). |
| **FFmpeg con x264** dentro de PyAV (clips en H.264, opcional) | **GPL** + patentes de H.264 | Usarlo en el propio equipo está bien. Distribuirlo empaquetado obliga a la GPL. Sin PyAV los clips salen en VP8/WebM, libre de regalías. |

> Esto es una guía técnica, no asesoría legal. Antes de comercializar el
> sistema conviene que un abogado revise estas tres licencias.

## Si el sistema se va a comercializar

- **YOLO11 → otro detector.** Las placas ya usan YOLOv9 de
  `open-image-models` (MIT). Para personas y vehículos hay detectores con
  licencia Apache-2.0 (RT-DETR, YOLOX) que se pueden exportar a ONNX y cargar
  con onnxruntime, igual que las placas. La alternativa es la licencia
  Enterprise de Ultralytics.
- **buffalo_l → un modelo de rostros con licencia comercial**, en el mismo
  formato ONNX de InsightFace (p. ej. AuraFace, Apache-2.0), o licenciar
  `buffalo_l`. Ojo: cambiar de modelo cambia los embeddings; hay que volver a
  dar de alta a las personas de la lista negra y recalibrar
  `FACE_MATCH_THRESHOLD`.
- **Clips en VP8** (`CLIP_CODEC=vp8`, o sin instalar PyAV).

## Todo lo demás

| Componente | Licencia |
|---|---|
| fast-plate-ocr, open-image-models, fast-alpr (placas) | MIT |
| onnxruntime / onnxruntime-gpu | MIT |
| PyTorch, torchvision | BSD-3-Clause |
| OpenCV | Apache-2.0 |
| open_clip (búsqueda por descripción, opcional) | MIT |
| Pesos SigLIP multilingüe de Google (`webli`) | Apache-2.0 |
| transformers, sentencepiece | Apache-2.0 |
| FastAPI, Starlette, Pydantic, SQLModel, SQLAlchemy, Alembic | MIT |
| uvicorn, httpx | BSD-3-Clause |
| psycopg | LGPL-3.0 (uso como librería: sin obligaciones para el sistema) |
| redis-py, PyJWT | MIT |
| bcrypt, cryptography, tzdata | Apache-2.0 / BSD |
| NumPy, psutil | BSD-3-Clause |
| PyAV (sin contar el FFmpeg que trae) | BSD-3-Clause |
| **Lucide** (iconos, incluido en `web/vendor/`) | ISC — `web/vendor/LICENSE-lucide.txt` |
| **Leaflet** (mapa, incluido en `web/vendor/leaflet/`) | BSD-2-Clause — `web/vendor/leaflet/LICENSE` |
| Mosaicos de OpenStreetMap | Datos ODbL; atribución visible en el mapa. Su [política de uso](https://operations.osmfoundation.org/policies/tiles/) no permite uso intensivo: para muchos puestos de monitoreo, un servidor de mosaicos propio o un proveedor comercial |
| go2rtc | MIT |
| Caddy | Apache-2.0 |
| PostgreSQL | PostgreSQL License (tipo MIT) |
| Redis 7.4+ (imagen `redis:7-alpine`) | RSALv2 / SSPLv1: uso interno sin problema; no permite ofrecer Redis como servicio. Alternativa idéntica y BSD: `valkey/valkey:8-alpine` |

## El código de este proyecto

El repositorio no declara licencia propia: por defecto, todos los derechos
quedan con sus autores. Si se va a compartir o vender, conviene agregar un
`LICENSE` explícito y que sea compatible con las decisiones de arriba (por
ejemplo, con YOLO11 sin licencia Enterprise, el sistema completo tendría que
ser AGPL-3.0).
