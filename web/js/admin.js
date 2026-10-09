/* Administracion: usuarios y bitacora de auditoria (solo administradores), y
 * cambio de contrasena propio (cualquier usuario). */

import {
  $, accion, almacen, api, descargar, escapar, estado, fechaCompleta, iconos, modal, NOMBRES_ROL,
} from './nucleo.js';
import { confirmar } from './avisos.js';

let accionesBitacora = null;

accion('abrir-admin', async () => {
  modal('modalAdmin', true);
  await Promise.all([cargarUsuarios(), prepararBitacora()]);
});
accion('cerrar-admin', () => modal('modalAdmin', false));

accion('pestana-admin', (el) => {
  document.querySelectorAll('#modalAdmin .pest').forEach((x) => x.classList.toggle('activa', x === el));
  for (const tab of ['Usuarios', 'Bitacora', 'Datos', 'Notificaciones']) {
    $('tab' + tab).style.display = el.dataset.tab === tab.toLowerCase() ? 'block' : 'none';
  }
  if (el.dataset.tab === 'bitacora') cargarBitacora();
  if (el.dataset.tab === 'notificaciones') {
    $('resultadoPrueba').innerHTML = '';
    cargarNotificaciones().catch((err) => alert(err.message));
  }
});

/* ------------------------------------------------------------------ */
/* Usuarios                                                            */
/* ------------------------------------------------------------------ */

async function cargarUsuarios() {
  const usuarios = await api('/api/users');
  $('tablaUsuarios').innerHTML = usuarios.map((u) => {
    const yo = estado.usuario && u.username === estado.usuario.username;
    return `
    <tr class="${u.active ? '' : 'inactivo'}">
      <td class="mono">${escapar(u.username)}${yo ? ' <span class="tenue">(tú)</span>' : ''}</td>
      <td>${escapar(u.display_name)}</td>
      <td>
        <select data-usuario="${Number(u.id)}" class="rolUsuario" ${yo ? 'disabled' : ''}>
          ${Object.entries(NOMBRES_ROL).map(([rol, nombre]) =>
            `<option value="${rol}" ${rol === u.role ? 'selected' : ''}>${nombre}</option>`).join('')}
        </select>
      </td>
      <td>${u.totp_activo
        ? '<span class="etiqueta ok" title="Verificación en dos pasos activa"><i data-lucide="shield-check"></i>Activa</span>'
        : '<span class="tenue">—</span>'}</td>
      <td class="tenue">${fechaCompleta(u.last_login)}</td>
      <td><div class="acciones-fila">
        <button class="sec chico" data-accion="restablecer-password" data-id="${Number(u.id)}"
                data-usuario="${escapar(u.username)}" title="Restablecer contraseña"><i data-lucide="key-round"></i></button>
        ${u.totp_activo && !yo ? `<button class="sec chico" data-accion="restablecer-2fa" data-id="${Number(u.id)}"
                data-usuario="${escapar(u.username)}" title="Quitar verificación en dos pasos (celular perdido)">
                <i data-lucide="shield-off"></i></button>` : ''}
        ${yo ? '' : `<button class="${u.active ? 'peligro' : 'sec'} chico" data-accion="activar-usuario"
                data-id="${Number(u.id)}" data-activo="${u.active ? '1' : '0'}"
                title="${u.active ? 'Desactivar' : 'Reactivar'}">
                <i data-lucide="${u.active ? 'user-x' : 'user-check'}"></i></button>`}
      </div></td>
    </tr>`;
  }).join('');
  iconos();
}

$('tablaUsuarios').addEventListener('change', async (e) => {
  const sel = e.target.closest('select.rolUsuario');
  if (!sel) return;
  try {
    await api('/api/users/' + sel.dataset.usuario, { method: 'PATCH', body: JSON.stringify({ role: sel.value }) });
    confirmar('Rol actualizado. Sus sesiones abiertas se cerraron.');
  } catch (err) {
    alert(err.message);
  }
  await cargarUsuarios();
});

accion('activar-usuario', async (el) => {
  const activar = el.dataset.activo !== '1';
  if (!activar && !confirm('¿Desactivar a este usuario? Se cerrarán sus sesiones abiertas.')) return;
  try {
    await api('/api/users/' + el.dataset.id, { method: 'PATCH', body: JSON.stringify({ active: activar }) });
  } catch (err) {
    alert(err.message);
  }
  await cargarUsuarios();
});

accion('restablecer-password', async (el) => {
  const nueva = prompt(`Nueva contraseña para ${el.dataset.usuario} (mínimo 10 caracteres):`);
  if (!nueva) return;
  try {
    await api('/api/users/' + el.dataset.id, { method: 'PATCH', body: JSON.stringify({ password: nueva }) });
    confirmar('Contraseña restablecida');
  } catch (err) {
    alert(err.message);
  }
});

accion('restablecer-2fa', async (el) => {
  if (!confirm(`¿Quitar la verificación en dos pasos de ${el.dataset.usuario}? `
    + 'Úsalo solo si perdió el celular y sus códigos de respaldo. Se cerrarán sus sesiones.')) return;
  try {
    await api('/api/users/' + el.dataset.id, { method: 'PATCH', body: JSON.stringify({ restablecer_2fa: true }) });
    confirmar('Verificación en dos pasos restablecida');
  } catch (err) {
    alert(err.message);
  }
  await cargarUsuarios();
});

$('formUsuario').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('errorUsuario').textContent = '';
  try {
    await api('/api/users', {
      method: 'POST',
      body: JSON.stringify({
        username: $('nuUsuario').value.trim(),
        display_name: $('nuNombre').value.trim(),
        role: $('nuRol').value,
        password: $('nuPassword').value,
      }),
    });
    $('formUsuario').reset();
    confirmar('Usuario creado');
    await cargarUsuarios();
  } catch (err) {
    $('errorUsuario').textContent = err.message;
  }
});

/* ------------------------------------------------------------------ */
/* Bitacora                                                            */
/* ------------------------------------------------------------------ */

async function prepararBitacora() {
  if (accionesBitacora) return;
  accionesBitacora = await api('/api/audit/acciones');
  $('bAccion').innerHTML = '<option value="">Todas</option>' + Object.entries(accionesBitacora)
    .map(([clave, nombre]) => `<option value="${escapar(clave)}">${escapar(nombre)}</option>`).join('');
}

function filtrosBitacora() {
  const p = new URLSearchParams();
  if ($('bUsuario').value.trim()) p.set('usuario', $('bUsuario').value.trim());
  if ($('bAccion').value) p.set('accion', $('bAccion').value);
  if ($('bDesde').value) p.set('desde', new Date($('bDesde').value + 'T00:00:00').toISOString());
  if ($('bHasta').value) p.set('hasta', new Date($('bHasta').value + 'T23:59:59.999').toISOString());
  return p;
}

async function cargarBitacora() {
  const p = filtrosBitacora();
  p.set('limite', '300');
  const entradas = await api('/api/audit?' + p.toString());
  $('tablaBitacora').innerHTML = entradas.length ? entradas.map((e) => `
    <tr>
      <td class="mono tenue" style="white-space:nowrap">${fechaCompleta(e.ts)}</td>
      <td class="mono">${escapar(e.usuario || '—')}</td>
      <td>${escapar((accionesBitacora && accionesBitacora[e.accion]) || e.accion)}</td>
      <td class="mono">${escapar(e.objetivo || '')}</td>
      <td class="tenue detalle-bitacora" title="${escapar(e.detalle || '')}">${escapar(e.detalle || '')}</td>
      <td class="mono tenue">${escapar(e.ip || '')}</td>
    </tr>`).join('')
    : '<tr><td colspan="6" class="vacio">Sin entradas con esos filtros.</td></tr>';
}

$('formBitacora').addEventListener('submit', (e) => {
  e.preventDefault();
  cargarBitacora().catch((err) => alert(err.message));
});

accion('exportar-bitacora', () =>
  descargar('/api/audit/export.csv?' + filtrosBitacora().toString(), 'bitacora.csv')
    .catch((err) => alert(err.message)));

/* ------------------------------------------------------------------ */
/* Notificaciones externas                                             */
/* ------------------------------------------------------------------ */

const NOMBRES_SEVERIDAD = { critical: 'solo críticas', warning: 'advertencias y críticas', info: 'todas' };

async function cargarNotificaciones() {
  const r = await api('/api/notificaciones');
  const canales = r.canales.length
    ? r.canales.map((c) => `<div class="canal"><i data-lucide="check-circle-2"></i>${escapar(c.descripcion)}</div>`).join('')
    : '<div class="canal tenue"><i data-lucide="bell-off"></i>Ningún canal configurado.</div>';
  const caida = r.camara_caida_s > 0
    ? `Cámara sin imagen más de ${Number(r.camara_caida_s)} s: se avisa (y al recuperarse).`
    : 'Aviso de cámara caída desactivado (NOTIFY_CAMARA_CAIDA_S=0).';
  $('estadoNotificaciones').innerHTML = `${canales}
    <div class="ayuda">Se notifican: ${escapar(NOMBRES_SEVERIDAD[r.min_severidad] || r.min_severidad)}.
      ${escapar(caida)} Foto en el aviso: ${r.incluir_foto ? 'sí' : 'no'}.
      Enviados desde el arranque: ${Number(r.enviados)}; fallidos: ${Number(r.fallidos)}.</div>`;
  iconos();
}

function pintarPrueba(resultados) {
  $('resultadoPrueba').innerHTML = Object.entries(resultados).map(([canal, res]) => res === 'ok'
    ? `<div class="canal"><i data-lucide="check"></i>${escapar(canal)}: enviado</div>`
    : `<div class="canal error"><i data-lucide="x"></i>${escapar(canal)}: ${escapar(res)}</div>`).join('');
  iconos();
}

accion('probar-notificaciones', async (el) => {
  el.disabled = true;
  $('resultadoPrueba').innerHTML = '<div class="ayuda">Enviando…</div>';
  try {
    const r = await api('/api/notificaciones/prueba', { method: 'POST' });
    pintarPrueba(r.resultados);
    await cargarNotificaciones();
  } catch (err) {
    $('resultadoPrueba').innerHTML = `<div class="canal error">${escapar(err.message)}</div>`;
  } finally {
    el.disabled = false;
  }
});

/* ------------------------------------------------------------------ */
/* Cambio de contrasena propio                                         */
/* ------------------------------------------------------------------ */

accion('abrir-password', () => {
  $('formPassword').reset();
  $('errorPassword').textContent = '';
  modal('modalPassword', true);
  $('pwActual').focus();
});
accion('cerrar-password', () => modal('modalPassword', false));

$('formPassword').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('errorPassword').textContent = '';
  if ($('pwNueva').value !== $('pwConfirma').value) {
    $('errorPassword').textContent = 'La confirmación no coincide.';
    return;
  }
  try {
    const s = await api('/api/auth/password', {
      method: 'POST',
      body: JSON.stringify({ actual: $('pwActual').value, nueva: $('pwNueva').value }),
    });
    // Las demas sesiones se cerraron; esta sigue con el token nuevo.
    estado.token = s.token;
    almacen.guardar('token', s.token);
    modal('modalPassword', false);
    confirmar('Contraseña actualizada. Tus otras sesiones se cerraron.');
  } catch (err) {
    $('errorPassword').textContent = err.message;
  }
});
