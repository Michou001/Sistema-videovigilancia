/* Pantalla de acceso: hora del centro de monitoreo, sitio y estado del servidor.
 * Solo usa /api/health, que es publico y no dice nada de camaras ni usuarios. */
import { $ } from './nucleo.js';

const visible = () => $('login').style.display !== 'none';
const fecha = new Intl.DateTimeFormat('es-MX', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });

function reloj() {
  if (!visible()) return;
  const ahora = new Date();
  const dos = (n) => String(n).padStart(2, '0');
  $('accesoHora').firstChild.textContent = `${dos(ahora.getHours())}:${dos(ahora.getMinutes())}`;
  $('accesoSeg').textContent = ':' + dos(ahora.getSeconds());
  $('accesoFecha').textContent = fecha.format(ahora);
}

async function servidor() {
  if (!visible()) return;
  try {
    const r = await fetch('/api/health', { cache: 'no-store' });
    if (!r.ok) throw new Error();
    const s = await r.json();
    $('accesoPunto').className = 'punto on';
    $('accesoServidor').textContent = 'Servidor en línea';
    if (s.sitio) $('accesoSitio').textContent = s.sitio;
    $('accesoVersion').textContent = s.version ? `v${s.version}` : '';
  } catch {
    $('accesoPunto').className = 'punto off';
    $('accesoServidor').textContent = 'Sin conexión con el servidor';
  }
}

reloj();
servidor();
setInterval(reloj, 1000);
setInterval(servidor, 15000);

// Un error de acceso sacude el formulario: se nota aunque no se lea el texto.
new MutationObserver(() => {
  const f = $('formLogin');
  if (!$('errorLogin').textContent) return;
  f.classList.remove('sacudir');
  void f.offsetWidth;   // reinicia la animacion si se equivoca dos veces seguidas
  f.classList.add('sacudir');
}).observe($('errorLogin'), { childList: true, characterData: true, subtree: true });
