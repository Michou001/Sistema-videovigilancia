/* Avisos al operador: banner critico, avisos flotantes y sonido. */

import { $, accion, escapar, iconoTag, iconos } from './nucleo.js';

export function mostrarBanner(a) {
  $('bannerTitulo').textContent = a.title;
  $('bannerDetalle').textContent = a.detail || '';
  // El banner vive dentro de #barra, asi que al mostrarlo empuja la cabecera
  // hacia abajo por si solo. Nada que medir.
  $('banner').style.display = 'flex';
  // Una alerta critica tambien suena, y distinto que un aviso: el operador
  // puede estar mirando otra pantalla.
  sonarAviso(true);
}

accion('cerrar-banner', () => { $('banner').style.display = 'none'; });

/* Aviso (warning): se nota aunque el operador este viendo Monitoreo pero no
 * bloquea como el banner critico. Se apila con los demas y se retira solo. */
export function mostrarToast(a, { sonar = true, tipo = 'aviso', icono = null } = {}) {
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

/* Chime generado en el navegador (dos tonos suaves) -- nada de un archivo de
 * audio que cargar. Los navegadores bloquean audio antes de cualquier
 * interaccion; para cuando llega la primera alerta el operador ya inicio
 * sesion (eso cuenta). Si aun asi se bloquea, el aviso visual sigue igual. */
let audioCtx = null;
export function sonarAviso(critico = false) {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const ahora = audioCtx.currentTime;
    const tonos = critico ? [988, 740, 988, 740] : [880, 660];
    tonos.forEach((frecuencia, i) => {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.value = frecuencia;
      gain.gain.setValueAtTime(0.0001, ahora + i * 0.12);
      gain.gain.exponentialRampToValueAtTime(0.12, ahora + i * 0.12 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ahora + i * 0.12 + 0.18);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start(ahora + i * 0.12);
      osc.stop(ahora + i * 0.12 + 0.2);
    });
  } catch {}
}
