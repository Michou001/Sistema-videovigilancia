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
| Embeddings que sí coincidieron | 7 días | `RETENCION_EMBEDDINGS_DIAS` |

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

## Lo que este sistema NO hace, a propósito

- **No guarda video continuo.** Solo recortes de eventos concretos.
- **No identifica a personas que no estén en la lista negra.** No hay un
  "quién es esta persona" — solo un "¿es alguna de estas?".
- **No rastrea trayectorias** ni construye perfiles de movimiento.
- **No expone los embeddings** por ningún endpoint, ni los escribe en logs.

Estas ausencias son decisiones de diseño, no funciones pendientes. Agregar
cualquiera de ellas cambia por completo el perfil legal del sistema.
