# Ensayo de perspectiva con cámaras montadas a 2–3 m

Las dos cámaras ya están instaladas. Esta prueba usa **su video real**, no una
imagen inclinada artificialmente. La altura por sí sola no fija el resultado:
también importan la inclinación, la distancia horizontal, el lente y el lugar
por donde pasa el objetivo.

## Encuadre que se busca

```text
cam-01: rostro/persona                       cam-02: placa
cámara (2–3 m)                               cámara (2–3 m)
       \  vista oblicua                             \  vista oblicua
        \ rostro a ~1.6 m                           \ placa a ~0.6–1 m
         \  2 m | 3 m | 4 m                            \  2 m | 3 m | 4 m
          recorrido marcado en el piso               recorrido marcado
```

Para la prueba de placa, hay que poner el objetivo en el trayecto que la cámara
ve **de frente u oblicuo leve**. Una placa justo debajo de la cámara prueba
lectura cercana, pero no representa un acceso vehicular. Para rostros, comprueba
que se vea la cara, no solo la coronilla. No ajustes ambas cámaras para maximizar
el área visible si eso vuelve pequeños los objetivos.

## Prueba física

En **Monitoreo**, abre “Ensayo de perspectiva · cámaras montadas a 2–3 m”.
Elige cámara, objetivo y distancia y pulsa **Medir 30 s** antes de iniciar las
pasadas. La pantalla muestra p10, mediana y cuántas detecciones alcanzaron el
margen de instalación. Repite para cada combinación; el cuadro no estima la
distancia por imagen, solo usa la que marcaste en el piso.

1. Marca con cinta 2, 3 y 4 m **horizontales desde la vertical de cada cámara**.
   Anota la altura real y la inclinación aproximada. Coloca luz estable.
2. En cada marca, haz tres pasadas de una persona que haya aceptado participar.
   Mira hacia la cámara al cruzar la zona; sal completamente del cuadro entre
   pasadas para que se cierren los tracks.
3. En cada marca, presenta la placa sintética de `demo/` a la altura a la que
   iría en un vehículo, con fondo oscuro y sin acercarla a la lente. Haz tres
   pasadas separadas. Usa la misma placa y orientación en ambas cámaras.
4. Repite la matriz `cam-01`/`cam-02` × rostro/placa × 2/3/4 m. En cada ensayo
   ejecuta el medidor de abajo **antes** de las pasadas. No utilices los botones
   “Probar placa/rostro”: esos solo simulan la alerta del dashboard y Telegram.

```powershell
venv\Scripts\python.exe tools/ensayar_perspectiva.py --camara cam-01 --tipo face --distancia 3 --segundos 30 --csv data/ensayos-perspectiva.csv
venv\Scripts\python.exe tools/ensayar_perspectiva.py --camara cam-02 --tipo plate --distancia 3 --segundos 30 --esperado ZTP-482-A --csv data/ensayos-perspectiva.csv
```

Cambia cámara, tipo y distancia para completar las demás combinaciones. El
medidor solo lee la base de datos. Su CSV contiene conteos y tamaños, sin caras,
fotos ni textos de placas.

Para consultar una línea base de eventos anteriores usa `--minutos 120` sin
`--distancia`: el historial no registra a cuántos metros pasó cada objetivo.

## Cómo decidir si el encuadre sirve

- **Rostro:** busca al menos 100 px de ancho (margen doble respecto al mínimo
  de 50 px del detector), cara visible y tres eventos en tres pasadas.
- **Placa:** busca al menos 72 px de ancho (margen doble respecto a 36 px) y
  lectura correcta en las tres pasadas. Una caja grande sin lectura no basta.
- **Angulo de placa:** repite a 0°, 25°, 40° y 55° respecto al frente de la
  cámara, sin cambiar la distancia ni la luz. Anota por separado si aparece
  el recuadro ámbar (placa localizada) y si se vuelve verde (lectura validada).
  Si no aparece recuadro, el límite está en la localización o el encuadre; si
  aparece ámbar, el límite está en la lectura. No atribuyas un acierto en vista
  frontal a las pasadas oblicuas.
- Mira el **p10**, no solo la mediana: muestra qué pasa en las peores pasadas.
  Si una distancia falla, acerca la zona útil, reduce la inclinación o ajusta
  el lente y repite. Registra también fallos sin evento: el medidor no puede
  contarlos por sí solo.
- Una coincidencia de rostro requiere dar de alta previamente la referencia
  de una persona participante; sin esa referencia, la prueba valida detección
  y tamaño, pero no identificación ni alarma de lista negra.

El ensayo registra eventos reales en la instalación actual. Al acabar, revisa
las capturas y la alerta en el dashboard; separa estos resultados de los
botones de simulación al presentar la demo.
