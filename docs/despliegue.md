# Despliegue en un servidor 24/7

Guía para pasar el proyecto de tu laptop a la máquina que van a usar los
monitoristas.

---

## ¿Corre en otra máquina? Sí, con estas condiciones

El código es Python puro y funciona igual en Windows y Linux. Pero hay tres
cosas que **no** viajan con el repositorio:

| No se copia | Qué hacer |
|---|---|
| El entorno virtual `venv/` | Recrearlo en la máquina destino |
| El archivo `.env` | Está en `.gitignore` a propósito: lleva la contraseña de la cámara |
| La base de datos `data/` | Se crea sola con `tools/init_plataforma.py` |

Los modelos **sí** viajan: `models/plates_yolov5.pt` está versionado, y es
importante porque **es la única copia** — el dataset con que se entrenó estaba
en Kaggle y ya no está disponible.

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
ExecStart=/opt/sistema-videovigilancia/venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000
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
Environment=CAMERA_ID=%i
ExecStart=/opt/sistema-videovigilancia/venv/bin/python -m edge.worker
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now vigilancia-api
sudo systemctl enable --now vigilancia-worker@cam-entrada
```

`Restart=always` importa: si el worker muere por un fallo de red o de la
cámara, vuelve solo. Es la diferencia entre un sistema de vigilancia y un
script.

### Windows

Usa [NSSM](https://nssm.cc/) para registrar ambos comandos como servicios, o el
Programador de tareas con disparador "al iniciar el sistema".

---

## Varias cámaras

Un worker por cámara, cada uno con su `CAMERA_ID` y su `SOURCE`. Todos apuntan
a la misma API.

```bash
CAMERA_ID=cam-entrada SOURCE=rtsp://... python -m edge.worker
CAMERA_ID=cam-salida  SOURCE=rtsp://... python -m edge.worker
```

**Cuántas caben en una GPU:** cada worker con los tres detectores usa ~1.6 GB
de VRAM y ~44 ms de cómputo por frame. En una tarjeta de 6 GB caben unos **3
workers**; el límite de VRAM llega antes que el de cómputo.

---

## Antes de considerarlo en producción

- [ ] Cambiar la contraseña de `admin` (la de demo no sirve)
- [ ] Crear un usuario de cámara con rol **Operador**, no usar `admin` en el `.env`
- [ ] Poner la API detrás de HTTPS si se accede fuera de la LAN
- [ ] Migrar de SQLite a PostgreSQL si hay más de 2-3 cámaras escribiendo
- [ ] Verificar que `data/` está en un disco con espacio y respaldo
- [ ] Programar `tools/purgar_datos.py` como red de seguridad del purgado interno
- [ ] Colocar el aviso de privacidad en el punto de captura ([privacidad.md](privacidad.md))
- [ ] Validar en campo: umbral facial, armas reales, distancia de placas

---

## Salud del sistema

El dashboard muestra si cada cámara está en línea (sin señal por más de 60 s se
marca caída). Para monitoreo externo:

```bash
curl http://localhost:8000/api/health
```

Y en los logs del worker, tres números que vigilar:

| Métrica | Qué significa si sube |
|---|---|
| `reconexiones` | Red o cámara inestable |
| `frames perdidos por lentitud` | La inferencia no alcanza: baja `INFER_FPS` o `IMGSZ` |
| `descartados_sin_confirmar` (armas) | El filtro anti falsos positivos está trabajando — es sano |
