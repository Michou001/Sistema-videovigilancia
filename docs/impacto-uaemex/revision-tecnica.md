# Revisión técnica de integración

**7 de octubre de 2026.** La investigación de impacto es documental y estática: no se cambiaron configuraciones, listas ni datos reales. Después, por solicitud adicional del usuario, se revisó CI y corrigió una preparación de pruebas; respaldo y validación se documentan en [revisión de CI](../revision-ci-2026-10-07.md).

## Confirmación del usuario

No hay piloto ni permisos institucionales comprobados. La demo será con dos cámaras del equipo y la lista de prueba. Hace falta delimitar quién puede figurar y qué acción permite una coincidencia.

## Integrar en memoria/presentación

- Cambiar «impacto logrado en UAEMéx» por **hipótesis y plan de evaluación**.
- Mantener separados evidencia internacional, ensayo propio, objetivo y escenario ficticio. Los porcentajes 13 %/37 % no son una predicción de GOSS IP.
- Adoptar la [política propuesta de lista](lista-de-seguimiento.md): coincidencia para revisión humana, nunca juicio de culpabilidad.
- Mostrar el [protocolo de dos cámaras](protocolo-medicion.md) y reportar todos los intentos.
- No asignar números de beneficiarios o reducción por campus antes de conocer cobertura, aforo y línea base.
- Documentar integración con seguridad/TI universitaria y mantenimiento posterior al equipo estudiantil.

## Hallazgos que requieren atención

| Prioridad | Evidencia inspeccionada | Implicación / cambio propuesto |
|---|---|---|
| Antes de una demo facial | `.env.example` usa `ENABLE_FACES=true` | Una demo solo de placas requiere desactivar rostros en la configuración efectiva de ambos workers; todavía no se cambió. |
| Antes de registrar personas | `api/routers/faces.py`, alta con `legal_basis` libre | Tener tres caracteres no acredita autorización; definir soporte, verificación y quién aprueba. |
| Antes de prometer borrado | Bajas en `faces.py`/`blacklist.py` solo cambian `active`; `retention.py` no gestiona explícitamente `BlacklistFace` | No confundir baja con supresión; diseñar ciclo de referencia, foto, vector, eventos, auditoría y respaldos. |
| Antes de prometer caducidad facial configurable | `BlacklistFace.expires_at` existe; alta y respuesta de `faces.py` no lo exponen | Completar API/interfaz/validación de vigencia si el módulo facial se va a usar. |
| Antes de mezclar ensayo e historial | Altas lanzan `reescanear_placa`/`reescanear_rostro` | Aislar historial de demo; para institución, autorización y alcance independientes para búsqueda retrospectiva. |
| Antes de llamar a una placa «idéntica» | `shared/plates.py`: `normalizar` colapsa caracteres; `api/matching.py` usa `coincidencia.exacta` | `exact` significa igualdad normalizada; mostrar original, correcciones y referencia. No prueba identidad vehicular. |
| Antes de admitir registros institucionales | `AltaPlaca` exige motivo, pero no aprobación, fuente, ámbito o vencimiento obligatorio | Proponer estados solicitado/aprobado/activo/expirado/revocado, separación de aprobación y captura, ámbito por sede y auditoría. No implementado. |
| Antes de publicar tiempos de respuesta | `Alert.acknowledged_at` se usa al reconocer o descartar | Añadir marcas entrega/revisión/despacho/llegada/cierre y casos no atendidos; separar tiempos del sistema y de respuesta humana. |
| Antes de publicar precisión | 0 correcciones de 25 lecturas; CSV con 6/44 aciertos globales y 6/6 entre detectadas | Requiere verdad de referencia, positivos/negativos, omisiones e intervalos; no usar «100 % de precisión» sin denominador. |
| Antes de prometer escala | `docs/evidencias/README.md`, ensayos cortos con diferentes detectores | Medir carga real sostenida; 8–16 cámaras por nodo es planeación, no capacidad certificada; diagnóstico GIL pendiente de perfilado. |
| Antes de presentar cumplimiento UAEMéx | `docs/privacidad.md` formula obligaciones solo bajo LFPDPPP y generaliza consentimiento | Adaptar a responsable público y tratamiento concreto con revisión competente. El aviso de privacidad de otro servicio no autoriza GOSS IP. |
| Antes de usar datos en jurado | Fotos/videos/referencias pueden identificar aun sin embeddings persistentes | No afirmar «fuera de lista no se trata»; minimizar, informar y usar evidencia autorizada/desidentificada. |

Los hallazgos son observaciones verificadas de los archivos leídos, no un certificado de seguridad o auditoría jurídica completa. No se modificaron los documentos originales para evitar pisar la reorganización en curso.

## Cambios concurrentes observados: canalización y ficha

En `df6112a` se añadió el registro manual de canalizaciones y descarga de ficha ZIP. Es un avance para documentar atención y entregar evidencia; no significa que el sistema contacte automáticamente al 911, C5, Fiscalía o Protección Universitaria. La marca `ts` registra la captura del operador, no confirma recepción externa ni llegada de apoyo.

Revisión estática puntual de `api/ficha_evidencia.py` y `api/routers/alerts.py`:

- **Corregir antes de presentar la ficha como evidencia verificada:** el pie dice siempre «la verificó un operador humano», pero la descarga admite alertas sin atender y no exige un resultado de verificación. Usar una leyenda condicionada a una revisión documentada; de otro modo, indicar «pendiente de verificación humana». Reconocer una alerta tampoco acredita que la coincidencia sea correcta.
- `SHA256SUMS.txt` contiene las huellas de foto y clip; **no incluye `ficha.html`**. Si se promete integridad de todo el paquete, incluir la ficha terminada en el manifiesto y conservar una referencia confiable. Las huellas por sí solas no acreditan autenticidad de origen ni una cadena de custodia completa.
- Cuando faltan ambos medios, la ficha afirma que ya no están en disco «(retención)». La ausencia también puede deberse a que nunca hubo captura, una ruta inválida u otro error. Describir «no disponible» salvo que la causa de eliminación esté documentada.

Estos puntos quedan pendientes de integrar; no se reescribió la implementación ni se ejecutaron los endpoints nuevos. La corrección de CI tiene un alcance independiente y acotado.

También se leyó `docs/contexto-uaemex.md`, añadido en `6bb9501`. Sus fuentes periodísticas nuevas no quedaron verificadas individualmente en esta revisión. Antes de usarlo en la defensa: formular «no se encontró una estadística pública por plantel» en lugar de afirmar su inexistencia; presentar «acorta el tiempo» como objetivo hasta medirlo; y señalar que los umbrales de merodeo de 30/45 segundos son parámetros de ensayo, no valores validados por las noticias citadas. Una noticia de 2023 no determina por sí sola las zonas ni horarios de riesgo actuales. Las cifras municipales y de percepción no son tasas de incidentes universitarios.

## Próximo cambio de producto sugerido

Priorizar un **modo de demostración aislado** con señalización visible y registros temporales, y el ciclo de autorización de referencias. Para medir utilidad, priorizar resultado de verificación (`confirmado`, `falso_aviso`, `indeterminado`, `duplicado`, `ensayo`) y tiempos de atención separados. Mantener ese cambio en una tarea de implementación coordinada para no interferir con trabajo simultáneo.

Criterios futuros de aceptación: un registro no aprobado/expirado no alerta; no hay altas automáticas por inferencia; quedan registradas autorizaciones y cambios; puede demostrarse baja y tratamiento posterior; datos de ensayo no contaminan indicadores operativos; etiquetas distinguen sospecha técnica de hecho verificado.

## Archivos nuevos de esta revisión

Toda la investigación está en `docs/impacto-uaemex/`. [Resumen principal](README.md), [fuentes](fuentes.md), política, protocolo, script reproducible y gráficas. Las presentaciones y entregables no se reescribieron en esta revisión.
