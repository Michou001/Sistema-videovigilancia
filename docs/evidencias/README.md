# Evidencias medidas

Todo lo de esta carpeta lo midió el propio sistema. No hay porcentajes
estimados: cuando un dato no se pudo medir, se dice.

## Rendimiento en la laptop del equipo

**Equipo:** laptop con NVIDIA GeForce RTX 4050 Laptop (6 GB), Windows 11.
**Fecha:** 6 de octubre de 2026.
**Configuración:** la del `.env` del equipo: placas, rostros, movimiento y
pose activos; `IMGSZ=640`.

**Método:**
1. Cada cámara fue un video de 20 fps reproducido a velocidad real y en bucle
   (`SOURCE_LOOP`, `SOURCE_REALTIME`).
2. Con un archivo, el worker no descarta cuadros: analiza todos los que
   puede. Eso mide la **capacidad** del equipo. Con una cámara real se analizan
   `INFER_FPS=8` por cámara.
3. Se muestreó cada 5 s durante 60 s con `python tools/metricas.py --muestrear 60`,
   después del primer minuto de calentamiento de la GPU.
4. Los fps y los milisegundos son de la ventana de los últimos ~15 s, que
   reporta el latido del worker.

| Escenario | fps analizados por cámara | Total | GPU | VRAM | Archivo |
|---|---|---|---|---|---|
| 1 cámara | 13–17 | ~17 | 42 % | 0.9 GB | [rendimiento-1camara.csv](rendimiento-1camara.csv) |
| 4 cámaras en **1 proceso** | 2.8 | ~11 | 34 % | 2.0 GB | [rendimiento-4camaras-1proceso.csv](rendimiento-4camaras-1proceso.csv) |
| 4 cámaras en **4 procesos** | 4.6 | ~18 | 77 % | 3.2 GB | [rendimiento-4camaras-4procesos.csv](rendimiento-4camaras-4procesos.csv) |

Milisegundos por cuadro con 1 cámara (promedio de la ventana): placas 30,
rostros 15, pose 23, movimiento 19. La latencia de la API (`/api/health`)
promedió 7–15 ms.

**Qué se concluye:**
- **Capacidad:** con los cuatro detectores, la GPU de esta laptop analiza
  ~17–18 cuadros por segundo en total. A 8 fps por cámara alcanza para **2
  cámaras con todo activo**. Una cámara solo de placas o solo de movimiento
  pesa menos, así que caben más.
- **Varias cámaras en un solo proceso no escalan.** Con 4 cámaras la GPU
  quedó al 34 %: el límite fue Python, que ejecuta un hilo a la vez (GIL), no
  la GPU.
- **Un proceso por cámara rinde 67 % más en total** (18 contra 11 fps) a
  cambio de VRAM: ~0.8 GB por proceso. Por eso el catálogo de cámaras inicia
  un worker por cámara.
- **Para crecer** se agrega GPU o nodos de borde: cada nodo con su GPU atiende
  sus cámaras y todos reportan a la misma API.

**Limitación:** los videos de prueba no tenían placas ni personas que el
modelo reconociera (0 eventos). Es la carga base del pipeline; con tráfico
real se suman el OCR (menos de 1 ms por lectura) y el seguimiento.

### Con las dos cámaras reales del stand

7 de octubre, 00:31. Las dos Hikvision (192.168.100.64 y .65) por la red del
stand, cada una en su proceso con placas y rostros, a `INFER_FPS=8`:

| Cámara | Cuadros analizados por segundo | Placas | Rostros |
|---|---|---|---|
| cam-01 | 7.4 | 38 ms | 19 ms |
| cam-02 | 7.7 | 38 ms | 15 ms |

GPU al 38 % con 1.6 de 6 GB; CPU de todo el equipo al 37 % (muestra de 5 s
con `psutil` y `nvidia-smi`, con el dashboard abierto). Ambas cámaras alcanzan
los 8 fps pedidos con margen de GPU. Falta la medición larga con
`tools/metricas.py --muestrear 300`.

## Operación registrada en la base de datos

[resumen-operacion-pruebas-sep-oct.md](resumen-operacion-pruebas-sep-oct.md)
resume las pruebas de septiembre y octubre con las cámaras Hikvision:
25 lecturas de placa, 12 coincidencias con la lista negra, 20 alertas y una
caída de cámara recuperada en 30 s. Se genera con
`python tools/metricas.py --resumen`. Para la demostración conviene generarlo
solo del día: `--desde 2026-10-07`.

El tiempo de atención de ese resumen no representa la operación real: son
alertas de prueba que se revisaron días después.

## Placa de prueba impresa

La placa de `demo/` pasada por el detector y el OCR del worker, al umbral de
producción, sobre 44 escenas generadas (11 condiciones, 4 escenas cada una):

| Fecha | `PLATE_CONF` | Detectada | Leída bien | Archivo |
|---|---|---|---|---|
| 6 oct | 0.45 | 10/44 | 10/44 | [validacion_placa_ZTP482A.csv](validacion_placa_ZTP482A.csv) |
| 7 oct | 0.60 | 6/44 | 6/44 | [validacion_placa_ZTP482A_umbral060.csv](validacion_placa_ZTP482A_umbral060.csv) |

- **OCR:** cuando la placa se detecta, la lectura es correcta siempre.
- **Detector:** es el filtro. De frente la acepta en algunas escenas entre 140
  y 300 px de ancho; de lado (30° y 45°), en ninguna. Subir `PLATE_CONF` a
  0.60 (para descartar etiquetas que parecían placas) le quitó 4 de 10.

Por eso, en la demo el accesorio principal es la foto de una placa real y
esta placa queda de respaldo (ver `tools/placa_demo.py`).

## Respaldo y recuperación

7 de octubre, 00:16: `python tools/respaldo.py --nube` copió código (bundle de
Git) y base de datos a OneDrive, sin `.env`, contraseñas ni fotos. Desde esa
copia se restauró en una carpeta aparte: las huellas SHA-256 del manifiesto
coincidieron, el código quedó en el mismo commit, la base pasó
`integrity_check` con los mismos conteos que la original (236 eventos,
24 alertas, 65 registros de bitácora) y las pruebas de la API pasaron sobre el
código restaurado. Restaurar tomó 2 s, más instalar dependencias en el equipo
nuevo.

## Avisos fuera del dashboard

7 de octubre, 00:05: con el bot de Telegram del equipo configurado en el
`.env`, el envío de prueba del sistema respondió `{'telegram': 'ok'}`
(Telegram aceptó el mensaje para el chat del equipo). Solo avisa alertas
críticas, con tope de 20 por minuto.

## Matriz de pruebas

[matriz-de-pruebas.md](matriz-de-pruebas.md): qué se probó, cómo, y qué falta
probar con la cámara del stand.

## Pendientes de medir con la cámara real

Se miden en el Bootcamp, con las cámaras Hikvision del stand:
- Lectura de las placas impresas frente a la cámara: distancia y ángulo, con
  `PLATE_CONF=0.60`.
- Tiempo desde que una persona de la lista negra da la cara hasta la alerta, y
  hasta el aviso en el celular.
- Tiempo desde que la placa entra en cuadro hasta que aparece la alerta.
- Tiempo de aviso de cámara caída y de recuperación (desconectar el cable).
- `python tools/metricas.py --muestrear 300` con la cámara real a 8 fps.
