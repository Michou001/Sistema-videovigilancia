# Revisión de las comprobaciones de innovatics-final

Fecha: 7 de octubre de 2026. [PR #1](https://github.com/Michou001/Sistema-videovigilancia/pull/1), head remoto observado `265d993725c9111d04a1990198ee015dd17113d6`. Carpeta local revisada: Escritorio, HEAD inicial `fe0a98b` y cambios de trabajo en curso. Se siguieron creando commits durante la revisión; el último observado fue `6bb9501`. Este diagnóstico corresponde a las ejecuciones remotas enlazadas, no certifica todos los cambios posteriores.

## Actualización: publicación autorizada

Por solicitud del usuario se publicó el commit `34997237c19ac53b79d621f53a39c6fd69ce86fe` en `innovatics-final`, junto con sus siete commits predecesores que todavía no estaban en remoto. La reorganización de carpetas sin commit y los documentos nuevos de investigación permanecen locales.

Antes de publicar se realizó otro respaldo (`respaldos/20261007-1936/`), pasó `ruff check .` y terminó sin fallos `venv\Scripts\python.exe tests/correr_todas.py` (24 archivos; Redis omitido en local). Se comprobó que el SHA remoto coincide con el publicado. Nuevas ejecuciones: [PR](https://github.com/Michou001/Sistema-videovigilancia/actions/runs/37714266627) y [push](https://github.com/Michou001/Sistema-videovigilancia/actions/runs/37714263376).

**Resultado final verificado: ambas ejecuciones completadas con `success`; 12/12 comprobaciones exitosas.** Incluyen Linux y Windows con Python 3.11/3.12, PostgreSQL 16 + Redis 7 y construcción/arranque Docker. No se fusionó el PR. Las secciones siguientes conservan el diagnóstico inicial y sus limitaciones previas a la publicación.

## Qué significa 2/12

Dos comprobaciones exitosas de doce, no dos errores. El workflow se ejecutó por `push` y por `pull_request`: seis jobs por ejecución. En cada una pasó Docker, fallaron cuatro jobs de Python al ejecutar el linter y falló el job PostgreSQL/Redis al ejecutar pruebas.

- [Ejecución del PR](https://github.com/Michou001/Sistema-videovigilancia/actions/runs/37582380900).
- [Ejecución por push](https://github.com/Michou001/Sistema-videovigilancia/actions/runs/37582374469).

La misma rama aparece en «Your branches» y «Active branches» porque son secciones de la interfaz; eso no implica ramas duplicadas. El PR reportó 23 commits frente a `main`; al inicio de la revisión la carpeta local ya tenía cinco commits posteriores al head remoto consultado, además de modificaciones sin commit. Ese número siguió aumentando con el trabajo concurrente.

## Causas verificadas

### Linter: resuelto previamente en local, pendiente en remoto

[Log del linter](https://github.com/Michou001/Sistema-videovigilancia/actions/runs/37582380900/job/112664756966):

- `edge/detectors/plates.py:497`: variable `lecturas` sin usar, F841.
- `tests/test_placas_api.py:261`: `with` y cuerpo en una línea, E701.
- `tests/test_video_independiente.py:55`: `if` y cuerpo en una línea, E701.

El commit local `16af4d0` ya corrige esos tres puntos. Esta revisión no repitió esos cambios. Las líneas anteriores corresponden a la revisión remota fallida.

### PostgreSQL: preparación incompleta de dos pruebas

[Log PostgreSQL/Redis](https://github.com/Michou001/Sistema-videovigilancia/actions/runs/37582380900/job/112664756837): fallan `test_base_de_datos_ajena_no_borra_nada` y `test_huerfanas_sueltas_se_borran`, ambas de `tests/test_retencion_huerfanas.py`.

El helper `_evento_con_foto` insertaba un evento de `cam-h` sin crear la fila de esa cámara. PostgreSQL aplica la clave foránea `events_camera_id_fkey` y rechaza el evento. El error se produce antes de comprobar la purga; no demuestra por sí mismo una falla de la lógica de purga.

**Corrección local:** el helper verifica si existe `cam-h` y, si falta, crea y confirma la cámara antes del evento. No se cambió la lógica de producción ni se desactivó la restricción.

## Validación realizada

- Respaldo previo con `venv\Scripts\python.exe tools/respaldo.py --con-evidencia` en `respaldos/20261007-1925/`, conforme a `AGENTS.md`. El bundle no incluye cambios sin commit, como advierte la herramienta.
- Reproducción antes del cambio con SQLite y `PRAGMA foreign_keys=ON` en cada conexión: **2/4 pasan**, los mismos dos errores de integridad.
- Mismo ensayo después: **4/4 pasan**. La prueba usa su base temporal y las carpetas aisladas de `bd_prueba.py`.
- Linter de los archivos modificados, `ruff check .` del repositorio y `git diff --check` del cambio: correctos en la instantánea comprobada.

No se ejecutó PostgreSQL local ni se subieron commits o reejecutaron jobs remotos. La comprobación decisiva del entorno PostgreSQL queda pendiente de una nueva corrida de CI con la corrección publicada. La captura seguirá reflejando el estado de la versión remota mientras no se actualice.

## Otros avisos observados

El healthcheck PostgreSQL usa `pg_isready` sin usuario, lo que genera mensajes de rol `root` inexistente; los tests sí llegaron a ejecutarse. No es la causa de estos dos fallos. La advertencia de migración de Node en las actions tampoco es el error que detuvo el linter. No se cambió el workflow durante esta revisión.

La descripción del PR afirma que solo cambia documentación, pero ya incluye código y pruebas; debe actualizarse cuando el equipo publique el conjunto final. Mantener separadas la reorganización de carpetas, esta corrección y la investigación de impacto al revisar qué se desea incluir.
