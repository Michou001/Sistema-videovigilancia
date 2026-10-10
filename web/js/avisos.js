/* Avisos al operador: banner critico, avisos flotantes y sonido. */

import { $, accion, escapar, iconoTag, iconos } from './nucleo.js';

export function mostrarBanner(a, { sonar = false } = {}) {
  $('bannerTitulo').textContent = a.title;
  $('bannerDetalle').textContent = a.detail || '';
  // El banner vive dentro de #barra, asi que al mostrarlo empuja la cabecera
  // hacia abajo por si solo. Nada que medir.
  $('banner').style.display = 'flex';
  // Una alerta critica tambien suena, y distinto que un aviso: el operador
  // puede estar mirando otra pantalla.
  if (sonar) sonarAviso();
}

accion('cerrar-banner', () => { $('banner').style.display = 'none'; });

/* Aviso (warning): se nota aunque el operador este viendo Monitoreo pero no
 * bloquea como el banner critico. Se apila con los demas y se retira solo. */
export function mostrarToast(a, { sonar = false, tipo = 'aviso', icono = null } = {}) {
  const div = document.createElement('div');
  div.className = 'toast ' + tipo;
  const simbolo = icono ? `<i data-lucide="${icono}"></i>`
    : a.type ? iconoTag(a.type) : '<i data-lucide="triangle-alert"></i>';
  div.innerHTML = `
    <span class="ico">${simbolo}</span>
    <div class="crece">
      <div class="t">${escapar(a.title)}</div>
      <div class="d">${escapar(a.detail || '')}</div>
    </div>
    <button class="cerrar" aria-label="Cerrar" data-accion="cerrar-toast">×</button>`;
  $('toasts').appendChild(div);
  iconos();
  if (sonar) sonarAviso();

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

accion('cerrar-toast', (el) => el.closest('.toast').remove());

/* Mensaje breve de confirmacion ("Usuario creado"), sin sonido. */
export function confirmar(texto) {
  mostrarToast({ title: texto, type: null }, { sonar: false, tipo: 'ok', icono: 'circle-check' });
}

/* Aviso de cinco segundos para coincidencias con la lista negra. Si llegan
 * varias casi juntas, se termina el aviso actual antes de empezar otro para
 * evitar que los sonidos se superpongan. El navegador puede bloquear audio
 * antes de la primera interaccion; el aviso visual permanece visible. */
let audioAviso = null;
export function sonarAviso() {
  try {
    if (audioAviso && !audioAviso.paused) return;
    audioAviso = new Audio('/static/audio/ensayo-alerta-evento.wav');
    audioAviso.play().catch(() => {});
  } catch {}
}
