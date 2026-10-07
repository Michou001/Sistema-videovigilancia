/* Titulo y descripcion de la cabecera segun el apartado abierto. */
import { $, escuchar } from './nucleo.js';

escuchar('vista', vista => {
  const textos = {
    monitoreo: ['Todo a la vista.', 'Observa tus cámaras. Identifica lo importante. Actúa con evidencia.'],
    registro: ['Cada evento, su contexto.', 'Revisa las capturas, valida las lecturas y atiende las alertas.'],
    mapa: ['Seguridad en su lugar.', 'Ubica las cámaras y encuentra el origen de cada evento.'],
    camaras: ['Tu red, conectada.', 'Administra las cámaras y comprueba su conexión.'],
  }[vista];
  if (textos) { $('tituloVista').textContent = textos[0]; $('descripcionVista').textContent = textos[1]; }
});
