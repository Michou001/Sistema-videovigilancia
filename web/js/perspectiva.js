/* Ensayo con video real: toma una ventana de 30 s y mide las cajas que
 * produjo la API. La distancia la marca la persona en el piso; no se deduce
 * de la imagen. Las fotos y los valores OCR no se copian al resultado. */

import { $, accion, api, escapar, escuchar, estado } from './nucleo.js';

const DURACION_S = 30;
const UMBRAL = { face: 100, plate: 72 };
let ensayando = false;

function poblarCamaras() {
  const selector = $('ensayoCamara');
  const elegido = selector.value;
  const ids = estado.camaras.map((c) => c.camera_id);
  if (selector.options.length === ids.length && ids.every((id, i) => selector.options[i].value === id)) return;
  selector.replaceChildren(...ids.map((id) => new Option(id, id)));
  if (ids.includes(elegido)) selector.value = elegido;
}

function percentil(valores, fraccion) {
  const ordenados = [...valores].sort((a, b) => a - b);
  return ordenados[Math.round((ordenados.length - 1) * fraccion)];
}

escuchar('stats', poblarCamaras);

accion('ensayar-perspectiva', async (boton) => {
  if (ensayando) return;
  const camara = $('ensayoCamara').value;
  const tipo = $('ensayoTipo').value;
  const distancia = Number($('ensayoDistancia').value);
  const salida = $('resultadoEnsayoPerspectiva');
  if (!camara || !['face', 'plate'].includes(tipo) || !Number.isFinite(distancia)
      || distancia < 0.5 || distancia > 30) {
    salida.textContent = 'Elige una cámara y una distancia horizontal válida.';
    return;
  }
  ensayando = true;
  boton.disabled = true;
  const desde = new Date();
  let restantes = DURACION_S;
  salida.textContent = `${camara}: ${tipo === 'face' ? 'rostro' : 'placa'} a ${distancia} m. Haz tres pasadas; quedan ${restantes} s.`;
  const reloj = setInterval(() => {
    restantes -= 1;
    if (restantes > 0) salida.textContent = `Haz tres pasadas y sal del cuadro entre ellas; quedan ${restantes} s.`;
  }, 1000);
  try {
    await new Promise((resolver) => setTimeout(resolver, DURACION_S * 1000));
    clearInterval(reloj);
    const hasta = new Date();
    const consulta = new URLSearchParams({ camera_id: camara, tipo,
      desde: desde.toISOString(), hasta: hasta.toISOString(), limite: '1000' });
    const eventos = await api(`/api/events?${consulta}`);
    const anchos = eventos.map((e) => e.bbox_x2 - e.bbox_x1)
      .filter((ancho) => Number.isFinite(ancho) && ancho > 0);
    if (!anchos.length) {
      salida.textContent = `No hubo ${tipo === 'face' ? 'rostros' : 'placas'} medibles en ${camara} a ${distancia} m. Revisa encuadre y repite.`;
      return;
    }
    const margen = anchos.filter((ancho) => ancho >= UMBRAL[tipo]).length;
    const etiqueta = tipo === 'face' ? 'Rostro' : 'Placa';
    salida.innerHTML = `<strong>${escapar(camara)} · ${etiqueta} · ${distancia} m declarados</strong><br>
      ${eventos.length} evento(s), ${anchos.length} con caja medible. Ancho: p10
      <strong>${percentil(anchos, 0.1)} px</strong>, mediana
      <strong>${percentil(anchos, 0.5)} px</strong>. Con margen de instalación
      (≥ ${UMBRAL[tipo]} px): <strong>${margen}/${anchos.length}</strong>.
      ${eventos.length < 3 ? 'Repite hasta tener al menos tres pasadas separadas.' : ''}
      ${tipo === 'plate' ? 'Comprueba además la lectura correcta en Registro.' : 'Comprueba que se vea la cara, no solo la coronilla.'}`;
  } catch (err) {
    salida.textContent = `No se pudo medir: ${err.message}`;
  } finally {
    clearInterval(reloj);
    ensayando = false;
    boton.disabled = false;
  }
});
