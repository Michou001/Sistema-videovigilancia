# Matriz de pruebas funcionales

Estado al 6 de octubre de 2026, 22:00 (inicio del Bootcamp). Cada fila dice
**cómo** se probó. La columna "Cámara real" se llena en el ensayo con la
Hikvision del stand.

Leyenda: ✅ probado y funciona · ⏳ pendiente · ❌ falla.

| # | Prueba | Automática | En este equipo | Cámara real | Cómo / evidencia |
|---|---|---|---|---|---|
| 1 | Buscar dispositivos en la red | ✅ | ✅ | ⏳ | Red de la casa: el router como "otro equipo" y una PC con Windows (WSD) descartada como cámara. La cámara no estaba encendida en la red. |
| 2 | Diagnosticar por ISAPI y confirmar streams por RTSP | ✅ | — | ⏳ | Cámara simulada con Digest (`tests/test_catalogo.py`): identificada, streams 101/102, vista previa. |
| 3 | Contraseña mala: un solo intento | ✅ | — | — | La cámara simulada cuenta 1 intento fallido (antes eran 6). |
| 4 | Recomendación de instalación | ✅ | ✅ | ⏳ | Coincide con la tabla de alcance medida (4.1/6.0/10.6 m y 10.2/15.1/26.6 m). |
| 5 | Alta manual con video de demostración | ✅ | ✅ | — | Probado en el navegador: diagnóstico, recomendación y alta; `.env` con `SOURCE_LOOP` y sin contraseñas heredadas. |
| 6 | Alta de cámara de red | ✅ | — | ⏳ | Prueba de API; la contraseña con `@` y `#` se codifica en la URL. |
| 7 | Iniciar worker desde el dashboard | — | ✅ | ⏳ | El equipo inició `cam-01` y `cam-02` desde el apartado Cámaras (bitácora 21:06). Arrancaron; `cam-01` reportó "no se pudo abrir la fuente" porque la cámara no estaba en la red. |
| 8 | Cámara que cambió de IP | ✅ | — | ⏳ | Se reconoce por serie; se propone el mismo identificador. |
| 9 | Placa → OCR → lista negra → alerta → foto → clip | ✅ | — | ⏳ | `test_api`, `test_clips`, `test_placas_api`. Historial real: 12 coincidencias con la lista negra (ver resumen). |
| 10 | Zona con horario (intrusión) | ✅ | — | ⏳ | `test_zonas` (19 pruebas). |
| 11 | Cámara caída y recuperación | ✅ | — | ✅ | Registro real: `cam-01` recuperada en 30 s. |
| 12 | Video como cámara (plan B) | ✅ | ✅ | — | 4 videos en bucle y a velocidad real durante la medición de rendimiento, sin cortes. |
| 13 | Rendimiento y capacidad | — | ✅ | ⏳ | [README de evidencias](README.md): 1 cámara ~17 fps; 4 procesos 18 fps. |
| 14 | Respaldo | — | ✅ | — | `tools/respaldo.py`: la base de datos copiada tiene los mismos conteos y pasa `integrity_check`. |
| 15 | Restaurar desde el respaldo | — | ✅ | — | 7 oct 00:16, desde la copia en OneDrive (`--nube`, sin `.env` ni secretos): huellas SHA-256 del manifiesto correctas, `git clone repo.bundle` en el commit 343f9d7, base con `integrity_check` ok y los mismos conteos que la original (236 eventos, 24 alertas, 65 de bitácora); `test_api` y `test_rostros_movimiento` pasan sobre el código restaurado. Tiempo: 2 s más la instalación. |
| 16 | Placa de prueba impresa | — | ✅ | ⏳ | OCR siempre correcto cuando se detecta; el detector la acepta en ~50 % de escenas generadas. |
| 17 | Las pruebas no tocan la evidencia real | ✅ | ✅ | — | `test_retencion_huerfanas`; `data/` sin cambios después de correr las 269 pruebas. |

## Para llenar en el ensayo (cámara real)

| Medición | Resultado | Hora |
|---|---|---|
| Placa de la demo leída frente a la cámara (distancia, ángulo) | | |
| Tiempo desde que la placa sale de cuadro hasta la alerta en pantalla | | |
| Intrusión en la zona del stand: tiempo hasta el aviso | | |
| Cable desconectado: tiempo hasta el aviso de cámara caída | | |
| Cable reconectado: tiempo hasta "cámara recuperada" | | |
| `python tools/metricas.py --muestrear 300` con la cámara real | | |
