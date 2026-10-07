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

[validacion_placa_ZTP482A.csv](validacion_placa_ZTP482A.csv) muestra la placa
de `demo/` pasada por el detector y el OCR del worker, al umbral de
producción, sobre escenas generadas:
- **OCR:** cuando la placa se detecta, la lectura es correcta siempre.
- **Detector:** acepta la placa dibujada en cerca de la mitad de las escenas,
  entre 140 y 300 px de ancho.

Por eso, en la demo el accesorio principal es la foto de una placa real y
esta placa queda de respaldo (ver `tools/placa_demo.py`).

## Pendientes de medir con la cámara real

Se miden en el Bootcamp, con la cámara Hikvision del stand:
- Lectura de la placa de prueba frente a la cámara: distancia y ángulo.
- Tiempo desde que la placa entra en cuadro hasta que aparece la alerta.
- Tiempo de aviso de cámara caída y de recuperación (desconectar el cable).
- `python tools/metricas.py --muestrear 300` con la cámara real a 8 fps.
