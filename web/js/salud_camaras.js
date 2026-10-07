/* La fuente y el video se verifican por separado para evitar mostrar cuadros viejos como vivos. */
export function estadoCamara(meta, video, errorVideo = false) {
  if (meta?.enabled === false) return { clave: 'deshabilitada', nombre: 'Deshabilitada' };
  const latido = meta?.segundos_sin_senal;
  if (meta && (latido === null || latido >= 60)) return { clave: 'sin-senal', nombre: 'Sin señal' };
  if (meta?.connected === false) return { clave: 'reconectando', nombre: 'Reconectando cámara' };
  if (errorVideo) return { clave: 'error-video', nombre: 'Reconectando video' };
  if (video) return { clave: 'en-linea', nombre: 'En línea' };
  if (meta?.online) return { clave: 'procesando', nombre: 'Conectada · esperando video' };
  return { clave: 'sin-senal', nombre: 'Sin señal' };
}
export function nombreFuncion(funcion) {
  return ({ lpr: 'LPR · acceso vehicular', peatonal: 'Zona peatonal · pose',
    pasillo: 'Pasillo', zona: 'Zona de seguridad', patio: 'Patio',
    estacionamiento: 'Estacionamiento' })[funcion] || 'Rol sin asignar';
}
/* Tiempo observado por el panel; no equivale al instante físico de desconexión. */
export function observarRecuperacion(anterior, actual, ahora) {
  if (!anterior) return { estado: actual, caida: null, recuperacion: null };
  const perdida = ['sin-senal', 'reconectando', 'error-video'];
  let caida = anterior.caida;
  let recuperacion = anterior.recuperacion;
  if (anterior.estado === 'en-linea' && perdida.includes(actual)) caida = ahora;
  if (actual === 'en-linea' && caida !== null) {
    recuperacion = Math.max(0, (ahora - caida) / 1000); caida = null;
  }
  return { estado: actual, caida, recuperacion };
}
