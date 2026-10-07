/* Apartado Mapa: donde esta cada camara y como esta.
 *
 * Para quien sale a atender: "alerta en cam-07" no dice nada; un punto rojo
 * parpadeando en la esquina de Juarez y Morelos, si. Cada camara con
 * coordenadas es un circulo verde (en linea) o gris (sin senal); una alerta
 * critica lo pone rojo y parpadeando unos segundos.
 *
 * Leaflet va dentro del proyecto (web/vendor/leaflet, licencia BSD-2). Los
 * mosaicos de calles vienen de OpenStreetMap y necesitan internet; sin el,
 * las camaras se ven igual sobre fondo liso.
 */

import { $, accion, api, emitir, escapar, escuchar, estado, iconos, nombreCamara, puede } from './nucleo.js';
import { confirmar } from './avisos.js';

const CENTRO_MX = [19.4326, -99.1332];   // CDMX, si ninguna camara tiene coordenadas
let mapa = null;
let ubicando = null;                       // camera_id que se esta colocando
const marcadores = new Map();              // camera_id -> circleMarker
const destacadas = new Map();              // camera_id -> temporizador

escuchar('vista', (v) => { if (v === 'mapa') abrir(); });
escuchar('stats', () => { if (mapa) actualizar(false); });
escuchar('ws:alert', (a) => {
  if (a && a.severity === 'critical') destacar(a.camera_id);
});

function abrir() {
  if (!window.L) {
    $('mapa').textContent = 'No se pudo cargar la librería del mapa.';
    return;
  }
  if (!mapa) {
    mapa = window.L.map('mapa', { zoomControl: true, attributionControl: true });
    window.L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      // El sitio manda Referrer-Policy: no-referrer, y los servidores de
      // OpenStreetMap rechazan (403 "Access blocked") los mosaicos pedidos sin
      // Referer. Solo para esta capa se manda el origen, sin la ruta.
      referrerPolicy: 'strict-origin-when-cross-origin',
    }).addTo(mapa);
    mapa.setView(CENTRO_MX, 12);
    mapa.on('click', colocar);
    actualizar(true);
  } else {
    actualizar(false);
  }
  // El contenedor estaba oculto: Leaflet necesita medirlo otra vez.
  setTimeout(() => mapa && mapa.invalidateSize(), 0);
}

function color(c) {
  if (destacadas.has(c.camera_id)) return '#ff4757';
  return c.online ? '#34d399' : '#7c8a99';
}

function contenido(c) {
  const estadoTxt = c.online ? 'En línea' : (c.segundos_sin_senal != null ? 'Sin señal' : 'Sin reportar');
  return `<div class="popup-camara">
      <b>${escapar(nombreCamara(c.camera_id))}</b>
      ${c.location ? `<div>${escapar(c.location)}</div>` : ''}
      <div class="tenue">${escapar(c.camera_id)} · ${estadoTxt}</div>
      <button class="sec chico" data-accion="mapa-ver-camara" data-camara="${escapar(c.camera_id)}">Ver en vivo</button>
    </div>`;
}

function actualizar(encuadrar) {
  const L = window.L;
  const conUbicacion = estado.camaras.filter((c) => c.lat != null && c.lon != null);
  const vistos = new Set();
  for (const c of conUbicacion) {
    vistos.add(c.camera_id);
    let m = marcadores.get(c.camera_id);
    if (!m) {
      m = L.circleMarker([c.lat, c.lon], { radius: 9, weight: 2, fillOpacity: 0.85 }).addTo(mapa);
      marcadores.set(c.camera_id, m);
    }
    m.setLatLng([c.lat, c.lon]);
    m.setStyle({ color: '#0b0f14', fillColor: color(c) });
    m.bindPopup(contenido(c));
    m.bindTooltip(escapar(nombreCamara(c.camera_id)), { direction: 'top', offset: [0, -8] });
  }
  for (const [id, m] of marcadores) {
    if (!vistos.has(id)) { m.remove(); marcadores.delete(id); }
  }
  if (encuadrar && conUbicacion.length) {
    const limites = L.latLngBounds(conUbicacion.map((c) => [c.lat, c.lon]));
    mapa.fitBounds(limites.pad(0.3), { maxZoom: 17 });
  }
  pintarSinUbicacion();
}

function pintarSinUbicacion() {
  const sin = estado.camaras.filter((c) => c.lat == null || c.lon == null);
  $('mapaSinUbicacion').innerHTML = sin.length
    ? sin.map((c) => `
        <div class="fila-mapa">
          <span class="crece">${escapar(nombreCamara(c.camera_id))}</span>
          ${puede('admin') ? `<button class="sec chico" data-accion="mapa-ubicar" data-camara="${escapar(c.camera_id)}">
            <i data-lucide="map-pin"></i>Ubicar</button>` : ''}
        </div>`).join('')
    : '<div class="vacio">Todas las cámaras están en el mapa.</div>';
  $('mapaAyuda').textContent = ubicando
    ? `Haz clic en el mapa donde está ${nombreCamara(ubicando)}. Esc para cancelar.`
    : '';
  iconos();
}

function destacar(id) {
  clearTimeout(destacadas.get(id));
  destacadas.set(id, setTimeout(() => {
    destacadas.delete(id);
    const m = marcadores.get(id);
    if (m && m.getElement()) m.getElement().classList.remove('marcador-alerta');
    if (mapa) actualizar(false);
  }, 30000));
  if (!mapa) return;
  actualizar(false);
  const m = marcadores.get(id);
  if (m && m.getElement()) m.getElement().classList.add('marcador-alerta');
}

accion('mapa-ubicar', (el) => {
  ubicando = el.dataset.camara;
  $('mapa').classList.add('colocando');
  pintarSinUbicacion();
});

async function colocar(e) {
  if (!ubicando) return;
  const c = estado.camaras.find((x) => x.camera_id === ubicando);
  const id = ubicando;
  ubicando = null;
  $('mapa').classList.remove('colocando');
  try {
    await api('/api/cameras/' + encodeURIComponent(id), {
      method: 'PUT',
      body: JSON.stringify({
        name: (c && c.name) || id,
        location: (c && c.location) || null,
        lat: Math.round(e.latlng.lat * 1e6) / 1e6,
        lon: Math.round(e.latlng.lng * 1e6) / 1e6,
      }),
    });
    confirmar('Cámara ubicada en el mapa');
    emitir('pedir-stats');
  } catch (err) {
    alert(err.message);
  }
  pintarSinUbicacion();
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && ubicando) {
    ubicando = null;
    $('mapa').classList.remove('colocando');
    pintarSinUbicacion();
  }
});

accion('mapa-ver-camara', (el) => {
  emitir('ir-a-camara', el.dataset.camara);
});
