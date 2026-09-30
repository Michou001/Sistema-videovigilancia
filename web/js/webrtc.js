/* Video en vivo por WebRTC desde go2rtc, con las cajas de los detectores
 * dibujadas encima en el navegador.
 *
 * Por que: el MJPEG que arma el worker es comodo pero caro (recodifica cada
 * frame a JPEG) y va a baja resolucion. go2rtc toma el H.264 de la camara y lo
 * entrega por WebRTC SIN recodificar: resolucion completa, menos de medio
 * segundo de retraso y casi nada de CPU. Las cajas (placas, personas, armas,
 * esqueletos) llegan aparte, como datos, por Server-Sent Events.
 *
 * Si go2rtc no responde o el navegador no puede reproducir el codec, la
 * camara vuelve sola al MJPEG de siempre.
 */

const HUESOS = [[5, 6], [5, 7], [7, 9], [6, 8], [8, 10], [5, 11], [6, 12], [11, 12], [11, 13], [13, 15],
  [12, 14], [14, 16], [0, 5], [0, 6]];
const VIGENCIA_MS = 1500;   // cajas mas viejas que esto ya no se dibujan

function esperarIce(pc, maximo = 2000) {
  if (pc.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((listo) => {
    const t = setTimeout(listo, maximo);
    pc.addEventListener('icegatheringstatechange', () => {
      if (pc.iceGatheringState === 'complete') { clearTimeout(t); listo(); }
    });
  });
}

/* Conecta `video` con el stream `camara` de go2rtc. Resuelve con la
 * RTCPeerConnection ya conectada y reproduciendo; rechaza si no se pudo. */
export async function abrirWebRTC(base, camara, video, espera = 8000) {
  const pc = new RTCPeerConnection();
  try {
    // Solo video: el audio de las camaras no se usa (y es un dato personal mas).
    pc.addTransceiver('video', { direction: 'recvonly' });
    const flujo = new MediaStream();
    pc.addEventListener('track', (e) => {
      flujo.addTrack(e.track);
      video.srcObject = flujo;
    });
    await pc.setLocalDescription(await pc.createOffer());
    await esperarIce(pc);
    const r = await fetch(`${base}/api/webrtc?src=${encodeURIComponent(camara)}`, {
      method: 'POST', body: pc.localDescription.sdp, credentials: 'same-origin',
    });
    if (!r.ok) throw new Error(`go2rtc respondió ${r.status}`);
    await pc.setRemoteDescription({ type: 'answer', sdp: await r.text() });
    await new Promise((listo, error) => {
      const t = setTimeout(() => error(new Error('WebRTC: tiempo de espera agotado')), espera);
      const revisar = () => {
        if (pc.connectionState === 'failed' || pc.connectionState === 'closed') {
          clearTimeout(t);
          error(new Error('WebRTC: ' + pc.connectionState));
        }
      };
      pc.addEventListener('connectionstatechange', revisar);
      // "Conectado" no basta: el navegador puede no tener el codec. Se
      // espera a que el video pinte su primer cuadro.
      video.addEventListener('loadeddata', () => { clearTimeout(t); listo(); }, { once: true });
      video.addEventListener('error', () => { clearTimeout(t); error(new Error('codec')); }, { once: true });
    });
    video.play().catch(() => {});
    return pc;
  } catch (err) {
    pc.close();
    throw err;
  }
}

/* Lienzo sobre el video con las cajas que manda el worker. */
export class Superposicion {
  constructor(contenedor, video, camara) {
    this.video = video;
    this.lienzo = document.createElement('canvas');
    this.lienzo.className = 'superposicion';
    contenedor.append(this.lienzo);
    this.datos = null;
    this.llegada = 0;
    this.fuente = new EventSource(`/api/preview/${encodeURIComponent(camara)}/pistas.sse`);
    this.fuente.onmessage = (e) => {
      try {
        this.datos = JSON.parse(e.data);
        this.llegada = performance.now();
        this.dibujar();
      } catch {}
    };
    this.observador = new ResizeObserver(() => this.dibujar());
    this.observador.observe(contenedor);
    // Sin mensajes nuevos, las cajas viejas se borran solas.
    this.reloj = setInterval(() => {
      if (this.datos && performance.now() - this.llegada > VIGENCIA_MS) this.dibujar();
    }, 500);
  }

  cerrar() {
    this.fuente.close();
    this.observador.disconnect();
    clearInterval(this.reloj);
    this.lienzo.remove();
  }

  /* Rectangulo donde se ve el video dentro de su caja (object-fit: contain). */
  area() {
    const w = this.lienzo.clientWidth;
    const h = this.lienzo.clientHeight;
    const vw = this.video.videoWidth || 16;
    const vh = this.video.videoHeight || 9;
    const escala = Math.min(w / vw, h / vh);
    return { x: (w - vw * escala) / 2, y: (h - vh * escala) / 2, w: vw * escala, h: vh * escala };
  }

  dibujar() {
    const ratio = window.devicePixelRatio || 1;
    const c = this.lienzo;
    c.width = Math.round(c.clientWidth * ratio);
    c.height = Math.round(c.clientHeight * ratio);
    const ctx = c.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, c.width, c.height);
    const d = this.datos;
    if (!d || !d.ancho || !d.alto || performance.now() - this.llegada > VIGENCIA_MS) return;
    const a = this.area();
    const sx = a.w / d.ancho;
    const sy = a.h / d.alto;
    const px = (x) => a.x + x * sx;
    const py = (y) => a.y + y * sy;
    ctx.lineWidth = 2;
    ctx.font = '600 11px system-ui, sans-serif';
    for (const o of d.objetos || []) {
      ctx.strokeStyle = o.c || '#ccc';
      ctx.fillStyle = o.c || '#ccc';
      if (o.k === 'zona' && Array.isArray(o.p) && o.p.length >= 2) {
        ctx.beginPath();
        o.p.forEach(([x, y], i) => (i ? ctx.lineTo(px(x), py(y)) : ctx.moveTo(px(x), py(y))));
        if (o.p.length > 2) {
          ctx.closePath();
          ctx.globalAlpha = 0.15;
          ctx.fill();
          ctx.globalAlpha = 1;
        }
        ctx.stroke();
        ctx.fillText(o.t || '', px(o.p[0][0]) + 4, py(o.p[0][1]) - 6);
        continue;
      }
      if (o.k === 'esqueleto' && Array.isArray(o.p)) {
        for (const [i, j] of HUESOS) {
          const p = o.p[i];
          const q = o.p[j];
          if (!p || !q) continue;
          ctx.beginPath();
          ctx.moveTo(px(p[0]), py(p[1]));
          ctx.lineTo(px(q[0]), py(q[1]));
          ctx.stroke();
        }
        continue;
      }
      const [x1, y1, x2, y2] = o.b;
      ctx.strokeRect(px(x1), py(y1), (x2 - x1) * sx, (y2 - y1) * sy);
      if (o.t) {
        const ancho = ctx.measureText(o.t).width + 8;
        ctx.fillRect(px(x1), py(y1) - 16, ancho, 16);
        ctx.fillStyle = '#000';
        ctx.fillText(o.t, px(x1) + 4, py(y1) - 4);
      }
    }
  }
}
