# Delimitación: lista de seguimiento autorizada

Propuesta funcional para el equipo, **7 de octubre de 2026**. No constituye autorización de la UAEMéx ni afirma que los controles propuestos ya estén implementados.

## 1. Qué hace y qué significa

Nombre recomendado ante usuarios: **Lista de seguimiento autorizada**. En la demostración: **Lista de prueba**. Los identificadores internos `blacklist_*` pueden conservarse por compatibilidad hasta planear un cambio.

La lista contiene referencias que un responsable ha autorizado comparar para una finalidad concreta y durante un plazo. Cuando el sistema encuentra una posible coincidencia, muestra un aviso con cámara, fecha, lectura, referencia, motivo y evidencia disponible. Un operador revisa y sigue el protocolo aplicable.

**No es un catálogo de delincuentes.** La IA no decide quién entra. Una placa identifica un registro vehicular, no a quien conduce; puede estar mal leída, clonada o desactualizada. Un parecido facial no prueba identidad. «Crítica» es una prioridad operativa, no culpabilidad ni certeza del 100 %.

Flujo propuesto: `solicitud documentada → revisión competente → aprobación → registro temporal → posible coincidencia → verificación humana → actuación autorizada → cierre y auditoría → revisión/baja`.

## 2. Quién puede estar en la lista

| Caso | Admisión propuesta | Condiciones |
|---|---|---|
| Placa/objetivo de demostración | Sí, solo lista de prueba | Material del equipo, sin asociarlo a hechos reales; motivo «ensayo técnico», fin de sesión y datos separados |
| Voluntario adulto para demo facial | Solo si se decide demostrar biometría | Participación libre, autorización informada y específica para la demo, identificación como participante y procedimiento de retiro/borrado; no captar visitantes ajenos |
| Vehículo con reporte verificable de robo o búsqueda | Potencial caso institucional, no habilitado hoy | Solicitud verificada y vigente, competencia/base jurídica confirmadas y protocolo acordado con autoridad competente; ninguna conexión a una base oficial se presume |
| Referencia cubierta por una restricción formal de acceso | Solo tras evaluación institucional | Acto/documento válido, alcance, vigencia y revisión; preferir control de acceso menos intrusivo cuando resuelva el problema |
| Persona reportada como desaparecida o en riesgo | Fuera de la demo; requiere decisión específica | Instrumento y canal competentes, necesidad/proporcionalidad y operación especializada; no incorporarla informalmente «por buena intención» |
| Persona/placa denunciada informalmente por un alumno | No se admite automáticamente | Canalizar el reporte para atención; el equipo técnico no investiga ni convierte una acusación en una orden de vigilancia |
| Toda la matrícula, docentes, visitantes o vehículos | No | La matrícula no es una lista de seguimiento; autorización general de cámaras no prueba permiso de biometría masiva |
| Persona por apariencia, forma de vestir, origen, discapacidad, género, opinión o protesta | No | No son criterios de inclusión ni señales de peligrosidad |
| Deudas, desacuerdos académicos o conflictos personales | No para este sistema de seguridad | No reutilizarlo como control disciplinario o mecanismo de presión |

Son **límites de diseño propuestos**, no una afirmación de que todo caso de la tabla sea jurídicamente admisible en cualquier institución. Ante duda sobre fundamento o autoridad, el registro queda pendiente fuera de la lista activa.

## 3. Quién decide y quién opera

- **Solicitante:** expone el caso y aporta la referencia verificable. No activa registros.
- **Responsable institucional competente:** determina finalidad, legitimidad, alcance y plazo, con asesoría jurídica y de protección de datos cuando corresponda.
- **Administrador técnico:** captura lo aprobado; no sustituye a la autoridad que lo autoriza.
- **Operador:** verifica la coincidencia y avisa al personal competente. No incorpora nuevas personas por observación o intuición.
- **Revisor:** revisa altas, cambios, bajas y actuaciones; atiende errores y solicitudes de derechos mediante el procedimiento institucional.

Para producción se propone separación entre aprobación y captura. Hoy el sistema exige rol administrador para el alta, pero eso **no constituye doble aprobación institucional**. En el ensayo, una persona puede cubrir funciones técnicas, identificando explícitamente que es simulación.

## 4. Datos mínimos por registro

Propuesta: ID de caso; tipo de objetivo; referencia necesaria; finalidad; categoría objetiva del motivo; fuente y fecha de verificación; referencia del documento habilitante; solicitante; aprobador; capturista; sedes/cámaras autorizadas; fecha de inicio, revisión y vencimiento; acción permitida; prioridad justificada; estado; motivo de baja.

Guardar la referencia del expediente y controlar el acceso al soporte. No colocar expedientes completos, acusaciones o documentos de identidad en notas visibles a todos. Las notificaciones deben minimizar identificadores y remitir al sistema autenticado.

**No basta escribir «seguridad» en `reason` o «autorizado» en `legal_basis`.** Un texto libre no valida la autenticidad, competencia ni vigencia del fundamento. La expiración automática debe ser obligatoria o existir una revisión periódica documentada con responsable; no dejar registros indefinidos por comodidad.

## 5. Qué pasa cuando hay coincidencia

1. El aviso dice «Posible coincidencia; verificar» e identifica la referencia y su vigencia. Mantener visibles la lectura original y la forma comparada.
2. El operador revisa calidad de imagen, caracteres, contexto y motivo autorizado. Si no puede confirmar, registra **indeterminado**, no fuerza un acierto.
3. Para placas, la semejanza y corrección OCR siempre exigen revisión. Una placa coincidente no prueba identidad del ocupante ni propiedad actual del vehículo.
4. Para rostros, la similitud no se muestra como probabilidad de identidad. Una puntuación 0.80 no significa «80 % seguro». Evaluar errores de identificación y rechazos en condiciones reales; no inferir confiabilidad de un modelo distinto. [NIST, F13](fuentes.md).
5. Si procede, el operador canaliza al responsable competente mediante el protocolo. El sistema no detiene personas, abre/cierra barreras, sanciona, publica fotografías ni solicita intervención policial automáticamente.
6. Se registran resultado, motivo y tiempos separados. «Descartado» puede significar error, duplicado, ejercicio o referencia vencida; no todo descarte es un falso positivo del detector.

Si un registro no coincide, no se incorpora a la lista automáticamente. Sin embargo, el programa actual puede conservar eventos/fotos normales y efectuar comparación facial: **estar fuera de la lista no equivale a no ser tratado**.

## 6. Vigencia, baja y supresión

Dar de baja debe detener nuevas comparaciones operativas. La eliminación o conservación posterior de referencia, fotos, vectores, clips, eventos, auditoría y copias de respaldo son decisiones distintas, sujetas a finalidad y obligaciones aplicables.

Para la demo se propone vencimiento al finalizar la sesión y eliminación de datos identificables de participantes una vez documentados los resultados agregados, en el plazo comunicado. Si se acuerda conservar alguna evidencia para el jurado, obtener autorización separada y definir plazo; preferir material desidentificado. No ejecutar una limpieza general de `data/`: contiene evidencias previas del equipo.

**Brecha actual:** las bajas de placas y rostros son lógicas (`active=False`). El modelo facial tiene `expires_at`, pero el endpoint de alta revisado no ofrece ese campo. La purga revisada gestiona embeddings de eventos y no acredita un ciclo de eliminación de los registros `BlacklistFace` ni de todas sus fotos de referencia. Se requiere un procedimiento explícito antes de prometer borrado al participante.

Un alta activa actualmente una búsqueda retroactiva de eventos/fotos. En producción ese uso debe justificarse por separado, acotarse en tiempo y ámbito y auditarse. En demo, aislar el historial; de otro modo el alta puede revisar datos anteriores ajenos al ensayo.

## 7. Alcance aprobado por el equipo para preparar la demo

La confirmación recibida es únicamente: dos cámaras del equipo, prueba de lista y ninguna prueba en campus. Se propone iniciar **solo con placas/material de ensayo**; el reconocimiento facial es opcional y no indispensable para demostrar utilidad. Si no se han resuelto autorización de participantes, captación incidental y supresión, omitirlo.

Aunque se usen placas ficticias impresas, no afirmar que es imposible coincidir con una matrícula real: mantener escena cerrada, registros aislados y cero conexión operativa a terceros. Si una impresión se detecta mal, reportarlo; no excluir intentos fallidos ni usar una placa real ajena sin permiso para mejorar la presentación.

Mensaje recomendado en pantalla y exposición:

> DEMOSTRACIÓN. Coincidencias con registros de prueba. Revisión humana obligatoria. No es una lista de delincuentes ni una autorización para operar en campus.

La señalización es una propuesta pendiente de implementar; este documento no modifica la interfaz ni las configuraciones activas.

## 8. Autorizaciones por resolver antes de campus

Confirmar responsable público, finalidad y competencia; documentos/avisos vigentes y su alcance para placas, video y biometría; procedimiento de derechos; necesidad/proporcionalidad; condiciones de acceso y transferencias; retención; incidentes de seguridad; posible evaluación de impacto y requisitos para menores.

El marco de sujetos obligados contiene reglas y supuestos que deben aplicarse al tratamiento concreto; no afirmar que todo se resuelve con un cartel o que el consentimiento es siempre la única base. La propuesta no presume permisos existentes. [Marco verificado, F10–F12](fuentes.md).
