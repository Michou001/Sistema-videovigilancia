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
  for (const tab of ['Usuarios', 'Bitacora']) {
    $('tab' + tab).style.display = el.dataset.tab === tab.toLowerCase() ? 'block' : 'none';
  }
  if (el.dataset.tab === 'bitacora') cargarBitacora();
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
      <td class="tenue">${fechaCompleta(u.last_login)}</td>
      <td><div class="acciones-fila">
        <button class="sec chico" data-accion="restablecer-password" data-id="${Number(u.id)}"
                data-usuario="${escapar(u.username)}" title="Restablecer contraseña"><i data-lucide="key-round"></i></button>
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
