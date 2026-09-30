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
let temporizadorStats = null;

const $ = (id) => document.getElementById(id);

// Si el CDN de iconos no cargo (red de las camaras sin salida a internet), el
// panel sigue funcionando sin iconos. Sin esto, la primera llamada a lucide
// lanzaba un error y el script entero se detenia: ni siquiera el login servia.
if (!window.lucide) window.lucide = { createIcons() {} };

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
  // Un 401 del login es "contrasena incorrecta", no "sesion expirada".
  if (r.status === 401 && token && ruta !== '/api/auth/login') {
    salir();
    throw new Error('Sesión expirada');
  }
  if (!r.ok) {
    let detalle = 'Error ' + r.status;
    try {
      const cuerpo = await r.json();
      // FastAPI devuelve los errores de validacion como lista de objetos.
      detalle = Array.isArray(cuerpo.detail)
        ? cuerpo.detail.map((d) => d.msg).join('; ')
        : (cuerpo.detail || detalle);
    } catch {}
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
  // Todos los temporizadores: sin esto, tras cerrar sesion el reintento del
  // WebSocket y el refresco de estadisticas seguian corriendo, y al volver a
  // entrar se duplicaban.
  clearTimeout(reconexion);
  clearInterval(temporizadorStats);
  temporizadorStats = null;
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
  const esAdmin = usuario.role === 'admin';
  $('btnListaNegra').style.display = esAdmin ? '' : 'none';
  $('btnCamaras').style.display = esAdmin ? '' : 'none';
  // cargarPlacas() tambien alimenta la metrica "en lista negra" de la cabecera,
  // por eso se llama al arrancar y no solo al abrir el modal.
  await Promise.all([
    cargarEventos(), cargarAlertas(), refrescarStats(),
    cargarPlacas(), cargarDetecciones(),
  ]);
  conectarWs();
  clearInterval(temporizadorStats);
  temporizadorStats = setInterval(refrescarStats, 15000);
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
    r.el.querySelector('.fps').textContent = vivo ? `${vivo.fps} fps video` : '';
    // Salud reportada por el latido del worker: fps a los que de verdad esta
    // detectando y cuantas veces tuvo que reconectar con la camara.
    const salud = [];
    if (meta && meta.fps != null) salud.push(`${meta.fps} fps análisis`);
    if (meta && meta.reconexiones) salud.push(`${meta.reconexiones} reconex.`);
    r.el.querySelector('.salud').textContent = salud.join(' · ');

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
      <span class="salud"></span>
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
    `<span class="v">${escapar(valorLegible(ev))}</span>` +
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
      <div class="v">${iconoTag(ev.type)} ${escapar(valorLegible(ev))}</div>
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
    const img = miniatura(ev.snapshot_path,
      `${NOMBRES[ev.type] || ev.type} ${valorLegible(ev)} · ${fechaHora(ev.ts)} · ${ev.camera_id}`);
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
      // Con una busqueda activa, la tabla muestra el resultado de esa busqueda:
      // meterle eventos en vivo que quiza no cumplen el filtro la contradiria.
      if (!filtroActivo()) agregarEvento(data, true);
      agregarDeteccion(data, true);
      marcarEnCamara(data);
      pedirStats();
    }
    else if (type === 'alert') {
      agregarAlerta(data, true);
      pedirStats();
      if (data.severity === 'critical') mostrarBanner(data);
      else if (data.severity === 'warning') mostrarToast(data);
    }
    else if (type === 'alert_resolved') { cargarAlertas(); pedirStats(); }
  };

  ws.onclose = () => {
    if (!token) return;
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

/* El valor tal como lo lee el operador. En placas y armas es el dato mismo;
 * en rostros y movimiento el worker manda una etiqueta interna. */
const VALORES = { movimiento_subito: 'Movimiento súbito', rostro: 'Rostro' };
const valorLegible = (ev) => VALORES[ev.value] || ev.value;

const ETIQUETAS_SEVERIDAD = { critical: 'crítico', warning: 'aviso', info: 'info' };

function hora(iso) {
  return new Date(iso).toLocaleTimeString('es-MX', { hour12: false });
}

/* Hora sola si es de hoy; fecha y hora si no. En el registro conviven eventos
 * de varios dias y "14:02" a secas no dice de cual. */
function fechaHora(iso) {
  const d = new Date(iso);
  const hoy = new Date();
  if (d.toDateString() === hoy.toDateString()) return hora(iso);
  return d.toLocaleDateString('es-MX', { day: '2-digit', month: 'short' }) + ' ' + hora(iso);
}

/* Miniatura de evidencia que se amplia al hacer clic. La foto guardada puede
 * ser la captura HD de 3200 px: a 66 px de ancho el operador no ve nada. */
function miniatura(ruta, descripcion, clase = '') {
  const img = document.createElement('img');
  img.alt = '';
  img.className = ('ampliable ' + clase).trim();
  img.loading = 'lazy';
  img.src = '/media/' + encodeURIComponent(ruta.split('/').pop());
  img.addEventListener('click', (e) => { e.stopPropagation(); abrirVisor(img.src, descripcion); });
  return img;
}

function abrirVisor(src, texto) {
  $('visorImg').src = src;
  $('visorTexto').textContent = texto || '';
  $('visor').style.display = 'grid';
}
function cerrarVisor() {
  $('visor').style.display = 'none';
  $('visorImg').removeAttribute('src');
}
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') cerrarVisor(); });

function agregarEvento(ev, nuevo = false) {
  $('sinEventos').style.display = 'none';
  const tr = document.createElement('tr');
  if (nuevo) tr.className = 'nuevo';
  tr.innerHTML = `
    <td class="mono" style="color:var(--tenue);white-space:nowrap">${fechaHora(ev.ts)}</td>
    <td>${iconoTag(ev.type)} ${NOMBRES[ev.type] || escapar(ev.type)}</td>
    <td class="mono"><strong>${escapar(valorLegible(ev))}</strong></td>
    <td style="color:var(--tenue)">${escapar(ev.camera_id)}</td>
    <td><span class="etiqueta ${escapar(ev.severity)}">${ETIQUETAS_SEVERIDAD[ev.severity] || escapar(ev.severity)}</span></td>
    <td></td>`;
  if (ev.snapshot_path) {
    const img = miniatura(ev.snapshot_path,
      `${NOMBRES[ev.type] || ev.type} ${valorLegible(ev)} · ${fechaHora(ev.ts)} · ${ev.camera_id}`, 'miniatura');
    img.onerror = () => img.remove();
    tr.lastElementChild.append(img);
  }
  const tbody = $('tablaEventos');
  if (nuevo) tbody.prepend(tr); else tbody.append(tr);
  while (tbody.children.length > 300) tbody.lastChild.remove();
  $('contadorEventos').textContent = tbody.children.length + ' mostrados';
  lucide.createIcons();
}

/* ---- Busqueda en el registro ---- */

function filtros() {
  const p = new URLSearchParams();
  const texto = $('fTexto').value.trim();
  if (texto) p.set('q', texto);
  if ($('fTipo').value) p.set('tipo', $('fTipo').value);
  if ($('fSeveridad').value) p.set('severidad', $('fSeveridad').value);
  // Las fechas del formulario son dias LOCALES. Se mandan como instante con
  // zona para que la API compare contra UTC sin correr el dia seis horas.
  if ($('fDesde').value) p.set('desde', new Date($('fDesde').value + 'T00:00:00').toISOString());
  if ($('fHasta').value) p.set('hasta', new Date($('fHasta').value + 'T23:59:59.999').toISOString());
  return p;
}

function filtroActivo() {
  return [...filtros().keys()].length > 0;
}

async function cargarEventos() {
  const p = filtros();
  const activo = [...p.keys()].length > 0;
  p.set('limite', activo ? '500' : '100');
  const eventos = await api('/api/events?' + p.toString());
  $('tablaEventos').innerHTML = '';
  eventos.forEach((e) => agregarEvento(e));
  $('sinEventos').textContent = activo
    ? 'Ningún evento coincide con la búsqueda.'
    : 'Sin eventos todavía. Inicia el worker para empezar a detectar.';
  $('sinEventos').style.display = eventos.length ? 'none' : 'block';
  $('contadorEventos').textContent = eventos.length + (activo ? ' encontrados' : ' mostrados');
  $('filtroActivo').textContent = activo ? '· búsqueda activa' : '';
}

$('formFiltros').addEventListener('submit', (e) => {
  e.preventDefault();
  cargarEventos().catch((err) => { $('contadorEventos').textContent = err.message; });
});
$('btnLimpiarFiltros').addEventListener('click', () => {
  $('formFiltros').reset();
  cargarEventos().catch(() => {});
});

/* ------------------------------------------------------------------ */
/* Alertas                                                             */
/* ------------------------------------------------------------------ */

const ESTADOS_ALERTA = { acknowledged: 'atendida', dismissed: 'falso positivo' };
const TIPOS_COINCIDENCIA = {
  exact: 'coincidencia exacta', fuzzy: 'coincidencia aproximada',
  biometric: 'coincidencia biométrica', rule: 'regla',
};

function agregarAlerta(a, nuevo = false) {
  $('sinAlertas').style.display = 'none';
  const div = document.createElement('div');
  div.className = 'alerta ' + a.severity + (a.status && a.status !== 'new' ? ' resuelta' : '');
  const acciones = (!a.status || a.status === 'new') && a.id
    ? `<div class="acciones">
         <button onclick="resolver(${Number(a.id)},'acknowledge')"><i data-lucide="check"></i>Atendida</button>
         <button class="sec" onclick="resolver(${Number(a.id)},'dismiss')"><i data-lucide="x"></i>Falso positivo</button>
       </div>` : '';
  const partes = [fechaHora(a.ts || a.created_at), escapar(a.camera_id)];
  if (a.match_kind && a.match_kind !== 'none') {
    let coincidencia = TIPOS_COINCIDENCIA[a.match_kind] || escapar(a.match_kind);
    if (a.match_score != null && a.match_kind !== 'rule') {
      coincidencia += ` ${Math.round(a.match_score * 100)}%`;
    }
    partes.push(coincidencia);
  }
  if (a.status && a.status !== 'new') partes.push(ESTADOS_ALERTA[a.status] || escapar(a.status));
  div.innerHTML = `
    <div class="crece">
      <div class="t">${escapar(a.title)}</div>
      <div class="d">${escapar(a.detail || '')}</div>
      <div class="meta">${partes.join(' · ')}</div>
      ${acciones}
    </div>`;
  if (a.snapshot_path) {
    const img = miniatura(a.snapshot_path, `${a.title} · ${fechaHora(a.ts || a.created_at)}`);
    img.onerror = () => img.remove();
    div.prepend(img);
  }
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
  // Una alerta critica tambien suena, y distinto que un aviso: el operador
  // puede estar mirando otra pantalla.
  sonarAviso(true);
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
function sonarAviso(critico = false) {
  try {
    _audioCtx = _audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const ahora = _audioCtx.currentTime;
    const tonos = critico ? [988, 740, 988, 740] : [880, 660];
    tonos.forEach((frecuencia, i) => {
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

/* Pide estadisticas como mucho una vez cada 2 s. Antes se pedian con CADA
 * evento que llegaba por WebSocket: con trafico, eran decenas de consultas
 * COUNT por segundo por cada dashboard abierto, para mostrar un numero. */
let _statsPendiente = null;
function pedirStats() {
  if (_statsPendiente) return;
  _statsPendiente = setTimeout(() => {
    _statsPendiente = null;
    refrescarStats();
  }, 2000);
}

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

/* ------------------------------------------------------------------ */
/* Alta de camara                                                      */
/* ------------------------------------------------------------------ */

// El ultimo resultado probado, para que "Guardar" no tenga que volver a pedir
// nada: si ya se confirmo que la ruta funciona, guardar es solo escribirla.
let camaraProbada = null;

$('btnCamaras').addEventListener('click', () => {
  $('modalCamaras').style.display = 'grid';
});
function cerrarModalCamaras() { $('modalCamaras').style.display = 'none'; }

$('btnBuscarCamaras').addEventListener('click', async () => {
  const boton = $('btnBuscarCamaras');
  boton.disabled = true;
  $('estadoBusqueda').innerHTML =
    '<div class="cargando"><i data-lucide="loader"></i>Escaneando tu red (~20s)…</div>';
  lucide.createIcons();

  try {
    const dispositivos = await api('/api/camera-setup/descubrir', { method: 'POST' });
    $('estadoBusqueda').textContent = dispositivos.length
      ? `${dispositivos.length} dispositivo(s) con puertos de cámara abiertos:`
      : '';
    $('dispositivosLan').innerHTML = dispositivos.length
      ? dispositivos.map((d) => `
          <button type="button" class="dispositivo" onclick="elegirDispositivo('${d.host}')">
            <i data-lucide="camera"></i>
            <span class="host">${escapar(d.host)}</span>
            <span class="crece"></span>
            <span class="puertos">${d.puertos.map((p) => p.etiqueta).join(' · ')}</span>
          </button>`).join('')
      : '<div class="vacio">Nada encontrado. Verifica que la cámara este en la misma red que esta PC.</div>';
    lucide.createIcons();
  } catch (err) {
    $('estadoBusqueda').textContent = '';
    $('errorCamara').textContent = err.message;
  } finally {
    boton.disabled = false;
  }
});

function elegirDispositivo(host) {
  $('camHost').value = host;
  $('camHost').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

$('formCamara').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('errorCamara').textContent = '';
  $('resultadoCamara').innerHTML = '';
  camaraProbada = null;

  const boton = $('btnProbarCamara');
  const original = boton.innerHTML;
  boton.disabled = true;
  boton.innerHTML = '<i data-lucide="loader"></i>Probando… (unos segundos; hasta 2-3 min si la ruta recomendada no responde)';
  lucide.createIcons();

  const datos = {
    host: $('camHost').value.trim(),
    user: $('camUser').value.trim() || 'admin',
    password: $('camPassword').value,
  };

  try {
    const r = await api('/api/camera-setup/probar', { method: 'POST', body: JSON.stringify(datos) });
    camaraProbada = { ...datos, ruta: r.ruta, camera_id: $('camId').value.trim() || 'cam-01' };
    $('resultadoCamara').innerHTML = `
      <div class="resultado-camara">
        <img src="data:image/jpeg;base64,${r.preview_b64}" alt="Vista previa de la cámara">
        <div class="datos">
          <div class="ok"><i data-lucide="circle-check"></i>Conexión confirmada</div>
          <dl>
            <dt>Resolución</dt><dd>${r.ancho}×${r.alto}</dd>
            <dt>FPS</dt><dd>${r.fps}</dd>
            <dt>Ruta</dt><dd>${escapar(r.ruta)}</dd>
          </dl>
          <button type="button" style="margin-top:12px" onclick="guardarCamara()">
            <i data-lucide="save"></i>Guardar esta cámara
          </button>
        </div>
      </div>`;
    lucide.createIcons();
  } catch (err) {
    $('errorCamara').textContent = err.message;
  } finally {
    boton.disabled = false;
    boton.innerHTML = original;
    lucide.createIcons();
  }
});

async function guardarCamara() {
  if (!camaraProbada) return;
  try {
    const r = await api('/api/camera-setup/guardar', { method: 'POST', body: JSON.stringify(camaraProbada) });
    $('resultadoCamara').innerHTML += `
      <div class="aviso-reinicio">
        <i data-lucide="triangle-alert"></i>
        <span><strong>Guardado en ${escapar(r.archivo)}.</strong> El worker lee su
        configuración al arrancar: (re)inicia el de esta cámara con
        <code class="comando">${escapar(r.comando)}</code></span>
      </div>`;
    lucide.createIcons();
  } catch (err) {
    $('errorCamara').textContent = err.message;
  }
}

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

  const boton = $('formRostro').querySelector('button[type=submit]');
  boton.disabled = true;
  try {
    const r = await fetch('/api/blacklist/faces', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + token },
      body: fd,
    });
    if (r.status === 401) { salir(); throw new Error('Sesión expirada'); }
    if (!r.ok) {
      let detalle = 'Error ' + r.status;
      try {
        const cuerpo = await r.json();
        detalle = Array.isArray(cuerpo.detail)
          ? cuerpo.detail.map((d) => d.msg).join('; ') : (cuerpo.detail || detalle);
      } catch {}
      throw new Error(detalle);
    }
    $('formRostro').reset();
    await cargarRostros();
  } catch (err) {
    $('errorRostro').textContent = err.message;
  } finally {
    boton.disabled = false;
  }
});

async function quitarRostro(id) {
  await api('/api/blacklist/faces/' + id, { method: 'DELETE' });
  await cargarRostros();
}

async function cargarPlacas() {
  const placas = await api('/api/blacklist/plates');
  $('mLista').textContent = placas.length;
  const ahora = Date.now();
  $('listaPlacas').innerHTML = placas.length
    ? placas.map((p) => {
        const vencida = p.expires_at && new Date(p.expires_at).getTime() < ahora;
        const vigencia = !p.expires_at ? ''
          : vencida ? '<span style="color:var(--critico);font-size:11px">vencida</span>'
          : `<span style="color:var(--tenue);font-size:11px">hasta ${new Date(p.expires_at)
              .toLocaleDateString('es-MX', { day: '2-digit', month: 'short', year: 'numeric' })}</span>`;
        return `
        <div class="fila">
          <strong class="mono">${escapar(p.plate)}</strong>
          <span class="crece" style="color:var(--tenue)">${escapar(p.reason)}</span>
          ${vigencia}
          <button class="peligro" style="padding:3px 9px;font-size:12px"
                  onclick="quitarPlaca(${Number(p.id)})"><i data-lucide="trash-2"></i>Quitar</button>
        </div>`;
      }).join('')
    : '<div class="vacio">La lista negra está vacía.</div>';
  lucide.createIcons();
}

$('formPlaca').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('errorPlaca').textContent = '';
  const dias = parseInt($('vigencia').value, 10);
  const vence = dias ? new Date(Date.now() + dias * 86400000).toISOString() : null;
  try {
    await api('/api/blacklist/plates', {
      method: 'POST',
      body: JSON.stringify({ plate: $('nuevaPlaca').value, reason: $('motivo').value, expires_at: vence }),
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
