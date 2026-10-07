/* Ubicación obtenida del dispositivo; nunca deducida de la IP. */
export function obtenerUbicacion() {
  return new Promise((resolve, reject) => {
    if (!window.isSecureContext || !navigator.geolocation) {
      reject(new Error('Abre GOSS en localhost o HTTPS para usar la ubicación del equipo.'));
      return;
    }
    navigator.geolocation.getCurrentPosition(p => resolve({
      lat: p.coords.latitude, lon: p.coords.longitude,
      precision: p.coords.accuracy, fecha: p.timestamp,
    }), e => reject(new Error({
      1: 'La ubicación está bloqueada. Permítela en el navegador y en Windows, o coloca las cámaras manualmente.',
      2: 'El dispositivo no pudo determinar su ubicación. Puedes colocar las cámaras manualmente.',
      3: 'Se agotó la espera de ubicación. Intenta de nuevo o usa el mapa.',
    }[e.code] || 'No se pudo obtener la ubicación.')),
    { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
  });
}
