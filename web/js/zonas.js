/* Editor de zonas y reglas de una camara (solo administradores).
 *
 * Se dibuja SOBRE el video en vivo de la camara: el administrador ve la
 * puerta, el estacionamiento o la banqueta y marca la zona encima. Los puntos
 * se guardan normalizados (0..1), asi la zona sirve aunque el worker procese a
 * otra resolucion.
 *
 *   intrusion / merodeo  poligono: clic para agregar puntos, arrastrar para
 *                        mover, "Deshacer punto" para quitar el ultimo.
 *   linea / conteo       dos puntos, A y B. La flecha marca el sentido de
 *                        "entrada" (la derecha de quien camina de A a B).
 *
 * Las mismas reglas de geometria y horario viven en shared/zonas.py: el
 * worker las aplica y la API decide la severidad.
 */

import { $, accion, api, escapar, escuchar, iconos, modal, nombreCamara } from './nucleo.js';
import { confirmar } from './avisos.js';

const TIPOS = {
  intrusion: { nombre: 'Intrusión', ayuda: 'Avisa cuando alguien entra al área (ideal con horario: de noche, fines de semana).', linea: false },
  linea: { nombre: 'Cruce de línea', ayuda: 'Avisa cuando alguien cruza la línea; puedes limitarlo a un sentido.', linea: true },
  merodeo: { nombre: 'Merodeo', ayuda: 'Avisa cuando alguien permanece en el área más de los segundos indicados.', linea: false },
  conteo: { nombre: 'Conteo', ayuda: 'Cuenta personas o vehículos que cruzan la línea. No genera alertas.', linea: true },
};
const COLORES = { intrusion: '#ff4757', linea: '#ffc400', merodeo: '#3c8cff', conteo: '#34d399' };
const DIAS = ['L', 'M', 'M', 'J', 'V', 'S', 'D'];
const NOMBRES_DIA = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo'];
const RADIO = 9;   // px para agarrar un vertice

const ed = {
  camara: null,
  zonas: [],
  editando: null,     // { id|null, tipo, puntos: [[x,y]...] }
  arrastrando: -1,
  observador: null,
};

/* ------------------------------------------------------------------ */
/* Abrir / cerrar                                                      */
/* ------------------------------------------------------------------ */

accion('abrir-zonas', async (el) => {
  ed.camara = el.dataset.camara;
  ed.editando = null;
  $('zonasTitulo').textContent = 'Zonas y reglas — ' + nombreCamara(ed.camara);
  $('zonasError').textContent = '';
  mostrarFormulario(false);
  modal('modalZonas', true);
  const video = $('zonasVideo');
  video.hidden = false;
  $('zonasSinVideo').hidden = true;
  video.onerror = () => { video.hidden = true; $('zonasSinVideo').hidden = false; };
  // La caja toma la proporcion real de la camara (4:3, 16:9...) con el
  // primer cuadro: asi la zona no se dibuja sobre una imagen deformada.
  video.onload = () => {
    if (video.naturalWidth && video.naturalHeight) {
      const escenario = $('zonasEscenario');
      escenario.style.aspectRatio = `${video.naturalWidth} / ${video.naturalHeight}`;
      // Una camara vertical no debe pasar del alto de la pantalla: se limita
      // el ancho para que la altura quede en 68vh sin deformar la imagen.
      escenario.style.maxWidth = `calc(68vh * ${video.naturalWidth / video.naturalHeight})`;
    }
    ajustarLienzo();
  };
  video.src = `/api/preview/${encodeURIComponent(ed.camara)}/live.mjpg?_=${Date.now()}`;
  if (!ed.observador) {
    ed.observador = new ResizeObserver(ajustarLienzo);
    ed.observador.observe($('zonasEscenario'));
  }
  await cargarZonas();
  ajustarLienzo();
});

function cerrarEditor() {
  const video = $('zonasVideo');
  video.onerror = null;
  video.removeAttribute('src');   // corta el flujo MJPEG
  ed.camara = null;
  ed.editando = null;
  modal('modalZonas', false);
}
accion('cerrar-zonas', cerrarEditor);

escuchar('ws:zonas', (d) => {
  if (ed.camara && d && d.camera_id === ed.camara && !ed.editando) cargarZonas().catch(() => {});
});

/* ------------------------------------------------------------------ */
/* Lista de zonas                                                      */
/* ------------------------------------------------------------------ */

async function cargarZonas() {
  ed.zonas = await api('/api/zonas?camera_id=' + encodeURIComponent(ed.camara));
  pintarLista();
  dibujar();
  for (const z of ed.zonas.filter((x) => x.tipo === 'conteo')) {
    api(`/api/zonas/${z.id}/conteo`).then((c) => {
      const el = document.querySelector(`[data-conteo="${z.id}"]`);
      if (el) el.textContent = `${c.total} en 24 h (${c.por_sentido.entrada || 0} entrada · ${c.por_sentido.salida || 0} salida)`;
    }).catch(() => {});
  }
}

function pintarLista() {
  const lista = $('zonasLista');
  if (!ed.zonas.length) {
    lista.innerHTML = '<div class="vacio">Sin zonas. Crea la primera con "Nueva zona".</div>';
    return;
  }
  lista.innerHTML = ed.zonas.map((z) => {
    const estadoTxt = !z.activa ? '<span class="chip-zona apagada">Desactivada</span>'
      : z.tipo === 'conteo' ? '<span class="chip-zona conteo">Contando</span>'
        : z.armada ? '<span class="chip-zona armada">Armada</span>'
          : '<span class="chip-zona">Fuera de horario</span>';
    const extra = z.tipo === 'merodeo' ? ` · ${Number(z.segundos)} s`
      : (z.tipo === 'linea' || z.tipo === 'conteo') && z.direccion !== 'ambas' ? ` · solo ${escapar(z.direccion)}` : '';
    const clases = z.clases.map((c) => (c === 'persona' ? 'personas' : 'vehículos')).join(' y ');
    return `
      <div class="zona-fila" data-zona="${Number(z.id)}">
        <span class="muestra-color" data-tipo="${escapar(z.tipo)}"></span>
        <div class="crece">
          <div class="t">${escapar(z.nombre)} ${estadoTxt}</div>
          <div class="m">${escapar(TIPOS[z.tipo]?.nombre || z.tipo)} de ${clases}${extra}
            · ${escapar(z.horario_texto)}${z.tipo !== 'conteo' && z.severidad === 'critical' ? ' · <b>crítica</b>' : ''}</div>
          ${z.tipo === 'conteo' ? `<div class="m" data-conteo="${Number(z.id)}"></div>` : ''}
        </div>
        <button class="sec chico" data-accion="editar-zona" data-id="${Number(z.id)}" title="Editar"><i data-lucide="pencil"></i></button>
        <button class="sec chico" data-accion="alternar-zona" data-id="${Number(z.id)}"
                title="${z.activa ? 'Desactivar' : 'Activar'}"><i data-lucide="${z.activa ? 'pause' : 'play'}"></i></button>
        <button class="peligro chico" data-accion="borrar-zona" data-id="${Number(z.id)}" title="Borrar"><i data-lucide="trash-2"></i></button>
      </div>`;
  }).join('');
  lista.querySelectorAll('.muestra-color').forEach((m) => { m.style.background = COLORES[m.dataset.tipo] || '#888'; });
  iconos();
}

accion('alternar-zona', async (el) => {
  const z = ed.zonas.find((x) => x.id === Number(el.dataset.id));
  if (!z) return;
  try {
    await api('/api/zonas/' + z.id, { method: 'PATCH', body: JSON.stringify({ activa: !z.activa }) });
    await cargarZonas();
  } catch (err) { $('zonasError').textContent = err.message; }
});

accion('borrar-zona', async (el) => {
  const z = ed.zonas.find((x) => x.id === Number(el.dataset.id));
  if (!z || !confirm(`¿Borrar la zona "${z.nombre}"? Los eventos que ya generó se conservan.`)) return;
  try {
    await api('/api/zonas/' + z.id, { method: 'DELETE' });
    confirmar('Zona borrada');
    await cargarZonas();
  } catch (err) { $('zonasError').textContent = err.message; }
});

/* ------------------------------------------------------------------ */
/* Formulario                                                          */
/* ------------------------------------------------------------------ */

function mostrarFormulario(visible) {
  $('formZona').hidden = !visible;
  $('zonasListaCaja').hidden = visible;
  $('zonasAyudaDibujo').hidden = !visible;
}

accion('nueva-zona', () => {
  ed.editando = { id: null, tipo: 'intrusion', puntos: [] };
  $('formZona').reset();
  $('zNombre').value = '';
  $('zTipo').value = 'intrusion';
  $('zTipo').disabled = false;
  $('zPersona').checked = true;
  $('zVehiculo').checked = false;
  $('zDireccion').value = 'ambas';
  $('zSegundos').value = '60';
  $('zSeveridad').value = 'warning';
  $('zSiempre').checked = true;
  $('zFranjas').innerHTML = '';
  $('zonasError').textContent = '';
  prepararSegunTipo();
  mostrarFormulario(true);
  $('zNombre').focus();
  dibujar();
});

accion('editar-zona', (el) => {
  const z = ed.zonas.find((x) => x.id === Number(el.dataset.id));
  if (!z) return;
  ed.editando = { id: z.id, tipo: z.tipo, puntos: z.puntos.map((p) => [...p]) };
  $('zNombre').value = z.nombre;
  $('zTipo').value = z.tipo;
  $('zTipo').disabled = true;     // cambiar el tipo es otra regla: se borra y se crea
  $('zPersona').checked = z.clases.includes('persona');
  $('zVehiculo').checked = z.clases.includes('vehiculo');
  $('zDireccion').value = z.direccion;
  $('zSegundos').value = z.segundos || 60;
  $('zSeveridad').value = z.severidad;
  $('zSiempre').checked = !z.horario.length;
  $('zFranjas').innerHTML = '';
  z.horario.forEach(agregarFranja);
  $('zonasError').textContent = '';
  prepararSegunTipo();
  mostrarFormulario(true);
  dibujar();
});

accion('cancelar-zona', () => {
  ed.editando = null;
  mostrarFormulario(false);
  dibujar();
});

$('zTipo').addEventListener('change', () => {
  if (!ed.editando) return;
  const antes = TIPOS[ed.editando.tipo].linea;
  ed.editando.tipo = $('zTipo').value;
  if (TIPOS[ed.editando.tipo].linea !== antes) ed.editando.puntos = [];
  prepararSegunTipo();
  dibujar();
});

function prepararSegunTipo() {
  const tipo = $('zTipo').value;
  const t = TIPOS[tipo];
  $('zTipoAyuda').textContent = t.ayuda;
  $('zCampoDireccion').hidden = !t.linea;
  $('zCampoSegundos').hidden = tipo !== 'merodeo';
  $('zCampoSeveridad').hidden = tipo === 'conteo';
  $('zonasAyudaDibujo').textContent = t.linea
    ? 'Haz clic en dos puntos (A y B) sobre el video. La flecha marca el sentido de "entrada".'
    : 'Haz clic sobre el video para marcar las esquinas del área (mínimo 3). Arrastra un punto para moverlo.';
}

$('zSiempre').addEventListener('change', () => {
  if (!$('zSiempre').checked && !$('zFranjas').children.length) {
    agregarFranja({ dias: [0, 1, 2, 3, 4, 5, 6], desde: '22:00', hasta: '06:00' });
  }
  $('zFranjasCaja').hidden = $('zSiempre').checked;
});
accion('agregar-franja', () => agregarFranja({ dias: [0, 1, 2, 3, 4], desde: '20:00', hasta: '07:00' }));
accion('quitar-franja', (el) => el.closest('.franja').remove());
accion('dia-franja', (el) => el.classList.toggle('activo'));

function agregarFranja(f) {
  $('zSiempre').checked = false;
  $('zFranjasCaja').hidden = false;
  const div = document.createElement('div');
  div.className = 'franja';
  div.innerHTML = `
    <div class="dias">${DIAS.map((d, i) => `<button type="button" class="dia${f.dias.includes(i) ? ' activo' : ''}"
      data-accion="dia-franja" data-dia="${i}" title="${NOMBRES_DIA[i]}">${d}</button>`).join('')}</div>
    <input type="time" class="desde" value="${escapar(f.desde)}" required>
    <span class="tenue">a</span>
    <input type="time" class="hasta" value="${escapar(f.hasta)}" required>
    <button type="button" class="sec chico" data-accion="quitar-franja" title="Quitar"><i data-lucide="x"></i></button>`;
  $('zFranjas').append(div);
  iconos();
}

function leerHorario() {
  if ($('zSiempre').checked) return [];
  return [...$('zFranjas').querySelectorAll('.franja')].map((f) => ({
    dias: [...f.querySelectorAll('.dia.activo')].map((d) => Number(d.dataset.dia)),
    desde: f.querySelector('.desde').value,
    hasta: f.querySelector('.hasta').value,
  }));
}

accion('deshacer-punto', () => {
  if (!ed.editando) return;
  ed.editando.puntos.pop();
  dibujar();
});

$('formZona').addEventListener('submit', async (e) => {
  e.preventDefault();
  const z = ed.editando;
  if (!z) return;
  $('zonasError').textContent = '';
  const clases = [$('zPersona').checked && 'persona', $('zVehiculo').checked && 'vehiculo'].filter(Boolean);
  const necesarios = TIPOS[z.tipo].linea ? 2 : 3;
  if (z.puntos.length < necesarios) {
    $('zonasError').textContent = TIPOS[z.tipo].linea
      ? 'Marca los dos extremos de la línea sobre el video.'
      : 'Marca al menos 3 esquinas del área sobre el video.';
    return;
  }
  const datos = {
    nombre: $('zNombre').value.trim(),
    puntos: z.puntos,
    clases,
    direccion: $('zDireccion').value,
    segundos: Number($('zSegundos').value) || null,
    horario: leerHorario(),
    severidad: $('zSeveridad').value,
  };
  try {
    if (z.id) {
      await api('/api/zonas/' + z.id, { method: 'PATCH', body: JSON.stringify(datos) });
    } else {
      await api('/api/zonas', { method: 'POST',
                                body: JSON.stringify({ ...datos, camera_id: ed.camara, tipo: z.tipo }) });
    }
    confirmar(z.id ? 'Zona actualizada' : 'Zona creada. El worker la aplica en unos segundos.');
    ed.editando = null;
    mostrarFormulario(false);
    await cargarZonas();
  } catch (err) {
    $('zonasError').textContent = err.message;
  }
});

/* ------------------------------------------------------------------ */
/* Lienzo                                                              */
/* ------------------------------------------------------------------ */

function ajustarLienzo() {
  const lienzo = $('zonasLienzo');
  const caja = $('zonasEscenario').getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  lienzo.width = Math.max(1, Math.round(caja.width * ratio));
  lienzo.height = Math.max(1, Math.round(caja.height * ratio));
  dibujar();
}

function aPixeles([x, y], w, h) { return [x * w, y * h]; }

function dibujar() {
  const lienzo = $('zonasLienzo');
  const ctx = lienzo.getContext('2d');
  const w = lienzo.width;
  const h = lienzo.height;
  const ratio = window.devicePixelRatio || 1;
  ctx.clearRect(0, 0, w, h);
  ctx.lineWidth = 2 * ratio;
  ctx.font = `${12 * ratio}px system-ui, sans-serif`;
  for (const z of ed.zonas) {
    if (ed.editando && ed.editando.id === z.id) continue;
    trazar(ctx, z.tipo, z.puntos, w, h, ratio, z.activa ? 0.9 : 0.35, z.nombre);
  }
  if (ed.editando) {
    trazar(ctx, ed.editando.tipo, ed.editando.puntos, w, h, ratio, 1, $('zNombre').value || 'Nueva zona', true);
  }
}

function trazar(ctx, tipo, puntos, w, h, ratio, alfa, nombre, editando = false) {
  if (!puntos.length) return;
  const color = COLORES[tipo] || '#ccc';
  const pts = puntos.map((p) => aPixeles(p, w, h));
  ctx.globalAlpha = alfa;
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.setLineDash(editando ? [8 * ratio, 5 * ratio] : []);
  ctx.beginPath();
  pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  if (!TIPOS[tipo].linea && pts.length > 2) {
    ctx.closePath();
    ctx.globalAlpha = alfa * 0.18;
    ctx.fill();
    ctx.globalAlpha = alfa;
  }
  ctx.stroke();
  ctx.setLineDash([]);
  if (TIPOS[tipo].linea && pts.length === 2) flecha(ctx, pts[0], pts[1], ratio);
  if (editando) {
    pts.forEach(([x, y], i) => {
      ctx.beginPath();
      ctx.arc(x, y, 5 * ratio, 0, Math.PI * 2);
      ctx.fillStyle = '#fff';
      ctx.fill();
      ctx.stroke();
      if (TIPOS[tipo].linea) {
        ctx.fillStyle = color;
        ctx.fillText(i ? 'B' : 'A', x + 8 * ratio, y - 8 * ratio);
      }
    });
  }
  ctx.fillStyle = color;
  ctx.fillText(nombre, pts[0][0] + 6 * ratio, pts[0][1] - 8 * ratio);
  ctx.globalAlpha = 1;
}

/* Flecha hacia la "entrada": la derecha de quien camina de A a B. Con la y
 * hacia abajo, esa normal es (-dy, dx). Igual que en shared/zonas.py. */
function flecha(ctx, [ax, ay], [bx, by], ratio) {
  const mx = (ax + bx) / 2;
  const my = (ay + by) / 2;
  const largo = Math.hypot(bx - ax, by - ay) || 1;
  const nx = -(by - ay) / largo;
  const ny = (bx - ax) / largo;
  const L = 28 * ratio;
  const px = mx + nx * L;
  const py = my + ny * L;
  ctx.beginPath();
  ctx.moveTo(mx, my);
  ctx.lineTo(px, py);
  ctx.stroke();
  const ang = Math.atan2(py - my, px - mx);
  ctx.beginPath();
  ctx.moveTo(px, py);
  ctx.lineTo(px - 9 * ratio * Math.cos(ang - 0.5), py - 9 * ratio * Math.sin(ang - 0.5));
  ctx.lineTo(px - 9 * ratio * Math.cos(ang + 0.5), py - 9 * ratio * Math.sin(ang + 0.5));
  ctx.closePath();
  ctx.fill();
  ctx.fillText('entrada', px + 4 * ratio, py + 4 * ratio);
}

function puntoDelEvento(e) {
  const r = $('zonasLienzo').getBoundingClientRect();
  const x = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  const y = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
  return [Math.round(x * 10000) / 10000, Math.round(y * 10000) / 10000];
}

function verticeCercano(e) {
  if (!ed.editando) return -1;
  const r = $('zonasLienzo').getBoundingClientRect();
  return ed.editando.puntos.findIndex(([x, y]) =>
    Math.hypot(x * r.width - (e.clientX - r.left), y * r.height - (e.clientY - r.top)) <= RADIO);
}

$('zonasLienzo').addEventListener('pointerdown', (e) => {
  if (!ed.editando) return;
  e.preventDefault();
  const i = verticeCercano(e);
  if (i >= 0) {
    ed.arrastrando = i;
    $('zonasLienzo').setPointerCapture(e.pointerId);
    return;
  }
  const p = puntoDelEvento(e);
  if (TIPOS[ed.editando.tipo].linea) {
    if (ed.editando.puntos.length >= 2) ed.editando.puntos = [];
    ed.editando.puntos.push(p);
  } else if (ed.editando.puntos.length < 24) {
    ed.editando.puntos.push(p);
  }
  dibujar();
});

$('zonasLienzo').addEventListener('pointermove', (e) => {
  if (ed.arrastrando < 0 || !ed.editando) {
    $('zonasLienzo').style.cursor = ed.editando ? (verticeCercano(e) >= 0 ? 'grab' : 'crosshair') : 'default';
    return;
  }
  ed.editando.puntos[ed.arrastrando] = puntoDelEvento(e);
  dibujar();
});

['pointerup', 'pointercancel'].forEach((t) => $('zonasLienzo').addEventListener(t, () => { ed.arrastrando = -1; }));
$('zNombre').addEventListener('input', dibujar);

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && ed.camara && $('modalZonas').style.display === 'grid') {
    if (ed.editando) { ed.editando = null; mostrarFormulario(false); dibujar(); } else cerrarEditor();
  }
});
