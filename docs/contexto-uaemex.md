# Contexto UAEMéx: qué problema atiende GOSS IP y con qué fuentes

Fuentes consultadas el 7 de octubre de 2026. Cada dato lleva su fecha y su
enlace; antes de citarlo en la defensa conviene abrir el enlace de nuevo.

## 1. El problema, según fuentes públicas recientes

| Dato | Fecha | Fuente |
|---|---|---|
| Caso de acoso en una unidad del Potrobús; el agresor bajó sin ser identificado. | 4 sep 2026 | [AD Noticias](https://adnoticias.mx/reforzara-uaemex-seguridad-potrobus/) |
| Estudiantes reportan asaltos en calles cercanas al campus, acoso y falta de acciones concretas. | 17 sep 2026 | [Quadratín Edomex](https://edomex.quadratin.com.mx/preocupa-a-estudiantes-de-la-uaemex-inseguridad-y-acoso-en-toluca/) |
| La UAEMéx acuerda con el Enjambre Estudiantil: más cámaras, credencial digital para entrar a CU y al Potrobús, GPS en 18 de 49 unidades, rutas vigiladas por el C4 de San Mateo Atenco, alumbrado con Toluca. | 6 oct 2026 | [AD Noticias](https://adnoticias.mx/reforzara-uaemex-seguridad-potrobus/), [Plana Mayor](https://planamayor.com.mx/uaemex-acuerda-reforzar-seguridad-en-campus-y-rutas-de-potrobus-tras-reunion-con-enjambre-estudiantil/) |
| Percepción de inseguridad en Toluca: 71 % (junio 2026), contra 78.9 % un año antes. La ENSU amplió su muestra en Toluca, así que parte de la baja puede ser metodológica. | 1 oct 2026 | [Ayuntamiento de Toluca](https://www2.toluca.gob.mx/la-percepcion-de-inseguridad-en-toluca-es-la-mas-baja-en-su-historia-ricardo-moreno/), [DigitalMex](https://digitalmex.mx/municipios/story/71247/toluca-baja-percepcion-inseguridad-inegi-71) |
| Toluca reporta baja de 25.36 % en robo a transeúnte y 56.09 % en robo de vehículo (comunicado municipal, no desglosa por colonia). | 21 ene 2026 | [Ayuntamiento de Toluca](https://www2.toluca.gob.mx/registra-toluca-incidencia-delictiva-mas-baja-en-una-decada-ricardo-moreno/) |
| Operativo "UAEMéx" al regreso a clases: policía de Toluca, Secretaría de Seguridad estatal y Seguridad Institucional de la UAEMéx en CU, Medicina, Los Uribe y preparatorias, desde las 5:45 h. | 4 feb 2025 | [La Jornada Edomex](https://lajornadaestadodemexico.com/implementan-el-operativo-uaemex/) |
| Asaltos de motociclistas a peatones en la col. Universidad de 6 a 8 h y de 19 a 20 h; robo de autopartes de madrugada. | 24 feb 2023 | [N+](https://www.nmas.com.mx/estado-de-mexico/denuncian-aumento-de-asaltos-a-bordo-de-motos-en-toluca/) (antecedente; base de las plantillas de reglas) |

No hay una estadística oficial pública de delitos por plantel. La UAEMéx
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
5. **Canalizar**: registra a quién pasó el caso (Protección Universitaria,
   911/C5, C4 municipal, Fiscalía) y el folio externo. Queda en la bitácora.
6. **Ficha de evidencia**: ZIP con ficha imprimible, foto, clip y huellas
   SHA-256 para entregar a la autoridad; las huellas quedan en la bitácora.
7. Retención: fotos de eventos normales 7 días, eventos 30, clips 90, alertas
   y su foto 1 año.

El sistema no llama a la policía por su cuenta ni identifica a nadie como
culpable: acorta el tiempo entre el hecho y que el guardia lo sabe, y deja la
evidencia ordenada para una denuncia.

## 4. Plantillas de reglas (editor de zonas)

| Plantilla | Regla | Horario | Base |
|---|---|---|---|
| Acceso: entrada y salida | merodeo > 45 s, crítica | lun–sáb 06:00–08:30 y 19:00–21:00 | asaltos en horas de llegada y salida |
| Estacionamiento: madrugada | merodeo > 30 s, crítica | todos los días 22:00–06:00 | robo de autopartes nocturno |
| Área restringida | intrusión, crítica | noches y fines de semana | laboratorios, site, almacén |

Son un punto de partida; se ajustan al plantel después de medir falsos
positivos en campo.
