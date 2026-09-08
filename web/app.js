/* Dashboard del sistema de videovigilancia.
 *
 * Sin framework a proposito: son dos pantallas con listas que se actualizan
 * por WebSocket. Un framework aqui seria mas codigo de andamiaje que de
 * aplicacion.
 *
 * La pagina tiene dos apartados y solo uno vive a la vez:
 *
 *   MONITOREO -- las camaras en vivo y, a su lado, las detecciones conforme
 *                entran. Es la pantalla de guardia: se mira, no se opera.
 *   REGISTRO  -- el historico de eventos y las alertas por atender. Es la
 *                pantalla de trabajo: se busca, se resuelve, se descarta.
 *
 * Estan separados porque no se usan a la vez ni por lo mismo, y porque el
 * video en vivo cuesta ancho de banda: al salir de Monitoreo los flujos se
 * cierran y el worker deja de codificar y subir frames.
 */

const API = '';
let token = localStorage.getItem('token') || '';
let usuario = null;
let ws = null;
let reconexion = null;
let intentos = 0;

const $ = (id) => document.getElementById(id);

// Convierte los <i data-lucide> ya presentes en el HTML (login, botones de
// cabecera) apenas carga el script. El resto de la app llama a esto de nuevo
// cada vez que inserta HTML nuevo con iconos -- ver iconoTag() mas abajo.
lucide.createIcons();

/* ------------------------------------------------------------------ */
/* Llamadas a la API                                                    */
/* ------------------------------------------------------------------ */

async function api(ruta, opciones = {}) {
  const r = await fetch(API + ruta, {
    ...opciones,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: 'Bearer ' + token } : {}),
      ...(opciones.headers || {}),
    },
  });
  if (r.status === 401) { salir(); throw new Error('Sesión expirada'); }
  if (!r.ok) {
    let detalle = 'Error ' + r.status;
    try { detalle = (await r.json()).detail || detalle; } catch {}
    throw new Error(detalle);
  }
  return r.status === 204 ? null : r.json();
}

/* ------------------------------------------------------------------ */
/* Sesion                                                              */
/* ------------------------------------------------------------------ */

$('formLogin').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('errorLogin').textContent = '';
  try {
    const s = await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username: $('usuario').value, password: $('password').value }),
    });
    token = s.token;
    localStorage.setItem('token', token);
    usuario = s;
    arrancar();
  } catch (err) {
    $('errorLogin').textContent = err.message;
  }
});

function salir() {
  localStorage.removeItem('token');
  token = '';
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  detenerCamaras();
  clearInterval(temporizadorCamaras);
  $('app').style.display = 'none';
  $('login').style.display = 'grid';
}

async function arrancar() {
  $('login').style.display = 'none';
  $('app').style.display = 'block';
  $('quien').textContent = usuario.display_name + ' · ' + usuario.role;
  if (usuario.role !== 'admin') $('btnListaNegra').style.display = 'none';
  // cargarPlacas() tambien alimenta la metrica "en lista negra" de la cabecera,
  // por eso se llama al arrancar y no solo al abrir el modal.
  await Promise.all([
    cargarEventos(), cargarAlertas(), refrescarStats(),
    cargarPlacas(), cargarDetecciones(),
  ]);
  conectarWs();
  setInterval(refrescarStats, 15000);
  // El apartado se recuerda entre recargas: si el operador dejo el navegador
  // en Registro, un F5 no deberia devolverlo a Monitoreo.
  mostrarVista(localStorage.getItem('vista') || 'monitoreo');
}

/* ------------------------------------------------------------------ */
/* Los dos apartados                                                   */
/* ------------------------------------------------------------------ */

let vistaActual = null;

function mostrarVista(nombre) {
  if (nombre !== 'monitoreo' && nombre !== 'registro') nombre = 'monitoreo';
  vistaActual = nombre;
  localStorage.setItem('vista', nombre);

  $('vistaMonitoreo').classList.toggle('activa', nombre === 'monitoreo');
  $('vistaRegistro').classList.toggle('activa', nombre === 'registro');
  $('navMonitoreo').classList.toggle('activo', nombre === 'monitoreo');
  $('navRegistro').classList.toggle('activo', nombre === 'registro');

  if (nombre === 'monitoreo') {
    iniciarCamaras();
  } else {
    // Los flujos MJPEG se cierran al salir. Dejarlos abiertos mantendria al
    // worker subiendo video a una pantalla que nadie esta viendo.
    detenerCamaras();
  }
}

document.querySelectorAll('nav.apartados button').forEach((b) => {
  b.addEventListener('click', () => mostrarVista(b.dataset.vista));
});

/* Con la pestaña del navegador en segundo plano tampoco hace falta el video.
 * Es el caso mas comun de todos: el dashboard abierto en una pestaña olvidada. */
document.addEventListener('visibilitychange', () => {
  if (!token) return;
  if (document.hidden) detenerCamaras();
  else if (vistaActual === 'monitoreo') iniciarCamaras();
});

/* ------------------------------------------------------------------ */
/* Camaras en vivo                                                     */
/* ------------------------------------------------------------------ */

/* camera_id -> { el, img, cuerpo, transmitiendo } */
const recuadros = new Map();
let camarasRegistradas = [];   // las que existen en la BD (heartbeat)
let temporizadorCamaras = null;

function iniciarCamaras() {
  refrescarCamaras();
  clearInterval(temporizadorCamaras);
  // 5 s: es lo que tarda en aparecer una camara que acaba de arrancar. Mas
  // frecuente no aporta, porque el worker tarda hasta 2 s en enterarse de que
  // hay alguien mirando.
  temporizadorCamaras = setInterval(refrescarCamaras, 5000);
}

function detenerCamaras() {
  clearInterval(temporizadorCamaras);
  temporizadorCamaras = null;
  recuadros.forEach((r) => detenerFlujo(r));
  lucide.createIcons();
}

function detenerFlujo(r) {
  if (!r.transmitiendo) return;
  // removeAttribute y no src='': una cadena vacia hace que el navegador pida
  // la propia pagina como imagen. Quitar el atributo aborta la conexion, que
  // es justo lo que se quiere.
  r.img.removeAttribute('src');
  r.transmitiendo = false;
  r.cuerpo.innerHTML = PLACEHOLDER_PAUSA;
}

const PLACEHOLDER_SIN_SENAL =
  '<div class="sinsenal"><span class="icono"><i data-lucide="camera-off"></i></span>Sin señal</div>';
const PLACEHOLDER_PAUSA =
  '<div class="sinsenal"><span class="icono"><i data-lucide="pause"></i></span>Video en pausa</div>';

async function refrescarCamaras() {
  let enVivo = [];
  try {
    enVivo = await api('/api/preview/camaras');
  } catch {
    return;   // sin red: se conserva lo que ya esta pintado
  }

  const porId = new Map(enVivo.map((c) => [c.camera_id, c]));

  // La lista que se pinta es la union de las camaras registradas en la BD y
  // las que estan mandando video. Asi una camara que se cae no desaparece de
  // la pantalla: se queda en su sitio con "sin señal", que es la informacion
  // que el operador necesita.
  const ids = [...new Set([
    ...camarasRegistradas.map((c) => c.camera_id),
    ...porId.keys(),
  ])].sort();

  for (const id of ids) {
    const vivo = porId.get(id);
    const r = recuadros.get(id) || crearRecuadro(id);
    const meta = camarasRegistradas.find((c) => c.camera_id === id);

    r.el.querySelector('.nom').textContent = (meta && meta.name) || id;
    r.el.querySelector('.punto').className =
      'punto ' + (vivo ? 'on' : (meta && meta.online ? '' : 'off'));
    r.el.querySelector('.fps').textContent = vivo ? `${vivo.fps} fps` : '';

    if (vivo) arrancarFlujo(r, id);
    else if (r.transmitiendo) { detenerFlujo(r); r.cuerpo.innerHTML = PLACEHOLDER_SIN_SENAL; }
  }

  // Camaras que ya no existen ni en la BD ni transmitiendo.
  for (const [id, r] of recuadros) {
    if (!ids.includes(id)) { detenerFlujo(r); r.el.remove(); recuadros.delete(id); }
  }

  const rejilla = $('rejilla');
  rejilla.classList.toggle('una', ids.length === 1);
  $('sinCamaras').style.display = ids.length ? 'none' : 'block';
  $('contadorCamaras').textContent = ids.length
    ? `${enVivo.length}/${ids.length} en vivo` : '';
  lucide.createIcons();
}

function crearRecuadro(id) {
  const el = document.createElement('div');
  el.className = 'camara';
  el.innerHTML = `
    <div class="camara-cab">
      <span class="punto"></span>
      <span class="nom">${escapar(id)}</span>
      <span class="crece"></span>
      <span class="fps"></span>
    </div>
    <div class="camara-video">${PLACEHOLDER_SIN_SENAL}</div>`;
  $('rejilla').append(el);

  const r = { el, img: new Image(), cuerpo: el.querySelector('.camara-video'),
              transmitiendo: false };
  r.img.alt = 'Cámara ' + id;
  // Si el flujo no arranca (token caducado, API reiniciada), se marca como
  // detenido para que el refresco siguiente lo vuelva a intentar en vez de
  // dejar el recuadro con la imagen rota hasta que alguien recargue.
  r.img.onerror = () => {
    if (!r.transmitiendo) return;
    r.transmitiendo = false;
    r.img.removeAttribute('src');
    r.cuerpo.innerHTML = PLACEHOLDER_SIN_SENAL;
  };
  recuadros.set(id, r);
  return r;
}

function arrancarFlujo(r, id) {
  if (r.transmitiendo) return;
  // El token va en la URL porque un <img> no puede mandar cabeceras. Es el
  // mismo compromiso que en /ws/alerts y por el mismo motivo; es un token de
  // sesion con caducidad, nunca el de ingesta del worker.
  const url = `/api/preview/${encodeURIComponent(id)}/live.mjpg`
            + `?token=${encodeURIComponent(token)}&_=${Date.now()}`;
  r.cuerpo.innerHTML = '';
  r.cuerpo.append(r.img);
  // La capa de la ultima deteccion se recrea con el flujo; se vuelve a llenar
  // en cuanto entre un evento de esta camara.
  const capa = document.createElement('div');
  capa.className = 'ultima';
  r.cuerpo.append(capa);
  r.capa = capa;
  r.img.src = url;
  r.transmitiendo = true;
}

/* Marca en el recuadro de la camara lo ultimo que encontro. */
function marcarEnCamara(ev) {
  const r = recuadros.get(ev.camera_id);
  if (!r || !r.capa) return;
  r.capa.innerHTML =
    `<span>${iconoTag(ev.type)}</span>` +
    `<span class="v">${escapar(ev.value)}</span>` +
    `<span class="c">${hora(ev.ts)}</span>`;
  r.capa.classList.add('visible');
  lucide.createIcons();

  clearTimeout(r.temporizadorCapa);
  r.temporizadorCapa = setTimeout(() => r.capa.classList.remove('visible'), 8000);

  if (ev.severity === 'critical') {
    r.el.classList.add('destacada');
    clearTimeout(r.temporizadorAlerta);
    r.temporizadorAlerta = setTimeout(() => r.el.classList.remove('destacada'), 20000);
  }
}

/* ------------------------------------------------------------------ */
/* Detecciones en vivo (columna derecha de Monitoreo)                  */
/* ------------------------------------------------------------------ */

function agregarDeteccion(ev, nueva = false) {
  $('sinDetecciones').style.display = 'none';
  const div = document.createElement('div');
  div.className = 'deteccion ' + (ev.severity || 'info') + (nueva ? ' nueva' : '');

  div.innerHTML = `
    <div class="crece">
      <div class="v">${iconoTag(ev.type)} ${escapar(ev.value)}</div>
      <div class="m">${hora(ev.ts)} · ${escapar(ev.camera_id)}
        ${ev.observations ? '· ' + ev.observations + ' frames' : ''}</div>
    </div>`;

  // La miniatura se construye con el DOM y no interpolada en el HTML: el
  // onerror que hace falta para el hueco cuando la captura ya se purgo no cabe
  // legiblemente dentro de un atributo.
  const hueco = document.createElement('div');
  hueco.className = 'sinfoto';
  hueco.innerHTML = iconoTag(ev.type);

  if (ev.snapshot_path) {
    const img = document.createElement('img');
    img.alt = '';
    img.src = '/media/' + ev.snapshot_path.split('/').pop();
    // La captura pudo borrarla la politica de retencion: se cae al icono en
    // vez de dejar la imagen rota del navegador.
    img.onerror = () => img.replaceWith(hueco);
    div.prepend(img);
  } else {
    div.prepend(hueco);
  }

  const lista = $('listaDetecciones');
  if (nueva) lista.prepend(div); else lista.append(div);
  // 40 y no 200 como en la tabla del registro: esta columna es "lo que acaba
  // de pasar". Para mirar hacia atras esta el apartado de Registro.
  while (lista.children.length > 40) lista.lastChild.remove();
  $('contadorDetecciones').textContent = lista.children.length + ' recientes';
  lucide.createIcons();
}

async function cargarDetecciones() {
  const eventos = await api('/api/events?limite=30');
  $('listaDetecciones').innerHTML = '';
  eventos.forEach((e) => agregarDeteccion(e));
  $('sinDetecciones').style.display = eventos.length ? 'none' : 'block';
}

/* ------------------------------------------------------------------ */
/* WebSocket                                                           */
/* ------------------------------------------------------------------ */

function conectarWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/alerts?token=${encodeURIComponent(token)}`);

  ws.onopen = () => {
    intentos = 0;
    $('puntoWs').className = 'punto on';
    $('textoWs').textContent = 'en vivo';
  };

  ws.onmessage = (e) => {
    const { type, data } = JSON.parse(e.data);
    if (type === 'event') {
      agregarEvento(data, true);
      agregarDeteccion(data, true);
      marcarEnCamara(data);
      refrescarStats();
    }
    else if (type === 'alert') {
      agregarAlerta(data, true);
      if (data.severity === 'critical') mostrarBanner(data);
      else if (data.severity === 'warning') mostrarToast(data);
    }
    else if (type === 'alert_resolved') cargarAlertas();
  };

  ws.onclose = () => {
    $('puntoWs').className = 'punto off';
    // Reintento con espera creciente: si el servidor se reinicia, el navegador
    // no debe martillarlo con una conexion por milisegundo.
    const espera = Math.min(30000, 1000 * Math.pow(2, intentos++));
    $('textoWs').textContent = `reconectando en ${Math.round(espera / 1000)}s`;
    clearTimeout(reconexion);
    reconexion = setTimeout(conectarWs, espera);
  };

  ws.onerror = () => ws.close();
}

/* ------------------------------------------------------------------ */
/* Eventos                                                             */
/* ------------------------------------------------------------------ */

// Nombres de icono de Lucide, no emoji: se ven iguales en cualquier SO y con
// el mismo trazo tecnico que el resto del panel. `iconoTag` los envuelve en
// el <i data-lucide> que lucide.createIcons() convierte a SVG -- hay que
// llamarla despues de insertar cualquier HTML que use esto.
const ICONOS = { plate: 'car', face: 'user-round', weapon: 'shield-alert', anomaly: 'footprints' };
const NOMBRES = { plate: 'Placa', face: 'Rostro', weapon: 'Arma', anomaly: 'Movimiento' };
const iconoTag = (tipo) => `<i data-lucide="${ICONOS[tipo] || 'circle-dot'}"></i>`;

function hora(iso) {
  return new Date(iso).toLocaleTimeString('es-MX', { hour12: false });
}

function agregarEvento(ev, nuevo = false) {
  $('sinEventos').style.display = 'none';
  const tr = document.createElement('tr');
  if (nuevo) tr.className = 'nuevo';
  tr.innerHTML = `
    <td class="mono" style="color:var(--tenue)">${hora(ev.ts)}</td>
    <td>${iconoTag(ev.type)} ${NOMBRES[ev.type] || ev.type}</td>
    <td class="mono"><strong>${escapar(ev.value)}</strong></td>
    <td style="color:var(--tenue)">${escapar(ev.camera_id)}</td>
    <td><span class="etiqueta ${ev.severity}">${ev.severity}</span></td>`;
  const tbody = $('tablaEventos');
  tbody.prepend(tr);
  while (tbody.children.length > 200) tbody.lastChild.remove();
  $('contadorEventos').textContent = tbody.children.length + ' mostrados';
  lucide.createIcons();
}

async function cargarEventos() {
  const eventos = await api('/api/events?limite=100');
  $('tablaEventos').innerHTML = '';
  // Llegan del mas reciente al mas antiguo; se invierte porque agregarEvento
  // hace prepend y asi el orden final vuelve a quedar correcto.
  eventos.reverse().forEach((e) => agregarEvento(e));
  if (eventos.length === 0) $('sinEventos').style.display = 'block';
}

/* ------------------------------------------------------------------ */
/* Alertas                                                             */
/* ------------------------------------------------------------------ */

function agregarAlerta(a, nuevo = false) {
  $('sinAlertas').style.display = 'none';
  const div = document.createElement('div');
  div.className = 'alerta ' + a.severity + (a.status && a.status !== 'new' ? ' resuelta' : '');
  const img = a.snapshot_path
    ? `<img src="/media/${a.snapshot_path.split('/').pop()}" alt="" onerror="this.style.display='none'">`
    : '';
  const acciones = (!a.status || a.status === 'new') && a.id
    ? `<div class="acciones">
         <button onclick="resolver(${a.id},'acknowledge')"><i data-lucide="check"></i>Atendida</button>
         <button class="sec" onclick="resolver(${a.id},'dismiss')"><i data-lucide="x"></i>Falso positivo</button>
       </div>` : '';
  div.innerHTML = `
    ${img}
    <div class="crece">
      <div class="t">${escapar(a.title)}</div>
      <div class="d">${escapar(a.detail || '')}</div>
      <div class="meta">${hora(a.ts || a.created_at)} · ${escapar(a.camera_id)}
        ${a.match_kind && a.match_kind !== 'none' ? '· ' + a.match_kind : ''}
        ${a.status && a.status !== 'new' ? '· ' + a.status : ''}</div>
      ${acciones}
    </div>`;
  const lista = $('listaAlertas');
  if (nuevo) lista.prepend(div); else lista.append(div);
  while (lista.children.length > 60) lista.lastChild.remove();
  lucide.createIcons();
}

async function cargarAlertas() {
  const alertas = await api('/api/alerts?limite=50');
  $('listaAlertas').innerHTML = '';
  alertas.forEach((a) => agregarAlerta(a));
  $('sinAlertas').style.display = alertas.length ? 'none' : 'block';
}

async function resolver(id, accion) {
  const motivo = accion === 'dismiss' ? 'falso positivo' : null;
  await api(`/api/alerts/${id}/resolver`, {
    method: 'POST',
    body: JSON.stringify({ accion, motivo }),
  });
  await Promise.all([cargarAlertas(), refrescarStats()]);
}

function mostrarBanner(a) {
  $('bannerTitulo').textContent = a.title;
  $('bannerDetalle').textContent = a.detail || '';
  // El banner vive dentro de #barra, asi que al mostrarlo empuja la cabecera
  // hacia abajo por si solo. Nada que medir.
  $('banner').style.display = 'flex';
}
function cerrarBanner() { $('banner').style.display = 'none'; }

/* Aviso (warning): se nota aunque el operador este viendo Monitoreo -- que
 * es justo el problema que resolvia esto -- pero no bloquea como el banner
 * critico. Se apila con los demas y se retira solo. */
function mostrarToast(a) {
  const div = document.createElement('div');
  div.className = 'toast';
  div.innerHTML = `
    <span class="ico">${a.type ? iconoTag(a.type) : '<i data-lucide="triangle-alert"></i>'}</span>
    <div class="crece">
      <div class="t">${escapar(a.title)}</div>
      <div class="d">${escapar(a.detail || '')}</div>
    </div>
    <button class="cerrar" aria-label="Cerrar" onclick="this.closest('.toast').remove()">×</button>`;
  $('toasts').appendChild(div);
  lucide.createIcons();
  sonarAviso();

  const TIEMPO_VISIBLE_MS = 8000;
  setTimeout(() => {
    div.classList.add('saliendo');
    setTimeout(() => div.remove(), 250);
  }, TIEMPO_VISIBLE_MS);

  // No se acumulan sin limite: si el operador se ausenta y llegan varios,
  // se apilan los ultimos 4 y se descartan los mas viejos en silencio.
  const pila = $('toasts');
  while (pila.children.length > 4) pila.firstChild.remove();
}

/* Chime generado en el navegador (dos tonos suaves) -- nada de un archivo de
 * audio que cargar. Se crea el AudioContext al primer uso: los navegadores
 * bloquean audio antes de cualquier interaccion del usuario, y para cuando
 * llega la primera alerta el operador ya inicio sesion (eso cuenta como
 * interaccion). Si el navegador lo bloquea de todos modos, se ignora --
 * el toast visual sigue apareciendo igual. */
let _audioCtx = null;
function sonarAviso() {
  try {
    _audioCtx = _audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const ahora = _audioCtx.currentTime;
    [880, 660].forEach((frecuencia, i) => {
      const osc = _audioCtx.createOscillator();
      const gain = _audioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.value = frecuencia;
      gain.gain.setValueAtTime(0.0001, ahora + i * 0.12);
      gain.gain.exponentialRampToValueAtTime(0.12, ahora + i * 0.12 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ahora + i * 0.12 + 0.18);
      osc.connect(gain).connect(_audioCtx.destination);
      osc.start(ahora + i * 0.12);
      osc.stop(ahora + i * 0.12 + 0.2);
    });
  } catch {}
}

/* ------------------------------------------------------------------ */
/* Estadisticas                                                        */
/* ------------------------------------------------------------------ */

async function refrescarStats() {
  try {
    const s = await api('/api/stats');
    $('mEventos').textContent = s.total_eventos;
    $('mAlertas').textContent = s.alertas_nuevas;
    $('mPlacas').textContent = s.eventos_por_tipo.plate || 0;

    // Con el operador en Monitoreo, el globo del otro apartado es lo unico que
    // le dice que hay alertas esperando.
    const globo = $('globoAlertas');
    globo.textContent = s.alertas_nuevas || '';
    globo.hidden = !s.alertas_nuevas;

    camarasRegistradas = s.camaras || [];
    const activas = camarasRegistradas.filter((c) => c.online).length;
    $('estadoCamaras').innerHTML = camarasRegistradas.length
      ? `<span class="punto ${activas ? 'on' : 'off'}"></span>
         <span>${activas}/${camarasRegistradas.length} cámara${camarasRegistradas.length > 1 ? 's' : ''}</span>`
      : '<span class="punto"></span><span>sin cámaras</span>';
  } catch {}
}

/* ------------------------------------------------------------------ */
/* Lista negra                                                         */
/* ------------------------------------------------------------------ */

$('btnListaNegra').addEventListener('click', async () => {
  $('modal').style.display = 'grid';
  await Promise.all([cargarPlacas(), cargarRostros()]);
});
function cerrarModal() { $('modal').style.display = 'none'; }

document.querySelectorAll('.pest').forEach((b) => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.pest').forEach((x) => x.classList.remove('activa'));
    b.classList.add('activa');
    $('tabPlacas').style.display = b.dataset.tab === 'placas' ? 'block' : 'none';
    $('tabRostros').style.display = b.dataset.tab === 'rostros' ? 'block' : 'none';
  });
});

/* --- Rostros --------------------------------------------------------- */

async function cargarRostros() {
  const rostros = await api('/api/blacklist/faces');
  $('listaRostros').innerHTML = rostros.length
    ? rostros.map((r) => `
        <div class="fila">
          <strong>${escapar(r.label)}</strong>
          <span class="crece" style="color:var(--tenue)">${escapar(r.reason)}</span>
          <button class="peligro" style="padding:3px 9px;font-size:12px"
                  onclick="quitarRostro(${r.id})"><i data-lucide="trash-2"></i>Quitar</button>
        </div>`).join('')
    : '<div class="vacio">Ninguna persona registrada.</div>';
  lucide.createIcons();
}

$('formRostro').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('errorRostro').textContent = '';
  const archivo = $('fotoRostro').files[0];
  if (!archivo) { $('errorRostro').textContent = 'Falta la foto'; return; }

  // FormData en vez de JSON: la foto es binaria. Se omite Content-Type a
  // proposito para que el navegador ponga el boundary del multipart.
  const fd = new FormData();
  fd.append('label', $('nombreRostro').value);
  fd.append('reason', $('motivoRostro').value);
  fd.append('legal_basis', $('fundamento').value);
  fd.append('foto', archivo);

  try {
    const r = await fetch('/api/blacklist/faces', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + token },
      body: fd,
    });
    if (!r.ok) throw new Error((await r.json()).detail || 'Error ' + r.status);
    $('formRostro').reset();
    await cargarRostros();
  } catch (err) {
    $('errorRostro').textContent = err.message;
  }
});

async function quitarRostro(id) {
  await api('/api/blacklist/faces/' + id, { method: 'DELETE' });
  await cargarRostros();
}

async function cargarPlacas() {
  const placas = await api('/api/blacklist/plates');
  $('mLista').textContent = placas.length;
  $('listaPlacas').innerHTML = placas.length
    ? placas.map((p) => `
        <div class="fila">
          <strong class="mono">${escapar(p.plate)}</strong>
          <span class="crece" style="color:var(--tenue)">${escapar(p.reason)}</span>
          <button class="peligro" style="padding:3px 9px;font-size:12px"
                  onclick="quitarPlaca(${p.id})"><i data-lucide="trash-2"></i>Quitar</button>
        </div>`).join('')
    : '<div class="vacio">La lista negra está vacía.</div>';
  lucide.createIcons();
}

$('formPlaca').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('errorPlaca').textContent = '';
  try {
    await api('/api/blacklist/plates', {
      method: 'POST',
      body: JSON.stringify({ plate: $('nuevaPlaca').value, reason: $('motivo').value }),
    });
    $('nuevaPlaca').value = '';
    $('motivo').value = '';
    await cargarPlacas();
  } catch (err) {
    $('errorPlaca').textContent = err.message;
  }
});

async function quitarPlaca(id) {
  await api('/api/blacklist/plates/' + id, { method: 'DELETE' });
  await cargarPlacas();
}

/* ------------------------------------------------------------------ */

function escapar(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

/* Sesion persistida: al recargar, se valida el token antes de mostrar nada. */
(async () => {
  if (!token) return;
  try {
    usuario = await api('/api/auth/me');
    await arrancar();
  } catch {
    salir();
  }
})();
