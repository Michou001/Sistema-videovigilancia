/* Canal de alertas en vivo (WebSocket).
 *
 * La sesion viaja en la cookie HttpOnly del login: el navegador la manda sola
 * en el handshake, y el token ya no aparece en la URL.
 */

import { $, emitir, estado } from './nucleo.js';

let ws = null;
let reconexion = null;
let intentos = 0;

export function conectarWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);

  ws.onopen = () => {
    intentos = 0;
    $('puntoWs').className = 'punto on';
    $('textoWs').textContent = 'en vivo';
  };

  ws.onmessage = (e) => {
    let mensaje;
    try { mensaje = JSON.parse(e.data); } catch { return; }
    // Cada tipo de mensaje lo atiende el modulo al que le importa.
    emitir('ws:' + mensaje.type, mensaje.data);
  };

  ws.onclose = () => {
    if (!estado.token) return;
    $('puntoWs').className = 'punto off';
    // Reintento con espera creciente: si el servidor se reinicia, el navegador
    // no debe martillarlo con una conexion por milisegundo.
    const espera = Math.min(30000, 1000 * Math.pow(2, intentos++));
    $('textoWs').textContent = `reconectando en ${Math.round(espera / 1000)}s`;
    clearTimeout(reconexion);
    reconexion = setTimeout(conectarWs, espera);
  };

  ws.onerror = () => ws.close();
}

export function desconectarWs() {
  clearTimeout(reconexion);
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
}
