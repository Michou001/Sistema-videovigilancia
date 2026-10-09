# Caso de estudio UAEMéx y defensa en InnovaTICs

GOSS IP es una plataforma para instalaciones institucionales en general; la
UAEMéx es su **caso de estudio**: el entorno de referencia para diseñarlo y
evaluarlo. **No es un cliente confirmado** y la universidad no ha autorizado ni
adoptado el proyecto. Este documento reúne las fuentes del caso y los
argumentos para defenderlo ante el jurado (categoría Gobierno y Sociedad
Digital).

Fuentes consultadas el 7 de octubre de 2026. Cada dato lleva su fecha y su
enlace; antes de citarlo en la defensa conviene abrir el enlace de nuevo.

## 1. El problema, según fuentes públicas recientes

| Dato | Fecha | Fuente |
|---|---|---|
| Caso de acoso en una unidad del Potrobús; el agresor bajó sin ser identificado. | 4 sep 2026 | [AD Noticias](https://adnoticias.mx/reforzara-uaemex-seguridad-potrobus/) |
| Estudiantes reportan asaltos en calles cercanas al campus, acoso y falta de acciones concretas. | 17 sep 2026 | [Quadratín Edomex](https://edomex.quadratin.com.mx/preocupa-a-estudiantes-de-la-uaemex-inseguridad-y-acoso-en-toluca/) |
| La UAEMéx acuerda con el Enjambre Estudiantil: más cámaras, credencial digital para entrar a CU y al Potrobús, GPS en 18 de 49 unidades, rutas vigiladas por el C4 de San Mateo Atenco, alumbrado con Toluca. | 6 oct 2026 | [AD Noticias](https://adnoticias.mx/reforzara-uaemex-seguridad-potrobus/), [Plana Mayor](https://planamayor.com.mx/uaemex-acuerda-reforzar-seguridad-en-campus-y-rutas-de-potrobus-tras-reunion-con-enjambre-estudiantil/) |
| Percepción de inseguridad en Toluca: 71 % (junio 2026), contra 78.9 % un año antes, según el Ayuntamiento; la cifra por ciudad no se pudo leer como texto en la presentación de INEGI. La ENSU amplió su muestra en Toluca, así que parte de la baja puede ser metodológica. Es percepción de adultos de la ciudad, no de alumnos. | 1 oct 2026 | [Ayuntamiento de Toluca](https://www2.toluca.gob.mx/la-percepcion-de-inseguridad-en-toluca-es-la-mas-baja-en-su-historia-ricardo-moreno/), [DigitalMex](https://digitalmex.mx/municipios/story/71247/toluca-baja-percepcion-inseguridad-inegi-71) |
| Toluca reporta baja de 25.36 % en robo a transeúnte y 56.09 % en robo de vehículo (comunicado municipal, no desglosa por colonia ni es una tasa de incidentes universitarios). | 21 ene 2026 | [Ayuntamiento de Toluca](https://www2.toluca.gob.mx/registra-toluca-incidencia-delictiva-mas-baja-en-una-decada-ricardo-moreno/) |
| Operativo "UAEMéx" al regreso a clases: policía de Toluca, Secretaría de Seguridad estatal y Seguridad Institucional de la UAEMéx en CU, Medicina, Los Uribe y preparatorias, desde las 5:45 h. | 4 feb 2025 | [La Jornada Edomex](https://lajornadaestadodemexico.com/implementan-el-operativo-uaemex/) |
| Asaltos de motociclistas a peatones en la col. Universidad de 6 a 8 h y de 19 a 20 h; robo de autopartes de madrugada. | 24 feb 2023 | [N+](https://www.nmas.com.mx/estado-de-mexico/denuncian-aumento-de-asaltos-a-bordo-de-motos-en-toluca/) (antecedente de 2023: inspira los horarios de las plantillas, no prueba que sigan siendo los de mayor riesgo) |

No se encontró una estadística oficial pública de delitos por plantel. La UAEMéx
anunció un Atlas de Seguridad con las denuncias de su comunidad, pero no está
publicado. En la defensa: "reportes documentados", no cifras por campus.

## 2. ¿Hay convenio con la policía?

No se encontró un convenio público UAEMéx–Secretaría de Seguridad para
enlazar cámaras universitarias al C5. Lo que sí hay es coordinación operativa:
el Operativo UAEMéx (2025), botones de pánico de la FaCiCo enlazados al C2 y C5
de Toluca ([DigitalMex, 2022](https://www.digitalmex.mx/seguridad/story/32849/uaemex-reforzara-seguridad-planteles))
y el C4 municipal de San Mateo Atenco para las rutas del Potrobús (2026).

El C5 del Edomex opera 20,000 cámaras en 5,000 postes y 86 arcos carreteros
con lector de placas ([SSEM](https://sseguridad.edomex.gob.mx/c-5)). El 26 sep
2026 obtuvo acceso a las 185 cámaras del Circuito Exterior Mexiquense mediante
convenio con su concesionaria ([Heraldo Edomex](https://estadodemexico.heraldodemexico.com.mx/municipios/2026/9/26/c5-tendra-acceso-a-camaras-del-circuito-exterior-mexiquense-para-detectar-incidentes-12480.html)):
es el precedente de cómo un tercero comparte video con el C5.

## 3. Qué hace GOSS IP cuando detecta algo

1. El borde detecta (placa, rostro, persona en zona, caída, cámara caída).
2. La API cruza contra lista negra y reglas de zona/horario y fija la severidad.
3. Alerta con folio, foto y clip en el dashboard; las críticas llegan por Telegram.
4. El monitorista la atiende o la descarta, con nota.
5. **Canalizar**: registra a quién pasó el caso (seguridad institucional
   —en la UAEMéx, Seguridad Institucional—, 911/C5, C4 municipal, Fiscalía) y
   el folio externo. Queda en la bitácora.
6. **Ficha de evidencia**: ZIP con ficha imprimible, foto, clip y huellas
   SHA-256 (ficha incluida) para entregar a la autoridad; las huellas quedan en
   la bitácora. Prueban que el paquete no cambió después de descargarlo, no
   sustituyen una cadena de custodia.
7. Retención: fotos de eventos normales 7 días, eventos 30, clips 90, alertas
   y su foto 1 año. De un rostro sin coincidencia no se guarda ni la foto ni el
   vector ([privacidad](privacidad.md)).

El sistema no llama a la policía por su cuenta ni identifica a nadie como
culpable. Su objetivo es acortar el tiempo entre el hecho y que el guardia lo
sabe, y dejar la evidencia ordenada para una denuncia; ese tiempo todavía no se
ha medido en un plantel. La hora de una canalización es la de su registro, no
confirma que la otra instancia la recibió.

## 4. Plantillas de reglas (editor de zonas)

| Plantilla | Regla | Horario | Base |
|---|---|---|---|
| Acceso: entrada y salida | merodeo > 45 s, crítica | lun–sáb 06:00–08:30 y 19:00–21:00 | asaltos en horas de llegada y salida |
| Estacionamiento: madrugada | merodeo > 30 s, crítica | todos los días 22:00–06:00 | robo de autopartes nocturno |
| Área restringida | intrusión, crítica | noches y fines de semana | laboratorios, site, almacén |

Los 30 y 45 segundos son parámetros de ensayo, no valores validados: se
ajustan al plantel después de medir falsos positivos en campo.

## 5. Defensa ante el jurado

### Qué resuelve, para quién y por qué

- **Qué resuelve:** apoya la detección, priorización y revisión de eventos
  relevantes en instalaciones con muchas cámaras y poco personal para mirarlas.
- **Para quién:** el personal de supervisión y seguridad de instituciones con
  cámaras IP compatibles (escuelas, edificios públicos); la UAEMéx como caso.
- **Por qué hace falta:** tener cámaras no garantiza que un evento se vea ni se
  atienda a tiempo; la mayor parte del video se revisa después.
- **Qué ofrece:** analítica automática, alertas con evidencia verificable,
  revisión humana obligatoria con resultado registrado, canalización y
  administración por roles.
- **Qué lo diferencia:** la integración modular y adaptable de esas piezas,
  procesando en equipos de la institución y con cámaras existentes. Que eso sea
  más útil o más económico que las alternativas es una **hipótesis** con plan
  de medición ([validación §13–14](validacion-innovatics.md#13-cómo-se-mide-la-utilidad)).

### Alcance de hoy

Prototipo funcional con validación técnica parcial: se puede demostrar en vivo
la lectura de placas de cerca, el cruce con un registro simulado, la alerta con
folio, foto y clip, la revisión y la canalización, la ficha de evidencia, las
reglas por zona y el aviso de cámara caída. **No** es un piloto autorizado ni
un sistema listo para producción ([etapas](validacion-innovatics.md#12-etapas-de-madurez-dónde-está-goss-ip)).

### Preguntas difíciles y respuesta honesta

| Pregunta | Respuesta |
|---|---|
| ¿No existe ya esto (Hikvision, Milestone, Axis)? | Sí existen plataformas maduras. No inventamos la lectura de placas ni la comparación facial; integramos analítica, revisión humana y trazabilidad en un flujo, con cámaras existentes y procesamiento local. Si eso supera a las alternativas es lo que el piloto debe medir ([comparación](validacion-innovatics.md#14-comparación-con-soluciones-existentes)). |
| ¿La IA decide a quién detener? | No. Genera una alerta preliminar; una persona la revisa, la clasifica y, si corresponde, la canaliza. Ninguna coincidencia dispara una acción por sí sola. |
| ¿Qué tan preciso es? | En ensayo, 35/35 lecturas de placa en la prueba de ángulos y lecturas correctas en el stand a 1–2 m. No hay todavía una precisión de campo con denominador; a más de ~3 m con lente gran angular la lectura no es confiable. |
| ¿Y la privacidad? | Procesamiento local, rostros de quien no coincide sin guardar, baja que borra la biometría, retención automática, roles con verificación en dos pasos y bitácora. Procesar localmente no exime de la ley: el aviso y la base jurídica los define la institución. |
| ¿La UAEMéx ya lo usa? | No. Es el caso de estudio; un piloto requiere su autorización. |
| ¿Cuánto cuesta? | Software propio, sin licencia por cámara de terceros, pero dos modelos tienen licencias que restringen el uso comercial y habría que cotizarlas o reemplazarlos ([licencias](licencias.md)). El costo por cámara depende de la capacidad por GPU, aún no medida en producción. |
| ¿Qué pasa si falla la red o la API? | El worker guarda los eventos en disco y los reenvía; una cámara caída genera aviso; un detector que falla no detiene a los demás (cubierto por pruebas automatizadas). |
