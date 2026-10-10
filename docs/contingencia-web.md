# Contingencia web si se apaga la laptop

El objetivo es conservar dashboard, video, deteccion, listas negras, alarmas y
Telegram. Una pagina estatica no puede ejecutar los workers de Python ni leer
las camaras. La contingencia necesita un **servidor independiente con GPU NVIDIA**
y un camino de red hacia ambas camaras que tampoco dependa de la laptop.

## Arquitectura

```text
cam-01 y cam-02 -- red local -- router/gateway VPN siempre encendido
                                      |
                                  VPN privada
                                      |
                         servidor GPU independiente
                         worker + API + PostgreSQL
                         Redis + evidencias + Caddy
                                      |
                            HTTPS para operadores
                                      |
                                  Telegram
```

El gateway VPN puede ser un router compatible o una pequena PC dedicada. No
publiques RTSP (554), la interfaz de las camaras ni la API (8000) directamente
en internet. Si se apaga la laptop, el gateway debe seguir encendido y con
internet; si falla tambien la energia o el enlace de las camaras, el servidor
conserva el historial pero no puede producir detecciones nuevas.

## Preparacion del servidor

1. Contrata un servidor Linux con GPU NVIDIA, almacenamiento persistente y
   direccion publica; instala Docker, Compose, controlador NVIDIA y NVIDIA
   Container Toolkit. Comprueba `nvidia-smi` y el acceso a la GPU desde Docker.
2. Establece la VPN entre el servidor y la red de camaras por un gateway ajeno
   a la laptop. Comprueba que ambas URL RTSP respondan desde el servidor antes
   de arrancar el worker. Las credenciales quedan solo en `camaras/*.env` del
   servidor, nunca en Git.
3. Configura un nombre DNS que apunte al servidor. En
   `docker/compose.env`, establece `SITIO`, secretos propios para PostgreSQL,
   API, JWT y TOTP, `EXIGIR_2FA=admin,operator`, `GO2RTC_URL=` y
   `GO2RTC_BIND_IP=127.0.0.1`. Con estas dos ultimas variables el navegador
   usa el video MJPEG que entrega la API tras comprobar la sesion, y el puerto
   8555 no queda abierto al publico. Caddy obtiene HTTPS para el dominio.
4. Configura `docker/api.env` con las opciones de Telegram y retencion. Los
   tokens del bot y las referencias biometricas son datos sensibles: transferir
   por un canal cifrado y no copiarlos a archivos versionados.
5. Arranca la pila existente:

   ```bash
   docker compose --env-file docker/compose.env config --quiet
   docker compose --env-file docker/compose.env up -d --build
   docker compose --env-file docker/compose.env exec api python tools/init_plataforma.py
   ```

   La pila esta definida en `docker-compose.yml`. Publica 80/443 y mantiene la
   API, PostgreSQL y Redis dentro de la red de Compose. En el firewall del
   servidor permite solo 80/443 y SSH/VPN restringidos.

## Datos y activacion

- La base local actual es SQLite; la pila Docker usa PostgreSQL. **No basta con
  copiar `data/vigilancia.db` al volumen Docker.** Antes de anunciar el sitio
  como respaldo operativo hay que migrar usuarios, listas, referencias, zonas,
  eventos y evidencias, o volver a darlos de alta de forma controlada. El
  repositorio todavia no incluye una migracion SQLite → PostgreSQL.
- Programa copias cifradas, fuera del servidor, de PostgreSQL y del volumen
  `datos`. Verifica una restauracion. Sin sincronizacion continua, la nube
  puede funcionar pero no mostrar los eventos producidos solo en la laptop.
- Para probar la contingencia, abre el sitio por HTTPS desde otra red, valida
  acceso con 2FA, video de ambas camaras, deteccion real de placa y rostro,
  coincidencia autorizada y aviso de Telegram. Luego apaga la laptop y repite.
  Mide cuanto tarda en quedar activa y documenta el resultado.

## Estado actual

El codigo de la pila Docker y el ajuste para no publicar el puerto WebRTC estan
listos. Falta contratar/proporcionar el servidor, nombre DNS y gateway VPN;
despues corresponde cargar secretos y datos, desplegar y ejecutar la prueba
con la laptop apagada. Hasta entonces no existe una URL de contingencia.
