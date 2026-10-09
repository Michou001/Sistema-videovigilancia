/* Cabecera de cada apartado: que lugar es, en que apartado se esta y el
 * estado REAL en una linea (camaras en linea, detecciones de hoy, ultima
 * deteccion, alertas por atender). Antes eran lemas fijos; el operador que
 * llega al turno necesita saber como esta el sistema, no un eslogan. */
import { $, escuchar, estado, haceCuanto, NOMBRES, NOMBRES_ROL, nombreCamara, valorLegible } from './nucleo.js';

const TITULOS = {
  monitoreo: ['EN VIVO', 'Monitoreo en vivo'],
  registro: ['REGISTRO', 'Eventos y alertas'],
  mapa: ['MAPA', 'Ubicación de las cámaras'],
  camaras: ['CÁMARAS', 'Cámaras y conexión'],
};

let vista = 'monitoreo';
let stats = null;
let sitio = '';

const plural = (n, uno, varios) => `${n} ${n === 1 ? uno : varios}`;

function resumen() {
  if (!stats) return 'Cargando el estado de las cámaras…';
  const camaras = stats.camaras || [];
  const enLinea = camaras.filter((c) => c.online).length;
  const partes = [];
  const ultimo = stats.ultimo_evento;
  let textoUltimo = 'sin detecciones todavía';
  if (ultimo) {
    const tipo = NOMBRES[ultimo.type] || ultimo.type;
    const valor = valorLegible(ultimo);
    // Un rostro sin coincidencia trae "Rostro" como valor: no repetirlo.
    const que = valor && valor !== tipo ? `${tipo.toLowerCase()} ${valor}` : tipo.toLowerCase();
    textoUltimo = `última detección: ${que} en ${nombreCamara(ultimo.camera_id)}, ${haceCuanto(ultimo.ts)}`;
  }

  if (vista === 'monitoreo') {
    partes.push(camaras.length ? `${enLinea} de ${plural(camaras.length, 'cámara', 'cámaras')} en línea`
      : 'Ninguna cámara ha reportado todavía');
    partes.push(plural(stats.eventos_hoy || 0, 'detección hoy', 'detecciones hoy'));
    partes.push(textoUltimo);
  } else if (vista === 'registro') {
    const n = stats.alertas_nuevas || 0;
    partes.push(n ? `${plural(n, 'alerta', 'alertas')} por atender`
      + (stats.alertas_criticas ? ` (${stats.alertas_criticas} crítica${stats.alertas_criticas === 1 ? '' : 's'})` : '')
      : 'Sin alertas por atender');
    partes.push(plural(stats.eventos_hoy || 0, 'evento hoy', 'eventos hoy'));
    partes.push(textoUltimo);
  } else if (vista === 'mapa') {
    const ubicadas = camaras.filter((c) => c.lat != null && c.lon != null).length;
    partes.push(`${ubicadas} de ${plural(camaras.length, 'cámara ubicada', 'cámaras ubicadas')}`);
    if (ubicadas < camaras.length) partes.push('las que faltan se ubican desde el panel de la derecha');
  } else if (vista === 'camaras') {
    const caidas = camaras.filter((c) => !c.online).length;
    partes.push(plural(camaras.length, 'cámara registrada', 'cámaras registradas'));
    partes.push(`${enLinea} en línea`);
    if (caidas) partes.push(`${caidas} sin señal`);
  }
  const texto = partes.join(' · ');
  return texto.charAt(0).toUpperCase() + texto.slice(1) + '.';
}

function pintar() {
  const [eyebrow, titulo] = TITULOS[vista] || TITULOS.monitoreo;
  $('eyebrowVista').textContent = `${(sitio || 'Centro de monitoreo').toUpperCase()} / ${eyebrow}`;
  $('tituloVista').textContent = titulo;
  $('descripcionVista').textContent = resumen();
  const u = estado.usuario;
  if (u) $('selloTurno').textContent = `${u.display_name} · ${NOMBRES_ROL[u.role] || u.role}`;
}

escuchar('vista', (v) => { vista = v; pintar(); });
escuchar('stats', (s) => { stats = s; pintar(); });

// "hace 3 min" envejece aunque no lleguen estadisticas nuevas.
setInterval(() => { if (stats) $('descripcionVista').textContent = resumen(); }, 30000);

fetch('/api/health', { cache: 'no-store' })
  .then((r) => (r.ok ? r.json() : {}))
  .then((s) => { sitio = (s.sitio || '').trim(); pintar(); })
  .catch(() => {});
