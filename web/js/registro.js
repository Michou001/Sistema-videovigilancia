/* Apartado de Registro: historico de eventos (busqueda y reporte) y alertas
 * por atender. Es la pantalla de trabajo: se busca, se resuelve, se descarta.
 */

import {
  $, accion, api, colorTexto, descargar, emitir, escapar, ETIQUETAS_SEVERIDAD, fechaHora,
  folio, iconoTag, iconos, NOMBRES, nombreCamara, puede, valorLegible,
} from './nucleo.js';
import { miniatura } from './evidencia.js';

/* ------------------------------------------------------------------ */
/* Eventos                                                             */
/* ------------------------------------------------------------------ */

export function agregarEvento(ev, nuevo = false) {
  $('sinEventos').style.display = 'none';
  const tr = document.createElement('tr');
  if (nuevo) tr.className = 'nuevo';
  const corregido = ev.meta && ev.meta.lectura_original
    ? ` <span class="corregida" title="Leída como ${escapar(ev.meta.lectura_original)}">corregida</span>` : '';
  const revision = ev.meta?.revision_placa;
  const revisada = revision === 'no_es_placa' ? ' <span class="etiqueta warning">No es placa</span>'
    : revision === 'confirmada' ? ' <span class="corregida">Validada</span>' : '';
  tr.innerHTML = `
    <td class="mono" style="color:var(--tenue);white-space:nowrap">${fechaHora(ev.ts)}</td>
    <td>${iconoTag(ev.type)} ${escapar(NOMBRES[ev.type] || ev.type)}</td>
    <td class="mono"><strong>${escapar(valorLegible(ev))}</strong>${corregido}${revisada}</td>
    <td style="color:var(--tenue)">${escapar(nombreCamara(ev.camera_id))}${colorTexto(ev)}</td>
    <td><span class="etiqueta ${escapar(ev.severity)}">${escapar(ETIQUETAS_SEVERIDAD[ev.severity] || ev.severity)}</span></td>
    <td><div class="acciones-fila"></div></td>`;
  const celda = tr.lastElementChild.firstElementChild;
  if (ev.snapshot_path) {
    const img = miniatura(ev.snapshot_path,
      `${NOMBRES[ev.type] || ev.type} ${valorLegible(ev)} · ${fechaHora(ev.ts)} · ${nombreCamara(ev.camera_id)}`,
      'miniatura');
    img.onerror = () => img.remove();
    celda.append(img);
  }
  if (ev.type === 'plate' && ev.event_id && puede('operator')) {
    // El operador corrige una lectura mirando la foto: la busqueda la
    // encuentra, se vuelve a cruzar con la lista negra y queda como dato
    // para reentrenar el OCR con placas mexicanas.
    const b = document.createElement('button');
    b.className = 'icono-fila';
    b.title = 'Revisar o corregir esta lectura';
    b.dataset.accion = 'corregir-placa';
    b.dataset.evento = ev.event_id;
    b.dataset.valor = ev.value;
    b.dataset.foto = ev.snapshot_path || '';
    b.innerHTML = '<i data-lucide="pencil-line"></i>';
    celda.append(b);
  }
  if (ev.similitud != null) {
    // Busqueda por descripcion: que tanto se parece la captura a la frase.
    const s = document.createElement('span');
    s.className = 'similitud';
    s.title = 'Parecido con la descripción buscada';
    s.textContent = Math.round(ev.similitud * 100) / 100;
    celda.prepend(s);
  }
  const tbody = $('tablaEventos');
  if (nuevo) tbody.prepend(tr); else tbody.append(tr);
  while (tbody.children.length > 300) tbody.lastChild.remove();
  $('contadorEventos').textContent = tbody.children.length + ' mostrados';
  iconos();
}

function filtros() {
  const p = new URLSearchParams();
  const texto = $('fTexto').value.trim();
  if (texto) p.set('q', texto);
  if ($('fTipo').value) p.set('tipo', $('fTipo').value);
  if ($('fSeveridad').value) p.set('severidad', $('fSeveridad').value);
  if ($('fCamara').value) p.set('camera_id', $('fCamara').value);
  // Las fechas del formulario son dias LOCALES. Se mandan como instante con
  // zona para que la API compare contra UTC sin correr el dia seis horas.
  if ($('fDesde').value) p.set('desde', new Date($('fDesde').value + 'T00:00:00').toISOString());
  if ($('fHasta').value) p.set('hasta', new Date($('fHasta').value + 'T23:59:59.999').toISOString());
  return p;
}

export function filtroActivo() {
  return [...filtros().keys()].length > 0 || !!$('fDescripcion').value.trim();
}

/* La busqueda por descripcion solo aparece si la API la tiene activada. */
export async function prepararBusquedaSemantica() {
  try {
    const e = await api('/api/busqueda/estado');
    $('campoDescripcion').hidden = e.estado === 'desactivado';
    $('estadoSemantica').textContent = e.estado === 'cargando' ? '(cargando modelo…)'
      : e.estado === 'error' ? '(no disponible)' : '';
  } catch {
    $('campoDescripcion').hidden = true;
  }
}

async function buscarPorDescripcion(frase) {
  const p = filtros();
  p.delete('q');
  p.delete('severidad');
  p.set('q', frase);
  p.set('limite', '60');
  const r = await api('/api/busqueda?' + p.toString());
  $('tablaEventos').innerHTML = '';
  r.resultados.forEach((e) => agregarEvento(e));
  $('sinEventos').textContent = 'Ninguna captura se parece a esa descripción.';
  $('sinEventos').style.display = r.resultados.length ? 'none' : 'block';
  $('contadorEventos').textContent = r.resultados.length + ' parecidos a "' + r.consulta + '"';
  $('filtroActivo').textContent = '· búsqueda por descripción';
}

export async function cargarEventos() {
  const frase = $('fDescripcion').value.trim();
  if (frase.length >= 2) return buscarPorDescripcion(frase);
  const p = filtros();
  const activo = [...p.keys()].length > 0;
  p.set('limite', activo ? '500' : '100');
  const eventos = await api('/api/events?' + p.toString());
  $('tablaEventos').innerHTML = '';
  eventos.forEach((e) => agregarEvento(e));
  $('sinEventos').textContent = activo
    ? 'Ningún evento coincide con la búsqueda.'
    : 'Sin eventos todavía. Inicia el worker para empezar a detectar.';
  $('sinEventos').style.display = eventos.length ? 'none' : 'block';
  $('contadorEventos').textContent = eventos.length + (activo ? ' encontrados' : ' mostrados');
  $('filtroActivo').textContent = activo ? '· búsqueda activa' : '';
}

/* Las camaras del filtro salen de las registradas; se refresca con las stats. */
export function actualizarFiltroCamaras(camaras) {
  const sel = $('fCamara');
  const actual = sel.value;
  sel.innerHTML = '<option value="">Todas</option>' + camaras.map((c) =>
    `<option value="${escapar(c.camera_id)}">${escapar(c.name || c.camera_id)}</option>`).join('');
  sel.value = actual;
}

$('formFiltros').addEventListener('submit', (e) => {
  e.preventDefault();
  cargarEventos().catch((err) => { $('contadorEventos').textContent = err.message; });
});

accion('exportar-eventos', async () => {
  try {
    await descargar('/api/events/export.csv?' + filtros().toString(), 'eventos.csv');
  } catch (err) {
    $('contadorEventos').textContent = err.message;
  }
});

accion('limpiar-filtros', () => {
  $('formFiltros').reset();
  cargarEventos().catch(() => {});
});

/* ------------------------------------------------------------------ */
/* Alertas                                                             */
/* ------------------------------------------------------------------ */

const ESTADOS_ALERTA = { acknowledged: 'atendida', dismissed: 'falso positivo' };
const TIPOS_COINCIDENCIA = {
  exact: 'coincidencia exacta', fuzzy: 'coincidencia aproximada',
  biometric: 'coincidencia biométrica', rule: 'regla',
};

/* Respuestas frecuentes de un centro de monitoreo. Un clic las escribe en la
 * nota; el operador puede completarla. La nota queda como bitacora del turno. */
const RESPUESTAS_RAPIDAS = [
  'Se avisó a patrulla / seguridad',
  'Verificado en video',
  'Sin novedad al revisar',
  'Se canalizó al 911',
];

/* A quien se le pasa el caso. El sistema no llama a nadie por su cuenta: el
 * monitorista canaliza y aqui queda constancia (api/ficha_evidencia.py). */
const DESTINOS = {
  proteccion_universitaria: 'Protección Universitaria',
  '911_c5': '911 / C5 Edomex',
  c4_municipal: 'C4 municipal',
  fiscalia: 'Fiscalía (denuncia)',
  otro: 'Otra instancia',
};

function listaCanalizaciones(lista) {
  if (!lista || !lista.length) return '';
  return lista.map((c) => `<span class="chip-canal" title="${escapar(c.nota || '')}">
      <i data-lucide="send"></i>${escapar(DESTINOS[c.destino] || c.destino)}${c.referencia ? ` · ${escapar(c.referencia)}` : ''}
      <span class="tenue">${escapar(fechaHora(c.ts))} · ${escapar(c.por || '')}</span></span>`).join('');
}

/* Otro dashboard canalizo: solo se rehace la lista, no la tarjeta, para no
 * borrar la nota que este operador quiza esta escribiendo. */
export function actualizarCanalizaciones(a) {
  const caja = $('listaAlertas').querySelector(`[data-alerta="${Number(a.id)}"] .canalizaciones`);
  if (!caja) return;
  caja.innerHTML = listaCanalizaciones(a.canalizaciones);
  iconos();
}

export function agregarAlerta(a, nuevo = false) {
  $('sinAlertas').style.display = 'none';
  const div = document.createElement('div');
  const id = Number(a.id);
  div.className = 'alerta ' + escapar(a.severity) + (a.status && a.status !== 'new' ? ' resuelta' : '');
  div.dataset.alerta = String(id);

  const abierta = (!a.status || a.status === 'new') && a.id;
  const acciones = abierta && puede('operator')
    ? `<div class="acciones">
         <button data-accion="abrir-atencion" data-id="${id}"><i data-lucide="check"></i>Atender</button>
         <button class="sec" data-accion="resolver-alerta" data-id="${id}" data-modo="dismiss"><i data-lucide="x"></i>Falso positivo</button>
       </div>
       <div class="atencion" id="atencion-${id}" hidden>
         <div class="rapidas">
           ${RESPUESTAS_RAPIDAS.map((r) => `<button type="button" class="chip" data-accion="respuesta-rapida" data-id="${id}">${escapar(r)}</button>`).join('')}
         </div>
         <textarea id="nota-${id}" rows="2" maxlength="1000" placeholder="¿Qué se hizo? (a quién se avisó, qué se vio en el video)"></textarea>
         <div class="acciones">
           <button data-accion="resolver-alerta" data-id="${id}" data-modo="acknowledge"><i data-lucide="save"></i>Cerrar alerta</button>
           <button class="sec" data-accion="abrir-atencion" data-id="${id}">Cancelar</button>
         </div>
       </div>` : '';

  const partes = [`<span class="folio">${folio(id)}</span>`, fechaHora(a.ts || a.created_at),
                  escapar(nombreCamara(a.camera_id))];
  if (a.match_kind && a.match_kind !== 'none') {
    let coincidencia = escapar(TIPOS_COINCIDENCIA[a.match_kind] || a.match_kind);
    if (a.match_score != null && a.match_kind !== 'rule') {
      coincidencia += ` ${Math.round(a.match_score * 100)}%`;
    }
    partes.push(coincidencia);
  }
  if (a.status && a.status !== 'new') partes.push(escapar(ESTADOS_ALERTA[a.status] || a.status));

  const clip = botonClip(a);
  const respuesta = a.id && puede('operator')
    ? `<div class="acciones respuesta">
         <button type="button" class="sec chico" data-accion="abrir-canalizar" data-id="${id}"><i data-lucide="send"></i>Canalizar</button>
         <button type="button" class="sec chico" data-accion="ficha-evidencia" data-id="${id}"><i data-lucide="file-archive"></i>Ficha de evidencia</button>
       </div>
       <form class="canalizar" id="canalizar-${id}" data-id="${id}" hidden>
         <select name="destino" aria-label="Instancia">
           ${Object.entries(DESTINOS).map(([k, v]) => `<option value="${k}">${escapar(v)}</option>`).join('')}
         </select>
         <input name="referencia" maxlength="80" placeholder="Folio externo (911, reporte, denuncia)" autocomplete="off">
         <input name="nota" maxlength="500" placeholder="Nota (opcional)" autocomplete="off">
         <div class="acciones">
           <button type="submit"><i data-lucide="send"></i>Registrar</button>
           <button type="button" class="sec" data-accion="abrir-canalizar" data-id="${id}">Cancelar</button>
         </div>
       </form>` : '';

  div.innerHTML = `
    <div class="crece">
      <div class="t">${escapar(a.title)}</div>
      <div class="d">${escapar(a.detail || '')}</div>
      <div class="meta">${partes.join(' · ')}</div>
      ${a.notes ? `<div class="nota">${escapar(a.notes)}${a.acknowledged_by ? ` — ${escapar(a.acknowledged_by)}` : ''}</div>` : ''}
      <div class="canalizaciones">${listaCanalizaciones(a.canalizaciones)}</div>
      ${clip}
      ${acciones}
      ${respuesta}
    </div>`;
  if (a.snapshot_path) {
    const img = miniatura(a.snapshot_path, `${a.title} · ${fechaHora(a.ts || a.created_at)}`);
    img.onerror = () => img.remove();
    div.prepend(img);
  }
  const lista = $('listaAlertas');
  const previa = lista.querySelector(`[data-alerta="${id}"]`);
  if (previa) previa.replaceWith(div);
  else if (nuevo) lista.prepend(div);
  else lista.append(div);
  while (lista.children.length > 60) lista.lastChild.remove();
  iconos();
}

function botonClip(a) {
  return a.clip_path
    ? `<button class="sec chip-clip" data-accion="ver-clip" data-clip="${escapar(a.clip_path)}"
               data-descripcion="${escapar(a.title)} · ${escapar(fechaHora(a.ts || a.created_at))}">
         <i data-lucide="film"></i>Ver clip</button>` : '';
}

/* El clip llega unos segundos despues que la alerta. Solo se agrega el boton:
 * rehacer la tarjeta borraria la nota que el operador quiza ya esta
 * escribiendo en ella. */
export function actualizarClip(a) {
  const tarjeta = $('listaAlertas').querySelector(`[data-alerta="${Number(a.id)}"]`);
  if (!tarjeta || !a.clip_path) return;
  tarjeta.querySelector('.chip-clip')?.remove();
  const plantilla = document.createElement('template');
  plantilla.innerHTML = botonClip(a).trim();
  const contenedor = tarjeta.querySelector('.crece');
  const antes = contenedor.querySelector(':scope > .acciones, :scope > .atencion');
  contenedor.insertBefore(plantilla.content.firstChild, antes);
  iconos();
}

export async function cargarAlertas() {
  const alertas = await api('/api/alerts?limite=50');
  $('listaAlertas').innerHTML = '';
  alertas.forEach((a) => agregarAlerta(a));
  $('sinAlertas').style.display = alertas.length ? 'none' : 'block';
}

accion('abrir-atencion', (el) => {
  const caja = $('atencion-' + el.dataset.id);
  if (!caja) return;
  caja.hidden = !caja.hidden;
  if (!caja.hidden) $('nota-' + el.dataset.id).focus();
});

accion('respuesta-rapida', (el) => {
  const campo = $('nota-' + el.dataset.id);
  const texto = el.textContent;
  campo.value = campo.value ? campo.value.trim() + '. ' + texto : texto;
  campo.focus();
});

accion('abrir-canalizar', (el) => {
  const form = $('canalizar-' + el.dataset.id);
  if (!form) return;
  form.hidden = !form.hidden;
  if (!form.hidden) form.elements.referencia.focus();
});

$('listaAlertas').addEventListener('submit', async (e) => {
  const form = e.target.closest('form.canalizar');
  if (!form) return;
  e.preventDefault();
  const boton = form.querySelector('button[type="submit"]');
  boton.disabled = true;
  try {
    const a = await api(`/api/alerts/${form.dataset.id}/canalizar`, {
      method: 'POST',
      body: JSON.stringify({
        destino: form.elements.destino.value,
        referencia: form.elements.referencia.value.trim() || null,
        nota: form.elements.nota.value.trim() || null,
      }),
    });
    actualizarCanalizaciones(a);
    form.reset();
    form.hidden = true;
  } catch (err) {
    alert(err.message);
  } finally {
    boton.disabled = false;
  }
});

accion('ficha-evidencia', async (el) => {
  el.disabled = true;
  try {
    await descargar(`/api/alerts/${el.dataset.id}/ficha.zip`, `${folio(Number(el.dataset.id))}-evidencia.zip`);
  } catch (err) {
    alert(err.message);
  } finally {
    el.disabled = false;
  }
});

accion('resolver-alerta', async (el) => {
  const id = el.dataset.id;
  const modo = el.dataset.modo;
  const campo = $('nota-' + id);
  const nota = campo ? campo.value.trim() : '';
  el.disabled = true;
  try {
    await api(`/api/alerts/${id}/resolver`, {
      method: 'POST',
      body: JSON.stringify({ accion: modo, motivo: modo === 'dismiss' ? 'falso positivo' : null,
                             nota: nota || null }),
    });
  } catch (err) {
    alert(err.message);
    el.disabled = false;
    return;
  }
  await cargarAlertas();
  emitir('pedir-stats');
});
