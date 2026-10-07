# Material de la demostración

| Archivo | Para qué |
|---|---|
| `placa_ZTP482A.pdf` | Placa de prueba para imprimir al 100 % en hoja carta horizontal. Serie ZTP: sin entidad asignada, no corresponde a un vehículo real. |
| `placa_ZTP482A.png` | La misma placa en imagen (para mostrarla en una tablet o en la presentación). |
| `videos/` | Videos del ensayo para el plan B (no se suben a Git). Se graban con `python tools/grabar_video.py --segundos 90`. |

## Cómo usar la placa

1. Imprimir sin ajustar a la página, recortar por las marcas y pegar en un
   cartón oscuro con forma de defensa: el detector se apoya en el contexto de
   un vehículo y una placa suelta se detecta peor.
2. Darla de alta en la lista negra: `ZTP-482-A`, motivo "Prueba de
   demostración", vigencia 7 días.
3. Mostrarla de frente a ~1 m de la cámara, quieta 2–3 s, y retirarla: el
   evento se emite cuando la placa sale de cuadro.

Resultado de la validación con los modelos (escenas generadas, no fotos):
cuando el detector la encuentra el OCR la lee bien siempre, pero el
detector la acepta en cerca de la mitad de las escenas
([docs/evidencias](../docs/evidencias/README.md)). Por eso el accesorio
principal de la demo es la **foto a color de una placa real** (el auto de un
integrante, con su permiso) impresa a tamaño real; esta placa queda de
respaldo.
