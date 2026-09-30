# Conectar la cámara Hikvision a la PC

Guía del proceso completo, de la caja al stream funcionando en el worker.

---

## El concepto que hay que entender primero

Tu cámara **no se conecta a la computadora**. Se conecta a la **red**, y la
computadora la consume por ahí. No hay cable USB, no hay driver que instalar.

Ahora mismo la ves desde el celular porque la app **Hik-Connect** pasa por los
servidores de Hikvision en internet (la cámara sale a internet, el celular
también, y se encuentran en medio). Eso sirve para mirar video, pero **no sirve
para este proyecto**: Hik-Connect no expone el stream crudo que OpenCV necesita.

Lo que necesitamos es hablarle a la cámara **directo, dentro de tu red local**,
por un protocolo llamado **RTSP**. Para eso la cámara y la PC tienen que estar
en el mismo router.

```
   HOY (Hik-Connect)                    LO QUE NECESITAMOS (RTSP local)

   Cámara ──> Internet ──> Nube         Cámara ──┐
                            │                     ├── Router ── PC (worker)
   Celular <────────────────┘           PC     ──┘
                                        (sin salir a internet)
```

---

## Paso 1 — Conectar la cámara al router

Depende del modelo:

**Si es PoE** (la mayoría de las Hikvision tipo bala/domo, un solo cable de red):
un cable Ethernet de la cámara a un **switch PoE** o a un **inyector PoE**, y de
ahí al router. El PoE lleva datos y corriente por el mismo cable.

**Si es de 12V** (trae eliminador aparte): el eliminador a la corriente, y un
cable Ethernet de la cámara al router.

**Si es Wi-Fi**: se configura desde la app Hik-Connect (Configuración → Red →
Wi-Fi) para unirla a tu red. Funciona, pero **para vigilancia con inferencia
prefiere cable**: el Wi-Fi pierde paquetes y verás frames rasgados y
reconexiones constantes (el worker las cuenta y te las reporta).

> El punto crítico: la cámara debe quedar en **el mismo router** que tu PC. Si
> tu PC está en Wi-Fi y la cámara por cable en ese mismo router, está bien.

---

## Paso 2 — Encontrar su dirección IP

La cámara toma una IP del router. Para hablarle hay que saber cuál es.

```bash
python tools/probe_camara.py --descubrir
```

Escanea tu red buscando equipos con el puerto RTSP (554) abierto. Tarda ~20 s y
reemplaza a la herramienta **SADP** de Hikvision.

Alternativas si el escaneo no la encuentra:
- Entra a tu router (normalmente `192.168.1.1` o `192.168.0.1`) y busca la lista
  de dispositivos conectados o "concesiones DHCP". La cámara aparece como
  `HIKVISION` o con MAC que empieza en `44:19:B6` / `BC:AD:28`.
- En Hik-Connect: Configuración del dispositivo → Red → verás su IP local.
- IP de fábrica si nunca se configuró: **192.168.1.64**

**Recomendación:** una vez que sepas cuál es, entra al router y asígnale una
**IP fija** (reserva DHCP por MAC). Si no, el router puede darle otra IP la
próxima vez que se reinicie y tu `.env` deja de funcionar sin explicación
aparente.

---

## Paso 3 — Crear un usuario para el stream

**No uses la cuenta `admin` en el `.env`.** Esa contraseña da control total de
la cámara y va a quedar escrita en un archivo de texto de tu proyecto.

Entra a `http://<IP-DE-LA-CAMARA>` desde el navegador con la cuenta admin, y en
**Configuración → Sistema → Administración de usuarios** crea un usuario nuevo
con rol **Operador** (solo lectura de video). Ese es el que va en el `.env`.

En esa misma interfaz, verifica que RTSP esté activo:
**Configuración → Red → Avanzada → Protocolos → RTSP** (puerto 554).

---

## Paso 4 — Encontrar la URL RTSP correcta

```bash
python tools/probe_camara.py --host 192.168.1.64 --user operador --password TU_PASS
```

Prueba nueve rutas RTSP distintas (Hikvision cambió el formato entre versiones
de firmware), abre cada una de verdad, mide resolución/fps/latencia, guarda una
captura de prueba y te imprime la línea `SOURCE=...` lista para pegar.

Para ver el video y confirmar el encuadre, agrega `--ver`.

### Sobre cuál canal usar

| Ruta | Qué es | Cuándo |
|---|---|---|
| `/Streaming/Channels/101` | Main stream (3200×1800 en la cámara del proyecto) | Evidencia |
| `/Streaming/Channels/102` | **Sub-stream (1280×720)** | **Inferencia — usa este** |

El sub-stream es el correcto para el worker: los modelos trabajan a 640 px de
todos modos, así que traer el main stream solo gasta ancho de banda y CPU en
decodificación. El main stream se usa **solo** para la evidencia: cuando un
detector encuentra algo, el worker pide una foto fija del canal 101 por ISAPI
(`SNAPSHOT_HD_ENABLED=true`) con las mismas credenciales de `SOURCE`.

---

## Paso 5 — Ponerlo en el `.env`

```bash
SOURCE=rtsp://operador:TU_PASS@192.168.1.64:554/Streaming/Channels/102
```

Y verifica que el pipeline lo recibe bien:

```bash
python -m edge.worker --diagnostico --segundos 30
```

Qué mirar en la salida:

- **`fps reales`** — debe acercarse a lo que declara la cámara (típico 15–25).
- **`latencia`** — debe quedarse en decenas de milisegundos. Si crece con el
  tiempo, hay un problema de buffer (no debería: `sources.py` lo maneja).
- **`reconexiones`** — debe ser **0**. Si sube, la red es inestable: pasa a
  cable o baja la resolución del sub-stream.

---

## Problemas frecuentes

**"Ninguna ruta funcionó"**
1. Contraseña incorrecta. Hikvision **bloquea la IP tras ~5 intentos fallidos**:
   espera 30 minutos o reinicia la cámara.
2. RTSP desactivado (Paso 3).
3. La cámara está en otra subred que la PC.

**La contraseña tiene `@`, `:`, `#` o `/`**
Rompe la URL. `probe_camara.py` la codifica automáticamente, pero si armas la
URL a mano tienes que escaparla (`@` → `%40`). Es la causa número uno de "mi
RTSP no funciona". Lo más simple: pon una contraseña alfanumérica al usuario
operador.

**Frames verdes, rasgados o "corruptos"**
RTSP sobre UDP perdiendo paquetes. `sources.py` ya fuerza TCP
(`OPENCV_FFMPEG_CAPTURE_OPTIONS`), así que si lo ves, es la red física.

**Funciona en VLC pero no en Python**
Casi siempre es la contraseña sin escapar. Prueba con `probe_camara.py`, que
maneja la codificación.

---

## Eventos de la propia cámara (sabotaje, pérdida de video)

La cámara avisa cosas que el video no dice: que le taparon el lente, que la
movieron, que perdió la señal, o que su propia analítica vio a alguien cruzar
una línea. El worker las escucha por ISAPI (`/ISAPI/Event/notification/alertStream`)
con el mismo usuario de `SOURCE` y las manda a la API como eventos de cámara:

| En la cámara (Configuración > Evento) | Llega como | Severidad |
|---|---|---|
| Video tampering / Scene change / Defocus | Sabotaje de cámara | crítica |
| Video loss | Pérdida de video | crítica |
| Line crossing detection | Cruce de línea (cámara) | advertencia |
| Intrusion detection / Region entrance | Intrusión (cámara) | advertencia |
| Motion detection | Movimiento (cámara) | apagado (`ISAPI_EVENTOS`) |

1. En la interfaz web de la cámara activa el evento que quieras y, en
   **Método de vinculación**, marca **Notificar al centro de vigilancia**
   (*Notify Surveillance Center*). Sin eso la cámara no lo manda por ISAPI.
2. El usuario del stream necesita el permiso **Notificar al centro de
   vigilancia / Alarma remota**; con solo "Vista en vivo" la cámara responde 401
   y el worker lo dice en su log.
3. Nada más: con `ISAPI=auto` (el default) el worker se conecta solo si
   `SOURCE` es `rtsp://usuario:contraseña@...`. Con un NVR, filtra por el canal
   de la URL (`Channels/102` → canal 1).

Independiente de esto, la API avisa si una cámara deja de entregar imagen
(`NOTIFY_CAMARA_CAIDA_S`, 2 minutos por defecto), sea cual sea la marca.

---

## Sin la cámara a la mano

El sistema entero funciona igual con:

```bash
SOURCE=webcam:0                       # la webcam de la laptop
SOURCE=file:videos/prueba.mp4         # un video grabado (pruebas reproducibles)
```

Y un **celular también sirve como cámara IP** de prueba: *IP Webcam* (Android)
o *EpocCam* dan una URL RTSP/HTTP que `open_source()` acepta sin cambios.

Pasar de una a otra es cambiar la línea `SOURCE` del `.env`. Ni una línea de
código.
