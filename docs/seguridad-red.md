# Seguridad en red: quién puede llegar al sistema y cómo se cierra

> La observación de la asesoría fue: *si hay internet, aunque sea solo para subir
> la lista negra o la ubicación, alguien conectado a la red puede vulnerar el
> sistema*. Es cierta. Este documento dice qué queda expuesto, qué ya está
> cerrado en el código y qué hay que configurar al instalar.

## 1. Qué se expone y a quién

| Pieza | Puerto | Quién puede llegar | Riesgo si se deja abierto |
|---|---|---|---|
| Dashboard / API | 8000 (o 443 con Caddy) | Quien esté en la misma red que el equipo | Adivinar contraseñas, ver video y fotos, cambiar la lista negra |
| Cámaras Hikvision | 554 (RTSP), 80 (web) | Quien esté en la red de las cámaras | Ver el video directo, cambiar la configuración de la cámara |
| Worker → API | 8000 | El propio equipo (127.0.0.1) | Inyectar detecciones falsas si el token de ingesta se filtra |
| Avisos (Telegram) | salida 443 | — (solo sale, nada entra) | Ninguno de entrada; el token del bot va en `.env` |
| go2rtc (video WebRTC) | 8555 | Red local | Solo video y solo con sesión (Caddy pide `/api/auth/verificar`) |

**Subir la lista negra o la ubicación no requiere abrir nada hacia adentro.**
El riesgo aparece cuando el *dashboard* queda escuchando en una red compartida
(el Wi-Fi de la escuela, una IP pública del campus) o cuando se hace
*port forwarding* del router para verlo desde fuera.

## 2. Lo que el sistema ya hace

- **Solo escucha en este equipo por defecto.** `python -m api` usa
  `API_HOST=127.0.0.1`: desde otra computadora no se llega aunque estén en la
  misma red. Abrirlo (`API_HOST=0.0.0.0`) es una decisión explícita, y si se
  hace sin HTTPS el arranque muestra una advertencia grande.
- **HTTPS** con certificado propio (`tools/generar_certificado.py`) o con Caddy
  en Docker (Let's Encrypt o CA interna). Por esa conexión pasan contraseñas,
  códigos de verificación, fotos y video.
- **Verificación en dos pasos (TOTP)** con Google o Microsoft Authenticator.
  Con `EXIGIR_2FA=admin,operator`, una contraseña robada no basta para entrar.
  El secreto se guarda cifrado (AES-GCM) y cada código sirve una sola vez.
- **Límite de intentos**: 5 fallos por IP en 5 minutos, para la contraseña y
  para el código. Todo intento queda en la bitácora con la IP.
- **Roles**: solo un administrador modifica la lista negra, las cámaras, las
  zonas o los usuarios.
- **Token de ingesta** distinto del login para el worker, comparado en tiempo
  constante.
- **Cabeceras de seguridad**: CSP estricta (sin JavaScript en línea), cookies
  `HttpOnly` y `SameSite=Strict`, `frame-ancestors 'none'`, HSTS con HTTPS.
- **Contenedores** sin root, sin capacidades del kernel, sin escalar
  privilegios y con el código de solo lectura (`docker-compose.yml`).
- **CI de seguridad** (`.github/workflows/seguridad.yml`): dependencias con
  pip-audit, secretos en todo el historial con gitleaks y Dockerfiles con
  Trivy, en cada push y cada lunes.

## 3. Configuración al instalar (lista de verificación)

### Equipo del centro de monitoreo

1. **Dejar `API_HOST` vacío o en `127.0.0.1`** si el dashboard solo se ve en
   ese equipo (el caso del stand).
2. Si otros equipos lo deben ver, **HTTPS obligatorio**:

       python tools/generar_certificado.py --ip 192.168.100.10

   y en `.env`: `API_HOST=0.0.0.0`, `SSL_CERTFILE=data/tls/servidor.crt`,
   `SSL_KEYFILE=data/tls/servidor.key`.
3. **Firewall de Windows**: permitir el puerto solo desde la red de monitoreo.
   Lo ejecuta un administrador del equipo en PowerShell *como administrador*:

       New-NetFirewallRule -DisplayName "GOSS IP dashboard" -Direction Inbound `
         -Protocol TCP -LocalPort 8000 -RemoteAddress 192.168.100.0/24 -Action Allow

   y que el perfil de la red del campus sea **Pública**. Ojo: el perfil Público
   solo protege si no hay reglas que lo abran. Cuando Windows pregunta
   "¿Permitir que Python se comunique en estas redes?" y se acepta, crea reglas
   de entrada para Python **también en redes públicas**. Revisarlas:

       Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True |
         Where-Object DisplayName -match 'python' |
         Select-Object DisplayName, Profile, Name

   y desactivar las de perfil `Public` (como administrador):

       Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True |
         Where-Object { $_.DisplayName -match 'python' -and $_.Profile -match 'Public' } |
         Disable-NetFirewallRule
4. **`EXIGIR_2FA=admin,operator`** en `.env`, y que cada usuario dé de alta su
   app la primera vez que entra.
5. Contraseñas de al menos 10 caracteres (el sistema lo exige) y un usuario por
   persona: la bitácora sirve de poco si todos usan "admin".

### Cámaras

6. **Red aparte para las cámaras.** El switch PoE de las cámaras conectado
   solo al equipo de monitoreo (segunda tarjeta de red, como en el stand:
   `192.168.100.x`), o una VLAN sin salida a internet. Las cámaras no
   necesitan internet para que el sistema funcione.
7. **Desactivar Hik-Connect / P2P y UPnP** en cada cámara (web de la cámara →
   Configuración → Red → Avanzado). Hik-Connect publica la cámara en la nube
   del fabricante; UPnP puede abrir puertos del router sin avisar.
8. **Contraseña propia por cámara** y un usuario de solo lectura para el
   worker (Hikvision: Usuario → Operador con permiso de vista en vivo). La
   contraseña de administración de la cámara no debe estar en el `.env`.
9. Firmware al día (Hikvision publica avisos de seguridad para la serie 2CD).

### Acceso desde fuera (subir la lista negra, ver la ubicación)

10. **Nunca hacer *port forwarding*** del puerto 8000 ni del 554 en el router.
11. Para administrar a distancia, una **VPN** (WireGuard o Tailscale): el
    equipo remoto entra a la red como si estuviera ahí, y nada queda expuesto a
    internet. Para un despliegue institucional, Caddy con dominio y HTTPS
    (`docker-compose.yml`) detrás del firewall del campus, con 2FA exigido.
12. La lista negra se carga **desde el dashboard con sesión de administrador**
    (queda en la bitácora quién y cuándo), no copiando archivos a una carpeta
    compartida.

## 4. Qué NO cubre todavía

- **El tráfico RTSP de las cámaras va sin cifrar** dentro de su red: es una
  limitación del protocolo en estas cámaras. Por eso la red de cámaras debe
  estar aislada (puntos 6 y 7).
- La base SQLite y las fotos de evidencia están en el disco del equipo **sin
  cifrar**: activar BitLocker en la laptop protege si se la roban.
- No hay detección de intrusos en la red (IDS); en un despliegue del campus
  le corresponde a la DTIC de la universidad.
