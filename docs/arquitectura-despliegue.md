# Arquitectura de despliegue

Criterios para llevar GOSS IP de un prototipo de laboratorio a una instalación
institucional: escuelas, edificios públicos u otros conjuntos de edificios con
cámaras IP y personal de monitoreo. Los ejemplos usan como **caso de estudio**
una facultad o campus de la UAEMéx; la arquitectura no depende de él.

> Las alturas, lentes, rutas de cableado, VLAN, cuartos de telecomunicaciones y puntos exactos de cámara deben validarse mediante levantamiento físico con el área de TI y la de seguridad de la institución (en el caso de estudio, Infraestructura/TIC y Seguridad Institucional de la UAEMéx). Este documento fija criterios de ingeniería y no supone nada sobre una red interna concreta.

## 1. Objetivo

La plataforma debe poder leer placas en accesos vehiculares; detectar personas, vehículos, caídas, posturas y reglas por zona; detectar pérdida de señal o sabotaje; conservar evidencia y clips; enviar alertas; crecer de un piloto de 5–8 cámaras a decenas de cámaras; mantener cámaras y evidencia aisladas de la red de usuarios; y reducir ancho de banda y cómputo donde no aportan valor.

La regla base es: **no usar una sola cámara para resolver todos los problemas**. Una cámara LPR debe ver el carril; una panorámica debe cubrir contexto; una peatonal debe priorizar cuerpo y rostro.

## 2. Topología recomendada

### 2.1 Piloto de una facultad

~~~text
                         Red institucional
                                |
                         Router / firewall
                                |
                         Switch principal
                                |
               +----------------+----------------+
               |                                 |
         Switch PoE A                       Servidor GOSS
        /   /   |   \                      GPU + API + BD
      C1  C2   C3   C4                           |
                                                  +-- PostgreSQL
         Switch PoE B                             +-- Redis
        /   |   \                                 +-- go2rtc
      C5   C6   C7
~~~

Para el piloto, las cámaras pueden compartir una VLAN de videovigilancia y el servidor una VLAN de servidores. No hace falta un router por edificio.

### 2.2 Facultad con varios edificios

~~~text
                 Centro / cuarto principal de comunicaciones
                              |
                       Switch de distribución
                    ____/      |       \____
                   /           |            \
                fibra        fibra          fibra
                 |             |              |
            Switch PoE A  Switch PoE B   Switch PoE C
             C1..C4        C5..C8         C9..C12
~~~

Las cámaras se conectan por cobre al switch PoE más cercano. Los edificios o zonas distantes se unen mediante fibra al núcleo de red.

### 2.3 Varios campus o instalaciones separadas

No se propone una LAN física gigante. Cada sede puede tener su propio nodo de borde y enlazarse a una plataforma central mediante red institucional o VPN privada.

~~~text
Campus A ----\
Campus B -----+---- red institucional / VPN ---- plataforma central
Campus C ----/                                 API + BD + auditoría
      |
      +-- worker GPU local
      +-- cámaras locales
~~~

En sedes separadas, el video crudo no tiene que cruzar todo el tiempo el enlace WAN. La IA puede procesar cerca de las cámaras y centralizar eventos, alertas, clips y administración. El vivo se consulta bajo demanda.

## 3. Red lógica

### 3.1 Segmentación mínima

| Segmento | Ejemplo | Uso |
|---|---|---|
| VLAN 50 — Cámaras | 10.50.0.0/24 | RTSP/ISAPI de cámaras |
| VLAN 60 — Servidores | 10.60.0.0/24 | API, worker, BD, Redis, go2rtc |
| VLAN 70 — Operadores | 10.70.0.0/24 | PCs del centro de monitoreo |
| Red usuarios | institucional | Sin acceso directo a cámaras |

Reglas recomendadas:

1. worker/go2rtc hacia cámaras: solo RTSP, HTTPS/ISAPI y servicios necesarios;
2. operadores hacia Caddy/API: HTTPS;
3. usuarios generales hacia cámaras: denegado;
4. cámaras hacia Internet: denegado salvo excepción documentada;
5. PostgreSQL y Redis: solo desde la red de servidores;
6. API interna de go2rtc: no publicar directamente.

### 3.2 Direcciones e inventario

Para un piloto es válido un /24:

~~~text
Servidor GOSS      10.50.0.10
Switch PoE A       10.50.0.20
Switch PoE B       10.50.0.21
cam-veh-entrada    10.50.0.101
cam-veh-salida     10.50.0.102
cam-peat-entrada   10.50.0.111
cam-patio-01       10.50.0.121
~~~

En producción se recomienda DHCP con reservas o IP fija documentada. Convención de nombres:

~~~text
<campus>-<edificio>-<funcion>-<numero>

cu-ing-veh-entrada-01
cu-ing-peat-acceso-01
cu-ing-patio-01
~~~

Usar el mismo identificador como CAMERA_ID cuando sea posible.

## 4. Medios físicos y alcance

### 4.1 Cobre Ethernet

Un tramo Ethernet de cobre debe mantenerse dentro del límite de canal de 100 m. Para diseño conviene dejar margen y no planear justo al límite.

Si una cámara está más lejos: mover el switch PoE a un punto intermedio protegido o usar fibra hasta un switch PoE cercano.

**No se recomienda un amplificador o repetidor Wi‑Fi doméstico para videovigilancia 24/7.**

### 4.2 Fibra

Usar fibra para enlaces entre edificios, estacionamientos alejados, casetas, trayectos que exceden cobre o zonas con interferencia/diferencias de potencial.

~~~text
Cámara -- Cat6 --> Switch PoE local -- fibra --> Switch principal --> servidor
~~~

### 4.3 Wi‑Fi

Solo como excepción o prototipo temporal. Si es inevitable, usar AP administrado, cobertura medida y capacidad calculada. No depender de extensores domésticos para cámaras críticas.

## 5. PoE y switches

Las cámaras deben alimentarse preferentemente por PoE. Cada switch se dimensiona por número de puertos, estándar PoE, presupuesto total de potencia, uplink, VLAN, condiciones del gabinete y reserva de 20–30 %.

Ejemplo: 8 cámaras de 9 W consumen 72 W nominales. No conviene un switch con 75 W de presupuesto: hay que dejar margen para arranque, IR y crecimiento.

Para puntos críticos conviene UPS en switches y servidor.

## 6. Ubicación y propósito de cámaras

Estas son **alturas iniciales de diseño**, no valores universales. Deben ajustarse a lente, distancia, iluminación y escena.

| Punto | Altura inicial | Objetivo | Observación |
|---|---:|---|---|
| Acceso vehicular LPR | 1.5–2.5 m | placa | cámara dedicada al carril |
| Acceso peatonal | 2.5–3.0 m | persona/rostro/postura | evitar vista demasiado cenital |
| Pasillo / corredor | 2.7–3.5 m | seguimiento, zonas | cubrir dirección de circulación |
| Explanada / patio | 3–4.5 m | contexto, pose, intrusión | validar tamaño mínimo de persona |
| Estacionamiento panorámico | 4–6 m | contexto y merodeo | no usar como LPR principal |
| Zona restringida | 2.5–3.5 m | intrusión/cruce | priorizar punto de acceso |

### Acceso vehicular

Usar una cámara dedicada por sentido cuando la trazabilidad lo requiera. Encuadre estrecho del carril, iluminación estable, exposición suficiente y ángulo bajo respecto de la placa. El proyecto ofrece tools/calibrar_distancia.py; antes de fijar una cámara se debe comprobar el tamaño objetivo de placa con margen.

### Acceso peatonal

Ubicar la cámara donde la persona necesariamente atraviese una zona útil. Una altura excesiva mejora cobertura pero empeora pose y rostro. En accesos muy anchos, dos cámaras pueden ser mejores que una ultra gran angular.

### Patios y zonas comunes

Adecuados para intrusión fuera de horario, caída/postura, movimiento anómalo como señal de revisión, conteo, merodeo y cruces de línea. La IA apoya al operador; no presenta posible agresión como hecho confirmado.

### Áreas restringidas

Laboratorios, cuartos de servidores, almacenes y mantenimiento son buenos candidatos para reglas geométricas y horario, que son más explicables que una clasificación genérica de conducta sospechosa.

## 7. Piloto de una facultad

Un piloto de **7 cámaras** permite demostrar casi todo el sistema:

| ID | Ubicación funcional | Módulo principal |
|---|---|---|
| C1 | entrada vehicular | placa + lista negra |
| C2 | salida vehicular | placa + trazabilidad |
| C3 | acceso peatonal | personas + eventos |
| C4 | pasillo principal | pose + movimiento |
| C5 | explanada | zonas + merodeo |
| C6 | acceso restringido | intrusión + horario |
| C7 | estacionamiento panorámico | contexto + cámara caída |

No prescribe puntos físicos concretos de UAEMéx. El levantamiento debe ubicar esos roles en el mapa real de la facultad seleccionada.

## 8. Ancho de banda

El cuello de botella de red se calcula antes del despliegue.

~~~text
BW_total ~= suma de bitrates de streams activos
~~~

Ejemplo: 8 cámaras con substream de analítica de 2 Mb/s = 16 Mb/s. Si dos main streams de 6 Mb/s se consultan simultáneamente por WebRTC, total aproximado 28 Mb/s más overhead.

Para 40 cámaras a 4 Mb/s continuos:

~~~text
40 * 4 = 160 Mb/s
~~~

Un enlace gigabit aún tiene margen, pero se deben considerar uplinks, visualización simultánea y crecimiento.

Principios: usar substream para analítica cuando conserve el detalle necesario; mantener resolución suficiente en LPR; usar WebRTC bajo demanda; no reenviar todos los main streams a todos los operadores.

## 9. Cómputo central vs edge

En el piloto, un servidor GPU puede procesar todas las cámaras en un proceso y compartir modelos.

En campus grande, dividir por nodos edge cuando la GPU llegue a su límite, haya edificios remotos, se quiera evitar transportar RTSP por enlaces de distribución o se necesite limitar el dominio de fallo.

~~~text
Nodo edge A: edificio A, 8–16 cámaras
Nodo edge B: edificio B, 8–16 cámaras
Nodo edge C: estacionamiento/LPR
                   |
                   +--> API/BD central
~~~

El rango 8–16 es solo una unidad de planeación, no capacidad garantizada. **No fijar un número universal de cámaras por GPU.** Medir con --diagnostico, reporte del worker y tools/optimizar_modelos.py.

## 10. Evitar cuellos de botella

### Red
- no encadenar switches sin plan;
- no depender de Wi‑Fi repetido;
- uplinks con capacidad agregada;
- VLAN exclusiva;
- medir pérdida, jitter y reconexiones;
- preferir H.264 compatible con WebRTC.

### GPU
- varias cámaras por proceso para compartir modelos;
- limitar INFER_FPS a lo necesario;
- FP16/TensorRT solo después de validar;
- no activar modelos sin valor operativo;
- armas permanece desactivado hasta tener un modelo validado.

### Base de datos
- PostgreSQL para varias cámaras;
- Redis con varios procesos de API;
- purga y retención activas;
- respaldo de base y evidencia;
- almacenamiento dimensionado para clips.

### Operación
- no mostrar decenas de streams a resolución completa de forma permanente;
- usar alertas y mapa para dirigir atención;
- clips para revisión;
- vivo bajo demanda.

## 11. Disponibilidad y fallos

| Falla | Respuesta esperada |
|---|---|
| cámara pierde red | reconexión + alerta de cámara caída |
| API no responde | worker guarda eventos en spool y reenvía |
| detector falla en un frame | los demás continúan |
| una cámara falla | las demás continúan |
| servidor reinicia | Docker/systemd reinicia servicios |
| enlace sede-central cae | edge sigue procesando y reintenta |
| disco se llena | monitorización + política de retención |
| switch pierde energía | UPS en puntos críticos |

Para producción institucional conviene redundancia de uplink y energía en puntos de distribución críticos. El piloto puede operar sin ella, pero esa diferencia debe quedar documentada.

## 12. Seguridad y privacidad

- cámaras aisladas de usuarios;
- contraseñas únicas;
- cuentas de cámara con mínimos privilegios;
- HTTPS;
- secretos fuera de Git;
- acceso por rol y bitácora;
- evidencia autenticada;
- retención mínima necesaria;
- reconocimiento facial solo con fundamento, finalidad y autorización definidos;
- señalización/aviso de privacidad;
- revisión jurídica antes de operar biometría institucional.

Para concurso conviene presentar como base placas, zonas, pose, cámaras caídas y alertas. Rostros debe mostrarse como módulo controlado, no vigilancia indiscriminada.

## 13. Despliegue por fases

### Fase A — levantamiento
Plano, accesos, distancias, iluminación, cuartos de telecomunicaciones, rutas Cat6/fibra, switches, energía/UPS, capacidad de red y requisitos de privacidad.

### Fase B — piloto
5–8 cámaras, una facultad, VLAN, servidor GPU, PostgreSQL + Redis, reglas, alertas y métricas.

### Fase C — validación
Medir disponibilidad de stream, reconexiones, FPS, latencia de alerta, precisión de placa, falsos positivos, tiempo de atención y almacenamiento.

### Fase D — expansión
Añadir edificios por bloques solo después de aprobar métricas.

## 14. Ficha obligatoria por cámara

| Campo | Ejemplo |
|---|---|
| CAMERA_ID | cu-ing-veh-entrada-01 |
| función | LPR |
| altura | 2.0 m |
| distancia objetivo | 7 m |
| lente | definida tras prueba |
| resolución/FPS | medidos |
| bitrate | medido |
| switch/puerto | SW-A / Gi1/0/4 |
| VLAN/IP | 50 / 10.50.0.101 |
| longitud Cat6 | 42 m |
| PoE | 9 W medidos |
| UPS | sí/no |
| visión nocturna | aprobada/no |
| placa mínima en px | aprobada/no |
| privacidad | revisada |

Esto elimina instalaciones a ojo y vuelve repetible el despliegue.

## 15. Relación con el software actual

La arquitectura ya está soportada: edge.worker acepta varias cámaras; SOURCE desacopla ubicación física del código; CAMERA_ID identifica puntos; el mapa guarda ubicación; las zonas son por cámara; Heartbeat detecta cámaras/worker caídos; spool evita perder eventos; go2rtc entrega vivo sin recodificar; PostgreSQL/Redis escalan la API; Docker ofrece despliegue reproducible.

Por tanto, pasar a una facultad real exige principalmente **ingeniería de red, instalación, calibración y validación**, no reescribir la plataforma.

## 16. Mensaje para InnovaTICs

> Una capa de analítica y respuesta sobre infraestructura IP, con procesamiento local, reglas explicables, aislamiento de red y evidencia auditable. Se instala por puntos de riesgo, se mide y después se escala por campus.

