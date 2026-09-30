# Privacidad y protección de datos

Este sistema trata **datos personales** y, en el caso de los rostros, **datos
personales sensibles**. Este documento explica qué se recoge, cuánto tiempo se
conserva y qué obligaciones legales aplican.

> No es asesoría legal. Antes de instalarlo en un sitio real, esto debe
> revisarlo alguien del área jurídica de la institución.

---

## Qué datos trata el sistema

| Dato | Categoría | ¿Se guarda? |
|---|---|---|
| Placa vehicular (texto) | Personal — identifica al titular | Sí, 30 días |
| Foto del vehículo | Personal | 7 días si no hay coincidencia |
| **Embedding facial** | **Personal SENSIBLE (biométrico)** | **Solo si coincide con lista negra** |
| Foto de rostro | **Personal SENSIBLE** | Solo si coincide |
| Detección de arma | Personal (vinculado a quien la porta) | 1 año |
| Registros de operadores | Personal | Mientras dure la cuenta |
| **Video en vivo del dashboard** | Personal (imagen de quien pase) | **No. Solo en memoria** |
| Clip de video de una alerta (~20 s) | Personal (imagen de quien pase) | Solo de alertas, 90 días |
| Aviso por Telegram/correo/WhatsApp | Personal (texto de la alerta) | Lo guarda el servicio externo |
| Vector de búsqueda por descripción (opcional) | Personal (apariencia, derivado de la foto) | Lo que viva su foto |
| Cuadro de entrenamiento (opcional) | Personal (imagen de quien pase) | Solo alertas, 30 días, en el worker |

---

## La decisión de diseño más importante

**Los embeddings faciales de personas que NO están en la lista negra no se
guardan nunca.** Se calculan en memoria, se comparan, y se descartan.

El motivo es directo: ese vector ya cumplió su única función. Conservarlo
convertiría el sistema en una base de datos biométrica de todas las personas
que pasan frente a la cámara — alumnos, profesores, visitantes — sin ninguna
finalidad que lo justifique. Bajo la LFPDPPP eso es tratamiento de datos
sensibles sin base legal, y además crea un activo que habría que proteger.

Lo que no se guarda no se puede filtrar, no se puede robar y no hay que
protegerlo.

Implementado en [api/routers/events.py](../api/routers/events.py).

---

## Política de retención

Configurable por variables de entorno; los valores por defecto son:

| Dato | Plazo | Variable |
|---|---|---|
| Fotos de eventos sin coincidencia | 7 días | `RETENCION_FOTOS_DIAS` |
| Eventos sin coincidencia (solo texto) | 30 días | `RETENCION_EVENTOS_DIAS` |
| Alertas y su evidencia | 365 días | `RETENCION_ALERTAS_DIAS` |
| Clips de video de las alertas | 90 días | `RETENCION_CLIPS_DIAS` |
| Embeddings que sí coincidieron | 7 días | `RETENCION_EMBEDDINGS_DIAS` |
| Lecturas de placa corregidas por un operador | 180 días | `RETENCION_CORREGIDOS_DIAS` |
| Bitácora de auditoría | 730 días | `RETENCION_AUDITORIA_DIAS` |

La distinción entre las dos primeras filas es deliberada: para responder *"¿pasó
el coche ABC-123 por aquí el martes?"* basta una línea de texto de 50 bytes. La
foto de ese coche no aporta a esa respuesta y sí es un dato personal de alguien
que no hizo nada. Por eso la foto se va a los 7 días y el texto sobrevive hasta
los 30 — el tiempo típico en que se reporta un robo.

La purga corre **automáticamente cada 24 horas** dentro de la API, y también
puede ejecutarse a mano:

```bash
python tools/purgar_datos.py --simular   # cuenta sin borrar
python tools/purgar_datos.py             # aplica
```

---

## Obligaciones al instalarlo en un sitio real

Bajo la **LFPDPPP** (Ley Federal de Protección de Datos Personales en Posesión
de los Particulares) y su reglamento:

1. **Aviso de privacidad visible en el punto de captura.** Un letrero en la
   entrada del estacionamiento, legible antes de entrar, indicando que hay
   videovigilancia con reconocimiento de placas y rostros, quién es el
   responsable y dónde consultar el aviso completo.
2. **Consentimiento expreso para datos biométricos.** El tratamiento de datos
   sensibles exige consentimiento *expreso y por escrito* del titular. Esto es
   lo que en la práctica limita el reconocimiento facial a personas que ya
   están en una lista con fundamento — no a la población general.
3. **Finalidad acotada y declarada.** "Seguridad del estacionamiento" es una
   finalidad; "análisis de comportamiento de los alumnos" sería otra distinta y
   requeriría su propia base legal.
4. **Derechos ARCO.** Las personas pueden pedir Acceso, Rectificación,
   Cancelación y Oposición sobre sus datos. Debe existir un procedimiento y un
   responsable designado.
5. **Fundamento documentado por cada alta en lista negra.** El sistema lo exige
   en el campo `legal_basis`, que es obligatorio y no acepta valores vacíos.
6. **Medidas de seguridad.** Contraseñas con bcrypt, acceso por roles, la base
   de datos fuera del alcance de la red pública.

---

## Consideraciones específicas de un entorno escolar

Un estacionamiento escolar tiene un agravante: **puede haber menores de edad.**
El tratamiento de datos personales de menores tiene requisitos reforzados y el
consentimiento debe darlo quien ejerce la patria potestad.

Recomendación práctica para el proyecto: **limita el reconocimiento facial a la
demostración técnica** y deja el sistema en producción operando solo con
placas, salvo que la institución obtenga los consentimientos correspondientes.
La detección de armas no identifica a nadie y no tiene ese problema.

---

## La vista en vivo del dashboard

El apartado de Monitoreo muestra las cámaras en tiempo real. Es video de
personas, así que se trata con las mismas reglas que el resto:

- **Los frames no tocan el disco.** Nunca. La única copia vive en memoria de la
  API y la sobreescribe el frame siguiente, unos 170 ms después. No hay archivo
  que purgar porque no hay archivo. Lo que se conserva como evidencia son las
  capturas de los eventos, que sí pasan por `data/snapshots` con su política de
  retención.
- **No hay grabación continua.** No se puede retroceder ni revisar "qué pasó
  hace diez minutos" en el video. Lo único que se graba es un clip de unos
  segundos alrededor de una **alerta** (ver abajo); de lo demás queda el
  histórico de eventos, que es lo que sí tiene fundamento conservar.
- **Solo circula mientras alguien mira.** Al cerrar el apartado, o con la
  pestaña en segundo plano, el flujo se corta y el worker deja de enviar. Con
  el dashboard cerrado no sale un solo frame de la red de las cámaras.
- **Pasado el plazo, la API suelta el último frame** de una cámara que dejó de
  enviar. No es higiene de memoria: es no quedarse con la última imagen de una
  persona indefinidamente porque el worker murió en mal momento.
- **Ver requiere sesión.** El flujo valida la sesión antes de entregar el
  primer byte. Como un `<img>` no puede mandar cabeceras, la sesión viaja en
  una cookie `HttpOnly` y `SameSite=Strict` que pone el login: no queda en el
  DOM, ni en el historial, ni la puede leer un script. Caduca con `JWT_HOURS`
  y nunca es el token de ingesta del worker.

Si por política el video no debe salir de la red de las cámaras,
`PREVIEW_ENABLED=false` en el `.env` del worker lo desactiva. Los eventos y sus
capturas siguen llegando igual.

---

## Clips de video de las alertas

Cuando la API decide que un evento es alerta (advertencia o crítica), el worker
arma un clip con los segundos anteriores y posteriores (`CLIP_PRE_S`,
`CLIP_POST_S`, 10 + 10 por defecto) y lo sube a la API. Para un parte o una
denuncia, el clip es lo que muestra qué pasó.

- **Solo de alertas.** El worker guarda en memoria los últimos segundos de
  video, comprimidos, y los va sobrescribiendo. Si no hay alerta, nada llega a
  disco.
- **Plazo propio, más corto que la alerta** (`RETENCION_CLIPS_DIAS`, 90 días):
  el clip muestra a todos los que pasaban, no solo al involucrado. La alerta y
  su foto siguen su propio plazo. Un clip sin alerta que lo referencie se borra
  en la purga.
- **Mismo acceso que las fotos:** solo con sesión, desde `/media`.
- `CLIP_ENABLED=false` en el `.env` del worker lo desactiva.

---

## Notificaciones fuera del dashboard

Telegram, correo, WhatsApp y webhook (variables `NOTIFY_*`) llevan la alerta al
celular del responsable. Eso es una **transferencia de datos personales a un
tercero** (Telegram, el proveedor de correo, Twilio/Meta), así que:

- **Por defecto no se manda la foto** (`NOTIFY_INCLUDE_PHOTO=false`): el aviso
  lleva título, cámara, hora y folio; la evidencia se consulta en el dashboard.
  Actívala solo si el aviso de privacidad contempla esa transferencia. WhatsApp
  y el webhook nunca llevan foto.
- **Solo alertas críticas** por defecto (`NOTIFY_MIN_SEVERITY`), más la caída y
  recuperación de cámaras. Los avisos de coincidencias históricas (al dar de
  alta una placa) no se notifican.
- Los tokens y contraseñas viven en el `.env` del servidor: no se ven ni se
  cambian desde el navegador, y se ocultan de los mensajes de error y del log.
- Las pruebas de envío quedan en la bitácora.

---

## Búsqueda por descripción (opcional)

Con `SEMANTIC_SEARCH=true`, la API calcula de cada captura un vector que
permite buscar "camioneta blanca" o "persona con mochila roja". Buscar a
alguien por cómo se ve es sensible, así que:

- **No es reconocimiento facial.** El vector describe la escena (colores,
  ropa, tipo de vehículo); no identifica a una persona ni se compara contra
  la lista negra.
- **Vive lo que vive la foto.** Cuando la retención borra una captura (7 días
  en eventos normales), se borra también su vector.
- **Cada búsqueda queda en la bitácora** con quién la hizo y qué escribió.
- Viene apagada. Actívala solo si el aviso de privacidad contempla la
  búsqueda en el histórico de imágenes.

## Cuadros para reentrenar los modelos (opcional)

Con `DATASET_ENABLED=true` en el worker se guardan los cuadros de las alertas
para que, con el veredicto de los operadores, se afinen los modelos
([reentrenamiento.md](reentrenamiento.md)). Se quedan en la máquina del worker,
solo de alertas, y se borran a los `RETENCION_DATASET_DIAS` (30). El ZIP que
se arma para entrenar se guarda cifrado y se borra al terminar. Viene apagado.

---

## Lo que este sistema NO hace, a propósito

- **No guarda video continuo.** Solo recortes de eventos concretos. La vista
  en vivo del dashboard no se graba en ningún punto.
- **No identifica a personas que no estén en la lista negra.** No hay un
  "quién es esta persona" — solo un "¿es alguna de estas?".
- **No rastrea trayectorias** ni construye perfiles de movimiento.
- **No expone los embeddings** por ningún endpoint, ni los escribe en logs.

Estas ausencias son decisiones de diseño, no funciones pendientes. Agregar
cualquiera de ellas cambia por completo el perfil legal del sistema.
