# Matriz de pruebas funcionales

Estado al 7 de octubre de 2026, 01:30 (inicio del Bootcamp). Cada fila dice
**cómo** se probó. La columna "Cámara real" usa las dos Hikvision del stand
(192.168.100.64 y .65, red propia por cable con switch PoE).

Leyenda: ✅ probado y funciona · ⏳ pendiente · ❌ falla.

| # | Prueba | Automática | En este equipo | Cámara real | Cómo / evidencia |
|---|---|---|---|---|---|
| 1 | Buscar dispositivos en la red | ✅ | ✅ | ✅ | 6 oct: encontró las dos Hikvision en la red del stand (192.168.100.0/24). Antes, en la red de la casa: el router como "otro equipo" y una PC con Windows (WSD) descartada como cámara. |
| 2 | Diagnosticar por ISAPI y confirmar streams por RTSP | ✅ | — | ✅ | 6 oct, cámara .64: ISAPI devolvió modelo DS-2CD1063G2-LIU, serie y streams (101: 3200×1800, 102: 1280×720, H.265 a 20 fps). RTSP DESCRIBE con Digest respondió 200 tras corregir que el reto se mandaba en otra conexión. Cámara simulada en `tests/test_catalogo.py`. |
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
| 13 | Rendimiento y capacidad | — | ✅ | ✅ | [README de evidencias](README.md): 1 cámara ~17 fps; 4 procesos 18 fps. Con las dos cámaras reales (7 oct 00:31, placas y rostros en cada una, un proceso por cámara): 7.4 y 7.7 cuadros analizados por segundo, placas 38 ms y rostros 15–19 ms por cuadro; GPU 38 % y 1.6 de 6 GB; CPU del equipo 37 % (muestra de 5 s). Falta la medición larga con `metricas.py --muestrear 300`. |
| 14 | Respaldo | — | ✅ | — | `tools/respaldo.py`: la base de datos copiada tiene los mismos conteos y pasa `integrity_check`. |
| 15 | Restaurar desde el respaldo | — | ✅ | — | 7 oct 00:16, desde la copia en OneDrive (`--nube`, sin `.env` ni secretos): huellas SHA-256 del manifiesto correctas, `git clone repo.bundle` en el commit 343f9d7, base con `integrity_check` ok y los mismos conteos que la original (236 eventos, 24 alertas, 65 de bitácora); `test_api` y `test_rostros_movimiento` pasan sobre el código restaurado. Tiempo: 2 s más la instalación. |
| 16 | Placa de prueba impresa | — | ✅ | ⏳ | OCR siempre correcto cuando se detecta. Detector sobre 44 escenas generadas: 10 con `PLATE_CONF=0.45` (6 oct) y 6 con `PLATE_CONF=0.60` (7 oct, [CSV](validacion_placa_ZTP482A_umbral060.csv)). Para la demo: placas reales impresas; esta queda de respaldo. |
| 17 | Las pruebas no tocan la evidencia real | ✅ | ✅ | — | `test_retencion_huerfanas`; `data/` sin cambios después de correr las 277 pruebas. |
| 18 | Rostro de frente → evento en ~1 s, con la persona en cuadro | ✅ | — | ⏳ | `test_rostros_movimiento`: perfil al entrar y luego de frente → un solo evento con la vista frontal; quien nunca da la cara se reporta al salir. Medir el tiempo real en el ensayo. |
| 19 | Foto HD de perfil no reemplaza la frontal | ✅ | — | ⏳ | `test_foto_hd_de_perfil_no_reemplaza_la_frontal`. Visto con la cámara .65 el 6 oct: antes guardaba la foto de perfil. |
| 20 | Revisión de placa (confirmar / no es placa) | ✅ | ⏳ | — | `test_revision_excluye_negativo_del_ocr_y_conserva_evidencia`: la negativa sale del dataset de OCR y la foto se conserva; queda en la bitácora. |
| 21 | Aviso al celular por Telegram | ✅ | ✅ | ⏳ | 7 oct 00:05: Telegram aceptó el mensaje de prueba para el chat del equipo (`{'telegram': 'ok'}`). Falta ver una alerta real de lista negra con foto en el celular. |
| 22 | Video en vivo a 20 fps independiente de la IA | ✅ | ✅ | ✅ | `test_video_independiente`: el video avanza sin inferencia y nunca repite un cuadro congelado. Visto en el dashboard con las dos cámaras. |

## Para llenar en el ensayo (cámara real)

| Medición | Resultado | Hora |
|---|---|---|
| Placa de la demo leída frente a la cámara (distancia, ángulo) | | |
| Tiempo desde que la placa sale de cuadro hasta la alerta en pantalla | | |
| Intrusión en la zona del stand: tiempo hasta el aviso | | |
| Cable desconectado: tiempo hasta el aviso de cámara caída | | |
| Cable reconectado: tiempo hasta "cámara recuperada" | | |
| `python tools/metricas.py --muestrear 300` con la cámara real | | |
