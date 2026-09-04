"""Worker de borde: lee la camara, corre los detectores y emite eventos.

Estado actual (Fase 1): el esqueleto de captura esta completo y funcionando.
Los detectores todavia no estan conectados -- eso es Fase 2 (placas), Fase 4
(rostros) y Fase 5 (armas). Ejecutalo hoy para verificar que tu fuente de video
funciona antes de meterle modelos encima:

    python -m edge.worker --diagnostico

Con la webcam:            SOURCE=webcam:0
Con la Hikvision:         SOURCE=rtsp://admin:pass@192.168.1.64:554/Streaming/Channels/102
Con un video grabado:     SOURCE=file:videos/prueba.mp4

El codigo de abajo NO cambia al pasar de una a otra. Ese es justamente el punto
de edge/sources.py: la camara es un detalle de configuracion, no de codigo.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from edge.config import EdgeConfig, load_config  # noqa: E402
from edge.sources import LiveSource, open_source  # noqa: E402

log = logging.getLogger("edge.worker")

_detener = False


def _manejar_senal(signum, frame):  # noqa: ARG001
    """Ctrl+C limpio: cierra la camara en vez de dejar el handle colgado.
    En Windows un VideoCapture sin liberar deja la webcam ocupada hasta que
    se cierra el proceso padre."""
    global _detener
    if _detener:
        log.warning("Segunda senal recibida, saliendo a la fuerza")
        sys.exit(1)
    log.info("Senal recibida, cerrando ordenadamente...")
    _detener = True


def diagnostico(cfg: EdgeConfig, segundos: float = 20.0) -> int:
    """Mide la salud de la fuente sin cargar ningun modelo.

    Es la primera prueba que hay que pasar: si aqui los fps son inestables o la
    latencia crece, ningun modelo lo va a arreglar. Separar este diagnostico de
    la inferencia evita perder horas culpando a la GPU de un problema de red.
    """
    print("=" * 68)
    print(f"DIAGNOSTICO DE FUENTE  ({segundos:.0f}s)")
    print("=" * 68)
    print(f"  camera_id : {cfg.camera_id}")
    print(f"  source    : {_ocultar(cfg.source)}")
    print(f"  device    : {cfg.resolve_device()}")
    print(f"  infer_fps : {cfg.infer_fps}")
    print()

    try:
        fuente = open_source(cfg.source)
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
        print(f"  {'t':>5}  {'fps_real':>9}  {'procesados':>10}  {'perdidos':>9}  {'latencia':>9}")
        print("  " + "-" * 52)

        inicio = time.monotonic()
        procesados = 0
        latencia_max = 0.0
        ultimo_reporte = inicio

        for frame in fuente.frames(max_fps=cfg.infer_fps):
            if _detener:
                break
            procesados += 1
            latencia_max = max(latencia_max, frame.age)

            # Aqui es donde entraran los detectores en la Fase 2.
            # placas.procesar(frame) -> [DetectionEvent, ...]

            ahora = time.monotonic()
            if ahora - ultimo_reporte >= 2.0:
                st = fuente.status
                print(f"  {ahora - inicio:5.0f}  {st.measured_fps:9.1f}  {procesados:10d}"
                      f"  {st.frames_dropped:9d}  {latencia_max * 1000:7.0f}ms")
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

        # Los descartes NO son un error: son el mecanismo que mantiene la
        # latencia baja. Lo preocupante seria lo contrario.
        if st.reconnects > 0:
            print("\n  [!] Hubo reconexiones: la red o la camara son inestables.")
            print("      Con Wi-Fi es comun; por cable suele desaparecer.")
        if procesados < cfg.infer_fps * transcurrido * 0.7:
            print("\n  [!] No se alcanzo el fps objetivo. La fuente entrega menos")
            print("      de lo pedido, o la red no da abasto.")
        else:
            print("\n  [OK] Fuente estable. Lista para conectarle detectores (Fase 2).")
        print("=" * 68)

    return 0


def construir_detectores(cfg: EdgeConfig) -> list:
    """Instancia los detectores activados en la configuracion.

    Agregar el de armas en la Fase 5 sera anadir tres lineas aqui: el worker no
    necesita saber nada de sus modelos.
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

    if not detectores:
        raise RuntimeError("No hay ningun detector activo. Revisa ENABLE_* en el .env")
    return detectores


def ejecutar(cfg: EdgeConfig, segundos: Optional[float] = None) -> int:
    """Bucle principal: captura -> detectores -> eventos."""
    from edge.preview import crear_publicador
    from edge.sink import crear_sink

    print("=" * 68)
    print("WORKER DE BORDE")
    print("=" * 68)
    print(f"  camara    : {cfg.camera_id}  ({_ocultar(cfg.source)})")
    print(f"  device    : {cfg.resolve_device()}")
    print(f"  infer_fps : {cfg.infer_fps}")
    print()

    detectores = construir_detectores(cfg)
    print(f"  detectores: {', '.join(d.name for d in detectores)}")

    sink = crear_sink(cfg)
    preview = crear_publicador(cfg)
    fuente = open_source(cfg.source)
    total_eventos = 0

    try:
        with fuente:
            if isinstance(fuente, LiveSource) and not fuente.wait_until_ready(15.0):
                print("[x] La fuente no entrego frames. Corre --diagnostico.")
                return 1

            print("\n  Detectando. Ctrl+C para detener.\n")
            inicio = time.monotonic()
            ultimo_reporte = inicio
            frames = 0

            # esperar_cortes: un corte de la camara NO termina el worker. Es un
            # sistema que corre sin nadie mirando; si un parpadeo de red lo
            # apaga, nadie se entera hasta que alguien revisa al dia siguiente.
            # El diagnostico usa el valor por defecto (terminar), que es lo que
            # se quiere de una herramienta que mide y sale.
            for frame in fuente.frames(max_fps=cfg.infer_fps, esperar_cortes=True):
                if _detener:
                    break
                frames += 1

                for det in detectores:
                    for evento in det.procesar(frame):
                        sink.enviar(evento)
                        total_eventos += 1

                # El frame anotado se calcula UNA sola vez y sirve para las dos
                # cosas que lo quieren: la ventana local de depuracion y la
                # vista en vivo del dashboard. Se pregunta primero para no
                # dibujar cajas que nadie va a ver.
                para_preview = preview is not None and preview.quiere_frame()
                if cfg.show_window or para_preview:
                    vista = frame.frame.copy()
                    for det in detectores:
                        vista = det.anotar(vista)

                    if para_preview:
                        preview.publicar(vista)

                    if cfg.show_window:
                        cv2.imshow("Videovigilancia - 'q' para salir", vista)
                        if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                            break

                ahora = time.monotonic()
                if ahora - ultimo_reporte >= 10.0:
                    st = fuente.status
                    partes = " | ".join(f"{d.name}: {d.resumen}" for d in detectores)
                    mirando = ""
                    if preview is not None and preview.espectadores:
                        mirando = f" | {preview.espectadores} viendo"
                    print(f"  [{ahora - inicio:5.0f}s] {frames} frames, "
                          f"{st.measured_fps:.1f} fps camara, {total_eventos} eventos "
                          f"| {partes}{mirando}")
                    ultimo_reporte = ahora

                if segundos and (ahora - inicio) >= segundos:
                    break

            # Los objetos que seguian en pantalla al detener tambien cuentan.
            print("\n  Cerrando tracks abiertos...")
            for det in detectores:
                for evento in det.vaciar():
                    sink.enviar(evento)
                    total_eventos += 1
    finally:
        if cfg.show_window:
            cv2.destroyAllWindows()
        if preview is not None:
            preview.cerrar()
        for det in detectores:
            print(f"\n  Estadisticas de {det.name}: {det.stats}")
            det.cerrar()
        sink.cerrar()

    print(f"\n  Total de eventos emitidos: {total_eventos}")
    return 0


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
                   help="Mostrar ventana con las detecciones dibujadas")
    p.add_argument("--source", help="Sobrescribe SOURCE del .env")
    args = p.parse_args()

    cfg = load_config()
    if args.source:
        cfg.source = args.source

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx escribe una linea INFO por cada peticion. Con la vista en vivo eso
    # son PREVIEW_FPS lineas por segundo (10 con la configuracion actual), y el
    # reporte periodico de deteccion -- lo unico que de verdad se mira aqui --
    # queda enterrado. No aporta nada que el worker no diga mejor por su cuenta:
    # los fallos de envio ya los reporta el sink y el preview con su propio
    # mensaje. Se deja en WARNING para no perder los problemas reales.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    signal.signal(signal.SIGINT, _manejar_senal)

    if args.ventana:
        cfg.show_window = True

    if args.diagnostico:
        return diagnostico(cfg, args.segundos)
    return ejecutar(cfg, args.segundos if args.limitar else None)


if __name__ == "__main__":
    raise SystemExit(main())
