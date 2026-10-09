/* Apartado de Monitoreo: camaras en vivo y detecciones conforme entran.
 *
 * Es la pantalla de guardia: se mira, no se opera. El video cuesta ancho de
 * banda, asi que los flujos solo viven mientras este apartado esta a la vista
 * (al cambiar de apartado o esconder la pestana se cierran y el worker deja
 * de codificar y subir frames).
 */

import {
  $, accion, api, colorTexto, emitir, escapar, estado, escuchar, fechaHora, hora, iconoTag, iconos,
  NOMBRES, nombreCamara, puede, valorLegible,
} from './nucleo.js';
import { miniatura, abrirVisor, urlEvidencia } from './evidencia.js';
import { estadoCamara, nombreFuncion } from './salud_camaras.js';
import { abrirWebRTC, Superposicion } from './webrtc.js';

/* camera_id -> { el, img, cuerpo, transmitiendo, capa, pc, sup, sinWebrtc } */
const recuadros = new Map();
let temporizador = null;
let configVideo = null;   // { modo: 'mjpeg' | 'webrtc', go2rtc }

async function obtenerConfigVideo() {
  if (!configVideo) {
    try {
      configVideo = await api('/api/preview/config');
    } catch {
      return { modo: 'mjpeg' };
    }
  }
  return configVideo;
}

const PLACEHOLDER_SIN_SENAL =
  '<div class="sinsenal"><span class="icono"><i data-lucide="camera-off"></i></span>Sin señal</div>';
const PLACEHOLDER_PAUSA =
  '<div class="sinsenal"><span class="icono"><i data-lucide="pause"></i></span>Video en pausa</div>';

export function iniciarCamaras() {
  refrescarCamaras();
  clearInterval(temporizador);
  // 5 s: es lo que tarda en aparecer una camara que acaba de arrancar. Mas
  // frecuente no aporta: el worker tarda hasta 2 s en enterarse de que hay
  // alguien mirando.
  temporizador = setInterval(refrescarCamaras, 5000);
}

export function detenerCamaras() {
  clearInterval(temporizador);
  temporizador = null;
  recuadros.forEach((r) => {
    if (r.transmitiendo) {
      detenerFlujo(r);
      r.cuerpo.innerHTML = PLACEHOLDER_PAUSA;
    }
  });
  iconos();
}

function detenerFlujo(r) {
  if (!r.transmitiendo) return;
  // removeAttribute y no src='': una cadena vacia hace que el navegador pida
  // la propia pagina como imagen. Quitar el atributo aborta la conexion.
  r.img.removeAttribute('src');
  if (r.pc) { r.pc.close(); r.pc = null; }
  if (r.sup) { r.sup.cerrar(); r.sup = null; }
  if (r.video) { r.video.srcObject = null; r.video = null; }
  r.transmitiendo = false;
  emitir('flujo-detenido', r.id);
}

export async function refrescarCamaras() {
  let enVivo = [];
  const cfg = await obtenerConfigVideo();
  try {
    enVivo = await api('/api/preview/camaras');
  } catch {
    emitir('salud-video', null);
    return;   // sin red: se conserva lo que ya esta pintado
  }

  const porId = new Map(enVivo.map((c) => [c.camera_id, c]));

  // La lista que se pinta es la union de las camaras registradas en la BD y
  // las que estan mandando video. Asi una camara que se cae no desaparece de
  // la pantalla: se queda en su sitio con "sin señal", que es justo lo que el
  // operador necesita saber.
  const ids = [...new Set([
    ...estado.camaras.map((c) => c.camera_id),
    ...porId.keys(),
  ])].sort();

  const resumen = [];
  for (const id of ids) {
    const vivo = porId.get(id);
    const r = recuadros.get(id) || crearRecuadro(id);
    const meta = estado.camaras.find((c) => c.camera_id === id);

    r.el.querySelector('.nom').textContent = (meta && meta.name) || id;
    r.el.querySelector('.ubicacion').textContent = (meta && meta.location) || '';
    const saludCam = estadoCamara(meta, vivo, r.errorVideo);
    r.el.dataset.estado = saludCam.clave;
    r.el.querySelector('.estado-camara').textContent = saludCam.nombre;
    r.el.querySelector('.rol-camara').textContent = nombreFuncion(meta && meta.funcion, meta && meta.detectores);
    r.el.querySelector('.punto').className = 'punto ' +
      (saludCam.clave === 'en-linea' ? 'on' :
        ['reconectando', 'error-video'].includes(saludCam.clave) ? 'espera' :
          saludCam.clave === 'sin-senal' ? 'off' : '');
    resumen.push({ id, ...saludCam });
    r.el.querySelector('.fps').textContent = vivo ? `${vivo.fps} fps video` : '';
    // Salud reportada por el latido del worker: fps a los que de verdad esta
    // detectando y cuantas veces tuvo que reconectar con la camara.
    const salud = [];
    if (meta && meta.fps != null) salud.push(`${meta.fps} fps análisis`);
    if (meta && meta.reconexiones) salud.push(`${meta.reconexiones} reconex.`);
    r.el.querySelector('.salud').textContent = salud.join(' · ');

    if (vivo && !['sin-senal', 'deshabilitada', 'reconectando'].includes(saludCam.clave)) arrancarFlujo(r, cfg);
    else if (r.transmitiendo) { detenerFlujo(r); r.cuerpo.innerHTML = PLACEHOLDER_SIN_SENAL; }
    if (!r.transmitiendo) {
      const texto = r.cuerpo.querySelector('.sinsenal');
      if (texto) texto.textContent = saludCam.nombre;
    }
  }

  // Camaras que ya no existen ni en la BD ni transmitiendo.
  for (const [id, r] of recuadros) {
    if (!ids.includes(id)) { detenerFlujo(r); r.el.remove(); recuadros.delete(id); }
  }

  emitir('salud-video', resumen);
  $('rejilla').classList.toggle('una', ids.length === 1);
  $('sinCamaras').style.display = ids.length ? 'none' : 'block';
  $('contadorCamaras').textContent = ids.length ? `${resumen.filter(c => c.clave === 'en-linea').length}/${ids.length} en vivo` : '';
  iconos();
}

function crearRecuadro(id) {
  const el = document.createElement('div');
  el.className = 'camara';
  el.dataset.camara = id;
  const admin = puede('admin');
  el.innerHTML = `
    <div class="camara-cab">
      <span class="punto"></span>
      <span class="nom">${escapar(id)}</span>
      <span class="ubicacion"></span>
      ${admin ? `
        <button class="icono-cam" title="Nombre y ubicación" data-accion="editar-camara"
                data-camara="${escapar(id)}"><i data-lucide="pencil"></i></button>
        <button class="icono-cam" title="Zonas y reglas" data-accion="abrir-zonas"
                data-camara="${escapar(id)}"><i data-lucide="square-dashed"></i></button>` : ''}
      <span class="crece"></span>
      <span class="salud"></span>
      <span class="fps"></span>
      <button class="icono-cam ampliar-camara" title="Ampliar esta cámara" aria-label="Ampliar ${escapar(id)}" data-accion="ampliar-camara" data-camara="${escapar(id)}"><i data-lucide="maximize-2"></i></button>
    </div>
    <div class="camara-contexto"><span class="rol-camara"></span><span class="estado-camara" role="status">Comprobando</span></div>
    <div class="camara-video">${PLACEHOLDER_SIN_SENAL}</div>
    <div class="camara-evidencia"><span class="ultimo-evento">Sin capturas recientes de esta cámara</span><button class="sec" data-accion="evidencia-camara" data-camara="${escapar(id)}" disabled>Ver evidencia</button></div>`;
  $('rejilla').append(el);

  const r = { id, el, img: new Image(), cuerpo: el.querySelector('.camara-video'),
              transmitiendo: false, capa: null };
  r.img.alt = 'Cámara ' + id;
  // Si el flujo no arranca (sesion caducada, API reiniciada), se marca como
  // detenido para que el refresco siguiente lo vuelva a intentar en vez de
  // dejar el recuadro con la imagen rota hasta que alguien recargue.
  r.img.onerror = () => {
    if (!r.transmitiendo) return;
    r.errorVideo = true;
    r.el.querySelector('.estado-camara').textContent = 'Reconectando video';
    detenerFlujo(r);
    r.cuerpo.innerHTML = PLACEHOLDER_SIN_SENAL;
    iconos();
  };
  r.img.onload = () => { r.errorVideo = false; };
  recuadros.set(id, r);
  const ultimo = ultimosPorCamara.get(id);
  if (ultimo) ponerUltimo(r, ultimo);
  return r;
}

function capaUltima(r) {
  // Capa de la ultima deteccion; se llena en cuanto entra un evento.
  const capa = document.createElement('div');
  capa.className = 'ultima';
  r.cuerpo.append(capa);
  r.capa = capa;
}

function arrancarFlujo(r, cfg = { modo: 'mjpeg' }) {
  if (r.transmitiendo) return;
  if (cfg.modo === 'webrtc' && cfg.go2rtc && !r.sinWebrtc && window.RTCPeerConnection) {
    arrancarWebRTC(r, cfg.go2rtc);
    return;
  }
  // Sin token en la URL: la sesion viaja en la cookie HttpOnly que puso el
  // login. Antes iba como ?token= y quedaba en el historial del navegador.
  const url = `/api/preview/${encodeURIComponent(r.id)}/live.mjpg?_=${Date.now()}`;
  r.cuerpo.innerHTML = '';
  r.cuerpo.append(r.img);
  capaUltima(r);
  r.img.src = url;
  r.sup = new Superposicion(r.cuerpo, r.img, r.id);
  r.transmitiendo = true;
  emitir('flujo-iniciado', { id: r.id, cuerpo: r.cuerpo, img: r.img });
}

/* Video directo de go2rtc + cajas del worker encima. Si falla (go2rtc no
 * esta, la camara no esta en su configuracion, el navegador no tiene el
 * codec) esta camara se queda en MJPEG hasta recargar la pagina. */
function arrancarWebRTC(r, base) {
  const video = document.createElement('video');
  video.muted = true;
  video.autoplay = true;
  video.playsInline = true;
  r.cuerpo.innerHTML = '';
  r.cuerpo.append(video);
  capaUltima(r);
  r.video = video;
  r.transmitiendo = true;
  abrirWebRTC(base, r.id, video).then((pc) => {
    if (r.video !== video) { pc.close(); return; }     // se detuvo mientras conectaba
    r.pc = pc;
    r.sup = new Superposicion(r.cuerpo, video, r.id);
    const etiqueta = document.createElement('span');
    etiqueta.className = 'modo-video';
    etiqueta.textContent = 'WebRTC';
    r.cuerpo.append(etiqueta);
    pc.addEventListener('connectionstatechange', () => {
      if (pc.connectionState === 'failed' && r.pc === pc) {
        detenerFlujo(r);            // el refresco siguiente la vuelve a conectar
        r.cuerpo.innerHTML = PLACEHOLDER_SIN_SENAL;
        iconos();
      }
    });
    emitir('flujo-iniciado', { id: r.id, cuerpo: r.cuerpo, video });
  }).catch((err) => {
    if (r.video !== video) return;
    console.warn(`WebRTC no disponible para ${r.id}: ${err.message}. Se usa MJPEG.`);
    r.sinWebrtc = true;
    detenerFlujo(r);
    arrancarFlujo(r);
  });
}

export function recuadroDe(id) {
  return recuadros.get(id);
}

function enfocarCamara(id = null) {
  $('rejilla').classList.toggle('enfocada', !!id);
  $('verAmbas').hidden = !id;
  for (const [clave, r] of recuadros) r.el.hidden = !!id && clave !== id;
}
accion('ampliar-camara', el => enfocarCamara(el.dataset.camara));
accion('ver-ambas', () => enfocarCamara());
accion('anotaciones', el => {
  const ocultar = !$('rejilla').classList.contains('sin-anotaciones');
  $('rejilla').classList.toggle('sin-anotaciones', ocultar);
  el.setAttribute('aria-pressed', String(!ocultar));
  el.innerHTML = `<i data-lucide="scan"></i>${ocultar ? 'Mostrar análisis' : 'Análisis visible'}`;
  iconos();
});
escuchar('fin-sesion', () => enfocarCamara());
document.addEventListener('keydown', e => { if (e.key === 'Escape') enfocarCamara(); });

/* Lo ultimo que vio cada camara, de lo ya cargado: al recargar la pagina el
 * recuadro no debe decir "esperando" si el sistema ya tiene capturas suyas. */
const ultimosPorCamara = new Map();

function ponerUltimo(r, ev) {
  r.ultimoEvento = ev;
  r.el.querySelector('.ultimo-evento').textContent = `Última: ${valorLegible(ev)} · ${fechaHora(ev.ts)}`;
  r.el.querySelector('[data-accion="evidencia-camara"]').disabled = !ev.snapshot_path;
}

/* Marca en el recuadro de la camara lo ultimo que encontro. */
export function marcarEnCamara(ev) {
  if (ev.type !== 'camera') ultimosPorCamara.set(ev.camera_id, ev);
  const r = recuadros.get(ev.camera_id);
  if (!r) return;
  r.el.classList.remove('detecto'); void r.el.offsetWidth; r.el.classList.add('detecto');
  ponerUltimo(r, ev);
  if (!r.capa) return;
  r.capa.innerHTML =
    `<span>${iconoTag(ev.type)}</span>` +
    `<span class="v">${escapar(valorLegible(ev))}</span>` +
    `<span class="c">${hora(ev.ts)}</span>`;
  r.capa.classList.add('visible');
  iconos();

  clearTimeout(r.temporizadorCapa);
  r.temporizadorCapa = setTimeout(() => r.capa.classList.remove('visible'), 8000);

  if (ev.severity === 'critical') {
    r.el.classList.add('destacada');
    clearTimeout(r.temporizadorAlerta);
    r.temporizadorAlerta = setTimeout(() => r.el.classList.remove('destacada'), 20000);
  }
}

/* ------------------------------------------------------------------ */
/* Detecciones en vivo (columna derecha)                               */
/* ------------------------------------------------------------------ */

export function agregarDeteccion(ev, nueva = false) {
  $('sinDetecciones').style.display = 'none';
  const div = document.createElement('div');
  div.className = 'deteccion ' + (ev.severity || 'info') + (nueva ? ' nueva' : '');

  div.innerHTML = `
    <div class="crece">
      <div class="v">${iconoTag(ev.type)} ${escapar(valorLegible(ev))}</div>
      <div class="m">${hora(ev.ts)} · ${escapar(nombreCamara(ev.camera_id))}${colorTexto(ev)}
        ${ev.observations ? '· ' + Number(ev.observations) + ' frames' : ''}</div>
    </div>`;

  const hueco = document.createElement('div');
  hueco.className = 'sinfoto';
  hueco.innerHTML = iconoTag(ev.type);

  if (ev.snapshot_path) {
    const img = miniatura(ev.snapshot_path,
      `${NOMBRES[ev.type] || ev.type} ${valorLegible(ev)} · ${fechaHora(ev.ts)} · ${nombreCamara(ev.camera_id)}`);
    // La captura pudo borrarla la politica de retencion: se cae al icono en
    // vez de dejar la imagen rota del navegador.
    img.onerror = () => img.replaceWith(hueco);
    div.prepend(img);
  } else {
    div.prepend(hueco);
  }

  const lista = $('listaDetecciones');
  if (nueva) lista.prepend(div); else lista.append(div);
  // 40 y no 300 como en la tabla del registro: esta columna es "lo que acaba
  // de pasar". Para mirar hacia atras esta el apartado de Registro.
  while (lista.children.length > 40) lista.lastChild.remove();
  $('contadorDetecciones').textContent = lista.children.length + ' recientes';
  iconos();
}

export async function cargarDetecciones() {
  const eventos = await api('/api/events?limite=30');
  $('listaDetecciones').innerHTML = '';
  eventos.forEach((e) => agregarDeteccion(e));
  // Vienen del mas reciente al mas viejo: el primero de cada camara es el ultimo.
  for (const e of eventos) {
    if (e.type !== 'camera' && !ultimosPorCamara.has(e.camera_id)) ultimosPorCamara.set(e.camera_id, e);
  }
  for (const [id, ev] of ultimosPorCamara) {
    const r = recuadros.get(id);
    if (r && !r.ultimoEvento) ponerUltimo(r, ev);
  }
  $('sinDetecciones').style.display = eventos.length ? 'none' : 'block';
}

accion('editar-camara', (el) => emitir('editar-camara', el.dataset.camara));

accion('evidencia-camara', el => {
  const ev = recuadros.get(el.dataset.camara)?.ultimoEvento;
  if (ev?.snapshot_path) abrirVisor(urlEvidencia(ev.snapshot_path),
    `${nombreCamara(ev.camera_id)} · ${fechaHora(ev.ts)} · ${valorLegible(ev)}`);
});
escuchar('stats', () => { if (temporizador) refrescarCamaras(); });
escuchar('fin-sesion', () => {
  for (const r of recuadros.values()) {
    detenerFlujo(r); clearTimeout(r.temporizadorCapa); clearTimeout(r.temporizadorAlerta); r.el.remove();
  }
  recuadros.clear();
});
