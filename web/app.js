/* Dashboard del sistema de videovigilancia.
 *
 * Sin framework a proposito: es una sola pantalla con una lista que se
 * actualiza por WebSocket. Un framework aqui seria mas codigo de andamiaje
 * que de aplicacion.
 */

const API = '';
let token = localStorage.getItem('token') || '';
let usuario = null;
let ws = null;
let reconexion = null;
let intentos = 0;

const $ = (id) => document.getElementById(id);

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
  await Promise.all([cargarEventos(), cargarAlertas(), refrescarStats(), cargarPlacas()]);
  conectarWs();
  setInterval(refrescarStats, 15000);
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
    if (type === 'event') { agregarEvento(data, true); refrescarStats(); }
    else if (type === 'alert') { agregarAlerta(data, true); if (data.severity === 'critical') mostrarBanner(data); }
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

const ICONOS = { plate: '🚗', face: '👤', weapon: '🔪' };
const NOMBRES = { plate: 'Placa', face: 'Rostro', weapon: 'Arma' };

function hora(iso) {
  return new Date(iso).toLocaleTimeString('es-MX', { hour12: false });
}

function agregarEvento(ev, nuevo = false) {
  $('sinEventos').style.display = 'none';
  const tr = document.createElement('tr');
  if (nuevo) tr.className = 'nuevo';
  tr.innerHTML = `
    <td class="mono" style="color:var(--tenue)">${hora(ev.ts)}</td>
    <td>${ICONOS[ev.type] || '•'} ${NOMBRES[ev.type] || ev.type}</td>
    <td class="mono"><strong>${escapar(ev.value)}</strong></td>
    <td style="color:var(--tenue)">${escapar(ev.camera_id)}</td>
    <td><span class="etiqueta ${ev.severity}">${ev.severity}</span></td>`;
  const tbody = $('tablaEventos');
  tbody.prepend(tr);
  while (tbody.children.length > 200) tbody.lastChild.remove();
  $('contadorEventos').textContent = tbody.children.length + ' mostrados';
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
         <button onclick="resolver(${a.id},'acknowledge')">Atendida</button>
         <button class="sec" onclick="resolver(${a.id},'dismiss')">Falso positivo</button>
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
  $('banner').style.display = 'flex';
}
function cerrarBanner() { $('banner').style.display = 'none'; }

/* ------------------------------------------------------------------ */
/* Estadisticas                                                        */
/* ------------------------------------------------------------------ */

async function refrescarStats() {
  try {
    const s = await api('/api/stats');
    $('mEventos').textContent = s.total_eventos;
    $('mAlertas').textContent = s.alertas_nuevas;
    $('mPlacas').textContent = s.eventos_por_tipo.plate || 0;

    const camaras = s.camaras || [];
    const activas = camaras.filter((c) => c.online).length;
    $('estadoCamaras').innerHTML = camaras.length
      ? `<span class="punto ${activas ? 'on' : 'off'}"></span>
         <span>${activas}/${camaras.length} cámara${camaras.length > 1 ? 's' : ''}</span>`
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
                  onclick="quitarRostro(${r.id})">Quitar</button>
        </div>`).join('')
    : '<div class="vacio">Ninguna persona registrada.</div>';
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
                  onclick="quitarPlaca(${p.id})">Quitar</button>
        </div>`).join('')
    : '<div class="vacio">La lista negra está vacía.</div>';
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
