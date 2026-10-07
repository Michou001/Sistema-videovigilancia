# Plan de validación de campo e InnovaTICs

Esta guía define qué debe probarse antes de presentar GOSS IP como solución desplegable. El objetivo es convertir “el código funciona” en evidencia medible y una demo repetible.

## 1. Congelación de funciones para la final

| Módulo | Estado para demo | Acción |
|---|---|---|
| Placas + lista negra | usar | prueba día/noche y ángulo |
| Alertas + folio + evidencia | usar | validar flujo completo |
| Clips | usar | probar pre/post evento |
| Zonas/horarios | usar | escenario controlado |
| Cámara caída | usar | desconectar una cámara |
| Mapa | usar | ubicar cámaras del piloto |
| Pose/caída | usar con advertencia | validar en campo |
| Reconocimiento facial | opcional | calibrar y justificar uso |
| Búsqueda semántica | opcional | no depender de ella |
| Armas | no usar como función operativa | mantener desactivado |

Durante estabilización no agregar funciones salvo correcciones críticas.

## 2. Prueba de infraestructura

Por cada cámara:

1. ejecutar python -m edge.worker --diagnostico;
2. registrar FPS reales;
3. registrar reconexiones;
4. registrar frames perdidos;
5. medir latencia;
6. mantener stream al menos 30 min;
7. repetir en peor horario de red/iluminación.

Criterio inicial: 0 reconexiones en prueba corta estable, procesamiento de al menos 70 % del FPS objetivo, sin crecimiento continuo de latencia y pérdidas explicadas por limitación intencional.

## 3. Prueba de placas

Matriz mínima: frontal, 15–30°, distancia corta/media, vehículo lento/normal, poca luz, contraluz y placa parcialmente sucia.

Registrar placa real, lectura, confianza, tiempo, tamaño de placa en píxeles, normalización y tipo de alerta.

No presentar 35/35 como rendimiento universal; corresponde al conjunto específico de pruebas descrito en el proyecto.

## 4. Zonas y reglas

Probar intrusión dentro/fuera de horario, cruce permitido/prohibido, persona detenida sobre línea y merodeo por encima del umbral.

Objetivo: demostrar reglas reproducibles y explicables, no una clasificación opaca de persona sospechosa.

## 5. Pose y movimiento

Probar caída simulada, agacharse, sentarse, acostarse lentamente, manos arriba, caminar/correr y movimiento rápido sin agresión. Registrar falsos positivos y falsos negativos. Presentar “posible evento” hasta contar con suficiente validación estadística.

## 6. Resiliencia

### API caída
1. iniciar worker;
2. detener API;
3. provocar eventos;
4. comprobar data/spool;
5. reiniciar API;
6. confirmar reenvío.

### Cámara caída
Desconectar red/alimentación, medir tiempo hasta alerta, reconectar y verificar aviso de recuperación.

### Detector con fallo
Confirmar en logs que un detector que falla no detiene los demás.

## 7. Carga multicámara

Incrementar gradualmente y registrar:

| Cámaras | GPU | VRAM | FPS/cámara | CPU | red | eventos/s |
|---:|---|---:|---:|---:|---:|---:|
| 1 | | | | | | |
| 2 | | | | | | |
| 4 | | | | | | |
| 8 | | | | | | |

El máximo operativo se define cuando una métrica incumple su objetivo, no por un número teórico.

## 8. Almacenamiento

Medir 24 h: eventos, fotos, clips, base de datos y crecimiento de data. Proyectar GB/día y GB/mes. Aplicar retención y verificar que el disco converge en vez de crecer sin límite.

## 9. Demo de 3–5 minutos

1. mapa con cámaras en línea;
2. acceso vehicular en vivo;
3. pasar una placa de prueba;
4. generar cruce con lista negra;
5. mostrar alerta, folio y evidencia;
6. abrir clip;
7. mostrar zona/horario;
8. desconectar una cámara o ejecutar escenario preparado;
9. mostrar alerta de cámara sin señal;
10. cerrar con arquitectura de red y escalamiento.

Tener video de respaldo de los mismos escenarios por si falla la red, cámara o iluminación del evento.

## 10. Checklist de salida

- [ ] rama de demo con CI verde
- [ ] secretos fuera de Git
- [ ] admin con contraseña no demo
- [ ] cámaras con usuario dedicado
- [ ] hora sincronizada
- [ ] GPU verificada
- [ ] diagnóstico de cada fuente aprobado
- [ ] placa y lista negra de demostración preparadas
- [ ] clips probados
- [ ] notificación de prueba enviada
- [ ] zonas cargadas
- [ ] mapa con coordenadas
- [ ] retención activa
- [ ] respaldo de BD
- [ ] plan sin Internet comprobado
- [ ] video de contingencia listo
- [ ] privacidad/licencias preparadas
- [ ] armas desactivado

## 11. Criterio de éxito

El piloto no se considera exitoso por tener IA. Se considera exitoso si las cámaras permanecen disponibles, la red soporta streams, las alertas llegan a tiempo, los falsos positivos son aceptables, la evidencia sirve para revisar, un fallo parcial no pierde datos y personal ajeno al desarrollo puede operarlo.

Las métricas reales del piloto deben reemplazar progresivamente las estimaciones de laboratorio del README.
