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
if sys.platform == "win32":  # ver nota en edge/detectors/plates.py
    pathlib.PosixPath = pathlib.WindowsPath

RAIZ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from shared.plates import es_placa_valida, formatear, normalizar  # noqa: E402

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
    def __init__(self, umbral_deteccion: float = 0.35, umbral_ocr: float = 0.30) -> None:
        import torch

        print("Cargando modelo de placas...")
        self.model = torch.hub.load(
            str(RAIZ / "yolov5"), "custom",
            path=str(RAIZ / "models" / "plates_yolov5.pt"),
            source="local", force_reload=False, verbose=False,
        )
        # Umbral mas bajo que en produccion: aqui interesa saber DONDE deja de
        # detectar, no filtrar. Con el umbral normal no se veria la degradacion.
        self.model.conf = umbral_deteccion
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model.to(self.device)

        print("Cargando EasyOCR...")
        import easyocr

        self.reader = easyocr.Reader(["es", "en"], gpu=(self.device == "cuda"), verbose=False)
        self.umbral_ocr = umbral_ocr
        print(f"Listo (device={self.device})\n")

    def analizar(self, img: np.ndarray) -> dict:
        """Detecta la placa y trata de leerla. Devuelve metricas."""
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        res = self.model(rgb, size=640)
        cajas = res.xyxy[0].cpu().numpy()

        if len(cajas) == 0:
            return {"detecta": False, "plate_px": 0, "texto": None, "conf_ocr": 0.0}

        mejor = max(cajas, key=lambda c: (c[2] - c[0]) * (c[3] - c[1]))
        x1, y1, x2, y2 = (int(v) for v in mejor[:4])
        plate_px = x2 - x1

        h, w = img.shape[:2]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        recorte = img[y1:y2, x1:x2]

        salida = {"detecta": True, "plate_px": plate_px, "conf_det": float(mejor[4]),
                  "texto": None, "conf_ocr": 0.0, "es_placa": False}
        if recorte.size == 0:
            return salida

        # Mismo preprocesado que el detector en produccion, para que el numero
        # medido aqui sea el que se va a obtener de verdad.
        alto = recorte.shape[0]
        if alto < 64:
            escala = min(4.0, 64 / max(alto, 1))
            recorte = cv2.resize(recorte, None, fx=escala, fy=escala,
                                 interpolation=cv2.INTER_CUBIC)
        lab = cv2.cvtColor(recorte, cv2.COLOR_BGR2LAB)
        lab[:, :, 0] = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(lab[:, :, 0])
        recorte = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

        try:
            lecturas = [(t, float(c)) for _, t, c in self.reader.readtext(recorte)]
        except Exception:  # noqa: BLE001
            lecturas = []

        # Quedarse con el texto de mayor confianza NO funciona aqui: una placa
        # mexicana lleva impreso el estado ("EDOMEX") y a veces un lema
        # ("COMPROMISO"), y esos se leen MEJOR que el numero porque usan letras
        # mas grandes y limpias. Hay que preferir lo que tenga forma de placa.
        # Es la misma logica que shared.plates.elegir_mejor_lectura usa en
        # produccion; sin ella, esta calibracion mediria la legibilidad del
        # nombre del estado en vez de la de la matricula.
        validas = [(t, c) for t, c in lecturas if es_placa_valida(t)[0]]
        if validas:
            salida["texto"], salida["conf_ocr"] = max(validas, key=lambda x: x[1])
            salida["es_placa"] = True
        elif lecturas:
            # Sin candidata valida se guarda la mejor cruda, solo para ver en la
            # tabla como se degrada. No cuenta como lectura correcta.
            salida["texto"], salida["conf_ocr"] = max(lecturas, key=lambda x: x[1])
            salida["es_placa"] = False
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
                   help="Carpeta con fotos de placas (default: imagenes-prueba)")
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
