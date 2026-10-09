/* Nucleo del dashboard: llamadas a la API, estado de la sesion, formato y
 * acciones delegadas.
 *
 * Sin framework a proposito: son unas cuantas pantallas con listas que se
 * actualizan por WebSocket. Un framework aqui seria mas codigo de andamiaje
 * que de aplicacion, y exigiria un paso de compilacion para desplegar.
 *
 * SIN onclick="" EN EL HTML. Todos los botones declaran data-accion="nombre"
 * y un solo escuchador los despacha (ver `accion()`). Dos razones:
 *   1. La politica de seguridad (CSP) del servidor prohibe JavaScript en
 *      linea: aunque alguien lograra inyectar HTML, no podria ejecutarlo.
 *   2. Antes se interpolaban valores dentro de onclick="editar('${id}')".
 *      Escapar HTML no escapa comillas de JavaScript, y un identificador de
 *      camara con una comilla podia inyectar codigo en la pagina.
 */

export const $ = (id) => document.getElementById(id);

/* localStorage puede lanzar (navegacion privada, almacenamiento bloqueado):
 * el panel tiene que funcionar igual, solo sin recordar preferencias. */
export const almacen = {
  leer(clave, defecto = null) {
    try { return localStorage.getItem(clave) ?? defecto; } catch { return defecto; }
  },
  guardar(clave, valor) {
    try { localStorage.setItem(clave, valor); } catch {}
  },
  borrar(clave) {
    try { localStorage.removeItem(clave); } catch {}
  },
};

export const estado = {
  token: almacen.leer('token', ''),
  usuario: null,
  camaras: [],          // las que existen en la BD (heartbeat), con nombre y salud
};

/* Bus de eventos entre modulos, para no importarse en circulo:
 * 'sesion-expirada', 'stats', 'evento', 'alerta', 'alerta-resuelta'... */
export const bus = new EventTarget();
export const emitir = (tipo, detalle) => bus.dispatchEvent(new CustomEvent(tipo, { detail: detalle }));
export const escuchar = (tipo, fn) => bus.addEventListener(tipo, (e) => fn(e.detail));

/* ------------------------------------------------------------------ */
/* Llamadas a la API                                                    */
/* ------------------------------------------------------------------ */

export async function api(ruta, opciones = {}) {
  const esFormulario = opciones.body instanceof FormData;
  const r = await fetch(ruta, {
    ...opciones,
    headers: {
      // Con FormData NO se pone Content-Type: el navegador agrega el boundary.
      ...(esFormulario ? {} : { 'Content-Type': 'application/json' }),
      ...(estado.token ? { Authorization: 'Bearer ' + estado.token } : {}),
      ...(opciones.headers || {}),
    },
  });
  // Un 401 del login es "contrasena incorrecta", no "sesion expirada".
  if (r.status === 401 && estado.token && !ruta.startsWith('/api/auth/login')) {
    emitir('sesion-expirada');
    throw new Error('Sesión expirada');
  }
  // Su rol exige verificacion en dos pasos y aun no la activo.
  if (r.status === 403 && r.headers.get('X-Requiere-2FA')) emitir('requiere-2fa');
  if (!r.ok) {
    let detalle = 'Error ' + r.status;
    try {
      const cuerpo = await r.json();
      // FastAPI devuelve los errores de validacion como lista de objetos.
      detalle = Array.isArray(cuerpo.detail)
        ? cuerpo.detail.map((d) => d.msg).join('; ')
        : (cuerpo.detail || detalle);
    } catch {}
    throw new Error(detalle);
  }
  if (r.status === 204) return null;
  const tipo = r.headers.get('content-type') || '';
  return tipo.includes('application/json') ? r.json() : r;
}

/* Descarga un archivo que exige sesion (CSV, dataset). Va por fetch y no por
 * un enlace porque la peticion necesita la cabecera Authorization. */
export async function descargar(ruta, nombrePorDefecto) {
  const r = await fetch(ruta, { headers: { Authorization: 'Bearer ' + estado.token } });
  if (r.status === 401) { emitir('sesion-expirada'); throw new Error('Sesión expirada'); }
  if (!r.ok) {
    let detalle = 'Error ' + r.status;
    try { detalle = (await r.json()).detail || detalle; } catch {}
    throw new Error(detalle);
  }
  const nombre = (r.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/);
  const a = document.createElement('a');
  a.href = URL.createObjectURL(await r.blob());
  a.download = nombre ? nombre[1] : nombrePorDefecto;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

/* ------------------------------------------------------------------ */
/* Permisos                                                            */
/* ------------------------------------------------------------------ */

const NIVEL = { viewer: 0, operator: 1, admin: 2 };
export const NOMBRES_ROL = { viewer: 'Consulta', operator: 'Operador', admin: 'Administrador' };

/* Solo decide que se MUESTRA. Quien decide que se PUEDE es la API: un boton
 * oculto no es una medida de seguridad. */
export function puede(minimo) {
  return !!estado.usuario && (NIVEL[estado.usuario.role] ?? -1) >= NIVEL[minimo];
}

/* ------------------------------------------------------------------ */
/* Acciones delegadas                                                  */
/* ------------------------------------------------------------------ */

const acciones = new Map();

/* Registra la funcion que atiende los clics en [data-accion="nombre"]. La
 * funcion recibe el elemento (para leer sus data-*) y el evento. */
export function accion(nombre, fn) {
  acciones.set(nombre, fn);
}

document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-accion]');
  if (!el || el.disabled) return;
  const fn = acciones.get(el.dataset.accion);
  if (!fn) return;
  if (el.tagName === 'A' || el.type === 'submit') e.preventDefault();
  Promise.resolve(fn(el, e)).catch((err) => console.error(el.dataset.accion, err));
});

/* ------------------------------------------------------------------ */
/* Formato                                                             */
/* ------------------------------------------------------------------ */

export function escapar(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  // textContent -> innerHTML escapa < > &, pero no comillas: se escapan aqui
  // para que el resultado tambien sirva dentro de un atributo="".
  return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* Iconos Lucide: convierte los <i data-lucide> recien insertados en SVG. Si
 * el CDN no cargo (red de camaras sin internet), no hace nada. */
export function iconos() {
  try { window.lucide && window.lucide.createIcons(); } catch {}
}

export function hora(iso) {
  return new Date(iso).toLocaleTimeString('es-MX', { hour12: false });
}

/* Hora sola si es de hoy; fecha y hora si no. En el registro conviven eventos
 * de varios dias y "14:02" a secas no dice de cual. */
export function fechaHora(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const hoy = new Date();
  if (d.toDateString() === hoy.toDateString()) return hora(iso);
  return d.toLocaleDateString('es-MX', { day: '2-digit', month: 'short' }) + ' ' + hora(iso);
}

export function fechaCompleta(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('es-MX', {
    day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
  });
}

// Nombres de icono de Lucide, no emoji: se ven iguales en cualquier SO y con
// el mismo trazo tecnico que el resto del panel.
export const ICONOS = {
  plate: 'car', face: 'user-round', weapon: 'shield-alert', anomaly: 'footprints',
  zone: 'square-dashed', camera: 'cctv',
};
export const NOMBRES = {
  plate: 'Placa', face: 'Rostro', weapon: 'Arma', anomaly: 'Movimiento',
  zone: 'Zona', camera: 'Cámara',
};
export const iconoTag = (tipo) => `<i data-lucide="${ICONOS[tipo] || 'circle-dot'}"></i>`;

/* El valor tal como lo lee el operador. En placas y armas es el dato mismo;
 * en el resto el worker manda una etiqueta interna. */
const VALORES = {
  movimiento_subito: 'Movimiento súbito', persona_caida: 'Persona caída', rostro: 'Rostro',
  manos_arriba: 'Manos arriba', posible_agresion: 'Posible agresión', intrusion: 'Intrusión',
  cruce_linea: 'Cruce de línea', merodeo: 'Merodeo', conteo: 'Conteo', sin_senal: 'Cámara sin señal',
  senal_recuperada: 'Cámara recuperada', sabotaje: 'Sabotaje de cámara', perdida_video: 'Pérdida de video',
  deteccion_linea: 'Cruce de línea (cámara)', intrusion_camara: 'Intrusión (cámara)',
  movimiento_camara: 'Movimiento (cámara)',
};
export function valorLegible(ev) {
  const base = VALORES[ev.value] || ev.value;
  const zona = ev.meta && ev.meta.zona;
  return zona ? `${base} · ${zona}` : base;
}

export function colorTexto(ev) {
  const partes = [];
  if (ev.meta && ev.meta.tipo_placa && ev.meta.tipo_placa !== 'Placa extranjera') {
    partes.push(escapar(ev.meta.tipo_placa) + (ev.meta.entidad ? ' de ' + escapar(ev.meta.entidad) : ''));
  }
  if (ev.meta && ev.meta.pais && ev.meta.pais !== 'México') {
    partes.push(ev.meta.pais === 'Extranjera' ? 'placa extranjera' : 'placa de ' + escapar(ev.meta.pais));
  }
  if (ev.meta && ev.meta.color_vehiculo) partes.push('vehículo ' + escapar(ev.meta.color_vehiculo));
  return partes.length ? ' · ' + partes.join(' · ') : '';
}

export const ETIQUETAS_SEVERIDAD = { critical: 'crítico', warning: 'aviso', info: 'info' };

/* Folio legible para referirse a una alerta por radio o en un reporte. */
export const folio = (id) => id ? 'ALR-' + String(id).padStart(6, '0') : '';

/* Nombre que ve el operador: el que se le puso a la camara, o su id. */
export function nombreCamara(id) {
  const c = estado.camaras.find((x) => x.camera_id === id);
  return (c && c.name && c.name !== id) ? c.name : id;
}

/* Muestra u oculta un modal (clase .modal-overlay). */
export function modal(id, visible) {
  const el = $(id);
  if (el) el.style.display = visible ? 'grid' : 'none';
}
