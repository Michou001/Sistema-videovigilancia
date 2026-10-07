/* Apartado Cámaras: catálogo de las registradas y alta en cuatro pasos.
 *
 *   1. Buscar     ONVIF + puertos + huella sin contraseña, o captura manual
 *   2. Diagnóstico marca, modelo, streams (codec, resolución, fps) y vista previa
 *   3. Recomendación qué puede hacer la cámara según altura, distancia y lente
 *   4. Alta       archivo del worker + ficha en la base de datos
 *
 * Cada dato de la ficha se muestra con su fuente (ISAPI, ONVIF, MAC, RTSP o
 * el instalador): el catálogo no afirma nada que la cámara no haya dicho.
 * Solo administradores. */

import { $, accion, api, emitir, escapar, escuchar, estado, iconos, modal } from './nucleo.js';
import { confirmar } from './avisos.js';

const NOMBRE_ESTADO = {
  identificada: 'Identificada', parcial: 'Parcial', manual: 'Manual',
  registrada: 'Registrada', otro: 'Otro equipo',
};
const ICONO_USO = { ok: 'circle-check', limite: 'circle-alert', no: 'circle-x' };

let catalogo = null;        // GET /api/cameras/catalog
let conexion = null;        // cómo se llegó a la cámara: red (con credenciales) o manual
let diagnostico = null;     // POST /api/cameras/probe
let perfil = null;          // stream elegido para analizar
let recomendacion = null;   // POST /api/cameras/recommend
let vistaActiva = false;

const chip = (clase, texto) => `<span class="chip-estado ${clase}">${escapar(texto)}</span>`;
const fuente = (dato) => (dato ? `${escapar(dato.valor)} <span class="fuente">${escapar(dato.fuente)}</span>` : '—');

/* ------------------------------------------------------------------ */
/* Catálogo de las registradas                                          */
/* ------------------------------------------------------------------ */

escuchar('vista', (v) => {
  vistaActiva = v === 'camaras';
  if (vistaActiva) cargarCatalogo();
});
escuchar('stats', () => { if (vistaActiva && catalogo) cargarCatalogo(); });
accion('catalogo-recargar', () => cargarCatalogo());

async function cargarCatalogo() {
  try {
    catalogo = await api('/api/cameras/catalog');
  } catch (err) {
    $('tablaCatalogo').innerHTML = `<tr><td colspan="7" class="error">${escapar(err.message)}</td></tr>`;
    return;
  }
  pintarTabla();
  llenarFormularios();
}

function textoStream(f) {
  const p = f.perfil || {};
  if (!p.ancho) return '<span class="tenue">—</span>';
  const partes = [`${p.ancho}×${p.alto}`];
  if (p.codec) partes.push(escapar(p.codec));
  if (p.fps) partes.push(`${p.fps} fps`);
  return `<span class="mono">${partes.join(' · ')}</span>`;
}

function pintarTabla() {
  const filas = catalogo.camaras;
  $('contadorCatalogo').textContent = filas.length ? `${filas.filter((c) => c.online).length}/${filas.length} en línea` : '';
  $('sinCatalogo').hidden = filas.length > 0;
  $('tablaCatalogo').innerHTML = filas.map((c) => {
    const f = c.ficha || {};
    const equipo = f.modelo || f.fabricante
      ? `${f.fabricante ? escapar(f.fabricante.valor) : ''} ${f.modelo ? `<span class="mono">${escapar(f.modelo.valor)}</span>` : ''}
         <div class="tenue pequeno">${f.modelo ? 'según ' + escapar(f.modelo.fuente) : ''}</div>`
      : `<span class="tenue">${c.tipo_fuente === 'webcam' ? 'Webcam' : c.tipo_fuente === 'archivo' ? 'Video de demostración' : 'Sin ficha'}</span>`;
    const estadoTxt = c.online ? `En línea${c.fps ? ` · ${c.fps} fps` : ''}`
      : c.segundos_sin_senal != null ? 'Sin señal' : 'Sin reportar';
    const botonWorker = !catalogo.puede_iniciar || !c.archivo ? ''
      : c.worker_local
        ? `<button class="sec chico" data-accion="catalogo-detener" data-camara="${escapar(c.camera_id)}" title="Detener el worker">
             <i data-lucide="square"></i>Detener</button>`
        : c.online ? ''
          : `<button class="chico" data-accion="catalogo-iniciar" data-camara="${escapar(c.camera_id)}" title="Iniciar el worker en este equipo">
               <i data-lucide="play"></i>Iniciar</button>`;
    return `<tr>
      <td><span class="punto ${c.online ? 'on' : (c.segundos_sin_senal != null ? 'off' : '')}" title="${escapar(estadoTxt)}"></span></td>
      <td><strong>${escapar(c.name)}</strong>
          <div class="tenue pequeno"><span class="mono">${escapar(c.camera_id)}</span>${c.location ? ' · ' + escapar(c.location) : ''}</div></td>
      <td>${c.funcion_nombre ? escapar(c.funcion_nombre) : '<span class="tenue">—</span>'}</td>
      <td>${equipo}</td>
      <td>${textoStream(f)}</td>
      <td><span class="mono pequeno">${escapar(c.archivo || '—')}</span>
          ${f.estado ? '<div>' + chip(f.estado, NOMBRE_ESTADO[f.estado] || f.estado) + '</div>' : ''}</td>
      <td><div class="acciones-fila">
        ${botonWorker}
        ${c.host ? `<button class="icono-fila" data-accion="catalogo-rediagnosticar" data-host="${escapar(c.host)}" title="Diagnosticar"><i data-lucide="stethoscope"></i></button>` : ''}
        <button class="icono-fila" data-accion="catalogo-editar" data-camara="${escapar(c.camera_id)}" title="Nombre y ubicación"><i data-lucide="pencil"></i></button>
      </div></td>
    </tr>`;
  }).join('');
  iconos();
}

accion('catalogo-editar', (el) => emitir('editar-camara', el.dataset.camara));

accion('catalogo-iniciar', async (el) => {
  el.disabled = true;
  try {
    const r = await api(`/api/cameras/${encodeURIComponent(el.dataset.camara)}/worker`, { method: 'POST' });
    confirmar(r.mensaje);
    setTimeout(cargarCatalogo, 1500);
  } catch (err) {
    alert(err.message);
    el.disabled = false;
  }
});

accion('catalogo-detener', async (el) => {
  if (!window.confirm('¿Detener el worker de esta cámara? Dejará de analizar video y, pasado el tiempo configurado, se avisará como cámara caída.')) return;
  el.disabled = true;
  try {
    await api(`/api/cameras/${encodeURIComponent(el.dataset.camara)}/worker`, { method: 'DELETE' });
    confirmar('Worker detenido');
    cargarCatalogo();
  } catch (err) {
    alert(err.message);
    el.disabled = false;
  }
});

accion('catalogo-rediagnosticar', (el) => {
  conexion = { modo: 'red' };
  $('diagHost').value = el.dataset.host;
  irAPaso(2);
  $('diagPassword').focus();
});

function llenarFormularios() {
  const funcion = $('recFuncion').value;
  $('recFuncion').innerHTML = catalogo.funciones
    .map((f) => `<option value="${escapar(f.clave)}">${escapar(f.nombre)}</option>`).join('');
  if (funcion) $('recFuncion').value = funcion;
  const lente = $('recLente').value;
  $('recLente').innerHTML = catalogo.lentes
    .map((l) => `<option value="${l.mm}">${l.mm} mm (${l.hfov}°)</option>`).join('')
    + '<option value="otro">Otro (capturar campo de visión)</option>';
  if (lente) $('recLente').value = lente;
  else $('recLente').value = '4';
  $('manVideo').innerHTML = catalogo.videos_demo.length
    ? catalogo.videos_demo.map((v) => `<option value="${escapar(v)}">${escapar(v.replace('file:', ''))}</option>`).join('')
    : '<option value="">No hay videos en demo/ ni en videos/</option>';
  notaFuncion();
}

/* ------------------------------------------------------------------ */
/* Pasos                                                                */
/* ------------------------------------------------------------------ */

function irAPaso(n) {
  for (let i = 1; i <= 4; i++) $('paso' + i).hidden = i !== n;
  document.querySelectorAll('#pasosAlta li').forEach((li) => {
    const p = Number(li.dataset.paso);
    li.classList.toggle('activo', p === n);
    li.classList.toggle('hecho', p < n);
  });
  if (n === 4) prepararAlta();
  iconos();
}

accion('catalogo-paso', (el) => irAPaso(Number(el.dataset.paso)));

/* --- Paso 1: buscar ------------------------------------------------- */

accion('catalogo-buscar', async (boton) => {
  boton.disabled = true;
  $('formManual').hidden = true;
  $('resultadosDescubrimiento').innerHTML = '';
  $('estadoDescubrimiento').innerHTML =
    '<span class="cargando"><i data-lucide="loader"></i>Buscando en la red (ONVIF y puertos de cámara, unos segundos)…</span>';
  iconos();
  try {
    const r = await api('/api/cameras/discover');
    pintarDescubrimiento(r);
  } catch (err) {
    $('estadoDescubrimiento').innerHTML = `<span class="error">${escapar(err.message)}</span>`;
  } finally {
    boton.disabled = false;
  }
});

function filaDispositivo(d) {
  const protocolos = ['rtsp', 'onvif', 'isapi'].map((p) =>
    `<span class="proto ${d.protocolos[p] ? 'si' : ''}">${p.toUpperCase()}</span>`).join('');
  const identidad = [d.fabricante && fuente(d.fabricante), d.modelo && fuente(d.modelo)].filter(Boolean).join(' · ')
    || '<span class="tenue">Marca y modelo desconocidos</span>';
  const boton = (d.estado === 'registrada' && !d.ip_anterior) || !d.es_camara ? ''
    : `<button class="chico" data-accion="catalogo-elegir" data-host="${escapar(d.host)}"
         data-xaddr="${escapar(d.onvif_xaddr || '')}"><i data-lucide="stethoscope"></i>Diagnosticar</button>`;
  return `<div class="equipo-lan">
      <div class="crece">
        <div class="linea"><span class="mono host">${escapar(d.host)}</span>${chip(d.estado, NOMBRE_ESTADO[d.estado])}${protocolos}</div>
        <div class="pequeno">${identidad}</div>
        <div class="tenue pequeno">${escapar(d.motivo)}${d.mac ? ` · MAC <span class="mono">${escapar(d.mac)}</span>` : ''}</div>
      </div>
      ${boton}
    </div>`;
}

function pintarDescubrimiento(r) {
  const camaras = r.dispositivos.filter((d) => d.es_camara);
  const otros = r.dispositivos.filter((d) => !d.es_camara);
  $('estadoDescubrimiento').textContent =
    `Red ${r.subred} · ${r.duracion_s} s · ${camaras.length} cámara(s) probable(s)` +
    (r.onvif_respondieron ? ` · ${r.onvif_respondieron} respondieron por ONVIF` : '');
  $('resultadosDescubrimiento').innerHTML =
    (camaras.length ? camaras.map(filaDispositivo).join('')
      : `<div class="vacio">No encontré cámaras en ${escapar(r.subred)}. Revisa que estén encendidas y en la misma
         red que este equipo, o agrégala manualmente.</div>`)
    + (otros.length ? `<details class="otros"><summary>Otros equipos en la red (${otros.length})</summary>
         ${otros.map(filaDispositivo).join('')}</details>` : '');
  iconos();
}

accion('catalogo-elegir', (el) => {
  conexion = { modo: 'red', onvif_xaddr: el.dataset.xaddr || null };
  $('diagHost').value = el.dataset.host;
  $('resultadoDiagnostico').innerHTML = '';
  $('errorDiagnostico').textContent = '';
  $('formDiagnostico').hidden = false;
  irAPaso(2);
  $('diagPassword').focus();
});

accion('catalogo-manual', () => {
  $('formManual').hidden = !$('formManual').hidden;
  ajustarManual();
});

function ajustarManual() {
  const tipo = $('manTipo').value;
  const etiquetas = { ip: 'IP de la cámara', rtsp: 'URL RTSP', webcam: 'Número de webcam', archivo: 'Video' };
  const ejemplos = { ip: '192.168.1.64', rtsp: 'rtsp://usuario:contraseña@192.168.1.64:554/ruta', webcam: '0', archivo: '' };
  $('etiquetaManValor').textContent = etiquetas[tipo];
  $('manValor').placeholder = ejemplos[tipo];
  $('manValor').type = tipo === 'rtsp' ? 'password' : 'text';
  $('manValor').hidden = tipo === 'archivo';
  $('manVideo').hidden = tipo !== 'archivo';
}
$('manTipo').addEventListener('change', ajustarManual);

$('formManual').addEventListener('submit', async (e) => {
  e.preventDefault();
  const tipo = $('manTipo').value;
  const valor = $('manValor').value.trim();
  $('resultadoDiagnostico').innerHTML = '';
  $('errorDiagnostico').textContent = '';
  if (tipo === 'ip') {
    conexion = { modo: 'red', onvif_xaddr: null };
    $('diagHost').value = valor;
    $('formDiagnostico').hidden = false;
    irAPaso(2);
    $('diagPassword').focus();
    return;
  }
  const fuenteManual = tipo === 'rtsp' ? valor : tipo === 'webcam' ? `webcam:${valor || '0'}` : $('manVideo').value;
  conexion = { modo: 'manual', fuente: fuenteManual };
  $('formDiagnostico').hidden = true;
  irAPaso(2);
  await diagnosticar({ modo: 'manual', fuente: fuenteManual });
});

/* --- Paso 2: diagnóstico ------------------------------------------- */

$('formDiagnostico').addEventListener('submit', async (e) => {
  e.preventDefault();
  conexion = {
    modo: 'red',
    host: $('diagHost').value.trim(),
    user: $('diagUser').value.trim() || 'admin',
    password: $('diagPassword').value,
    puerto_rtsp: Number($('diagRtsp').value) || 554,
    puerto_http: Number($('diagHttp').value) || 80,
    onvif_xaddr: (conexion && conexion.onvif_xaddr) || null,
  };
  await diagnosticar(conexion);
});

async function diagnosticar(datos) {
  const boton = $('btnDiagnosticar');
  boton.disabled = true;
  $('errorDiagnostico').textContent = '';
  $('resultadoDiagnostico').innerHTML =
    '<div class="cargando"><i data-lucide="loader"></i>Diagnosticando… (identificación, streams y una vista previa)</div>';
  iconos();
  try {
    diagnostico = await api('/api/cameras/probe', { method: 'POST', body: JSON.stringify(datos) });
    perfil = (diagnostico.perfiles || []).find((p) => p.clave === diagnostico.perfil_analisis) || null;
    pintarDiagnostico();
  } catch (err) {
    $('resultadoDiagnostico').innerHTML = '';
    $('errorDiagnostico').textContent = err.message;
  } finally {
    boton.disabled = false;
  }
}

function pintarDiagnostico() {
  const d = diagnostico;
  const disp = d.dispositivo || {};
  const protocolos = Object.entries(d.protocolos || {}).map(([nombre, p]) =>
    `<span class="proto ${p.estado === 'ok' || p.estado === 'abierto' ? 'si' : p.estado === 'credenciales' ? 'mal' : ''}"
       title="${escapar(p.estado)}">${escapar(nombre.toUpperCase())}</span>`).join('');
  const filasPerfil = (d.perfiles || []).map((p) => `
      <tr class="${p.verificado ? '' : 'inactivo'}">
        <td><input type="radio" name="perfilAnalisis" value="${escapar(p.clave)}" ${perfil && p.clave === perfil.clave ? 'checked' : ''}
             ${p.verificado ? '' : 'disabled'} aria-label="Analizar este stream"></td>
        <td>${escapar(p.nombre)}${p.clave === d.perfil_principal ? ' <span class="tenue pequeno">(evidencia)</span>' : ''}</td>
        <td class="mono">${p.ancho ? `${p.ancho}×${p.alto}` : '—'}</td>
        <td>${escapar(p.codec || '—')}</td>
        <td class="mono">${p.fps ?? '—'}</td>
        <td>${p.webrtc && p.webrtc.compatible === true ? '<i data-lucide="check" class="ok-ico"></i>'
             : p.webrtc && p.webrtc.compatible === false ? `<span title="${escapar(p.webrtc.detalle)}"><i data-lucide="triangle-alert" class="aviso-ico"></i></span>` : '—'}</td>
        <td>${p.verificado ? '<i data-lucide="check" class="ok-ico"></i>' : '<i data-lucide="x" class="tenue"></i>'}
            <span class="fuente">${escapar(p.fuente)}</span></td>
      </tr>`).join('');
  const fam = d.familia || {};
  const listo = (d.perfiles || []).some((p) => p.verificado);
  $('resultadoDiagnostico').innerHTML = `
    <div class="diagnostico">
      <div class="cabeza">${chip(d.estado, NOMBRE_ESTADO[d.estado] || d.estado)}
        <span>${escapar(d.motivo)}</span><span class="crece"></span>
        <span class="tenue pequeno">${d.duracion_s} s</span></div>
      <div class="cuerpo">
        ${d.preview_b64 ? `<img class="vista-previa" src="data:image/jpeg;base64,${d.preview_b64}" alt="Vista previa de la cámara">` : ''}
        <dl class="ficha">
          <dt>Fabricante</dt><dd>${fuente(disp.fabricante)}</dd>
          <dt>Modelo</dt><dd>${fuente(disp.modelo)}</dd>
          <dt>Firmware</dt><dd>${fuente(disp.firmware)}</dd>
          <dt>Serie</dt><dd>${fuente(disp.serie)}</dd>
          <dt>Protocolos</dt><dd>${protocolos || '—'}</dd>
          <dt>Familia</dt><dd>${escapar(fam.nombre || '—')} <span class="fuente">catálogo GOSS</span></dd>
        </dl>
      </div>
      ${filasPerfil ? `<table class="perfiles">
        <thead><tr><th></th><th>Stream</th><th>Resolución</th><th>Codec</th><th>FPS</th><th>WebRTC</th><th>Confirmado</th></tr></thead>
        <tbody>${filasPerfil}</tbody></table>
        ${d.perfiles.length > 1 ? '<div class="tenue pequeno">Se analiza el stream marcado (el secundario, de fábrica); el principal se usa para la foto de evidencia.</div>' : ''}` : ''}
      ${(fam.capacidades || []).length ? `<div class="capacidades"><span class="rotulo">Qué hace GOSS con esta familia</span>
        <ul>${fam.capacidades.map((c) => `<li>${escapar(c)}</li>`).join('')}
            ${(fam.notas || []).map((c) => `<li class="nota">${escapar(c)}</li>`).join('')}</ul></div>` : ''}
      ${(d.advertencias || []).length ? `<ul class="advertencias">${d.advertencias.map((a) =>
        `<li><i data-lucide="triangle-alert"></i>${escapar(a)}</li>`).join('')}</ul>` : ''}
      ${d.registrada ? `<div class="aviso-reinicio"><i data-lucide="info"></i><span>Esta cámara ya está dada de alta como
        <strong>${escapar(d.registrada)}</strong>${d.ip_anterior ? ` (antes en ${escapar(d.ip_anterior)})` : ''};
        continuar actualiza su configuración.</span></div>` : ''}
      <div class="botonera">
        <button type="button" data-accion="catalogo-paso" data-paso="3" ${listo ? '' : 'disabled'}>
          Siguiente: recomendación<i data-lucide="arrow-right"></i></button>
      </div>
    </div>`;
  iconos();
  if (perfil && perfil.ancho) $('recAncho').value = perfil.ancho;
}

document.addEventListener('change', (e) => {
  if (e.target.name !== 'perfilAnalisis' || !diagnostico) return;
  perfil = diagnostico.perfiles.find((p) => p.clave === e.target.value) || null;
  if (perfil && perfil.ancho) $('recAncho').value = perfil.ancho;
});

/* --- Paso 3: recomendación ----------------------------------------- */

function funcionActual() {
  return catalogo && catalogo.funciones.find((f) => f.clave === $('recFuncion').value);
}

function notaFuncion() {
  const f = funcionActual();
  $('notaFuncion').textContent = f && f.nota ? f.nota : '';
}
$('recFuncion').addEventListener('change', notaFuncion);
$('recLente').addEventListener('change', () => {
  $('recHfov').hidden = $('recLente').value !== 'otro';
  $('recHfov').required = $('recLente').value === 'otro';
});

$('formRecomendacion').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('errorRecomendacion').textContent = '';
  const otro = $('recLente').value === 'otro';
  const principal = diagnostico && (diagnostico.perfiles || []).find((p) => p.clave === diagnostico.perfil_principal);
  try {
    recomendacion = await api('/api/cameras/recommend', {
      method: 'POST',
      body: JSON.stringify({
        funcion: $('recFuncion').value,
        altura_m: Number($('recAltura').value),
        distancia_m: Number($('recDistancia').value),
        ancho_px: Number($('recAncho').value),
        lente_mm: otro ? null : Number($('recLente').value),
        hfov_grados: otro ? Number($('recHfov').value) : null,
        angulo_horizontal: Number($('recAngulo').value) || 0,
        ancho_px_principal: principal && principal.ancho ? principal.ancho : null,
        codec: perfil ? perfil.codec : null,
      }),
    });
    pintarRecomendacion();
  } catch (err) {
    $('resultadoRecomendacion').innerHTML = '';
    $('errorRecomendacion').textContent = err.message;
  }
});

function pintarRecomendacion() {
  const r = recomendacion;
  const m = r.medidas;
  const usos = r.usos.map((u) => `
      <li class="uso ${u.estado}"><i data-lucide="${ICONO_USO[u.estado]}"></i>
        <div><strong>${escapar(u.nombre)}</strong><div class="tenue pequeno">${escapar(u.detalle)}</div></div></li>`).join('');
  $('resultadoRecomendacion').innerHTML = `
    <div class="recomendacion">
      <div class="medidas">
        <div><span class="n">${r.dori ? escapar(r.dori.nombre) : 'Bajo detección'}</span><span class="e">Nivel DORI</span></div>
        <div><span class="n">${m.px_por_metro}</span><span class="e">px por metro</span></div>
        <div><span class="n">${m.placa_px}</span><span class="e">px de placa</span></div>
        <div><span class="n">${m.rostro_px}</span><span class="e">px de rostro</span></div>
        <div><span class="n">${r.geometria.angulo_vertical}°</span><span class="e">ángulo vertical</span></div>
        <div><span class="n">${r.alcances.placa_lectura_m} m</span><span class="e">alcance de placas</span></div>
      </div>
      <ul class="usos">${usos}</ul>
      ${r.advertencias.length ? `<ul class="advertencias">${r.advertencias.map((a) =>
        `<li><i data-lucide="triangle-alert"></i>${escapar(a)}</li>`).join('')}</ul>` : ''}
      <div class="validaciones"><span class="rotulo">Validar en sitio</span>
        <ul>${r.validaciones.map((v) => `<li><i data-lucide="square"></i>${escapar(v)}</li>`).join('')}</ul></div>
      <div class="tenue pequeno">Reglas: densidad de píxeles IEC 62676-4 (DORI), umbrales medidos del sistema
        (placa ≥ 36 px, rostro ≥ 50 px, el doble para instalar) y campo de visión ${r.entrada.hfov_grados}°
        (${escapar(r.entrada.hfov_fuente)}).</div>
      <div class="botonera">
        <button type="button" data-accion="catalogo-usar-recomendacion">Usar estos detectores<i data-lucide="arrow-right"></i></button>
      </div>
    </div>`;
  iconos();
}

accion('catalogo-usar-recomendacion', () => irAPaso(4));

/* --- Paso 4: alta -------------------------------------------------- */

function prepararAlta() {
  $('errorAlta').textContent = '';
  $('resultadoAlta').innerHTML = '';
  if (diagnostico && diagnostico.registrada) {
    // Ya registrada (por IP, serie o MAC): se actualiza la misma, no una nueva.
    $('altaId').value = diagnostico.registrada;
    const c = catalogo && catalogo.camaras.find((x) => x.camera_id === diagnostico.registrada);
    if (c && !$('altaNombre').value) $('altaNombre').value = c.name;
    if (c && c.location && !$('altaUbicacion').value) $('altaUbicacion').value = c.location;
  } else if (!$('altaId').value || (diagnostico && diagnostico.id_sugerido && !$('altaId').dataset.tocado)) {
    $('altaId').value = (diagnostico && diagnostico.id_sugerido) || 'cam-01';
  }
  const detectores = recomendacion ? recomendacion.detectores : (funcionActual() || {}).detectores || {};
  document.querySelectorAll('#altaDetectores input').forEach((c) => { c.checked = !!detectores[c.value]; });
  const disp = (diagnostico && diagnostico.dispositivo) || {};
  const partes = [];
  if (disp.modelo) partes.push(`${escapar(disp.modelo.valor)}`);
  if (perfil && perfil.ancho) partes.push(`stream ${perfil.ancho}×${perfil.alto}${perfil.codec ? ' ' + escapar(perfil.codec) : ''}`);
  if (funcionActual()) partes.push(escapar(funcionActual().nombre));
  if (recomendacion && recomendacion.dori) partes.push(`nivel ${escapar(recomendacion.dori.nombre)}`);
  $('resumenAlta').innerHTML = partes.length ? `<i data-lucide="clipboard-list"></i>${partes.join(' · ')}` : '';
}
$('altaId').addEventListener('input', () => { $('altaId').dataset.tocado = '1'; });

function fichaParaGuardar() {
  const disp = (diagnostico && diagnostico.dispositivo) || {};
  const ficha = {
    estado: diagnostico ? diagnostico.estado : 'manual',
    familia: diagnostico && diagnostico.familia ? diagnostico.familia.clave : null,
    familia_nombre: diagnostico && diagnostico.familia ? diagnostico.familia.nombre : null,
  };
  for (const k of ['fabricante', 'modelo', 'firmware', 'serie', 'mac']) if (disp[k]) ficha[k] = disp[k];
  if (perfil) {
    ficha.perfil = {
      clave: perfil.clave, nombre: perfil.nombre, codec: perfil.codec, ancho: perfil.ancho,
      alto: perfil.alto, fps: perfil.fps, fuente: perfil.fuente,
    };
  }
  if (recomendacion) {
    ficha.recomendacion = {
      dori: recomendacion.dori ? recomendacion.dori.nombre : null,
      altura_m: recomendacion.entrada.altura_m, distancia_m: recomendacion.entrada.distancia_m,
      hfov_grados: recomendacion.entrada.hfov_grados, px_por_metro: recomendacion.medidas.px_por_metro,
      placa_px: recomendacion.medidas.placa_px, rostro_px: recomendacion.medidas.rostro_px,
      angulo_vertical: recomendacion.geometria.angulo_vertical,
    };
  }
  return ficha;
}

$('formAlta').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('errorAlta').textContent = '';
  if (!conexion || !diagnostico) {
    $('errorAlta').textContent = 'Primero diagnostica la cámara (paso 2).';
    return;
  }
  const detectores = {};
  document.querySelectorAll('#altaDetectores input').forEach((c) => { detectores[c.value] = c.checked; });
  const principal = (diagnostico.perfiles || []).find((p) => p.clave === diagnostico.perfil_principal);
  const cuerpo = {
    camera_id: $('altaId').value.trim(),
    nombre: $('altaNombre').value.trim(),
    ubicacion: $('altaUbicacion').value.trim() || null,
    funcion: recomendacion ? recomendacion.funcion.clave : $('recFuncion').value || null,
    detectores,
    ficha: fichaParaGuardar(),
    modo: conexion.modo,
  };
  if (conexion.modo === 'red') {
    const familia = diagnostico.familia || {};
    Object.assign(cuerpo, {
      host: conexion.host, user: conexion.user, password: conexion.password,
      puerto: (perfil && perfil.puerto) || conexion.puerto_rtsp || 554,
      ruta: perfil ? perfil.ruta : null,
      isapi: !!familia.isapi && (diagnostico.protocolos.isapi || {}).estado === 'ok',
      // Solo canales numericos de ISAPI (101, 201...): la foto HD se pide por canal.
      canal_principal: principal && /^\d+$/.test(principal.clave) ? principal.clave : null,
    });
  } else {
    cuerpo.fuente = conexion.fuente;
  }
  try {
    const r = await api('/api/cameras/register', { method: 'POST', body: JSON.stringify(cuerpo) });
    $('resultadoAlta').innerHTML = `
      <div class="alta-lista">
        <div class="ok"><i data-lucide="circle-check"></i>Cámara <strong>${escapar(r.camera_id)}</strong> dada de alta
          (${escapar(r.archivo)}).</div>
        ${r.puede_iniciar
          ? `<button type="button" data-accion="catalogo-iniciar" data-camara="${escapar(r.camera_id)}">
               <i data-lucide="play"></i>Iniciar worker ahora</button>
             <div class="tenue pequeno">O desde una terminal: <code class="comando">${escapar(r.comando)}</code></div>`
          : `<div class="tenue pequeno">Inicia su worker con <code class="comando">${escapar(r.comando)}</code></div>`}
      </div>`;
    iconos();
    confirmar('Cámara dada de alta');
    // La contraseña no se queda en memoria del navegador más de lo necesario.
    if (conexion.modo === 'red') conexion.password = '';
    $('diagPassword').value = '';
    emitir('pedir-stats');
    cargarCatalogo();
  } catch (err) {
    $('errorAlta').textContent = err.message;
  }
});

/* --- Nombre, ubicación y coordenadas de una cámara ----------------- */

escuchar('editar-camara', (id) => {
  const c = estado.camaras.find((x) => x.camera_id === id) || { camera_id: id };
  $('edCamId').textContent = id;
  $('edCamNombre').value = c.name || id;
  $('edCamUbicacion').value = c.location || '';
  $('edCamLat').value = c.lat ?? '';
  $('edCamLon').value = c.lon ?? '';
  $('errorEdCam').textContent = '';
  modal('modalEditarCamara', true);
  $('edCamNombre').focus();
});

accion('cerrar-editar-camara', () => modal('modalEditarCamara', false));

$('formEditarCamara').addEventListener('submit', async (e) => {
  e.preventDefault();
  const id = $('edCamId').textContent;
  const numero = (v) => (v.trim() === '' ? null : Number(v));
  try {
    await api('/api/cameras/' + encodeURIComponent(id), {
      method: 'PUT',
      body: JSON.stringify({
        name: $('edCamNombre').value.trim(),
        location: $('edCamUbicacion').value.trim() || null,
        lat: numero($('edCamLat').value),
        lon: numero($('edCamLon').value),
      }),
    });
    modal('modalEditarCamara', false);
    confirmar('Cámara actualizada');
    emitir('pedir-stats');
    if (vistaActiva) cargarCatalogo();
  } catch (err) {
    $('errorEdCam').textContent = err.message;
  }
});
