/* Punto de entrada del dashboard: sesion, apartados, estadisticas y reparto
 * de los mensajes en vivo a cada modulo.
 *
 * La pagina tiene apartados y solo uno vive a la vez:
 *
 *   MONITOREO -- las camaras en vivo y, a su lado, las detecciones conforme
 *                entran. Es la pantalla de guardia: se mira, no se opera.
 *   REGISTRO  -- el historico de eventos y las alertas por atender. Es la
 *                pantalla de trabajo: se busca, se resuelve, se descarta.
 *   MAPA      -- donde esta cada camara y como esta.
 *   CAMARAS   -- catalogo y alta de camaras (solo administradores).
 *
 * Estan separados porque no se usan a la vez ni por lo mismo, y porque el
 * video en vivo cuesta ancho de banda: al salir de Monitoreo los flujos se
 * cierran y el worker deja de codificar y subir frames.
 */

import {
  $, accion, almacen, api, emitir, escuchar, estado, iconos, NOMBRES_ROL, puede,
} from './nucleo.js';
import { mostrarBanner, mostrarToast } from './avisos.js';
import {
  agregarDeteccion, cargarDetecciones, detenerCamaras, iniciarCamaras, marcarEnCamara,
} from './monitoreo.js';
import {
  actualizarClip, actualizarFiltroCamaras, agregarAlerta, agregarEvento, cargarAlertas, cargarEventos, filtroActivo,
  prepararBusquedaSemantica,
} from './registro.js';
import { cargarPlacas } from './listanegra.js';
import { conectarWs, desconectarWs } from './tiempo_real.js';
import './camaras.js';
import './admin.js';
import './evidencia.js';
import './placas.js';
import './zonas.js';
import './mapa.js';
import './titulos.js';

iconos();

let temporizadorStats = null;

/* ------------------------------------------------------------------ */
/* Sesion                                                              */
/* ------------------------------------------------------------------ */

$('formLogin').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('errorLogin').textContent = '';
  const boton = $('formLogin').querySelector('button[type=submit]');
  boton.disabled = true;
  try {
    const s = await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username: $('usuario').value, password: $('password').value }),
    });
    estado.token = s.token;
    almacen.guardar('token', s.token);
    estado.usuario = s;
    $('password').value = '';
    await arrancar();
  } catch (err) {
    $('errorLogin').textContent = err.message;
  } finally {
    boton.disabled = false;
  }
});

function limpiarSesion() {
  almacen.borrar('token');
  estado.token = '';
  estado.usuario = null;
  // Todos los temporizadores: sin esto, tras cerrar sesion el reintento del
  // WebSocket y el refresco de estadisticas seguian corriendo, y al volver a
  // entrar se duplicaban.
  clearInterval(temporizadorStats);
  temporizadorStats = null;
  desconectarWs();
  emitir('fin-sesion');
  detenerCamaras();
  $('app').style.display = 'none';
  $('login').style.display = 'grid';
}

async function salir() {
  // Borra la cookie HttpOnly del lado del servidor (el JavaScript no puede).
  try { await api('/api/auth/logout', { method: 'POST' }); } catch {}
  limpiarSesion();
}

accion('salir', salir);
escuchar('sesion-expirada', limpiarSesion);

async function arrancar() {
  $('login').style.display = 'none';
  $('app').style.display = 'block';
  const u = estado.usuario;
  $('quien').textContent = u.display_name + ' · ' + (NOMBRES_ROL[u.role] || u.role);

  // Mostrar u ocultar segun el rol. Quien decide lo que se puede hacer es la
  // API; esto solo evita ofrecer botones que van a responder 403.
  document.querySelectorAll('[data-rol]').forEach((el) => {
    el.style.display = puede(el.dataset.rol) ? '' : 'none';
  });

  await refrescarStats();
  await Promise.all([
    cargarEventos(), cargarAlertas(), cargarDetecciones(),
    puede('admin') ? cargarPlacas().catch(() => {}) : Promise.resolve(),
    prepararBusquedaSemantica(),
  ]);
  conectarWs();
  clearInterval(temporizadorStats);
  temporizadorStats = setInterval(refrescarStats, 15000);
  // El apartado se recuerda entre recargas: si el operador dejo el navegador
  // en Registro, un F5 no deberia devolverlo a Monitoreo.
  mostrarVista(almacen.leer('vista', 'monitoreo'));
}

/* ------------------------------------------------------------------ */
/* Apartados                                                           */
/* ------------------------------------------------------------------ */

const VISTAS = ['monitoreo', 'registro', 'mapa', 'camaras'];
let vistaActual = null;

function mostrarVista(nombre) {
  if (!VISTAS.includes(nombre)) nombre = 'monitoreo';
  // El catalogo de camaras es solo de administradores: si alguien con otro
  // rol tenia ese apartado recordado, vuelve a Monitoreo.
  if (nombre === 'camaras' && !puede('admin')) nombre = 'monitoreo';
  vistaActual = nombre;
  almacen.guardar('vista', nombre);

  for (const v of VISTAS) {
    const sufijo = v[0].toUpperCase() + v.slice(1);
    $('vista' + sufijo).classList.toggle('activa', v === nombre);
    $('nav' + sufijo).classList.toggle('activo', v === nombre);
  }

  if (nombre === 'monitoreo') {
    iniciarCamaras();
  } else {
    // Los flujos MJPEG se cierran al salir. Dejarlos abiertos mantendria al
    // worker subiendo video a una pantalla que nadie esta viendo.
    detenerCamaras();
  }
  emitir('vista', nombre);
}

accion('vista', (el) => mostrarVista(el.dataset.vista));
escuchar('ir-a-vista', mostrarVista);

/* Desde el mapa: "Ver en vivo" lleva al recuadro de esa camara. */
escuchar('ir-a-camara', (id) => {
  mostrarVista('monitoreo');
  setTimeout(() => {
    const el = document.querySelector(`#rejilla [data-camara="${CSS.escape(id)}"]`);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, 300);
});

/* Con la pestana del navegador en segundo plano tampoco hace falta el video.
 * Es el caso mas comun de todos: el dashboard abierto en una pestana olvidada. */
document.addEventListener('visibilitychange', () => {
  if (!estado.token) return;
  if (document.hidden) detenerCamaras();
  else if (vistaActual === 'monitoreo') iniciarCamaras();
});

/* ------------------------------------------------------------------ */
/* Estadisticas                                                        */
/* ------------------------------------------------------------------ */

/* Pide estadisticas como mucho una vez cada 2 s. Antes se pedian con CADA
 * evento que llegaba por WebSocket: con trafico, eran decenas de consultas
 * COUNT por segundo por cada dashboard abierto, para mostrar un numero. */
let statsPendiente = null;
function pedirStats() {
  if (statsPendiente) return;
  statsPendiente = setTimeout(() => {
    statsPendiente = null;
    refrescarStats();
  }, 2000);
}
escuchar('pedir-stats', pedirStats);

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

    estado.camaras = s.camaras || [];
    actualizarFiltroCamaras(estado.camaras);
    const activas = estado.camaras.filter((c) => c.online).length;
    const n = estado.camaras.length;
    $('estadoCamaras').innerHTML = n
      ? `<span class="punto ${activas ? 'on' : 'off'}"></span>
         <span>${activas}/${n} cámara${n > 1 ? 's' : ''}</span>`
      : '<span class="punto"></span><span>sin cámaras</span>';
    emitir('stats', s);
  } catch {}
}

/* ------------------------------------------------------------------ */
/* Mensajes en vivo                                                    */
/* ------------------------------------------------------------------ */

escuchar('ws:event', (ev) => {
  // Con una busqueda activa, la tabla muestra el resultado de esa busqueda:
  // meterle eventos en vivo que quiza no cumplen el filtro la contradiria.
  if (!filtroActivo()) agregarEvento(ev, true);
  agregarDeteccion(ev, true);
  marcarEnCamara(ev);
  pedirStats();
});

escuchar('ws:alert', (a) => {
  agregarAlerta(a, true);
  pedirStats();
  if (a.severity === 'critical') mostrarBanner(a);
  else if (a.severity === 'warning') mostrarToast(a);
});

escuchar('ws:alert_resolved', () => { cargarAlertas(); pedirStats(); });
escuchar('lectura-corregida', () => { cargarEventos().catch(() => {}); pedirStats(); });
escuchar('ws:alert_updated', actualizarClip);
escuchar('ws:camera_status', pedirStats);
escuchar('pedir-stats', pedirStats);

/* ------------------------------------------------------------------ */

/* Sesion persistida: al recargar, se valida el token antes de mostrar nada.
 * /me tambien repone la cookie de sesion si el navegador la perdio. */
(async () => {
  if (!estado.token) return;
  try {
    estado.usuario = await api('/api/auth/me');
    await arrancar();
  } catch {
    limpiarSesion();
  }
})();
