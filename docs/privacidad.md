# Privacidad y protección de datos

GOSS IP trata **datos personales** (imágenes de personas, placas vehiculares)
y, si se activa la comparación facial, **datos personales sensibles**
(biométricos). Este documento describe lo que el software hace realmente con
esos datos, cuánto tiempo los conserva y qué debe resolver la institución antes
de usarlo.

> No es asesoría legal ni una declaración de cumplimiento. El responsable del
> tratamiento es la **institución que opere el sistema**, no el software; antes
> de instalarlo en un sitio real debe revisarlo su área jurídica y su unidad de
> transparencia o protección de datos. Que el video se procese en equipos
> locales **reduce transferencias**, pero **no exime** de ninguna obligación.

---

## Procesar, guardar, transferir y entrenar no son lo mismo

| Acción | Qué ocurre en GOSS IP |
|---|---|
| **Procesar** | Cada cuadro se analiza en memoria en el equipo del worker (placas, rostros, personas, vehículos). El video en vivo del dashboard tampoco toca el disco. |
| **Guardar** | Solo se conservan eventos, sus fotos, los clips de alertas y la bitácora, con los plazos de la tabla de abajo. No hay grabación continua. |
| **Transferir** | No sale nada del equipo salvo lo que se configure: avisos externos (Telegram, correo, WhatsApp, webhook), respaldos en la nube y, si se usan, los mosaicos del mapa (solo coordenadas, no video). |
| **Entrenar** | Ningún modelo se reentrena solo. Con `DATASET_ENABLED=true` se guardan cuadros de alertas para afinar modelos con el veredicto de los operadores; viene apagado. |

---

## Qué datos trata el sistema y cuánto tiempo los guarda

Los plazos son los valores por defecto y se cambian por variables de entorno.

| Dato | Categoría | ¿Se guarda? |
|---|---|---|
| Placa vehicular (texto) sin coincidencia | Personal | 30 días (`RETENCION_EVENTOS_DIAS`) |
| Placa con alerta | Personal | Con su alerta: 365 días (`RETENCION_ALERTAS_DIAS`) |
| Foto de un evento sin coincidencia (vehículo, persona en una zona) | Personal | 7 días (`RETENCION_FOTOS_DIAS`) |
| **Rostro sin coincidencia** | **Sensible** | **No se guarda ni el vector ni la foto.** Queda el evento (hora y cámara), 30 días. Conservar la foto exige `FOTOS_ROSTRO_SIN_COINCIDENCIA=true` y declararlo en el aviso de privacidad |
| **Vector facial que coincidió** | **Sensible** | 7 días (`RETENCION_EMBEDDINGS_DIAS`) |
| Foto de un rostro que coincidió | Sensible | Con su alerta: 365 días |
| Referencia del registro de alertas (foto y vector de la persona) | Sensible | Mientras el registro esté de alta; **se borran al darlo de baja** |
| Lecturas de placa corregidas por un operador | Personal | 180 días (`RETENCION_CORREGIDOS_DIAS`) |
| Clip de una alerta (~20 s) | Personal (todos los que pasaban) | 90 días (`RETENCION_CLIPS_DIAS`) |
| Bitácora de auditoría | Personal (operadores) | 730 días (`RETENCION_AUDITORIA_DIAS`) |
| Copia de depuración del worker (`data/eventos-*.jsonl`, sin vectores) | Personal | 30 días, igual que los eventos |
| Eventos pendientes de envío (`data/spool/`) | Personal | Hasta que la API los recibe |
| Video en vivo del dashboard | Personal | **No.** Solo en memoria |
| Vector de búsqueda por descripción (opcional) | Personal (apariencia) | Lo que viva su foto |
| Cuadros para reentrenar (opcional) | Personal | Solo alertas, 30 días (`RETENCION_DATASET_DIAS`) |
| Cuentas de operadores | Personal | Mientras exista la cuenta |

La purga corre **automáticamente cada 24 horas** dentro de la API y también a
mano:

```bash
python tools/purgar_datos.py --simular   # cuenta sin borrar
python tools/purgar_datos.py             # aplica
```

La distinción entre texto y foto es deliberada: para responder *"¿pasó el coche
ABC-123 el martes?"* basta una línea de texto; la foto de ese coche no aporta a
esa respuesta y sí es un dato de alguien que no hizo nada.

### Lo que la purga automática NO cubre

- **Respaldos** (`tools/respaldo.py`): copian la base de datos y, con
  `--con-evidencia`, las fotos. Con `--nube` se copian a OneDrive, lo que es una
  **transferencia a un tercero**. Los respaldos deben tener su propio plazo y
  borrarse a mano o con la política de la institución.
- **Grabaciones de contingencia** (`tools/grabar_video.py`): video continuo que
  se graba a mano para una demostración. Debe borrarse al terminar el evento y
  hacerse solo con personas que lo autorizaron.
- Los registros que las plataformas externas guardan de cada aviso (Telegram,
  correo, WhatsApp) quedan bajo las políticas de esas plataformas.

---

## Decisiones de minimización

**El rostro de quien no coincide con el registro no se conserva.** El vector se
calcula en memoria, se compara y se descarta; la foto del rostro se borra en
cuanto la API confirma que no hubo coincidencia
([api/routers/events.py](../api/routers/events.py)). Conservarlos convertiría
el sistema en una base biométrica de todas las personas que pasan frente a la
cámara (alumnos, personal, visitantes) sin finalidad que lo justifique.

**La baja de una persona borra su rostro.** La fila del registro se queda
(etiqueta, motivo, fundamento, quién la dio de alta) porque las alertas
históricas la referencian y la bitácora debe poder explicar por qué hubo una
coincidencia; el vector y la foto de referencia se eliminan
([api/routers/faces.py](../api/routers/faces.py)).

**No hay grabación continua ni seguimiento de trayectorias.** No hay un "quién
es esta persona" ni perfiles de movimiento: solo un "¿es alguna de las
registradas?". Agregar cualquiera de esas funciones cambia por completo el
perfil legal del sistema.

---

## Registro institucional de alertas (lista negra)

La función existe y se conserva técnicamente, pero su uso real depende de bases
de datos **autorizadas** y de criterios que fije la institución responsable. No
es una clasificación automática de personas peligrosas: una coincidencia abre
una alerta para que una persona la revise.

| Aspecto | Qué hace hoy el software | Qué debe definir la institución |
|---|---|---|
| Procedencia | Altas manuales desde el dashboard; no hay importación de bases externas | De dónde puede venir un registro (reporte interno, orden de autoridad competente). **No conectar fuentes policiales, gubernamentales o de terceros sin autorización expresa y fundamento legal** |
| Autoridad para dar de alta | Solo el rol administrador, con verificación en dos pasos si `EXIGIR_2FA` lo pide | Quién aprueba cada alta; hoy no existe un flujo de aprobación separado de la captura |
| Justificación | Motivo y fundamento obligatorios (texto libre; para rostros, campo `legal_basis`) | Qué soporte documental vale como fundamento; el sistema no lo verifica |
| Caducidad | Vigencia opcional (placas y rostros); vencido, deja de alertar | Plazo máximo por tipo de registro |
| Corrección y eliminación | Baja desde el dashboard; en rostros borra vector y foto; las lecturas de placa se pueden corregir | Procedimiento para solicitudes de acceso, rectificación, cancelación y oposición |
| Verificación de coincidencias | Placa exacta = crítica; placa a un carácter = "Posible placa…" (advertencia); rostro por similitud con umbral configurable | Qué revisa el monitorista antes de canalizar |
| Falsos positivos | El operador marca "falso aviso"; las métricas muestran confirmadas sobre revisadas | Umbral aceptable y revisión periódica |
| Acceso | Lectura de evidencia solo con sesión; altas y bajas solo administradores | Quién tiene cada rol |
| Trazabilidad | Bitácora de altas, bajas, búsquedas, descargas de evidencia y canalizaciones | Quién audita la bitácora y cada cuánto |

**Para demostraciones:** solo registros simulados (la placa de prueba
`ZTP-482-A` usa una serie sin entidad asignada) o datos de participantes que
firmaron su autorización. Nunca personas reales sin su consentimiento.

---

## Marco jurídico: qué debe revisar la institución

La ley aplicable depende de **quién es el responsable del tratamiento**:

- **Institución pública** (una universidad pública autónoma, un ayuntamiento,
  una dependencia estatal): es **sujeto obligado** y le aplica la normativa de
  protección de datos en posesión de sujetos obligados. A nivel general, la Ley
  General publicada en el DOF el 20 de marzo de 2025
  ([texto vigente](https://www.diputados.gob.mx/LeyesBiblio/pdf/LGPDPPSO.pdf));
  en el Estado de México, la Ley de Protección de Datos Personales en Posesión
  de Sujetos Obligados del Estado de México y Municipios (2017), más las
  políticas internas de la institución. **El régimen estatal está en
  transición:** en 2025 el Congreso mexiquense aprobó extinguir el INFOEM y el
  nuevo modelo seguía pendiente de leyes secundarias a inicios de 2026
  ([El Universal](https://www.eluniversal.com.mx/metropoli/avalan-la-extincion-del-instituto-de-transparencia-del-edomex/),
  [El Sol de Toluca](https://oem.com.mx/elsoldetoluca/local/nuevo-modelo-de-transparencia-quedara-listo-antes-de-junio-28354527)).
  Hay que confirmar la autoridad garante vigente al momento de instalarlo.
- **Institución privada:** Ley Federal de Protección de Datos Personales en
  Posesión de los Particulares y su reglamento.

En cualquiera de los dos casos, como mínimo:

1. **Aviso de privacidad** visible en el punto de captura (que hay
   videovigilancia con lectura de placas y, si aplica, comparación facial;
   quién es el responsable y dónde consultar el aviso completo).
2. **Finalidad acotada y declarada.** "Seguridad de los accesos" es una
   finalidad; "analizar el comportamiento de los alumnos" sería otra distinta.
3. **Base para los datos biométricos.** Si se activa la comparación facial,
   el fundamento (consentimiento, mandato legal u otra base) debe analizarlo
   el área jurídica; por eso viene apagada.
4. **Derechos ARCO** con un procedimiento y un responsable designado.
5. **Medidas de seguridad** documentadas: roles, verificación en dos pasos,
   HTTPS, red de cámaras aislada, bitácora ([seguridad-red.md](seguridad-red.md)).
6. **Plazos de conservación**, incluidos los respaldos.

**Menores de edad.** Las preparatorias de una universidad, o cualquier escuela,
pueden tener menores. Sus datos tienen protección reforzada. Recomendación:
operar con lectura de placas y reglas por zona, y dejar la comparación facial
apagada salvo autorización expresa.

---

## La vista en vivo del dashboard

- **Los cuadros no tocan el disco.** La única copia vive en memoria de la API y
  la sobreescribe el cuadro siguiente. Lo que se conserva como evidencia son las
  capturas de los eventos, con su plazo.
- **No hay grabación continua.** Lo único que se graba es el clip alrededor de
  una alerta.
- **Solo circula mientras alguien mira.** Con el apartado cerrado o la pestaña
  en segundo plano, el worker deja de enviar video.
- **Ver requiere sesión**, con cookie `HttpOnly` y `SameSite=Strict`.

`PREVIEW_ENABLED=false` en el worker desactiva la vista en vivo si la política
lo exige; los eventos siguen llegando.

## Clips de video de las alertas

Cuando un evento es alerta, el worker arma un clip con los segundos anteriores
y posteriores (`CLIP_PRE_S`, `CLIP_POST_S`, 10 + 10) y lo sube a la API. Solo de
alertas, con plazo propio de 90 días (el clip muestra a todos los que pasaban),
mismo acceso que las fotos. `CLIP_ENABLED=false` lo desactiva.

## Avisos fuera del dashboard

Telegram, correo, WhatsApp y webhook (`NOTIFY_*`) son una **transferencia a un
tercero**:

- **Por defecto el aviso no lleva foto** (`NOTIFY_INCLUDE_PHOTO=false`): título,
  cámara, hora y folio; la evidencia se consulta en el dashboard. Activarla
  exige que el aviso de privacidad contemple esa transferencia. WhatsApp y el
  webhook nunca llevan foto.
- Por defecto solo alertas críticas (`NOTIFY_MIN_SEVERITY`) y caídas de cámara.
- Los tokens viven en el `.env` del servidor y no se muestran en el navegador.

## Búsqueda por descripción (opcional)

Con `SEMANTIC_SEARCH=true` la API calcula de cada captura un vector para buscar
"camioneta blanca". No es reconocimiento facial, vive lo que vive la foto y cada
búsqueda queda en la bitácora. Viene apagada.

## Cuadros para reentrenar (opcional)

Con `DATASET_ENABLED=true` el worker guarda los cuadros de las alertas para
afinar los modelos con el veredicto de los operadores
([reentrenamiento.md](reentrenamiento.md)). Solo alertas, en el equipo del
worker, 30 días; el ZIP de entrenamiento se cifra y se borra al terminar. Viene
apagado.
