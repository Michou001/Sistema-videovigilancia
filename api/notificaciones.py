"""Notificaciones fuera del dashboard: Telegram, correo, WhatsApp y webhook.

El dashboard avisa con banner y sonido... si alguien lo esta mirando. En un
turno nocturno, en un cambio de guardia o con el monitor en otra pantalla,
una alerta critica puede quedarse horas sin atender. Estas notificaciones
llevan la alerta al celular del responsable.

CANALES (se activan solo con su configuracion en el .env):
  Telegram   NOTIFY_TELEGRAM_TOKEN, NOTIFY_TELEGRAM_CHATS (ids separados por coma)
  Correo     NOTIFY_SMTP_HOST, _PORT, _USER, _PASSWORD, _FROM, _TO, _TLS
  WhatsApp   por Twilio: NOTIFY_TWILIO_SID, _TOKEN, _FROM, _TO
  Webhook    NOTIFY_WEBHOOK_URL (+ NOTIFY_WEBHOOK_SECRETO para firmar): cualquier
             otra cosa (n8n, Make, un bot propio, la API de WhatsApp de Meta)

REGLAS
  - Nunca frena la ingesta: las alertas entran a una cola y un hilo aparte las
    manda, con 3 reintentos.
  - Solo severidad >= NOTIFY_MIN_SEVERITY (critical por defecto).
  - Sin inundar: la misma alerta (titulo + camara) no se repite en 60 s, y hay
    un tope de NOTIFY_MAX_POR_MINUTO por minuto.
  - PRIVACIDAD: por defecto NO se manda la foto (NOTIFY_INCLUDE_PHOTO=false).
    Una foto de una persona que sale a un servicio externo (Telegram, un
    correo) es una transferencia de datos personales: se activa solo si el
    aviso de privacidad lo contempla (ver docs/privacidad.md).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Optional

log = logging.getLogger(__name__)

NIVELES = {"info": 0, "warning": 1, "critical": 2}
ICONOS = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}


def _lista(valor: str) -> list[str]:
    return [x.strip() for x in (valor or "").split(",") if x.strip()]


def _severidad(valor: Optional[str]) -> str:
    valor = (valor or "critical").strip().lower()
    if valor not in NIVELES:
        log.error("NOTIFY_MIN_SEVERITY=%r no es info, warning ni critical; se usa critical", valor)
        return "critical"
    return valor


def _entero(nombre: str, defecto: int) -> int:
    valor = (os.getenv(nombre) or "").strip()
    if not valor:
        return defecto
    try:
        return int(valor)
    except ValueError:
        log.error("%s=%r no es un numero; se usa %d", nombre, valor, defecto)
        return defecto


@dataclass
class ConfigNotificaciones:
    min_severidad: str = "critical"
    telegram_token: str = ""
    telegram_chats: list[str] = field(default_factory=list)
    smtp_host: str = ""
    smtp_puerto: int = 587
    smtp_usuario: str = ""
    smtp_password: str = ""
    smtp_de: str = ""
    smtp_para: list[str] = field(default_factory=list)
    smtp_tls: str = "starttls"          # starttls | ssl | none
    twilio_sid: str = ""
    twilio_token: str = ""
    twilio_de: str = ""
    twilio_para: list[str] = field(default_factory=list)
    webhook_url: str = ""
    webhook_secreto: str = ""
    incluir_foto: bool = False
    url_dashboard: str = ""
    camara_caida_s: int = 120
    max_por_minuto: int = 20

    @classmethod
    def desde_entorno(cls) -> "ConfigNotificaciones":
        e = os.getenv
        return cls(
            min_severidad=_severidad(e("NOTIFY_MIN_SEVERITY", "critical")),
            telegram_token=e("NOTIFY_TELEGRAM_TOKEN", ""),
            telegram_chats=_lista(e("NOTIFY_TELEGRAM_CHATS", "")),
            smtp_host=e("NOTIFY_SMTP_HOST", ""),
            smtp_puerto=_entero("NOTIFY_SMTP_PORT", 587),
            smtp_usuario=e("NOTIFY_SMTP_USER", ""),
            smtp_password=e("NOTIFY_SMTP_PASSWORD", ""),
            smtp_de=e("NOTIFY_SMTP_FROM", "") or e("NOTIFY_SMTP_USER", ""),
            smtp_para=_lista(e("NOTIFY_SMTP_TO", "")),
            smtp_tls=(e("NOTIFY_SMTP_TLS", "starttls") or "starttls").lower(),
            twilio_sid=e("NOTIFY_TWILIO_SID", ""),
            twilio_token=e("NOTIFY_TWILIO_TOKEN", ""),
            twilio_de=e("NOTIFY_TWILIO_FROM", ""),
            twilio_para=_lista(e("NOTIFY_TWILIO_TO", "")),
            webhook_url=e("NOTIFY_WEBHOOK_URL", ""),
            webhook_secreto=e("NOTIFY_WEBHOOK_SECRETO", ""),
            incluir_foto=(e("NOTIFY_INCLUDE_PHOTO", "false") or "").lower() in {"1", "true", "si", "yes"},
            url_dashboard=(e("NOTIFY_URL_DASHBOARD", "") or "").rstrip("/"),
            camara_caida_s=max(0, _entero("NOTIFY_CAMARA_CAIDA_S", 120)),
            max_por_minuto=max(1, _entero("NOTIFY_MAX_POR_MINUTO", 20)),
        )


@dataclass
class Mensaje:
    titulo: str
    detalle: str = ""
    severidad: str = "critical"
    camara: str = ""
    ubicacion: str = ""
    cuando: Optional[datetime] = None
    folio: str = ""
    foto: Optional[bytes] = None
    enlace: str = ""
    tipo: str = "alerta"

    def texto(self) -> str:
        cuando = (self.cuando or datetime.now(timezone.utc)).astimezone()
        lineas = [f"{ICONOS.get(self.severidad, '')} {self.titulo}".strip()]
        if self.detalle:
            lineas.append(self.detalle)
        lugar = " · ".join(p for p in (self.camara, self.ubicacion) if p)
        if lugar:
            lineas.append(f"Cámara: {lugar}")
        lineas.append(f"Hora: {cuando:%d/%m/%Y %H:%M:%S}")
        if self.folio:
            lineas.append(f"Folio: {self.folio}")
        if self.enlace:
            lineas.append(self.enlace)
        return "\n".join(lineas)

    def como_json(self) -> dict:
        return {"tipo": self.tipo, "titulo": self.titulo, "detalle": self.detalle,
                "severidad": self.severidad, "camara": self.camara, "ubicacion": self.ubicacion,
                "fecha": (self.cuando or datetime.now(timezone.utc)).isoformat(),
                "folio": self.folio, "enlace": self.enlace}


# --------------------------------------------------------------------------
# Canales
# --------------------------------------------------------------------------

class Canal:
    nombre = "canal"

    async def enviar(self, m: Mensaje, http) -> None:
        raise NotImplementedError

    def descripcion(self) -> str:
        return self.nombre


class Telegram(Canal):
    nombre = "telegram"

    def __init__(self, token: str, chats: list[str]) -> None:
        self.token, self.chats = token, chats

    async def enviar(self, m: Mensaje, http) -> None:
        base = f"https://api.telegram.org/bot{self.token}"
        texto = html.escape(m.texto())
        texto = texto.replace(html.escape(m.titulo), f"<b>{html.escape(m.titulo)}</b>", 1)
        for chat in self.chats:
            if m.foto:
                r = await http.post(f"{base}/sendPhoto", data={"chat_id": chat, "caption": texto[:1000],
                                                             "parse_mode": "HTML"},
                                    files={"photo": ("evidencia.jpg", m.foto, "image/jpeg")})
            else:
                r = await http.post(f"{base}/sendMessage", json={"chat_id": chat, "text": texto,
                                                                "parse_mode": "HTML"})
            r.raise_for_status()

    def descripcion(self) -> str:
        return f"Telegram → {len(self.chats)} chat(s)"


class Correo(Canal):
    nombre = "correo"

    def __init__(self, cfg: ConfigNotificaciones) -> None:
        self.cfg = cfg

    def _enviar_sync(self, m: Mensaje) -> None:
        c = self.cfg
        msg = EmailMessage()
        msg["Subject"] = f"[GOSS IP] {m.titulo}"
        msg["From"] = c.smtp_de
        msg["To"] = ", ".join(c.smtp_para)
        msg.set_content(m.texto())
        if m.foto:
            msg.add_attachment(m.foto, maintype="image", subtype="jpeg", filename="evidencia.jpg")
        contexto = ssl.create_default_context()
        if c.smtp_tls == "ssl":
            servidor = smtplib.SMTP_SSL(c.smtp_host, c.smtp_puerto, timeout=20, context=contexto)
        else:
            servidor = smtplib.SMTP(c.smtp_host, c.smtp_puerto, timeout=20)
        with servidor:
            if c.smtp_tls == "starttls":
                servidor.starttls(context=contexto)
            if c.smtp_usuario:
                servidor.login(c.smtp_usuario, c.smtp_password)
            servidor.send_message(msg)

    async def enviar(self, m: Mensaje, http) -> None:
        await asyncio.to_thread(self._enviar_sync, m)

    def descripcion(self) -> str:
        return f"Correo ({self.cfg.smtp_host}) → {len(self.cfg.smtp_para)} destinatario(s)"


class WhatsAppTwilio(Canal):
    nombre = "whatsapp"

    def __init__(self, cfg: ConfigNotificaciones) -> None:
        self.cfg = cfg

    @staticmethod
    def _numero(n: str) -> str:
        return n if n.startswith("whatsapp:") else f"whatsapp:{n}"

    async def enviar(self, m: Mensaje, http) -> None:
        # La foto no va: Twilio solo acepta imagenes por URL publica, y la
        # evidencia no debe quedar publicada en internet.
        c = self.cfg
        url = f"https://api.twilio.com/2010-04-01/Accounts/{c.twilio_sid}/Messages.json"
        for destino in c.twilio_para:
            r = await http.post(url, auth=(c.twilio_sid, c.twilio_token),
                                data={"From": self._numero(c.twilio_de), "To": self._numero(destino),
                                      "Body": m.texto()[:1500]})
            r.raise_for_status()

    def descripcion(self) -> str:
        return f"WhatsApp (Twilio) → {len(self.cfg.twilio_para)} número(s)"


class Webhook(Canal):
    nombre = "webhook"

    def __init__(self, url: str, secreto: str = "") -> None:
        self.url, self.secreto = url, secreto

    async def enviar(self, m: Mensaje, http) -> None:
        cuerpo = json.dumps(m.como_json(), ensure_ascii=False).encode()
        cabeceras = {"Content-Type": "application/json"}
        if self.secreto:
            # El receptor verifica que el aviso viene de este sistema.
            firma = hmac.new(self.secreto.encode(), cuerpo, hashlib.sha256).hexdigest()
            cabeceras["X-Goss-Firma"] = f"sha256={firma}"
        r = await http.post(self.url, content=cuerpo, headers=cabeceras)
        r.raise_for_status()

    def descripcion(self) -> str:
        from urllib.parse import urlparse

        return f"Webhook → {urlparse(self.url).netloc}"


def canales_de(cfg: ConfigNotificaciones) -> list[Canal]:
    canales: list[Canal] = []
    if cfg.telegram_token and cfg.telegram_chats:
        canales.append(Telegram(cfg.telegram_token, cfg.telegram_chats))
    if cfg.smtp_host and cfg.smtp_para:
        canales.append(Correo(cfg))
    if cfg.twilio_sid and cfg.twilio_token and cfg.twilio_de and cfg.twilio_para:
        canales.append(WhatsAppTwilio(cfg))
    if cfg.webhook_url:
        canales.append(Webhook(cfg.webhook_url, cfg.webhook_secreto))
    return canales


# --------------------------------------------------------------------------
# Notificador
# --------------------------------------------------------------------------

class _OcultarSecretos(logging.Filter):
    """httpx escribe en el log la URL de cada peticion, y la de Telegram lleva
    el token del bot: cualquiera con acceso a los logs podria usar el bot."""

    def __init__(self, limpiar) -> None:
        super().__init__()
        self.limpiar = limpiar

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            texto = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        limpio = self.limpiar(texto)
        if limpio != texto:
            record.msg, record.args = limpio, None
        return True


class Notificador:
    REINTENTOS = 3
    ESPERA_BASE = 2.0           # segundos antes del 2o intento; se duplica
    VENTANA_REPETIDOS = 60.0

    def __init__(self, cfg: Optional[ConfigNotificaciones] = None, *, transporte=None,
                 datos_camara=None, foto_de=None) -> None:
        self.cfg = cfg or ConfigNotificaciones.desde_entorno()
        self.canales = canales_de(self.cfg)
        self._transporte = transporte
        self._datos_camara = datos_camara   # camera_id -> (nombre, ubicacion)
        self._foto_de = foto_de             # snapshot_path -> bytes | None
        self._cola: Optional[asyncio.Queue] = None
        self._tarea: Optional[asyncio.Task] = None
        self._recientes: dict[str, float] = {}
        self._envios: list[float] = []
        self.enviados = 0
        self.fallidos = 0
        self._filtro_log = _OcultarSecretos(self._sin_secretos)

    @property
    def activo(self) -> bool:
        return bool(self.canales)

    def _http(self):
        import httpx

        # Tambien aqui y no solo en iniciar(): el envio de prueba puede
        # correr con un notificador que no se inicio.
        logging.getLogger("httpx").addFilter(self._filtro_log)
        return httpx.AsyncClient(timeout=15.0, transport=self._transporte)

    async def iniciar(self) -> None:
        if not self.activo:
            log.info("Notificaciones externas: ninguna configurada (ver NOTIFY_* en .env.example)")
            return
        logging.getLogger("httpx").addFilter(self._filtro_log)
        self._cola = asyncio.Queue(maxsize=500)
        self._tarea = asyncio.create_task(self._consumir())
        log.info("Notificaciones externas: %s (severidad minima: %s)",
                 ", ".join(c.descripcion() for c in self.canales), self.cfg.min_severidad)

    async def detener(self) -> None:
        logging.getLogger("httpx").removeFilter(self._filtro_log)
        if self._tarea:
            self._tarea.cancel()
            try:
                await self._tarea
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # -- entrada -----------------------------------------------------------

    def alerta(self, datos: dict) -> None:
        """Enganche del hub: cada alerta difundida pasa por aqui."""
        if not self.activo or self._cola is None:
            return
        if not self.debe_notificar(datos):
            return
        try:
            self._cola.put_nowait(("alerta", datos))
        except asyncio.QueueFull:
            log.error("Cola de notificaciones llena: se descarta el aviso de '%s'", datos.get("title"))

    def debe_notificar(self, datos: dict) -> bool:
        severidad = NIVELES.get(datos.get("severity", "info"), 0)
        if ((datos.get("type") == "plate" and datos.get("match_kind") in {"exact", "fuzzy"})
                or (datos.get("type") == "face" and datos.get("match_kind") == "biometric")):
            # Una coincidencia posible tambien debe llegar al responsable,
            # aunque el minimo general este configurado como critical.
            return severidad >= NIVELES["warning"]
        if datos.get("type") == "camera" and self.cfg.camara_caida_s > 0:
            # Una camara sin senal o saboteada deja un punto ciego: se avisa
            # desde "warning" aunque el minimo general sea "critical".
            return severidad >= NIVELES["warning"]
        return severidad >= NIVELES.get(self.cfg.min_severidad, 2)

    def aviso(self, mensaje: Mensaje) -> None:
        """Un aviso armado a mano (camara recuperada, prueba)."""
        if self.activo and self._cola is not None:
            try:
                self._cola.put_nowait(("mensaje", mensaje))
            except asyncio.QueueFull:
                pass

    # -- armado y envio ----------------------------------------------------

    async def _armar(self, datos: dict) -> Mensaje:
        nombre, ubicacion = datos.get("camera_id", ""), ""
        if self._datos_camara:
            try:
                nombre, ubicacion = await asyncio.to_thread(self._datos_camara, datos.get("camera_id", ""))
            except Exception:  # noqa: BLE001
                pass
        foto = None
        if self.cfg.incluir_foto and datos.get("snapshot_path") and self._foto_de:
            try:
                foto = await asyncio.to_thread(self._foto_de, datos["snapshot_path"])
            except Exception:  # noqa: BLE001
                foto = None
        cuando = None
        if datos.get("ts"):
            try:
                cuando = datetime.fromisoformat(str(datos["ts"]).replace("Z", "+00:00"))
            except ValueError:
                cuando = None
        folio = f"ALR-{int(datos['id']):06d}" if datos.get("id") else ""
        enlace = f"{self.cfg.url_dashboard}/" if self.cfg.url_dashboard else ""
        return Mensaje(titulo=datos.get("title", "Alerta"), detalle=datos.get("detail") or "",
                       severidad=datos.get("severity", "critical"), camara=nombre or "",
                       ubicacion=ubicacion or "", cuando=cuando, folio=folio, foto=foto, enlace=enlace)

    def _permitido(self, m: Mensaje) -> bool:
        ahora = time.monotonic()
        clave = f"{m.titulo}|{m.camara}"
        self._recientes = {k: t for k, t in self._recientes.items() if ahora - t < self.VENTANA_REPETIDOS}
        if clave in self._recientes:
            return False
        self._envios = [t for t in self._envios if ahora - t < 60]
        if len(self._envios) >= self.cfg.max_por_minuto:
            log.warning("Tope de %d notificaciones por minuto: se omite '%s'",
                        self.cfg.max_por_minuto, m.titulo)
            return False
        self._recientes[clave] = ahora
        self._envios.append(ahora)
        return True

    def _sin_secretos(self, texto: str) -> str:
        """Los errores de httpx incluyen la URL, y la de Telegram lleva el
        token del bot: sin esto el token terminaba en el log, en la bitacora
        y en la pantalla del administrador."""
        c = self.cfg
        if c.webhook_url:
            texto = texto.replace(c.webhook_url, "<webhook>")
        for secreto in (c.telegram_token, c.smtp_password, c.twilio_token, c.webhook_secreto):
            if secreto and len(secreto) >= 4:
                texto = texto.replace(secreto, "***")
        return texto

    async def enviar_a_todos(self, m: Mensaje, reintentos: Optional[int] = None,
                            solo: Optional[str] = None) -> dict[str, str]:
        """Manda a todos los canales con reintentos. Devuelve el resultado
        por canal ('ok' o el error)."""
        resultados: dict[str, str] = {}
        intentos = reintentos or self.REINTENTOS
        async with self._http() as http:
            for canal in self.canales:
                if solo and canal.nombre != solo:
                    continue
                for intento in range(1, intentos + 1):
                    try:
                        await canal.enviar(m, http)
                        resultados[canal.nombre] = "ok"
                        self.enviados += 1
                        break
                    except Exception as e:  # noqa: BLE001
                        resultados[canal.nombre] = self._sin_secretos(
                            f"{type(e).__name__}: {str(e)}")[:200]
                        if intento < intentos:
                            await asyncio.sleep(self.ESPERA_BASE * 2 ** (intento - 1))
                else:
                    self.fallidos += 1
                    log.error("No se pudo notificar por %s: %s", canal.nombre, resultados[canal.nombre])
        return resultados

    async def _consumir(self) -> None:
        while True:
            tipo, dato = await self._cola.get()
            try:
                mensaje = await self._armar(dato) if tipo == "alerta" else dato
                if self._permitido(mensaje):
                    await self.enviar_a_todos(mensaje)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - un aviso fallido no detiene los demas
                log.error("Fallo al notificar: %s", self._sin_secretos(str(e)))

    async def prueba(self) -> dict[str, Any]:
        m = Mensaje(titulo="Prueba de notificaciones", severidad="info", tipo="prueba",
                    detalle="Si recibes esto, el canal esta bien configurado.",
                    enlace=f"{self.cfg.url_dashboard}/" if self.cfg.url_dashboard else "")
        if not self.activo:
            return {}
        # Un solo intento: el administrador espera la respuesta en pantalla.
        return await self.enviar_a_todos(m, reintentos=1)

    async def prueba_telegram(self, tipo: str) -> dict[str, str]:
        """Ensayo de coincidencia, enviado solamente al chat de Telegram."""
        if tipo not in {"plate", "face"}:
            raise ValueError("Tipo de prueba no valido")
        nombre = "placa" if tipo == "plate" else "rostro"
        m = Mensaje(titulo=f"PRUEBA · Coincidencia de {nombre} en lista negra",
                    severidad="critical", tipo="prueba",
                    detalle="Simulacion solicitada desde el panel. No es una deteccion real.",
                    enlace=f"{self.cfg.url_dashboard}/" if self.cfg.url_dashboard else "")
        return await self.enviar_a_todos(m, reintentos=1, solo="telegram")

    def resumen(self) -> dict:
        return {"canales": [{"nombre": c.nombre, "descripcion": c.descripcion()} for c in self.canales],
                "min_severidad": self.cfg.min_severidad, "incluir_foto": self.cfg.incluir_foto,
                "camara_caida_s": self.cfg.camara_caida_s, "enviados": self.enviados,
                "fallidos": self.fallidos}


notificador: Optional[Notificador] = None


def obtener() -> Optional[Notificador]:
    return notificador


def datos_camara_desde_bd(camera_id: str) -> tuple[str, str]:
    from sqlmodel import Session, select

    from api.database import engine
    from api.models import Camera

    with Session(engine) as s:
        c = s.exec(select(Camera).where(Camera.camera_id == camera_id)).first()
        return ((c.name if c else camera_id) or camera_id, (c.location if c else "") or "")


def foto_desde_disco(ruta: str) -> Optional[bytes]:
    from api.config import BASE_DIR

    archivo = (BASE_DIR / ruta).resolve()
    if not str(archivo).startswith(str(BASE_DIR.resolve())) or not archivo.is_file():
        return None
    datos = archivo.read_bytes()
    return datos if len(datos) <= 8 * 1024 * 1024 else None
