/* Verificacion en dos pasos: alta con QR, codigos de respaldo y baja.
 *
 * El QR lo genera el servidor (data URI SVG): el secreto nunca sale hacia un
 * servicio externo de QR. Los codigos de respaldo solo se ven una vez; aqui se
 * ofrecen para copiar o descargar antes de cerrar. */
import { $, accion, almacen, api, escuchar, estado, iconos, modal } from './nucleo.js';
import { confirmar } from './avisos.js';

const SECCIONES = ['df2Inicio', 'df2Qr', 'df2Respaldo', 'df2Activo'];
let codigosVisibles = [];
let obligatorio = false;
// Se activo porque el rol la exigia: el dashboard no ha cargado todavia.
let recargarAlTerminar = false;

function mostrar(id) {
  SECCIONES.forEach((s) => { $(s).hidden = s !== id; });
  $('error2fa').textContent = '';
  // Mientras se ven los codigos de respaldo, o si el rol la exige y aun no
  // esta activa, no se puede cerrar el modal sin terminar.
  $('cerrar2fa').hidden = id === 'df2Respaldo' || (obligatorio && id !== 'df2Activo');
  const foco = { df2Inicio: 'df2Password', df2Qr: 'df2Codigo', df2Activo: 'df2PassActivo' }[id];
  if (foco) setTimeout(() => $(foco).focus(), 50);
}

export async function abrir2fa() {
  let s;
  try {
    s = await api('/api/auth/2fa');
  } catch (err) {
    alert(err.message);
    return;
  }
  obligatorio = s.exigido && !s.activo;
  $('df2Exigido').hidden = !obligatorio;
  $('df2Desactivar').hidden = s.exigido;
  ['df2Password', 'df2Codigo', 'df2PassActivo', 'df2CodActivo'].forEach((i) => { $(i).value = ''; });
  if (s.activo) {
    $('df2Restantes').textContent = s.respaldos_restantes;
    mostrar('df2Activo');
  } else {
    mostrar('df2Inicio');
  }
  modal('modal2fa', true);
}

accion('abrir-2fa', abrir2fa);
accion('cerrar-2fa', () => { if (!$('cerrar2fa').hidden) modal('modal2fa', false); });

// La API respondio "activa la verificacion en dos pasos" (rol que la exige).
escuchar('requiere-2fa', () => { if ($('modal2fa').style.display !== 'grid') abrir2fa(); });

$('df2Inicio').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    const r = await api('/api/auth/2fa/iniciar', {
      method: 'POST', body: JSON.stringify({ password: $('df2Password').value }),
    });
    $('df2Password').value = '';
    $('df2Imagen').src = r.qr;
    $('df2Secreto').textContent = r.secreto;
    mostrar('df2Qr');
  } catch (err) {
    $('error2fa').textContent = err.message;
  }
});

function pintarRespaldo(codigos) {
  codigosVisibles = codigos;
  $('df2Lista').innerHTML = codigos.map((c) => `<li>${c}</li>`).join('');
  mostrar('df2Respaldo');
}

$('df2Qr').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    const r = await api('/api/auth/2fa/activar', {
      method: 'POST', body: JSON.stringify({ codigo: $('df2Codigo').value }),
    });
    // Las demas sesiones se cerraron; esta sigue con el token nuevo.
    estado.token = r.sesion.token;
    almacen.guardar('token', r.sesion.token);
    estado.usuario = { ...estado.usuario, ...r.sesion, token: undefined };
    $('df2Imagen').removeAttribute('src');
    $('df2Secreto').textContent = '';
    recargarAlTerminar = obligatorio;
    obligatorio = false;
    $('df2Exigido').hidden = true;
    pintarRespaldo(r.codigos_respaldo);
  } catch (err) {
    $('error2fa').textContent = err.message;
  }
});

$('df2Activo').addEventListener('submit', async (e) => {
  e.preventDefault();
  const que = e.submitter ? e.submitter.dataset.que : 'respaldo';
  const datos = JSON.stringify({ password: $('df2PassActivo').value, codigo: $('df2CodActivo').value });
  try {
    if (que === 'desactivar') {
      if (!confirm('¿Desactivar la verificación en dos pasos? Volverás a entrar solo con contraseña.')) return;
      const s = await api('/api/auth/2fa/desactivar', { method: 'POST', body: datos });
      estado.token = s.token;
      almacen.guardar('token', s.token);
      modal('modal2fa', false);
      confirmar('Verificación en dos pasos desactivada. Tus otras sesiones se cerraron.');
    } else {
      const r = await api('/api/auth/2fa/respaldo', { method: 'POST', body: datos });
      pintarRespaldo(r.codigos_respaldo);
    }
  } catch (err) {
    $('error2fa').textContent = err.message;
  }
});

const textoRespaldo = () => 'Códigos de respaldo de GOSS IP (' + (estado.usuario?.username || '') + ')\n'
  + 'Cada uno sirve una sola vez.\n\n' + codigosVisibles.join('\n') + '\n';

accion('copiar-respaldo', async () => {
  try {
    await navigator.clipboard.writeText(textoRespaldo());
    confirmar('Códigos copiados');
  } catch {
    alert('No se pudo copiar. Anótalos o descárgalos.');
  }
});

accion('descargar-respaldo', () => {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([textoRespaldo()], { type: 'text/plain' }));
  a.download = 'goss-ip-codigos-respaldo.txt';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
});

accion('respaldo-guardado', () => {
  codigosVisibles = [];
  $('df2Lista').innerHTML = '';
  modal('modal2fa', false);
  // Con la sesion nueva ya guardada, recargar arranca el dashboard completo.
  if (recargarAlTerminar) { location.reload(); return; }
  confirmar('Verificación en dos pasos lista');
});

iconos();
