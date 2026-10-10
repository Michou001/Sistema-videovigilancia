# Despliegue en un servidor 24/7

Guía para pasar el proyecto de tu laptop a la máquina que van a usar los
monitoristas.

Para mantener el sistema accesible si se apaga la laptop, consulta
**[contingencia-web.md](contingencia-web.md)**: requiere un servidor independiente
y acceso privado a las camaras.

## Equipo de respaldo con una memoria USB

Si el equipo principal falla, otra PC en la misma red puede tomar su lugar:

1. En el equipo principal, `copiar_a_memoria.bat`: detiene el sistema, copia la
   carpeta con `.env`, base de datos, evidencias y modelos (sin `venv` ni
   `respaldos`) y comprueba la copia y la integridad de la base de datos.
2. En la PC de respaldo, copiar la carpeta de la memoria al disco y, solo la
   primera vez, `preparar_respaldo.bat` (necesita internet): crea el entorno de
   Python e instala torch con CUDA y las dependencias. El `venv` no se copia
   porque apunta al Python del equipo donde se creó.
3. Arrancar con `iniciar_api.bat` y los `iniciar_worker*.bat`.

La memoria lleva contraseñas de las cámaras y datos personales: debe ir
cifrada con BitLocker To Go (el script avisa si no lo está). Repetir la copia
después de cambios en la lista de alertas, usuarios o zonas; los eventos
posteriores a la última copia no estarán en el equipo de respaldo.

## Aviso si se apaga el equipo completo

Si cae una cámara o un worker, la API avisa por Telegram. Si se apaga el equipo
entero (luz, red, falla de la laptop), ya no queda nadie que avise. Para eso la
API manda un latido a un servicio externo, que avisa cuando deja de recibirlo:

1. Crear una cuenta gratuita en [healthchecks.io](https://healthchecks.io) y un
   chequeo con periodo de 1 minuto y gracia de 3 minutos.
2. En **Integrations**, agregar **Telegram** y seguir el enlace con el bot de
   healthchecks.io al chat o grupo de los monitoristas.
3. Copiar la URL de ping del chequeo en el `.env` del equipo que corre la API:

   ```env
   LATIDO_EXTERNO_URL=https://hc-ping.com/<uuid-del-chequeo>
   LATIDO_EXTERNO_S=60
   ```

4. Reiniciar la API y comprobar en healthchecks.io que el chequeo está en
   verde. Para probarlo, cerrar la API: en unos 4 minutos llega el aviso a
   Telegram, y otro cuando vuelve.

Solo viaja la petición, sin datos de cámaras, eventos ni personas. La URL lleva
un identificador secreto: no se escribe en los logs ni se sube al repositorio.

Para red física, VLAN, switches PoE, fibra entre edificios, alturas de cámara,
ancho de banda y escalamiento por campus, ver
**[arquitectura-despliegue.md](arquitectura-despliegue.md)**.

---

## ¿Corre en otra máquina? Sí, con estas condiciones

El código es Python puro y funciona igual en Windows y Linux. Pero hay tres
cosas que **no** viajan con el repositorio:

| No se copia | Qué hacer |
|---|---|
| El entorno virtual `venv/` | Recrearlo en la máquina destino |
| El archivo `.env` | Está en `.gitignore` a propósito: lleva la contraseña de la cámara |
| La base de datos `data/` | Se crea sola con `tools/init_plataforma.py` |

Los modelos **no** van en el repositorio: se descargan solos la primera vez
que arranca el worker (detector y OCR de placas ~30 MB, InsightFace ~280 MB,
YOLO11 ~20 MB). La máquina necesita internet en ese primer arranque; después
funciona sin conexión.

### Requisitos de la máquina

| | Mínimo | Recomendado |
|---|---|---|
| GPU | NVIDIA con 4 GB | NVIDIA con 6 GB+ |
| RAM | 8 GB | 16 GB |
| Disco | 20 GB | 50 GB+ (evidencia) |
| Red | Misma LAN que las cámaras | Cable, no Wi-Fi |

**Sin GPU NVIDIA el sistema es inviable en tiempo real.** Los tres detectores
en CPU pasan de 44 ms a más de 200 ms por frame: bajarías de 8 fps a menos de 3,
y perderías vehículos.

---

## Instalación paso a paso

```bash
git clone <tu-repo> sistema-videovigilancia
cd sistema-videovigilancia

python -m venv venv
source venv/bin/activate          # Linux
venv\Scripts\activate             # Windows

# 1. torch CON CUDA -- aparte, NO desde requirements.txt
pip install torch==2.13.0+cu126 torchvision==0.28.0+cu126 \
    --index-url https://download.pytorch.org/whl/cu126

# 2. el resto
pip install -r requirements.txt

# 3. verificar GPU (debe decir True)
python -c "import torch; print(torch.cuda.is_available())"

# 4. verificar que onnxruntime ve la GPU (debe listar CUDAExecutionProvider)
python -c "import onnxruntime as ort; print(ort.get_available_providers())"
```

Si el paso 4 **no** lista `CUDAExecutionProvider`, es casi seguro el conflicto
de paquetes que documenta `requirements.txt`:

```bash
pip uninstall -y onnxruntime
pip install --force-reinstall --no-deps onnxruntime-gpu==1.22.0
```

Luego configura y arranca:

```bash
cp .env.example .env      # y edítalo con los datos de tus cámaras
python tools/init_plataforma.py
```

---

## Diferencias en Linux

El código detecta el sistema operativo solo, pero conviene saber qué cambia:

- **El parche de `PosixPath`** solo se aplica en Windows. En Linux sería
  destructivo (sustituiría la clase de rutas nativa del proceso), así que está
  condicionado. Ver [edge/detectors/plates.py](../edge/detectors/plates.py).
- **Las DLL de CUDA para onnxruntime**: en Windows se toman de `torch/lib`; en
  Linux se resuelven por `LD_LIBRARY_PATH` o los paquetes `nvidia-*-cu12`.
- **Rutas**: todo usa `pathlib`, no hay rutas con `\` escritas a mano.

---

## Correr como servicio permanente

Son **dos procesos**: la API (una sola) y un worker **por cada cámara**.

### Linux con systemd

`/etc/systemd/system/vigilancia-api.service`:

```ini
[Unit]
Description=API del sistema de videovigilancia
After=network.target

[Service]
Type=simple
User=vigilancia
WorkingDirectory=/opt/sistema-videovigilancia
# python -m api lee del .env API_HOST, API_PORT y el certificado (SSL_CERTFILE /
# SSL_KEYFILE). Por defecto escucha solo en 127.0.0.1: detras de un proxy
# (Caddy/nginx en el mismo equipo) asi debe quedar. Ver docs/seguridad-red.md.
ExecStart=/opt/sistema-videovigilancia/venv/bin/python -m api
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/vigilancia-worker@.service` (plantilla, una instancia por
cámara):

```ini
[Unit]
Description=Worker de videovigilancia (camara %i)
After=vigilancia-api.service

[Service]
Type=simple
User=vigilancia
WorkingDirectory=/opt/sistema-videovigilancia
ExecStart=/opt/sistema-videovigilancia/venv/bin/python -m edge.worker --env .env.%i
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now vigilancia-api
sudo systemctl enable --now vigilancia-worker@cam-entrada   # lee .env.cam-entrada
```

`Restart=always` importa: si el worker muere por un fallo de red o de la
cámara, vuelve solo. Es la diferencia entre un sistema de vigilancia y un
script.

### Windows

Usa [NSSM](https://nssm.cc/) para registrar ambos comandos como servicios, o el
Programador de tareas con disparador "al iniciar el sistema".

---

## Varias cámaras

Cada cámara tiene su archivo de entorno (su `CAMERA_ID` y su `SOURCE`). Lo
recomendado es correrlas **todas en un solo proceso**: los modelos se cargan
una vez y los comparten, y hay un solo contexto de CUDA.

```bash
python -m edge.worker --env .env --env .env.cam-salida --env .env.cam-patio
python -m edge.worker --carpeta camaras          # todos los camaras/*.env
```

El botón **Cámaras** del dashboard crea esos archivos: la primera cámara va a
`.env` y las siguientes a `.env.<camera_id>`, copiando el token y los
detectores activos. Cada cámara corre en su propio hilo: si una se cae, las
demás siguen.

Si un worker corre en **otra máquina** que la API (una PC junto a las
cámaras y el servidor en otro lado), no comparten disco: con
`SEND_SNAPSHOT_B64=auto` (el valor por defecto) el worker detecta que la API
no es `localhost` y adjunta cada captura al evento para que la API la guarde.

**Cuántas caben en una GPU:** con un proceso por cámara, en una tarjeta de 6 GB
cabían unas 3 (se acababa la VRAM). En un solo proceso los modelos se
comparten y el límite pasa a ser el cómputo: mídelo con el reporte periódico
del worker (ms por detector) y `tools/optimizar_modelos.py` (FP16/TensorRT).

---

## Con Docker

Para un equipo dedicado con GPU NVIDIA, `docker-compose.yml` levanta todo:

| Servicio | Qué hace |
|---|---|
| `caddy` | HTTPS en el 443: dashboard, API y el video WebRTC (solo con sesión) |
| `api` | Plataforma web, varios procesos compartiendo estado por Redis |
| `worker` | Detección en la GPU, todas las cámaras de `camaras/` en un proceso |
| `go2rtc` | Video de las cámaras al navegador por WebRTC, sin recodificar |
| `postgres` | Base de datos |
| `redis` | Alertas, video en vivo y límites de login compartidos entre procesos |

Requisitos: Docker con Compose, driver NVIDIA y
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/).
Verifica la GPU con `docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04 nvidia-smi`.

```bash
# 1. Secretos y dominio (o IP de la red local)
cp docker/compose.env.example docker/compose.env        # llena POSTGRES_PASSWORD, API_TOKEN, JWT_SECRET, SITIO

# 2. Una cámara por archivo (CAMERA_ID y SOURCE; API_URL y API_TOKEN los pone Docker)
mkdir camaras
cp .env.example camaras/entrada.env                      # edita CAMERA_ID=cam-entrada, SOURCE=rtsp://...

# 3. Video por WebRTC: go2rtc con las mismas cámaras (lleva contraseñas: fuera de git)
python tools/go2rtc_config.py --carpeta camaras --ip 192.168.1.10

# 4. Levantar y crear el usuario administrador
docker compose --env-file docker/compose.env up -d --build
docker compose --env-file docker/compose.env exec api python tools/init_plataforma.py
```

Configuración extra de la API (notificaciones, retención, búsqueda por
descripción): variables `NOTIFY_*`, `RETENCION_*`, `SEMANTIC_SEARCH` en
`docker/api.env` (opcional, fuera de git).

**HTTPS.** Con un dominio público en `SITIO`, Caddy saca el certificado de
Let's Encrypt solo. Con una IP de la red local, usa su propia CA: el
navegador avisa hasta que se instala la raíz en cada PC del centro de
monitoreo (`docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt`
y se importa como autoridad de confianza).

**Datos.** Base de datos, capturas, clips y modelos viven en volúmenes de
Docker (`docker volume ls`). Respalda al menos `goss-ip_postgres` y
`goss-ip_datos`.

---

## Video por WebRTC (go2rtc)

Sin configurar nada, la vista en vivo es MJPEG: el worker dibuja las cajas,
codifica JPEG y lo sube a la API. Funciona en cualquier red, pero va a baja
resolución y cuesta CPU del worker.

Con [go2rtc](https://github.com/AlexxIT/go2rtc) el navegador recibe el video
**directo de la cámara, sin recodificar**, por WebRTC: resolución completa y
menos de medio segundo de retraso. Las cajas de los detectores llegan aparte
como datos y el navegador las dibuja encima.

- `GO2RTC_URL` en la API dice dónde encuentra el navegador a go2rtc. Con
  Docker ya está (`/go2rtc`, detrás de Caddy).
- El nombre de cada stream de go2rtc es el `CAMERA_ID`:
  `tools/go2rtc_config.py` lo arma desde los `.env`.
- go2rtc no tiene contraseña propia. Caddy solo deja pasar la negociación de
  WebRTC (`POST /go2rtc/api/webrtc`) y solo con la sesión del dashboard; el
  resto de su API, que muestra las URLs RTSP **con contraseña**, no se publica.
- El video usa el puerto 8555 (TCP y UDP): ábrelo en el firewall de la PC
  para la red local, y pasa `--ip` con la IP de esa PC.
- La cámara debe entregar **H.264** (Chrome no reproduce H.265 por WebRTC):
  en Hikvision, *Configuración > Video > Codificación de video*.
- Si go2rtc no responde o el navegador no puede con el codec, esa cámara
  vuelve sola a MJPEG.

---

## Antes de considerarlo en producción

La puesta en producción exige además un levantamiento físico por cámara y una
validación de campo. Para el caso InnovaTICs se usa
**[validacion-innovatics.md](validacion-innovatics.md)**.


- [ ] Cambiar la contraseña de `admin` (la de demo no sirve)
- [ ] Crear un usuario de cámara con rol **Operador**, no usar `admin` en el `.env`
- [ ] Poner la API detrás de HTTPS (Caddy en Docker, o `tools/generar_certificado.py`)
- [ ] Si se usa go2rtc: que su API (1984) NO sea alcanzable directo, solo por el proxy
- [ ] Migrar de SQLite a PostgreSQL si hay más de 2-3 cámaras escribiendo
- [ ] Verificar que `data/` está en un disco con espacio y respaldo
- [ ] Programar `tools/purgar_datos.py` como red de seguridad del purgado interno
- [ ] Colocar el aviso de privacidad en el punto de captura ([privacidad.md](privacidad.md))
- [ ] Validar en campo: umbral facial, armas reales, distancia de placas

---

## Salud del sistema

Cada worker manda un latido cada 15 s (`HEARTBEAT_S`) con los fps reales, las
reconexiones y si la cámara entrega imagen. El dashboard marca caída una cámara
sin latido en 60 s, o cuyo worker sigue vivo pero ya no recibe video. Para
monitoreo externo:

```bash
curl http://localhost:8000/api/health
```

Y en los logs del worker, tres números que vigilar:

| Métrica | Qué significa si sube |
|---|---|
| `reconexiones` | Red o cámara inestable |
| `frames perdidos por lentitud` | La inferencia no alcanza: baja `INFER_FPS` o `IMGSZ` |
| `descartados_sin_confirmar` (armas) | El filtro anti falsos positivos está trabajando — es sano |
