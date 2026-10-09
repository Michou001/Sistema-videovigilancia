/* Lista negra: placas y personas. Solo administradores. */

import { $, accion, api, emitir, escapar, iconos, modal } from './nucleo.js';
import { confirmar } from './avisos.js';
import { validarEnVivo } from './placas.js';

validarEnVivo($('nuevaPlaca'), $('ayudaPlaca'), $('placaExtranjera'));

accion('abrir-lista-negra', async () => {
  modal('modal', true);
  await Promise.all([cargarPlacas(), cargarRostros()]);
});
accion('cerrar-lista-negra', () => modal('modal', false));

accion('pestana-lista', (el) => {
  document.querySelectorAll('#modal .pest').forEach((x) => x.classList.toggle('activa', x === el));
  $('tabPlacas').style.display = el.dataset.tab === 'placas' ? 'block' : 'none';
  $('tabRostros').style.display = el.dataset.tab === 'rostros' ? 'block' : 'none';
});

/* --- Placas ---------------------------------------------------------- */

export async function cargarPlacas() {
  const placas = await api('/api/blacklist/plates');
  // El contador de la cabecera (placas + rostros) lo pone /api/stats.
  emitir('pedir-stats');
  if (!$('listaPlacas')) return placas;
  const ahora = Date.now();
  $('listaPlacas').innerHTML = placas.length
    ? placas.map((p) => {
        const vencida = p.expires_at && new Date(p.expires_at).getTime() < ahora;
        const vigencia = !p.expires_at ? ''
          : vencida ? '<span style="color:var(--critico);font-size:11px">vencida</span>'
          : `<span style="color:var(--tenue);font-size:11px">hasta ${new Date(p.expires_at)
              .toLocaleDateString('es-MX', { day: '2-digit', month: 'short', year: 'numeric' })}</span>`;
        const tipo = p.tipo
          ? `<span class="tipo-placa">${escapar(p.tipo)}${p.entidad ? ' · ' + escapar(p.entidad) : ''}</span>` : '';
        return `
        <div class="fila">
          <span class="placa-lista"><strong class="mono">${escapar(p.plate)}</strong>${tipo}</span>
          <span class="crece" style="color:var(--tenue)">${escapar(p.reason)}</span>
          ${vigencia}
          <button class="peligro chico" data-accion="quitar-placa" data-id="${Number(p.id)}"
                  data-placa="${escapar(p.plate)}"><i data-lucide="trash-2"></i>Quitar</button>
        </div>`;
      }).join('')
    : '<div class="vacio">La lista negra está vacía.</div>';
  iconos();
  return placas;
}

$('formPlaca').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('errorPlaca').textContent = '';
  const dias = parseInt($('vigencia').value, 10);
  const vence = dias ? new Date(Date.now() + dias * 86400000).toISOString() : null;
  try {
    const r = await api('/api/blacklist/plates', {
      method: 'POST',
      body: JSON.stringify({ plate: $('nuevaPlaca').value, reason: $('motivo').value, expires_at: vence,
                             extranjera: $('placaExtranjera').checked }),
    });
    $('nuevaPlaca').value = '';
    $('motivo').value = '';
    $('placaExtranjera').checked = false;
    $('ayudaPlaca').textContent = '';
    confirmar(`Placa ${r.plate} agregada`);
    await cargarPlacas();
  } catch (err) {
    $('errorPlaca').textContent = err.message;
  }
});

accion('quitar-placa', async (el) => {
  if (!confirm(`¿Quitar la placa ${el.dataset.placa} de la lista negra?`)) return;
  await api('/api/blacklist/plates/' + el.dataset.id, { method: 'DELETE' });
  await cargarPlacas();
});

/* --- Rostros --------------------------------------------------------- */

async function cargarRostros() {
  const rostros = await api('/api/blacklist/faces');
  $('listaRostros').innerHTML = rostros.length
    ? rostros.map((r) => `
        <div class="fila">
          <strong>${escapar(r.label)}</strong>
          <span class="crece" style="color:var(--tenue)">${escapar(r.reason)}</span>
          <button class="peligro chico" data-accion="quitar-rostro" data-id="${Number(r.id)}"
                  data-nombre="${escapar(r.label)}"><i data-lucide="trash-2"></i>Quitar</button>
        </div>`).join('')
    : '<div class="vacio">Ninguna persona registrada.</div>';
  iconos();
}

$('formRostro').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('errorRostro').textContent = '';
  const archivo = $('fotoRostro').files[0];
  if (!archivo) { $('errorRostro').textContent = 'Falta la foto'; return; }

  const fd = new FormData();
  fd.append('label', $('nombreRostro').value);
  fd.append('reason', $('motivoRostro').value);
  fd.append('legal_basis', $('fundamento').value);
  fd.append('foto', archivo);

  const boton = $('formRostro').querySelector('button[type=submit]');
  boton.disabled = true;
  try {
    await api('/api/blacklist/faces', { method: 'POST', body: fd });
    $('formRostro').reset();
    confirmar('Persona agregada a la lista negra');
    await cargarRostros();
  } catch (err) {
    $('errorRostro').textContent = err.message;
  } finally {
    boton.disabled = false;
  }
});

accion('quitar-rostro', async (el) => {
  if (!confirm(`¿Quitar a ${el.dataset.nombre} de la lista negra?`)) return;
  await api('/api/blacklist/faces/' + el.dataset.id, { method: 'DELETE' });
  await cargarRostros();
});
