# Fuentes y trazabilidad

Consulta: **7 de octubre de 2026**, fecha local del equipo. Se distinguen publicaciones académicas, documentos institucionales y propuestas propias. No se encontró en lo consultado una evaluación causal de GOSS IP en UAEMéx.

| ID | Fuente primaria o documento original | Qué respalda y qué no |
|---|---|---|
| F1 | [Piza, Welsh, Farrington y Thomas, resumen de los autores (2019)](https://ericpiza.net/wp-content/uploads/2021/12/cctv-review-brief.pdf) · [DOI del artículo](https://doi.org/10.1111/1745-9133.12419) | 13 % agregado, 37 % estacionamientos; 76 evaluaciones con datos. No estima impacto UAEMéx. |
| F2 | [Artículo original completo, copia alojada por Steyning Parish Council](https://www.steyningpc.gov.uk/wp-content/uploads/2026/02/2.-Public-Area-CCTV-and-Crime-Prevention-An-updated-Systematic-Review-and-Meta-Analysis.pdf) | Tabla 1 y resultados; ausencia de efecto agregado significativo en violencia. El artículo es de 2019, aunque la URL diga 2026. |
| F3 | [Mona Tykesson, Effects of CCTV on Fear of Crime: A Systematic Literature Review (2025), CrimRxiv](https://www.crimrxiv.com/pub/odm3b7iv/release/1) | Manuscrito consultado sobre percepción; 15 estudios y resultados heterogéneos. No asumir revisión por pares por estar en un repositorio. |
| F4 | [CU Tianguistenco, Informe Anual de Actividades 2025](https://sdfi.uaemex.mx/docs/InfBasCon/UAPTianguistenco/CU-Tianguistenco-Informe-2025.pdf) | Matrícula 1,736, periodo 2025–2026, p. 16 impresa/hoja 16 del PDF; contexto académico. No línea base delictiva. |
| F5 | [UAEMéx, acuerdo de organización de seguridad (2019)](https://oag.uaemex.mx/normatividad/phpoffice/pdf/acuerdos/rector/2019/03.pdf) | Antecedente de coordinación del C2 y diagnósticos de videovigilancia, p. 4. Confirmar vigencia y responsables actuales. |
| F6 | [UAEMéx, PAI 140, peticiones de infraestructura](https://pai140.uaemex.mx/Consulta/public/Peticiones/3?pagina=1) | Demandas e intervenciones institucionales; el porcentaje de avance de una petición no es reducción delictiva. Página dinámica. |
| F7 | [UAEMéx, PAI 140, peticiones de seguridad](https://pai140.uaemex.mx/Consulta/public/index.php/Peticiones/4) | Contexto de necesidades, incluyendo cruces aledaños a CU; no tasas comparables por campus. Página dinámica. |
| F8 | [INEGI, ENSU](https://www.inegi.org.mx/programas/ensu/) · [Resultados de junio de 2026](https://www.inegi.org.mx/contenidos/programas/ensu/doc/ensu2026_junio_presentacion_ejecutiva.pdf) | Percepción de población de 18 años y más en áreas urbanas; no muestra específica de alumnos. Al consultar, el portal anunciaba siguiente publicación para 23 de octubre de 2026. No se incorporó una cifra particular de Toluca no verificada visualmente. |
| F9 | [SESNSP, datos abiertos de incidencia delictiva](https://www.gob.mx/sesnsp/acciones-y-programas/datos-abiertos-de-incidencia-delictiva) | Fuente para diagnóstico municipal; ficha indexada fechada 18 de septiembre de 2026. La apertura directa falló; no se descargaron ni analizaron bases ni se extrajeron tasas municipales. |
| F10 | [Cámara de Diputados, Ley General de Protección de Datos Personales en Posesión de Sujetos Obligados](https://www.diputados.gob.mx/LeyesBiblio/pdf/LGPDPPSO.pdf) | Texto consultado indica última reforma 14-11-2025. Principios, avisos y evaluación de impacto, arts. 68–73. El régimen y trámite concreto los debe confirmar la instancia competente. |
| F11 | [LEGISTEL, Ley de Protección de Datos Personales en Posesión de Sujetos Obligados del Estado de México y Municipios](https://legislacion.edomex.gob.mx/sites/legislacion.edomex.gob.mx/files/files/pdf/ley/vig/leyvig244.pdf) | Fuente estatal publicada en el portal oficial, consultada como parte del marco; revisar armonización y disposiciones vigentes al autorizar. |
| F12 | [UAEMéx, aviso de privacidad de aplicación SOS](https://appsos.uaemex.mx/aviso/) | Ejemplo institucional que remite al régimen de sujetos obligados. Es aviso de otro tratamiento; no habilita GOSS IP ni prueba autorización de biometría. |
| F13 | [NIST, FRVT Part 3: Demographic Effects (2019)](https://www.nist.gov/publications/face-recognition-vendor-test-part-3-demographic-effects) | Necesidad de evaluar falsos positivos/negativos y variaciones entre poblaciones y condiciones. No valida el modelo, umbral o cámaras del proyecto. |
| F14 | [Piza y colaboradores (2015), ensayo de monitoreo activo y patrullaje dirigido, OJP](https://ojp.gov/library/publications/effects-merging-proactive-cctv-monitoring-directed-police-patrol-randomized) | La intervención evaluada combina tecnología y respuesta; no prueba eficacia de una alerta sin personal. |

## Evidencia local revisada, sin acceder a datos personales

- `docs/evidencias/resumen-operacion-pruebas-sep-oct.md`: agregado de ensayos, no operación universitaria. 25 lecturas, 20 alertas; no se importó la base real.
- `docs/evidencias/matriz-de-pruebas.md` y `README.md`: alcances y pendientes de cámara real.
- `docs/evidencias/validacion_placa_ZTP482A_umbral060.csv`: 11 condiciones × 4 escenas generadas; 6 lecturas correctas de 44 oportunidades. No son 44 vehículos reales ni una muestra independiente de campus.
- `api/models.py`, `api/routers/blacklist.py`, `api/routers/faces.py`, `api/routers/alerts.py`, `api/matching.py`, `api/retention.py`, `api/retroactive.py`, `shared/plates.py`, `.env.example`: revisión estática del comportamiento descrito en la revisión técnica.

Las metas, escenarios, rúbricas y procedimientos de esta carpeta son **propuestas del análisis**. Las gráficas incluyen su tipo de evidencia y los cálculos no utilizan nombres, placas de terceros, rostros ni credenciales.
