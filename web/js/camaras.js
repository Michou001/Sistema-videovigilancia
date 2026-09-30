/* Alta de camaras (buscar en la red, probar, guardar) y edicion de nombre y
 * ubicacion. Solo administradores. */

import { $, accion, api, emitir, escapar, escuchar, estado, iconos, modal } from './nucleo.js';
import { confirmar } from './avisos.js';

// El ultimo resultado probado, para que "Guardar" no tenga que volver a pedir
// nada: si ya se confirmo que la ruta funciona, guardar es solo escribirla.
let camaraProbada = null;

accion('abrir-camaras', () => modal('modalCamaras', true));
accion('cerrar-camaras', () => modal('modalCamaras', false));

accion('buscar-camaras', async (boton) => {
  boton.disabled = true;
  $('errorCamara').textContent = '';
  $('estadoBusqueda').innerHTML =
    '<div class="cargando"><i data-lucide="loader"></i>Escaneando tu red (~20s)…</div>';
  iconos();

  try {
    const dispositivos = await api('/api/camera-setup/descubrir', { method: 'POST' });
    $('estadoBusqueda').textContent = dispositivos.length
      ? `${dispositivos.length} dispositivo(s) con puertos de cámara abiertos:` : '';
    $('dispositivosLan').innerHTML = dispositivos.length
      ? dispositivos.map((d) => `
          <button type="button" class="dispositivo" data-accion="elegir-dispositivo" data-host="${escapar(d.host)}">
            <i data-lucide="camera"></i>
            <span class="host">${escapar(d.host)}</span>
            <span class="crece"></span>
            <span class="puertos">${d.puertos.map((p) => escapar(p.etiqueta)).join(' · ')}</span>
          </button>`).join('')
      : '<div class="vacio">Nada encontrado. Verifica que la cámara esté en la misma red que esta PC.</div>';
    iconos();
  } catch (err) {
    $('estadoBusqueda').textContent = '';
    $('errorCamara').textContent = err.message;
  } finally {
    boton.disabled = false;
  }
});

accion('elegir-dispositivo', (el) => {
  $('camHost').value = el.dataset.host;
  $('camHost').scrollIntoView({ behavior: 'smooth', block: 'center' });
});

$('formCamara').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('errorCamara').textContent = '';
  $('resultadoCamara').innerHTML = '';
  camaraProbada = null;

  const boton = $('btnProbarCamara');
  const original = boton.innerHTML;
  boton.disabled = true;
  boton.innerHTML = '<i data-lucide="loader"></i>Probando… (unos segundos; hasta 2-3 min si la ruta recomendada no responde)';
  iconos();

  const datos = {
    host: $('camHost').value.trim(),
    user: $('camUser').value.trim() || 'admin',
    password: $('camPassword').value,
  };

  try {
    const r = await api('/api/camera-setup/probar', { method: 'POST', body: JSON.stringify(datos) });
    camaraProbada = { ...datos, ruta: r.ruta, camera_id: $('camId').value.trim() || 'cam-01' };
    const vista = r.preview_b64
      ? `<img src="data:image/jpeg;base64,${r.preview_b64}" alt="Vista previa de la cámara">` : '';
    $('resultadoCamara').innerHTML = `
      <div class="resultado-camara">
        ${vista}
        <div class="datos">
          <div class="ok"><i data-lucide="circle-check"></i>Conexión confirmada</div>
          <dl>
            <dt>Resolución</dt><dd>${Number(r.ancho)}×${Number(r.alto)}</dd>
            <dt>FPS</dt><dd>${escapar(r.fps)}</dd>
            <dt>Ruta</dt><dd>${escapar(r.ruta)}</dd>
          </dl>
          <button type="button" style="margin-top:12px" data-accion="guardar-camara">
            <i data-lucide="save"></i>Guardar esta cámara
          </button>
        </div>
      </div>`;
    iconos();
  } catch (err) {
    $('errorCamara').textContent = err.message;
  } finally {
    boton.disabled = false;
    boton.innerHTML = original;
    iconos();
  }
});

accion('guardar-camara', async () => {
  if (!camaraProbada) return;
  try {
    const r = await api('/api/camera-setup/guardar', { method: 'POST', body: JSON.stringify(camaraProbada) });
    $('resultadoCamara').insertAdjacentHTML('beforeend', `
      <div class="aviso-reinicio">
        <i data-lucide="triangle-alert"></i>
        <span><strong>Guardado en ${escapar(r.archivo)}.</strong> El worker lee su
        configuración al arrancar: (re)inicia el de esta cámara con
        <code class="comando">${escapar(r.comando)}</code></span>
      </div>`);
    iconos();
  } catch (err) {
    $('errorCamara').textContent = err.message;
  }
});

/* --- Nombre, ubicacion y coordenadas de una camara -------------------- */

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
  } catch (err) {
    $('errorEdCam').textContent = err.message;
  }
});
