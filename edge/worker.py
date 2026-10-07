"""Worker de borde: lee la camara, corre los detectores y emite eventos.

    python -m edge.worker                 # deteccion continua
    python -m edge.worker --diagnostico   # solo mide la fuente, sin modelos
    python -m edge.worker --env .env.cam2 # otra camara, con su propio .env
    python -m edge.worker --env .env --env .env.cam2 --env .env.cam3
                                          # VARIAS camaras en UN proceso

Con la webcam:            SOURCE=webcam:0
Con la Hikvision:         SOURCE=rtsp://admin:pass@192.168.1.64:554/Streaming/Channels/102
Con un video grabado:     SOURCE=file:videos/prueba.mp4

El codigo de abajo NO cambia al pasar de una a otra: la camara es un detalle
de configuracion (ver edge/sources.py), no de codigo.

VARIAS CAMARAS EN UN PROCESO: los modelos de onnxruntime (placas, rostros) se
cargan una sola vez y los comparten todas (ver edge/modelos.py), y hay un solo
contexto de CUDA. Con un proceso por camara, en una GPU de 6 GB cabian unas 3
camaras porque se acababa la VRAM; asi caben bastantes mas antes de que el
limite sea el computo. Cada camara corre en su propio hilo con su propia
fuente, detectores, envio y latido: si una camara se cae, las demas siguen.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from edge.config import EdgeConfig, cargar_configuraciones  # noqa: E402
from edge.sources import LiveSource, open_source  # noqa: E402

log = logging.getLogger("edge.worker")

DETENER = threading.Event()


def _manejar_senal(signum, frame):  # noqa: ARG001
    """Ctrl+C limpio: cierra las camaras en vez de dejar handles colgados.
    En Windows un VideoCapture sin liberar deja la webcam ocupada hasta que
    se cierra el proceso padre."""
    if DETENER.is_set():
        log.warning("Segunda senal recibida, saliendo a la fuerza")
        sys.exit(1)
    log.info("Senal recibida, cerrando ordenadamente...")
    DETENER.set()


def _uso_cpu() -> Optional[float]:
    try:
        import psutil

        return psutil.cpu_percent(interval=None)
    except Exception:  # noqa: BLE001
        return None


def diagnostico(cfg: EdgeConfig, segundos: float = 20.0) -> int:
    """Mide la salud de la fuente sin cargar ningun modelo.

    Es la primera prueba que hay que pasar: si aqui los fps son inestables o la
    latencia crece, ningun modelo lo va a arreglar. Separar este diagnostico de
    la inferencia evita perder horas culpando a la GPU de un problema de red.

    Tambien reporta el uso de CPU: es la forma de comparar HW_DECODE=off
    contra HW_DECODE=auto (la decodificacion es lo que mas CPU gasta).
    """
    print("=" * 68)
    print(f"DIAGNOSTICO DE FUENTE  ({segundos:.0f}s)")
    print("=" * 68)
    print(f"  camera_id : {cfg.camera_id}")
    print(f"  source    : {_ocultar(cfg.source)}")
    print(f"  device    : {cfg.resolve_device()}")
    print(f"  infer_fps : {cfg.infer_fps}")
    print(f"  hw_decode : {cfg.hw_decode}")
    print()

    try:
        fuente = open_source(cfg.source, hw_decode=cfg.hw_decode)
    except Exception as e:  # noqa: BLE001
        print(f"[x] No se pudo abrir la fuente: {e}")
        return 1

    with fuente:
        if isinstance(fuente, LiveSource):
            print("  esperando primer frame...", end=" ", flush=True)
            if not fuente.wait_until_ready(timeout=15.0):
                print("[x] TIMEOUT")
                print("\n  La fuente no entrego ningun frame en 15 s.")
                if cfg.source.startswith("rtsp"):
                    print("  Diagnostica la camara con:")
                    print("      python tools/probe_camara.py --descubrir")
                else:
                    print("  Verifica que ninguna otra app este usando la webcam.")
                return 1
            print("[OK]")

        primero = fuente.read(timeout=10.0)
        if primero is None:
            print("[x] No se recibieron frames")
            return 1

        ancho, alto = primero.shape
        print(f"  resolucion: {ancho}x{alto}")
        print()
        print(f"  {'t':>5}  {'fps_real':>9}  {'procesados':>10}  {'perdidos':>9}  {'latencia':>9}  {'CPU':>5}")
        print("  " + "-" * 60)

        inicio = time.monotonic()
        procesados = 0
        latencia_max = 0.0
        ultimo_reporte = inicio
        muestras_cpu: list[float] = []
        _uso_cpu()  # la primera lectura de psutil siempre es 0: se descarta

        for frame in fuente.frames(max_fps=cfg.infer_fps):
            if DETENER.is_set():
                break
            procesados += 1
            latencia_max = max(latencia_max, frame.age)

            ahora = time.monotonic()
            if ahora - ultimo_reporte >= 2.0:
                st = fuente.status
                cpu = _uso_cpu()
                if cpu is not None:
                    muestras_cpu.append(cpu)
                print(f"  {ahora - inicio:5.0f}  {st.measured_fps:9.1f}  {procesados:10d}"
                      f"  {st.frames_dropped:9d}  {latencia_max * 1000:7.0f}ms"
                      f"  {'' if cpu is None else f'{cpu:4.0f}%'}")
                ultimo_reporte = ahora
                latencia_max = 0.0

            if ahora - inicio >= segundos:
                break

        st = fuente.status
        print()
        print("=" * 68)
        print("RESULTADO")
        print("=" * 68)
        transcurrido = time.monotonic() - inicio
        print(f"  frames capturados por la fuente  : {st.frames_grabbed}")
        print(f"  frames omitidos por limite de fps: {st.frames_skipped}  (normal y esperado)")
        print(f"  frames perdidos por lentitud     : {st.frames_dropped}  "
              f"{'<-- revisar' if st.frames_dropped > st.frames_grabbed * 0.1 else ''}")
        print(f"  frames procesados                : {procesados}  "
              f"({procesados / transcurrido:.1f}/s, objetivo {cfg.infer_fps})")
        print(f"  reconexiones                     : {st.reconnects}")
        print(f"  fps reales de la camara          : {st.measured_fps:.1f}")
        if muestras_cpu:
            print(f"  uso de CPU promedio              : {sum(muestras_cpu) / len(muestras_cpu):.0f}%"
                  f"  (compara con HW_DECODE=auto / off)")

        # Los descartes NO son un error: son el mecanismo que mantiene la
        # latencia baja. Lo preocupante seria lo contrario.
        if st.reconnects > 0:
            print("\n  [!] Hubo reconexiones: la red o la camara son inestables.")
            print("      Con Wi-Fi es comun; por cable suele desaparecer.")
        if procesados < cfg.infer_fps * transcurrido * 0.7:
            print("\n  [!] No se alcanzo el fps objetivo. La fuente entrega menos")
            print("      de lo pedido, o la red no da abasto.")
        else:
            print("\n  [OK] Fuente estable. Lista para detectar: python -m edge.worker")
        print("=" * 68)

    return 0


def construir_detectores(cfg: EdgeConfig) -> list:
    """Instancia los detectores activados en la configuracion (ENABLE_*).

    El worker no sabe nada de sus modelos: todos cumplen edge/detectors/base.py.
    """
    detectores = []
    if cfg.enable_plates:
        from edge.detectors.plates import PlateDetector

        detectores.append(PlateDetector(cfg))
    if cfg.enable_faces:
        from edge.detectors.faces import FaceDetector

        detectores.append(FaceDetector(cfg))
    if cfg.enable_weapons:
        from edge.detectors.weapons import WeaponDetector

        detectores.append(WeaponDetector(cfg))
    if cfg.enable_pose:
        # Antes que movimiento: si la pose no carga (sin internet para bajar
        # el modelo, p.ej.), movimiento vuelve a estimar caidas por la caja.
        try:
            from edge.detectors.pose import PoseDetector

            detectores.append(PoseDetector(cfg))
        except Exception as e:  # noqa: BLE001
            log.error("[%s] No se pudo cargar el detector de pose (%s): las caidas se estiman "
                      "por la forma de la caja", cfg.camera_id, e)
            cfg.enable_pose = False
    if cfg.enable_motion:
        from edge.detectors.motion import MotionAnomalyDetector

        detectores.append(MotionAnomalyDetector(cfg))

    if not detectores:
        raise RuntimeError(f"[{cfg.camera_id}] No hay ningun detector activo. Revisa ENABLE_* en el .env")
    return detectores


class Camara:
    """Todo lo de una camara: fuente, detectores, envio, vista en vivo y latido.

    Cada camara es independiente: con varias en un proceso, una que pierde la
    senal o cuyo detector falla no afecta a las demas.
    """

    def __init__(self, cfg: EdgeConfig) -> None:
        from edge.preview import crear_publicador
        from edge.sink import crear_sink

        self.cfg = cfg
        self.id = cfg.camera_id
        self.detectores = construir_detectores(cfg)
        self.sink = crear_sink(cfg)
        self.preview = crear_publicador(cfg)
        self.fuente = open_source(cfg.source, hw_decode=cfg.hw_decode,
                                  loop=cfg.source_loop, realtime=cfg.source_realtime)
        self.complementos: list = []
        """Piezas opcionales que reciben cada frame y/o producen eventos por su
        cuenta (grabador de clips, eventos de la propia camara). Cumplen:
        al_frame(frame), eventos() -> list, cerrar(), estado() -> dict."""
        self.frames = 0
        self.total_eventos = 0
        self.fallos: dict[str, int] = {d.name: 0 for d in self.detectores}
        self.inicio = time.monotonic()
        self.latido = None
        if cfg.api_url and cfg.api_token:
            from edge.heartbeat import Heartbeat

            self.latido = Heartbeat(cfg.api_url, cfg.api_token, cfg.camera_id, self.estado,
                                    intervalo=cfg.heartbeat_s)

    # ------------------------------------------------------------------

    def estado(self) -> dict:
        """Lo que viaja en el latido hacia la API."""
        from edge.modelos import cargados

        transcurrido = max(1e-6, time.monotonic() - self.inicio)
        datos = {
            **self.fuente.status.as_dict(),
            "fps_procesados": round(self.frames / transcurrido, 2),
            "eventos": self.total_eventos,
            "detectores": {d.name: d.resumen for d in self.detectores},
            "fallos_detector": {k: v for k, v in self.fallos.items() if v},
            "modelos_compartidos": cargados(),
        }
        for c in self.complementos:
            try:
                datos.update(c.estado())
            except Exception:  # noqa: BLE001
                pass
        return datos

    def emitir(self, evento, frame=None) -> None:
        self.sink.enviar(evento)
        self.total_eventos += 1
        if frame is None:
            return
        # El cuadro en el que se detecto, para quien lo quiera (dataset de
        # reentrenamiento). Solo en vivo: al cerrar ya no hay cuadro.
        for c in self.complementos:
            if hasattr(c, "al_evento"):
                try:
                    c.al_evento(evento, frame)
                except Exception as e:  # noqa: BLE001
                    log.debug("[%s] %s.al_evento fallo: %s", self.id, type(c).__name__, e)

    def procesar(self, frame) -> None:
        """Un frame por todos los detectores y complementos."""
        self.frames += 1
        for det in self.detectores:
            # Un detector que falla en un frame (memoria de GPU, un recorte
            # raro para el OCR) no debe apagar a los demas ni al worker: se
            # registra y se sigue con el siguiente frame.
            try:
                eventos = det.procesar(frame)
            except Exception as e:  # noqa: BLE001
                self.fallos[det.name] += 1
                n = self.fallos[det.name]
                if n in (1, 10) or n % 100 == 0:
                    log.exception("[%s] El detector %s fallo (%d veces): %s", self.id, det.name, n, e)
                continue
            for evento in eventos:
                self.emitir(evento, frame)

        # Lo que siguen los detectores (personas, vehiculos), para las piezas
        # que razonan sobre objetos sin correr su propio modelo (zonas).
        pistas = [p for det in self.detectores for p in det.pistas()]
        for c in self.complementos:
            try:
                c.al_frame(frame)
                if hasattr(c, "al_pistas"):
                    c.al_pistas(frame, pistas)
                for evento in c.eventos():
                    self.emitir(evento, frame)
            except Exception as e:  # noqa: BLE001
                log.debug("[%s] complemento %s fallo: %s", self.id, type(c).__name__, e)

    def cajas_actuales(self, frame) -> dict:
        alto, ancho = frame.frame.shape[:2]
        objetos = []
        for pieza in [*self.detectores, *getattr(self, "complementos", [])]:
            if not hasattr(pieza, "cajas"):
                continue
            try:
                objetos += pieza.cajas()
            except Exception as e:  # noqa: BLE001
                log.debug("[%s] cajas de %s: %s", self.id, type(pieza).__name__, e)
        return {"ancho": ancho, "alto": alto, "ts": frame.ts, "objetos": objetos}

    def vista_anotada(self, frame):
        vista = frame.frame.copy()
        for det in self.detectores:
            try:
                vista = det.anotar(vista)
            except Exception as e:  # noqa: BLE001
                log.debug("No se pudo anotar %s: %s", det.name, e)
        for c in self.complementos:
            if hasattr(c, "anotar"):
                try:
                    vista = c.anotar(vista)
                except Exception as e:  # noqa: BLE001
                    log.debug("No se pudo anotar %s: %s", type(c).__name__, e)
        return vista

    def correr(self, segundos: Optional[float] = None, ventana: bool = False) -> int:
        """Bucle de captura -> detectores -> eventos hasta DETENER."""
        cfg = self.cfg
        with self.fuente:
            if isinstance(self.fuente, LiveSource) and not self.fuente.wait_until_ready(15.0):
                log.error("[%s] La fuente no entrego frames. Corre --diagnostico.", self.id)
                if not self.fuente.reconectando:
                    return 1
                log.warning("[%s] Se sigue esperando: la fuente reintenta sola", self.id)

            self.inicio = time.monotonic()
            ultimo_reporte = self.inicio

            # esperar_cortes: un corte de la camara NO termina el worker. Es un
            # sistema que corre sin nadie mirando; si un parpadeo de red lo
            # apaga, nadie se entera hasta que alguien revisa al dia siguiente.
            for frame in self.fuente.frames(max_fps=cfg.infer_fps, esperar_cortes=True):
                if DETENER.is_set():
                    break
                self.procesar(frame)

                # El frame anotado se calcula UNA sola vez y sirve para las
                # dos cosas que lo quieren: la ventana local de depuracion y la
                # vista en vivo del dashboard. Se pregunta primero para no
                # dibujar cajas que nadie va a ver.
                para_preview = self.preview is not None and self.preview.quiere_frame()
                if ventana or para_preview:
                    vista = self.vista_anotada(frame)
                    if para_preview:
                        self.preview.publicar(vista)
                    if ventana:
                        cv2.imshow(f"Videovigilancia {self.id} - 'q' para salir", vista)
                        if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                            DETENER.set()
                            break

                # Las cajas como datos, para el navegador que ve el video por
                # WebRTC (go2rtc). Unos cientos de bytes: se mandan solo si
                # alguien mira asi.
                if self.preview is not None and self.preview.quiere_pistas():
                    self.preview.publicar_pistas(self.cajas_actuales(frame))

                ahora = time.monotonic()
                if ahora - ultimo_reporte >= 10.0:
                    self._reportar(ahora)
                    ultimo_reporte = ahora

                if segundos and (ahora - self.inicio) >= segundos:
                    break

            # Los objetos que seguian en pantalla al detener tambien cuentan.
            log.info("[%s] Cerrando tracks abiertos...", self.id)
            for det in self.detectores:
                try:
                    for evento in det.vaciar():
                        self.emitir(evento)
                except Exception as e:  # noqa: BLE001
                    log.error("[%s] No se pudieron cerrar los tracks de %s: %s", self.id, det.name, e)
        return 0

    def _reportar(self, ahora: float) -> None:
        st = self.fuente.status
        partes = " | ".join(f"{d.name}: {d.resumen}" for d in self.detectores)
        mirando = ""
        if self.preview is not None and self.preview.espectadores:
            mirando = f" | {self.preview.espectadores} viendo"
        print(f"  [{self.id} {ahora - self.inicio:5.0f}s] {self.frames} frames, "
              f"{st.measured_fps:.1f} fps camara, {self.total_eventos} eventos "
              f"| {partes}{mirando}")

    def cerrar(self) -> None:
        for c in self.complementos:
            try:
                for evento in c.eventos():
                    self.emitir(evento)
                c.cerrar()
            except Exception as e:  # noqa: BLE001
                log.debug("[%s] cerrar complemento: %s", self.id, e)
        if self.latido is not None:
            self.latido.cerrar()
        if self.preview is not None:
            self.preview.cerrar()
        for det in self.detectores:
            print(f"\n  [{self.id}] Estadisticas de {det.name}: {det.stats}")
            try:
                det.cerrar()
            except Exception:  # noqa: BLE001
                pass
        self.sink.cerrar()
        print(f"  [{self.id}] Total de eventos emitidos: {self.total_eventos}")


def _complementos(camara: Camara) -> None:
    """Conecta las piezas opcionales de cada camara segun su config."""
    try:
        from edge.clips import crear_grabador

        grabador = crear_grabador(camara.cfg, camara.sink)
        if grabador is not None:
            camara.complementos.append(grabador)
    except ImportError:
        pass
    try:
        from edge.dataset import crear_recolector

        recolector = crear_recolector(camara.cfg, camara.sink)
        if recolector is not None:
            camara.complementos.append(recolector)
    except ImportError:
        pass
    try:
        from edge.zonas import crear_motor_zonas

        motor = crear_motor_zonas(camara.cfg)
        if motor is not None:
            camara.complementos.append(motor)
    except ImportError:
        pass
    try:
        from edge.isapi import crear_eventos_camara

        eventos_camara = crear_eventos_camara(camara.cfg)
        if eventos_camara is not None:
            camara.complementos.append(eventos_camara)
    except ImportError:
        pass


def ejecutar(configs: list[EdgeConfig], segundos: Optional[float] = None) -> int:
    """Arranca una o varias camaras y espera a que terminen o a Ctrl+C."""
    print("=" * 68)
    print("WORKER DE BORDE" + (f"  ({len(configs)} camaras en un proceso)" if len(configs) > 1 else ""))
    print("=" * 68)
    for cfg in configs:
        print(f"  camara    : {cfg.camera_id}  ({_ocultar(cfg.source)})")
    print(f"  device    : {configs[0].resolve_device()}")
    print(f"  infer_fps : {', '.join(str(c.infer_fps) for c in configs)}")
    print()

    # Las camaras se construyen en orden, en el hilo principal: la primera
    # carga los modelos y las siguientes los reutilizan (edge/modelos.py).
    camaras: list[Camara] = []
    try:
        for cfg in configs:
            camara = Camara(cfg)
            _complementos(camara)
            camaras.append(camara)
            print(f"  [{cfg.camera_id}] detectores: {', '.join(d.name for d in camara.detectores)}")
    except Exception:
        for c in camaras:
            c.cerrar()
        raise

    print("\n  Detectando. Ctrl+C para detener.\n")
    ventana = any(c.show_window for c in configs)
    codigos: dict[str, int] = {}
    try:
        if len(camaras) == 1:
            # Una sola camara en el hilo principal: cv2.imshow (la ventana de
            # depuracion) solo funciona ahi en varios sistemas operativos.
            codigos[camaras[0].id] = camaras[0].correr(segundos, ventana=ventana)
        else:
            if ventana:
                log.warning("SHOW_WINDOW se ignora con varias camaras: usa la vista en vivo del dashboard")

            def _hilo(c: Camara) -> None:
                try:
                    codigos[c.id] = c.correr(segundos)
                except Exception as e:  # noqa: BLE001
                    log.exception("[%s] La camara termino con error: %s", c.id, e)
                    codigos[c.id] = 1

            hilos = [threading.Thread(target=_hilo, args=(c,), name=f"camara-{c.id}", daemon=True)
                     for c in camaras]
            for h in hilos:
                h.start()
            while any(h.is_alive() for h in hilos):
                for h in hilos:
                    h.join(timeout=0.5)
    finally:
        DETENER.set()
        if ventana:
            cv2.destroyAllWindows()
        for c in camaras:
            c.cerrar()

    return max(codigos.values(), default=0)


def _ocultar(spec: str) -> str:
    """No imprimas la contrasena de la camara en pantalla ni en logs."""
    if "@" not in spec:
        return spec
    esquema, _, resto = spec.partition("://")
    cred, _, host = resto.rpartition("@")
    usuario = cred.split(":", 1)[0] if cred else ""
    return f"{esquema}://{usuario}:***@{host}"


def main() -> int:
    p = argparse.ArgumentParser(description="Worker de borde del sistema de videovigilancia")
    p.add_argument("--diagnostico", action="store_true",
                   help="Prueba la fuente de video sin cargar modelos")
    p.add_argument("--segundos", type=float, default=20.0,
                   help="Duracion en segundos (default: 20)")
    p.add_argument("--limitar", action="store_true",
                   help="Detener la deteccion tras --segundos (por defecto corre indefinido)")
    p.add_argument("--ventana", action="store_true",
                   help="Mostrar ventana con las detecciones dibujadas (una sola camara)")
    p.add_argument("--source", help="Sobrescribe SOURCE del .env (una sola camara)")
    p.add_argument("--env", action="append", default=[],
                   help="Archivo de entorno de una camara. Repetible para correr varias "
                        "camaras en un solo proceso: --env .env --env .env.cam2")
    p.add_argument("--carpeta", help="Todos los *.env de una carpeta, una camara por archivo "
                                     "(Docker: --carpeta camaras)")
    args = p.parse_args()

    archivos = list(args.env)
    if args.carpeta:
        carpeta = Path(args.carpeta)
        encontrados = sorted(str(x) for x in carpeta.glob("*.env") if x.is_file())
        if not encontrados:
            p.error(f"No hay archivos *.env en {carpeta}")
        archivos += encontrados
    configs = cargar_configuraciones(archivos)
    if args.source:
        if len(configs) > 1:
            p.error("--source solo aplica con una camara")
        configs[0].source = args.source

    logging.basicConfig(
        level=getattr(logging, configs[0].log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx escribe una linea INFO por cada peticion. Con la vista en vivo eso
    # son PREVIEW_FPS lineas por segundo por camara, y el reporte periodico de
    # deteccion -- lo unico que de verdad se mira aqui -- queda enterrado. Los
    # fallos de envio ya los reportan el sink y el preview con su propio
    # mensaje. Se deja en WARNING para no perder los problemas reales.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    signal.signal(signal.SIGINT, _manejar_senal)
    if hasattr(signal, "SIGTERM"):
        # systemd y NSSM detienen el servicio con SIGTERM: mismo cierre ordenado.
        signal.signal(signal.SIGTERM, _manejar_senal)

    if args.ventana:
        for c in configs:
            c.show_window = True

    if args.diagnostico:
        return max(diagnostico(c, args.segundos) for c in configs)
    return ejecutar(configs, args.segundos if args.limitar else None)


if __name__ == "__main__":
    raise SystemExit(main())
