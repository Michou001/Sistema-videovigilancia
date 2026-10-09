/* Capturas como albumes: uno general y uno por camara, con las 3 fotos mas
 * recientes. Al abrir un album, una rejilla de 4x4 o 5x5 por pagina; al abrir
 * una foto, el visor de siempre con su detalle. */

import {
  $, accion, almacen, api, escapar, escuchar, estado, fechaHora, hora, iconoTag, iconos,
  modal, NOMBRES, nombreCamara, valorLegible,
} from './nucleo.js';
import { abrirVisor, urlEvidencia } from './evidencia.js';

const POR_ALBUM = 3;
const recientes = new Map();     // '' (general) o camera_id -> ultimas capturas
let camarasVistas = null;      // null: todavia no se cargan
const galeria = { camara: '', tipo: '', tam: Number(almacen.leer('galeria-tam', '4')) || 4, pagina: 0 };

/* Texto del visor: lo mismo que mostraba la lista de detecciones. */
function detalle(ev) {
  const extra = [];
  if (ev.meta?.tipo_placa && ev.meta.tipo_placa !== 'Placa extranjera') {
    extra.push(ev.meta.tipo_placa + (ev.meta.entidad ? ' de ' + ev.meta.entidad : ''));
  }
  if (ev.meta?.color_vehiculo) extra.push('vehículo ' + ev.meta.color_vehiculo);
  if (ev.observations) extra.push(ev.observations + ' cuadros');
  return [`${NOMBRES[ev.type] || ev.type} ${valorLegible(ev)}`, fechaHora(ev.ts), nombreCamara(ev.camera_id), ...extra]
    .join(' · ');
}

function foto(ev, clase) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = `foto ${clase} ${ev.severity || 'info'}`;
  b.title = detalle(ev);
  b.innerHTML = `<img alt="" loading="lazy" src="${urlEvidencia(ev.snapshot_path)}">
    <span class="foto-pie">${iconoTag(ev.type)}<b>${escapar(valorLegible(ev))}</b><small>${hora(ev.ts)}</small></span>`;
  // La retencion pudo borrar la foto: se deja el hueco con el icono.
  b.querySelector('img').onerror = (e) => e.target.replaceWith(Object.assign(document.createElement('span'),
    { className: 'foto-sin', innerHTML: iconoTag(ev.type) }));
  b.addEventListener('click', (e) => {
    e.stopPropagation();
    abrirVisor(urlEvidencia(ev.snapshot_path), detalle(ev));
  });
  return b;
}

/* ------------------------------------------------------------ albumes */

function claves() {
  return ['', ...estado.camaras.map((c) => c.camera_id)];
}

async function cargarAlbum(clave) {
  const camara = clave ? `&camera_id=${encodeURIComponent(clave)}` : '';
  try {
    recientes.set(clave, await api(`/api/events?con_foto=true&limite=${POR_ALBUM}${camara}`));
  } catch {
    if (!recientes.has(clave)) recientes.set(clave, []);
  }
}

export async function cargarAlbumes() {
  camarasVistas = claves().join('|');
  await Promise.all(claves().map(cargarAlbum));
  pintarAlbumes();
}

function pintarAlbumes() {
  const cont = $('albumes');
  cont.replaceChildren();
  for (const clave of claves()) {
    const fotos = recientes.get(clave) || [];
    const card = document.createElement('div');
    card.className = 'album';
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    const titulo = clave ? nombreCamara(clave) : 'Todas las cámaras';
    card.setAttribute('aria-label', `Abrir capturas de ${titulo}`);
    const portada = document.createElement('div');
    portada.className = 'album-portada';
    for (let i = 0; i < POR_ALBUM; i++) {
      if (fotos[i]) portada.append(foto(fotos[i], i === 0 ? 'grande' : 'chica'));
      else portada.append(Object.assign(document.createElement('span'),
        { className: `foto-sin ${i === 0 ? 'grande' : 'chica'}`, innerHTML: '<i data-lucide="image"></i>' }));
    }
    const ultima = fotos[0];
    const info = document.createElement('div');
    info.className = 'album-info';
    info.innerHTML = `<strong>${escapar(titulo)}</strong>
      <span>${ultima ? `Última: ${escapar(valorLegible(ultima))} · ${fechaHora(ultima.ts)}` : 'Sin capturas todavía'}</span>
      <span class="album-ver">Ver todas <i data-lucide="chevron-right"></i></span>`;
    card.append(portada, info);
    const abrir = () => abrirGaleria(clave);
    card.addEventListener('click', abrir);
    card.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); abrir(); } });
    cont.append(card);
  }
  const hay = [...recientes.values()].some((f) => f.length);
  $('sinDetecciones').style.display = hay || !$('listaDetecciones').hidden ? 'none' : 'block';
  iconos();
}

/* Una captura nueva entra al frente del album general y del de su camara. */
escuchar('ws:event', (ev) => {
  if (!ev.snapshot_path) return;
  for (const clave of ['', ev.camera_id]) {
    const lista = recientes.get(clave) || [];
    recientes.set(clave, [ev, ...lista.filter((x) => x.event_id !== ev.event_id)].slice(0, POR_ALBUM));
  }
  if (!claves().includes(ev.camera_id)) return;   // camara nueva: llega con el siguiente stats
  pintarAlbumes();
  if ($('modalGaleria').style.display === 'grid' && galeria.pagina === 0 &&
      (!galeria.camara || galeria.camara === ev.camera_id) && (!galeria.tipo || galeria.tipo === ev.type)) {
    cargarPagina();
  }
});

/* Las camaras se conocen con las estadisticas: al cambiar, se recargan. */
escuchar('stats', () => {
  if (claves().join('|') !== camarasVistas) cargarAlbumes();
});
escuchar('lectura-corregida', () => cargarAlbumes());
escuchar('fin-sesion', () => {
  recientes.clear();
  camarasVistas = null;
  $('albumes').replaceChildren();
  modal('modalGaleria', false);
});

/* Lista de texto de siempre, para quien la prefiera. */
accion('ver-lista-detecciones', (el) => {
  const lista = $('listaDetecciones');
  lista.hidden = !lista.hidden;
  $('albumes').hidden = !lista.hidden;
  el.setAttribute('aria-pressed', String(!lista.hidden));
  el.innerHTML = lista.hidden ? '<i data-lucide="list"></i>Ver como lista' : '<i data-lucide="layout-grid"></i>Ver como álbumes';
  $('sinDetecciones').style.display = lista.hidden
    ? ([...recientes.values()].some((f) => f.length) ? 'none' : 'block')
    : (lista.children.length ? 'none' : 'block');
  iconos();
});

/* ------------------------------------------------------------ galeria */

function marcar(cont, atributo, valor) {
  for (const b of $(cont).querySelectorAll('button')) b.classList.toggle('activo', b.dataset[atributo] === String(valor));
}

function abrirGaleria(clave) {
  galeria.camara = clave;
  galeria.tipo = '';
  galeria.pagina = 0;
  $('galeriaTitulo').textContent = clave ? nombreCamara(clave) : 'Todas las cámaras';
  $('galeriaSub').textContent = clave ? `CAPTURAS · ${clave}` : 'CAPTURAS · GENERAL';
  marcar('galeriaTipos', 'tipo', '');
  modal('modalGaleria', true);
  cargarPagina();
}

async function cargarPagina() {
  const n = galeria.tam * galeria.tam;
  marcar('galeriaTamanos', 'tam', galeria.tam);
  const rejilla = $('galeriaRejilla');
  rejilla.style.setProperty('--columnas', galeria.tam);
  const params = new URLSearchParams({ con_foto: 'true', limite: String(n + 1), desde_n: String(galeria.pagina * n) });
  if (galeria.camara) params.set('camera_id', galeria.camara);
  if (galeria.tipo) params.set('tipo', galeria.tipo);
  let eventos = [];
  try { eventos = await api('/api/events?' + params); } catch { /* se queda vacia */ }
  const haySiguiente = eventos.length > n;
  rejilla.replaceChildren(...eventos.slice(0, n).map((ev) => foto(ev, 'celda')));
  $('galeriaVacia').hidden = eventos.length > 0;
  $('galeriaAnt').disabled = galeria.pagina === 0;
  $('galeriaSig').disabled = !haySiguiente;
  $('galeriaPagina').textContent = eventos.length ? `Página ${galeria.pagina + 1}` : '';
  iconos();
}

accion('galeria-tipo', (el) => {
  galeria.tipo = el.dataset.tipo;
  galeria.pagina = 0;
  marcar('galeriaTipos', 'tipo', galeria.tipo);
  cargarPagina();
});
accion('galeria-tam', (el) => {
  // Mismo punto del historial al cambiar de tamano: la primera foto visible sigue visible.
  const primera = galeria.pagina * galeria.tam * galeria.tam;
  galeria.tam = Number(el.dataset.tam);
  galeria.pagina = Math.floor(primera / (galeria.tam * galeria.tam));
  almacen.guardar('galeria-tam', String(galeria.tam));
  cargarPagina();
});
accion('galeria-pagina', (el) => {
  galeria.pagina = Math.max(0, galeria.pagina + Number(el.dataset.dir));
  cargarPagina();
});
accion('cerrar-galeria', () => modal('modalGaleria', false));
$('modalGaleria').addEventListener('click', (e) => { if (e.target.id === 'modalGaleria') modal('modalGaleria', false); });
// En captura, antes que el Escape del visor: si el visor esta abierto, ese
// Escape es suyo y la galeria se queda.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || $('modalGaleria').style.display !== 'grid') return;
  if ($('visor').style.display === 'grid') return;
  modal('modalGaleria', false);
}, true);
