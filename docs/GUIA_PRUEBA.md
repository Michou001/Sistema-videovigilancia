# Guía para probar el prototipo — GOSS IP

Instrucciones para que cualquier compañero de equipo pueda descargar, instalar
y probar el sistema en su propia computadora, sin necesitar las cámaras
físicas (se puede probar con la webcam o con un video grabado).

---

## 1. Qué se necesita antes de empezar

| Requisito | Detalle |
|---|---|
| **Windows 10/11** | El proyecto también corre en Linux, pero estas instrucciones son para Windows |
| **Python 3.11 o 3.12** | Descargar de [python.org](https://www.python.org/downloads/) — marcar "Add python.exe to PATH" al instalar |
| **Git** | Para descargar el repositorio ([git-scm.com](https://git-scm.com/download/win)) |
| **~6 GB libres en disco** | Los modelos y las librerías (PyTorch, OpenCV, etc.) pesan varios GB |
| **GPU NVIDIA (opcional)** | El sistema corre sin GPU (más lento, en CPU). Con una GPU NVIDIA con drivers CUDA se ve el rendimiento real |
| **Webcam** | Para probar sin las cámaras Hikvision. Cualquier laptop sirve |

No hace falta tener las cámaras IP del proyecto para probar el sistema: se
puede usar la webcam de la laptop o un video de prueba.

---

## 2. Descargar el proyecto

```bash
git clone https://github.com/Michou001/Sistema-videovigilancia.git
cd Sistema-videovigilancia
```

---

## 3. Instalar el entorno

Abrir una terminal **dentro de la carpeta del proyecto** y ejecutar:

```bash
python -m venv venv
venv\Scripts\activate
```

Instalar dependencias (dos pasos, en este orden):

```bash
# 1. PyTorch con soporte CUDA (si tienes GPU NVIDIA)
pip install torch==2.13.0+cu126 torchvision==0.28.0+cu126 --index-url https://download.pytorch.org/whl/cu126

# 2. El resto de librerías
pip install -r requirements.txt
```

> Si tu computadora **no tiene GPU NVIDIA**, en vez del paso 1 corre:
> `pip install torch torchvision` (versión normal, sin CUDA). El sistema
> funciona igual, solo más lento.

Verificar que la instalación quedó bien:

```bash
python -c "import torch; print(torch.cuda.is_available())"
```

Si imprime `True`, la GPU está lista. Si imprime `False`, el sistema usará CPU
(funciona, pero cada detector tarda más por frame).

---

## 4. Configurar el archivo `.env`

```bash
copy .env.example .env
```

Abrir `.env` con cualquier editor de texto y dejar esta línea (ya viene así
por defecto, para usar la webcam de la laptop):

```
SOURCE=webcam:0
```

Si la laptop tiene más de una cámara y no agarra la correcta, probar
`webcam:1`, `webcam:2`, etc.

Para probar con un video grabado en vez de la webcam:

```
SOURCE=file:videos/prueba.mp4
```

(usar cualquier `.mp4` que tengan a la mano; la carpeta `videos/` no se sube
al repositorio porque pesa).

Los comentarios al final de una línea del `.env` (`ENABLE_FACES=true  # nota`)
están permitidos.

---

## 5. Inicializar la plataforma (solo la primera vez)

Esto crea la base de datos, el usuario administrador y el token que necesita
el worker para mandar eventos:

```bash
python tools/init_plataforma.py
```

La terminal va a mostrar algo así **una sola vez** — hay que copiarlo, no se
vuelve a mostrar:

```
CREDENCIALES  (se muestran una sola vez)
  usuario    : admin
  contrasena : ********************

TOKEN DEL WORKER
  Agrega esta linea al .env para que el worker publique eventos:
      API_TOKEN=********************
```

Copiar esa línea `API_TOKEN=...` y pegarla en el archivo `.env` (reemplazando
la línea `API_TOKEN=` que ya existe, vacía).

Guardar usuario y contraseña del admin: son para entrar al dashboard.

---

## 6. Levantar el sistema

Se necesitan **dos terminales abiertas al mismo tiempo**, ambas dentro de la
carpeta del proyecto y con el entorno activado (`venv\Scripts\activate`).

**Terminal 1 — la plataforma web:**

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

**Terminal 2 — el worker (la parte que ve la cámara y detecta):**

```bash
python -m edge.worker
```

(En Windows también se puede hacer doble clic en `iniciar_api.bat` e
`iniciar_worker.bat`, que hacen lo mismo sin escribir comandos.)

---

## 7. Abrir el dashboard

Abrir el navegador en:

```
http://localhost:8000
```

Iniciar sesión con el usuario y contraseña generados en el paso 5.

### Qué probar

| Sección | Qué hacer |
|---|---|
| **Monitoreo** | Debe verse la cámara (webcam) en vivo, con cajas dibujadas cuando detecta algo |
| **Registro** | Ver los eventos que se van generando y buscarlos: escribir una placa sin guion (`abc123`) en el buscador la encuentra igual |
| **Lista negra** | Agregar una placa de prueba y verificar que si esa placa "aparece" (se le puede mostrar una foto impresa a la cámara) se genera una alerta `critical`. Si la placa ya había pasado antes, la alerta sale en el momento del alta (re-escaneo retroactivo) |
| **Cámaras** | Desde el dashboard, sin tocar el `.env` a mano, se puede buscar y agregar una cámara IP. La primera se guarda en `.env`; las siguientes en `.env.<id>`, y la pantalla indica el comando para arrancar su worker |
| **Evidencia** | Clic en cualquier miniatura para verla en grande |

---

## 8. Detectores que están activos por defecto

| Detector | Estado | Qué hace |
|---|---|---|
| **Placas** (YOLOv5 + OCR) | Activo | Lee placas vehiculares y las compara contra la lista negra |
| **Rostros** (InsightFace) | Activo | Compara rostros contra la lista negra biométrica. La primera vez descarga el modelo (~280 MB) |
| **Movimiento anómalo** (YOLO11) | Activo | Detecta movimientos bruscos/corridas por velocidad relativa de una persona |
| **Armas blancas** | Apagado (`ENABLE_WEAPONS=false`) | Probado contra cámara real y no detectó de forma confiable; el `README.md` explica por qué |

Estos se prenden/apagan editando `.env` y reiniciando el worker — no hace
falta tocar código.

---

## 9. Problemas comunes

| Síntoma | Causa probable | Solución |
|---|---|---|
| La webcam no abre / pantalla negra | Índice de cámara incorrecto | Probar `webcam:1`, `webcam:2` en `.env` |
| `ModuleNotFoundError` al correr algo | El entorno virtual no está activado | Correr `venv\Scripts\activate` de nuevo en esa terminal |
| El dashboard carga pero no llegan eventos | El worker no tiene el `API_TOKEN` correcto | Revisar que el `API_TOKEN` del `.env` sea igual al que imprimió `init_plataforma.py` |
| Todo va muy lento (pocos FPS) | Corriendo en CPU, o laptop en batería | Revisar `torch.cuda.is_available()` (paso 3) y conectar la laptop a la corriente: en batería la GPU entra en modo de ahorro |
| "Demasiados intentos fallidos" al entrar | 5 contraseñas incorrectas seguidas | Esperar 5 minutos |
| `python` no se reconoce como comando | Python no quedó en el PATH | Reinstalar Python marcando "Add to PATH", o usar la ruta completa `venv\Scripts\python.exe` |

---

## 10. Correr las pruebas automáticas

No necesitan cámara ni GPU:

```bash
python tests/correr_todas.py
```

Debe terminar con `Todas las pruebas pasan`.

---

## 11. Apagar el sistema

En cada terminal, `Ctrl+C` detiene el proceso (API y worker, uno en cada
terminal). No hace falta nada más — los datos ya guardados en la base de
datos y los `.jsonl` de respaldo no se pierden.

---

Para el detalle técnico completo (arquitectura, decisiones de diseño,
rendimiento medido, privacidad) ver [`README.md`](../README.md) y
[`bitacoravideovigilancia.html`](bitacoravideovigilancia.html).
