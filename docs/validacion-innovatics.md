# Validación: pruebas, métricas y condiciones para el piloto

Esta guía define qué debe probarse antes de presentar GOSS IP como solución desplegable en una institución (la UAEMéx es el caso de estudio). El objetivo es convertir “el código funciona” en evidencia medible y una demo repetible.

## 1. Congelación de funciones para la final

| Módulo | Estado para demo | Acción |
|---|---|---|
| Placas + lista negra | usar | prueba día/noche y ángulo |
| Alertas + folio + evidencia | usar | validar flujo completo |
| Clips | usar | probar pre/post evento |
| Zonas/horarios | usar | escenario controlado |
| Cámara caída | usar | desconectar una cámara |
| Mapa | usar | ubicar cámaras del piloto |
| Pose/caída/movimiento súbito | no presentar como función de seguridad | experimental; validar en campo |
| Comparación facial | opcional | solo con participantes que firmaron su autorización; umbral sin calibrar |
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

## 12. Etapas de madurez: dónde está GOSS IP

| Etapa | Qué exige | Estado |
|---|---|---|
| **Prototipo funcional** | El flujo completo corre de punta a punta con cámaras reales | **Cumplido.** Dos Hikvision DS-2CD1063G2-LIU, worker en GPU, alertas, revisión, canalización, ficha de evidencia; 301 pruebas automatizadas |
| **Prototipo técnicamente validado** | Métricas de campo con denominador (secciones 2–8): precisión de placas por distancia y luz, falsos positivos por cámara/hora, disponibilidad, almacenamiento | **Parcial.** Hay ensayos de laboratorio y del stand ([evidencias](evidencias/README.md)); faltan las pruebas de campo de esta guía |
| **Piloto institucional autorizado** | Autorización escrita de la institución, aviso de privacidad, responsables designados, cámaras en sitio real, registro solo con datos autorizados | **No iniciado.** La UAEMéx es caso de estudio; no ha autorizado un piloto |
| **Listo para producción** | Piloto con métricas aceptadas, soporte y mantenimiento definidos, licencias comerciales resueltas ([licencias.md](licencias.md)), revisión jurídica | **No** |

Que las pruebas automatizadas pasen demuestra que el software hace lo que se
diseñó; **no** que sea útil, preciso o seguro en una instalación real.

## 13. Cómo se mide la utilidad

| Métrica | Cómo se obtiene hoy | Qué falta |
|---|---|---|
| Tiempo de detección (evento → alerta en pantalla) | Diferencia entre `first_seen` del evento y la creación de la alerta, en la base de datos | Medirlo en campo, por tipo de evento |
| Tiempo de validación (alerta → revisión humana) | `/api/alerts/metricas`: mediana y p95 de `segundos_hasta_revision` | Turnos reales con monitoristas |
| Falsas alertas por cámara/hora | Alertas marcadas "falso aviso" ÷ (cámaras × horas de operación) | Operación continua de al menos una semana |
| Precisión de alertas revisadas | `/api/alerts/metricas`: confirmadas ÷ (confirmadas + falsos avisos), sin ensayos | Volumen suficiente de alertas reales |
| Incidentes detectados y omitidos | Requiere una verdad de referencia (bitácora del personal de seguridad) para contar lo que el sistema NO vio | Protocolo con el área de seguridad ([protocolo de medición](impacto-uaemex/protocolo-medicion.md)) |
| Disponibilidad del sistema | Latido de cada cámara y avisos de cámara caída/recuperada; `python tools/metricas.py --resumen` | 30 días de operación |
| Costo por cámara | Equipo de cómputo ÷ cámaras que soporta a la tasa objetivo, más licencias y mantenimiento | Capacidad medida por nodo (sección 7) y licencias comerciales |

## 14. Comparación con soluciones existentes

GOSS IP no compite con estos productos en madurez: son plataformas comerciales
con años en el mercado. La comparación sirve para ubicar la propuesta y separar
lo verificable de lo que todavía es hipótesis. Fuentes públicas consultadas el
9 de octubre de 2026; los precios son de revendedores y varían por región.

| Aspecto | Hikvision AcuSense | Milestone XProtect | AXIS Camera Station Pro | GOSS IP (hoy) |
|---|---|---|---|---|
| Qué es | Analítica dentro de cámaras y grabadores Hikvision | Software de gestión de video (VMS) | VMS para cámaras Axis y de terceros | Plataforma de analítica y alertas sobre cámaras IP |
| Analítica | Clasifica humano/vehículo para filtrar falsas alarmas; el fabricante declaró una precisión cercana a 98 % en un anuncio de 2020 ([Hikvision](https://www.hikvision.com/en/newsroom/latest-news/2020/hikvision-launches-new-generation-of-acusense-products)) | Grabación y gestión; la analítica se integra con complementos | Búsqueda, eventos y automatización; analítica según la cámara | Placas mexicanas, comparación facial opcional, reglas por zona y horario, cámara caída |
| Compatibilidad | Equipos Hikvision | Muchos modelos de varios fabricantes | Licencia Core para Axis; Universal para ONVIF/RTSP de terceros | RTSP/ONVIF; eventos ISAPI de Hikvision probados |
| Procesamiento | En la cámara o el grabador | Servidor propio | Servidor propio | Equipo local con GPU |
| Licenciamiento | Incluido en el equipo | Essential+ gratuita hasta 8 dispositivos ([Milestone](https://doc.milestonesys.com/2024R1/en-US/standard_features/sf_mc/sf_licensing/mc_licensesexplained.htm)); ediciones superiores por dispositivo | Por dispositivo; p. ej. Core 5 años ≈ 96 USD por cámara en un revendedor ([CDW](https://www.cdw.com/product/axis-camera-station-pro-core-subscription-license-5-years-1-device/7964181)) | Código propio; dos componentes con licencia restrictiva para uso comercial (YOLO11 AGPL, `buffalo_l` no comercial) |
| Revisión humana documentada | Alarmas y notificaciones | Gestión de alarmas | Gestión de eventos | Folio, resultado de la revisión, canalización, ficha con SHA-256 y métricas de precisión |

**Lo verificable hoy:** GOSS IP integra en un solo flujo lectura de placas con
formatos mexicanos, reglas por zona y horario, revisión humana obligatoria con
resultado registrado, canalización y ficha de evidencia, procesando en un
equipo local con cámaras existentes.

**Hipótesis de diferenciación que falta demostrar:**

1. Que esa integración reduzca el tiempo entre el evento y su atención frente a
   la operación actual de la institución (requiere medición de campo).
2. Que el costo por cámara sea competitivo una vez resueltas las licencias
   comerciales de los modelos (requiere capacidad medida por nodo y cotización).
3. Que el registro de la revisión humana (resultado, canalización, ficha) aporte
   trazabilidad que las instituciones no tienen hoy (requiere entrevistas con
   el personal de seguridad).
4. Que la precisión de placas en accesos reales sea suficiente con cámaras
   existentes (requiere la matriz de la sección 3 en sitio).

No debe afirmarse que GOSS IP es superior, más barato o más preciso que estas
plataformas sin esas mediciones.

## 15. Pendientes que requieren pruebas físicas, permisos o información externa

| Pendiente | Por qué no se resuelve en el código |
|---|---|
| Precisión de placas a 3–8 m desde un poste | Depende del lente y del punto de instalación; con lente de 2.8 mm la placa queda muy pequeña |
| Calibrar el umbral facial | Requiere un conjunto de personas que consientan, en condiciones reales de luz |
| Validar pose, caídas y movimiento súbito | Requiere escenarios controlados y conteo de falsos positivos y negativos |
| Capacidad por nodo (cámaras por GPU) | Requiere la prueba de carga de la sección 7 con el hardware del piloto |
| Autorización institucional y aviso de privacidad | Decisión de la institución, no del equipo |
| Autoridad garante de datos en el Estado de México | Régimen en transición desde 2025 ([privacidad.md](privacidad.md#marco-jurídico-qué-debe-revisar-la-institución)) |
| Licencias comerciales de YOLO11 y `buffalo_l` | Requiere cotización o cambio de modelo ([licencias.md](licencias.md)) |
| Integración con la red institucional (VLAN, firewall) | Requiere al área de TI ([arquitectura-despliegue.md](arquitectura-despliegue.md)) |
