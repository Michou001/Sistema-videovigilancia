/* Evidencia: miniaturas ampliables, visor de fotos y reproductor de clips.
 *
 * Las fotos y los clips se piden a /media/<archivo>. Un <img> o un <video> no
 * pueden mandar la cabecera Authorization: la sesion viaja en la cookie
 * HttpOnly del login, que el navegador agrega solo.
 */

import { $, accion } from './nucleo.js';

export const urlEvidencia = (ruta) => '/media/' + encodeURIComponent(String(ruta).split('/').pop());

/* Miniatura que se amplia al hacer clic. La foto guardada puede ser la
 * captura HD de 3200 px: a 66 px de ancho el operador no ve nada. */
export function miniatura(ruta, descripcion, clase = '') {
  const img = document.createElement('img');
  img.alt = '';
  img.className = ('ampliable ' + clase).trim();
  img.loading = 'lazy';
  img.src = urlEvidencia(ruta);
  img.addEventListener('click', (e) => { e.stopPropagation(); abrirVisor(img.src, descripcion); });
  return img;
}

export function abrirVisor(src, texto, { clip = null } = {}) {
  const img = $('visorImg');
  const video = $('visorVideo');
  $('visorSinFormato').hidden = true;
  if (clip) {
    img.style.display = 'none';
    img.removeAttribute('src');
    video.style.display = 'block';
    video.src = urlEvidencia(clip);
    $('visorDescarga').href = video.src;
    $('visorDescarga').download = String(clip).split('/').pop();
    video.play().catch(() => {});
  } else {
    video.pause();
    video.removeAttribute('src');
    video.style.display = 'none';
    img.style.display = 'block';
    img.src = src;
  }
  $('visorTexto').textContent = texto || '';
  $('visor').style.display = 'grid';
}

export function cerrarVisor() {
  $('visor').style.display = 'none';
  $('visorImg').removeAttribute('src');
  const video = $('visorVideo');
  video.pause();
  video.removeAttribute('src');
  video.load();
}

/* Un clip H.264 en un Chromium sin codecs propietarios (o un WebM en un
 * Safari viejo) no se reproduce: en vez de un cuadro negro, se ofrece
 * descargarlo. */
$('visorVideo').addEventListener('error', () => {
  const video = $('visorVideo');
  if (!video.getAttribute('src')) return;
  video.style.display = 'none';
  $('visorSinFormato').hidden = false;
});

accion('cerrar-visor', (el, e) => {
  // Un clic en los controles del video (o en el enlace de descarga) no
  // debe cerrar el visor.
  if (e.target.closest('video, .visor-aviso')) return;
  cerrarVisor();
});
accion('ver-clip', (el) => abrirVisor(null, el.dataset.descripcion || '', { clip: el.dataset.clip }));

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') cerrarVisor();
});
