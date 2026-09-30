/* Placas: correccion de lecturas por el operador, validacion en vivo de una
 * placa escrita a mano (formato de la NOM, tipo y entidad) y descarga del
 * dataset para reentrenar el OCR. */

import { $, accion, api, descargar, emitir, modal } from './nucleo.js';
import { confirmar } from './avisos.js';

/* Validacion mientras se escribe: dice que tipo de placa es y de donde, o
 * sugiere la correccion ("¿Quisiste decir PDW-123-A?"). Con espera de 300 ms
 * para no pedir una consulta por tecla. */
export function validarEnVivo(entrada, salida, casillaExtranjera) {
  let temporizador = null;
  const revisar = () => {
    clearTimeout(temporizador);
    const texto = entrada.value.trim();
    if (texto.length < 2) { salida.textContent = ''; salida.className = 'ayuda'; return; }
    temporizador = setTimeout(async () => {
      const p = new URLSearchParams({ texto, extranjera: casillaExtranjera && casillaExtranjera.checked });
      try {
        const r = await api('/api/placas/analizar?' + p.toString());
        if (r.valida) {
          salida.className = 'ayuda ok';
          salida.textContent = `${r.legible} · ${r.tipo}${r.entidad ? ' de ' + r.entidad : ''}`
                               + (r.norma === 'NOM-001-SCT-2-2016' ? ' (NOM vigente)' : '');
        } else {
          salida.className = 'ayuda mal';
          salida.textContent = 'No es un formato de placa mexicana.'
                               + (r.sugerencia ? ` ¿Quisiste decir ${r.sugerencia}?` : '');
        }
      } catch { salida.textContent = ''; }
    }, 300);
  };
  entrada.addEventListener('input', revisar);
  if (casillaExtranjera) casillaExtranjera.addEventListener('change', revisar);
  return revisar;
}

/* ------------------------------------------------------------------ */
/* Correccion de una lectura                                           */
/* ------------------------------------------------------------------ */

const revisarCorreccion = validarEnVivo($('corValor'), $('corAyuda'), $('corExtranjera'));

accion('corregir-placa', (el) => {
  $('corEvento').value = el.dataset.evento;
  $('corOriginal').textContent = el.dataset.valor;
  $('corValor').value = el.dataset.valor;
  $('corExtranjera').checked = false;
  $('errorCorreccion').textContent = '';
  modal('modalCorreccion', true);
  $('corValor').select();
  revisarCorreccion();
});
accion('cerrar-correccion', () => modal('modalCorreccion', false));

$('formCorreccion').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('errorCorreccion').textContent = '';
  try {
    const r = await api(`/api/events/${encodeURIComponent($('corEvento').value)}/correccion`, {
      method: 'POST',
      body: JSON.stringify({ valor: $('corValor').value, extranjera: $('corExtranjera').checked }),
    });
    modal('modalCorreccion', false);
    confirmar(r.alerta ? `Corregida a ${r.value}: coincide con la lista negra` : `Lectura corregida a ${r.value}`);
    emitir('lectura-corregida', r);
  } catch (err) {
    $('errorCorreccion').textContent = err.message;
  }
});

/* ------------------------------------------------------------------ */
/* Dataset para reentrenar el OCR                                      */
/* ------------------------------------------------------------------ */

accion('descargar-dataset', async (el) => {
  el.disabled = true;
  $('estadoDataset').textContent = 'Preparando…';
  try {
    const p = new URLSearchParams({ automaticas: $('dsAutomaticas').checked });
    await descargar('/api/dataset/placas.zip?' + p.toString(), 'dataset-placas.zip');
    $('estadoDataset').textContent = 'Descargado. Guárdalo cifrado: son placas reales.';
  } catch (err) {
    $('estadoDataset').textContent = err.message;
  } finally {
    el.disabled = false;
  }
});
