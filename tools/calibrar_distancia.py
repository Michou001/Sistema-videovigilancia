"""Mide hasta que distancia el sistema puede leer una placa.

    python tools/calibrar_distancia.py

LA IDEA
-------
No hace falta cinta metrica ni ir al estacionamiento. Una placa a 8 metros
ocupa en el sensor los mismos pixeles que una foto de cerca reducida al 12%.
Asi que se toma una foto real de una placa, se reduce progresivamente, y se
busca el punto exacto donde el detector deja de encontrarla o el OCR deja de
leerla.

Eso da el UMBRAL EN PIXELES, que es la propiedad real del sistema. Convertirlo
a metros es despues pura geometria, y depende solo de la resolucion y el lente
de la camara -- no del lugar donde se instale.

POR QUE IMPORTA
---------------
La distancia de lectura no se arregla con un modelo mejor: si la placa ocupa
15 pixeles, la informacion no esta ahi. Saber el numero exacto convierte
"esperemos que alcance" en un requisito de instalacion verificable.
"""

from __future__ import annotations

import argparse
import math
import pathlib
import sys
import warnings

warnings.filterwarnings("ignore")

RAIZ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from shared.plates import es_placa_valida, normalizar  # noqa: E402

# Dimensiones oficiales de la placa mexicana (NOM-001-SCT-2-2016).
ANCHO_PLACA_M = 0.305

# Campos de vision horizontales tipicos de camaras bala Hikvision.
LENTES = {
    "2.8 mm (gran angular)": 106.0,
    "4 mm (estandar)": 84.0,
    "6 mm (teleobjetivo)": 54.0,
}


def distancia_para(plate_px: float, ancho_imagen_px: int, fov_grados: float) -> float:
    """Metros a los que una placa se ve con `plate_px` pixeles de ancho.

    Geometria: el ancho real que abarca la camara a distancia d es
        A(d) = 2 * d * tan(FOV/2)
    y la placa ocupa una fraccion ANCHO_PLACA_M / A(d) del encuadre.
    """
    mitad = math.radians(fov_grados / 2.0)
    return (ANCHO_PLACA_M * ancho_imagen_px) / (2.0 * plate_px * math.tan(mitad))


class Calibrador:
    """Usa el MISMO detector y OCR que el worker (edge/detectors/plates.py),
    para que el umbral medido aqui sea el que se obtiene en operacion."""

    def __init__(self, umbral_deteccion: float = 0.30) -> None:
        from edge.config import load_config
        from edge.detectors.faces import configurar_onnx_gpu

        configurar_onnx_gpu()
        from fast_plate_ocr import LicensePlateRecognizer
        from open_image_models import LicensePlateDetector

        cfg = load_config()
        self.device = cfg.resolve_device()
        proveedores = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                       if self.device == "cuda" else ["CPUExecutionProvider"])
        print("Cargando modelos de placas...")
        # Umbral mas bajo que en produccion: aqui interesa saber DONDE deja de
        # detectar, no filtrar. Con el umbral normal no se veria la degradacion.
        self.detector = LicensePlateDetector(detection_model=cfg.plate_detector_model,
                                             conf_thresh=umbral_deteccion, providers=proveedores)
        self.ocr = LicensePlateRecognizer(cfg.plate_ocr_model, providers=proveedores)
        print(f"Listo (device={self.device})\n")

    def analizar(self, img: np.ndarray) -> dict:
        """Detecta la placa y trata de leerla. Devuelve metricas."""
        cajas = self.detector.predict(img)
        if not cajas:
            return {"detecta": False, "plate_px": 0, "texto": None, "conf_ocr": 0.0}

        mejor = max(cajas, key=lambda r: r.bounding_box.width * r.bounding_box.height)
        b = mejor.bounding_box
        h, w = img.shape[:2]
        x1, y1, x2, y2 = max(0, b.x1), max(0, b.y1), min(w, b.x2), min(h, b.y2)
        salida = {"detecta": True, "plate_px": x2 - x1, "conf_det": float(mejor.confidence),
                  "texto": None, "conf_ocr": 0.0, "es_placa": False}
        recorte = img[y1:y2, x1:x2]
        if recorte.size == 0:
            return salida

        color = self.ocr.config.image_color_mode
        if color == "grayscale":
            recorte = cv2.cvtColor(recorte, cv2.COLOR_BGR2GRAY)
        elif color == "rgb":
            recorte = cv2.cvtColor(recorte, cv2.COLOR_BGR2RGB)
        pred = self.ocr.run_one(recorte, return_confidence=True)
        texto = (pred.plate or "").replace("_", "")
        conf = float(np.mean(pred.char_probs[:max(1, len(texto))])) if pred.char_probs is not None else 0.0
        salida["texto"], salida["conf_ocr"] = texto, conf
        salida["es_placa"] = es_placa_valida(texto)[0]
        return salida


def calibrar(ruta: pathlib.Path, cal: Calibrador, esperado: str | None) -> list[dict]:
    original = cv2.imread(str(ruta))
    if original is None:
        print(f"  [x] no se pudo leer {ruta.name}")
        return []

    base = cal.analizar(original)
    if not base["detecta"]:
        print(f"  [x] {ruta.name}: no se detecta la placa ni a resolucion completa")
        return []
    if not esperado and not base.get("es_placa"):
        print(f"\n  [!] {ruta.name}: a resolucion completa el OCR no produce nada con")
        print(f"      formato de placa (leyo '{base['texto']}'). Sin referencia fiable")
        print("      se omite; pasa --esperado con el texto real para incluirla.")
        return []

    px_base = base["plate_px"]
    print(f"\n{'='*74}")
    print(f"{ruta.name}  ({original.shape[1]}x{original.shape[0]})")
    print(f"  placa a resolucion completa: {px_base} px de ancho")
    if base["texto"]:
        print(f"  lectura de referencia: '{base['texto']}' (conf {base['conf_ocr']:.2f})")
    referencia = esperado or (base["texto"] or "")
    print(f"{'='*74}")
    print(f"  {'escala':>7} {'placa px':>9} {'detecta':>8} {'OCR lee':>26} {'conf':>6}  {'ok':>4}")
    print("  " + "-" * 70)

    filas = []
    for escala in (1.0, 0.8, 0.6, 0.5, 0.4, 0.3, 0.25, 0.2, 0.15, 0.12, 0.1, 0.08):
        nuevo = (max(1, int(original.shape[1] * escala)), max(1, int(original.shape[0] * escala)))
        chico = cv2.resize(original, nuevo, interpolation=cv2.INTER_AREA)
        r = cal.analizar(chico)

        texto = r["texto"] or "-"
        correcto = (bool(r["texto"]) and r.get("es_placa")
                    and normalizar(r["texto"]) == normalizar(referencia))
        marca = "SI" if correcto else ("~" if r["texto"] else "")
        print(f"  {escala:7.2f} {r['plate_px']:9} {'si' if r['detecta'] else 'NO':>8} "
              f"{texto[:26]:>26} {r['conf_ocr']:6.2f}  {marca:>4}")

        filas.append({"escala": escala, "plate_px": r["plate_px"],
                      "detecta": r["detecta"], "correcto": correcto})
    return filas


def resumen(todas: list[dict]) -> None:
    if not todas:
        return

    correctas = [f["plate_px"] for f in todas if f["correcto"]]
    detectadas = [f["plate_px"] for f in todas if f["detecta"] and f["plate_px"] > 0]
    if not correctas:
        print("\nNinguna lectura correcta: no se puede calcular el umbral.")
        return

    px_ocr = min(correctas)
    px_det = min(detectadas) if detectadas else 0

    print(f"\n{'='*74}")
    print("UMBRALES MEDIDOS")
    print(f"{'='*74}")
    print(f"  Detecta la placa desde : {px_det} px de ancho")
    print(f"  LEE la placa desde     : {px_ocr} px de ancho   <-- el que manda")
    print()
    print("  Leer siempre exige mas pixeles que detectar. El limite real del")
    print("  sistema es el del OCR: una caja sin texto no sirve para la lista negra.")

    print(f"\n{'='*74}")
    print(f"DISTANCIA MAXIMA DE LECTURA  (placa de {ANCHO_PLACA_M*100:.0f} cm, {px_ocr} px)")
    print(f"{'='*74}")
    print(f"  {'lente':<24} {'sub 1280px':>14} {'main 3200px':>14}")
    print("  " + "-" * 56)
    for nombre, fov in LENTES.items():
        d_sub = distancia_para(px_ocr, 1280, fov)
        d_main = distancia_para(px_ocr, 3200, fov)
        print(f"  {nombre:<24} {d_sub:11.1f} m {d_main:11.1f} m")

    print("\n  Estas cifras suponen la placa DE FRENTE. En angulo se acorta:")
    print("  a 45 grados la placa se ve ~30% mas angosta, asi que multiplica por 0.7.")
    print()
    print("  Conclusiones para la instalacion:")
    print("   - Para placas usa el MAIN stream (3200px). El sub no alcanza.")
    print("   - Manda el lente: uno de 6 mm triplica el alcance de uno de 2.8 mm,")
    print("     a costa de cubrir menos ancho. Para leer placas eso es buen trato.")
    print("   - Apunta la camara al carril por donde pasan los coches, no al")
    print("     estacionamiento entero desde una esquina.")
    print(f"{'='*74}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--carpeta", default="imagenes-prueba",
                   help="Carpeta con fotos propias de placas, de frente y de cerca "
                        "(default: imagenes-prueba, no se sube al repositorio)")
    p.add_argument("--esperado", help="Texto real de la placa, si todas las fotos son la misma")
    args = p.parse_args()

    carpeta = RAIZ / args.carpeta
    imagenes = sorted(
        [f for f in carpeta.iterdir()
         if f.suffix.lower() in {".jpg", ".jpeg", ".png"}]
    ) if carpeta.is_dir() else []

    if not imagenes:
        print(f"No hay imagenes en {carpeta}")
        return 1

    print(f"Calibrando con {len(imagenes)} imagen(es) de {carpeta.name}\n")
    cal = Calibrador()

    todas: list[dict] = []
    for img in imagenes:
        todas.extend(calibrar(img, cal, args.esperado))
    resumen(todas)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
