"""Placa de prueba imprimible para la demostracion, validada con los modelos reales.

    python tools/placa_demo.py                      # ZTP-482-A
    python tools/placa_demo.py --placa ZKP-631-B
    python tools/placa_demo.py --sin-validar        # solo genera los archivos

Genera en demo/:

    placa_ZTP482A.png   imagen de la placa (2000 x 1000 px)
    placa_ZTP482A.pdf   hoja tamano carta horizontal para imprimir (28 x 14 cm)
y en docs/evidencias/ validacion_placa_ZTP482A.csv con el resultado de la prueba.

POR QUE UNA SERIE SIN ESTADO
La serie por defecto (ZTP) no esta asignada a ninguna entidad en las tablas
de shared/plates.py: el accesorio de papel no debe coincidir con la placa de
un vehiculo real. La alerta dira "Automovil particular" sin entidad.

LA VALIDACION
Pasa la placa por el MISMO detector y OCR que usa el worker, a varios
tamanos (los pixeles que mediria a distintas distancias), de lado, borrosa y
con poca luz. No sustituye la prueba con la camara real: la escena es generada
(la placa sobre la parte trasera de un vehiculo dibujado), no una foto de la
placa impresa.

LO QUE ENSENO LA VALIDACION (2026-10-06, umbral de produccion PLATE_CONF)
  - Cuando el detector encuentra la placa, el OCR la lee bien SIEMPRE.
  - El detector, en cambio, acepta la placa dibujada solo en la mitad de las
    escenas, y mejor entre 140 y 240 px de ancho que muy de cerca. Esta
    entrenado con fotos de placas reales: una placa de papel sin relieve ni
    fondo de estado no se le parece del todo.
  - Se apoya en el contexto: la misma placa flotando sola en la imagen casi
    no se detecta; sobre la trasera de un vehiculo, si.
Para la demo: el accesorio principal es la FOTO a color de una placa real (el
auto de un integrante, con su permiso) impresa a tamano real; esta placa
queda como respaldo, pegada en un carton oscuro con forma de defensa, de
frente y a ~1 m de la camara. Sirve para saber, antes de imprimir, que el diseno se
detecta y se lee.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from shared.plates import analizar_placa, corregir_placa, limpiar  # noqa: E402

FUENTES = Path("C:/Windows/Fonts")
ANCHO, ALTO = 2000, 1000                 # proporcion 2:1, como la placa de 30 x 15 cm


def _fuente(nombres: list[str], tamano: int) -> ImageFont.FreeTypeFont:
    for nombre in nombres:
        for carpeta in (FUENTES, Path("/usr/share/fonts/truetype/dejavu")):
            ruta = carpeta / nombre
            if ruta.exists():
                return ImageFont.truetype(str(ruta), tamano)
    return ImageFont.load_default(tamano)


def dibujar_placa(texto: str) -> Image.Image:
    """Placa blanca con texto chico arriba y abajo y caracteres grandes al
    centro, como una placa mexicana, pero sin escudos ni nombre de estado: dice
    claramente que es de prueba. Se probaron otras variantes (franja de color
    arriba, solo caracteres): esta es la que el detector reconoce con mas
    confianza."""
    img = Image.new("RGB", (ANCHO, ALTO), "white")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((12, 12, ANCHO - 12, ALTO - 12), radius=60, outline=(30, 30, 30), width=20)
    arriba = _fuente(["ARIALNB.TTF", "arialbd.ttf", "DejaVuSans-Bold.ttf"], 120)
    d.text((ANCHO / 2, 120), "PRUEBA  GOSS IP", font=arriba, fill=(20, 40, 110), anchor="mm")

    # Los caracteres ocupan ~84 % del ancho: si se salen del borde, el
    # detector deja de verla como placa.
    tamano = 600
    principal = _fuente(["ARIALNB.TTF", "arialbd.ttf", "DejaVuSansCondensed-Bold.ttf"], tamano)
    while d.textlength(texto, font=principal) > ANCHO * 0.84 and tamano > 200:
        tamano -= 10
        principal = _fuente(["ARIALNB.TTF", "arialbd.ttf", "DejaVuSansCondensed-Bold.ttf"], tamano)
    d.text((ANCHO / 2, 540), texto, font=principal, fill=(0, 0, 0), anchor="mm")

    abajo = _fuente(["ARIALN.TTF", "arial.ttf", "DejaVuSans.ttf"], 90)
    d.text((ANCHO / 2, 900), "SOLO DEMOSTRACIÓN", font=abajo, fill=(60, 60, 60), anchor="mm")
    return img


def hoja_para_imprimir(placa: Image.Image, destino: Path) -> None:
    """Carta horizontal a 200 ppp con la placa a 28 x 14 cm y marcas de corte.
    (La placa real mide 30.5 cm; 28 cm es lo que cabe con margen de
    impresora. Para calcular distancias, medir la impresa.)"""
    ppp = 200
    hoja = Image.new("RGB", (int(27.94 / 2.54 * ppp), int(21.59 / 2.54 * ppp)), "white")
    ancho = int(28.0 / 2.54 * ppp)
    if ancho > hoja.width - 40:
        ancho = hoja.width - 40
    alto = ancho // 2
    p = placa.resize((ancho, alto), Image.LANCZOS)
    x, y = (hoja.width - ancho) // 2, (hoja.height - alto) // 2
    hoja.paste(p, (x, y))
    d = ImageDraw.Draw(hoja)
    for cx, cy in ((x, y), (x + ancho, y), (x, y + alto), (x + ancho, y + alto)):
        d.line((cx - 30, cy, cx + 30, cy), fill=(150, 150, 150), width=2)
        d.line((cx, cy - 30, cx, cy + 30), fill=(150, 150, 150), width=2)
    nota = _fuente(["arial.ttf", "DejaVuSans.ttf"], 26)
    d.text((hoja.width / 2, y + alto + 70),
           "Imprimir al 100 % (sin ajustar a la página). Recortar por las marcas y pegar en cartón.",
           font=nota, fill=(110, 110, 110), anchor="mm")
    hoja.save(destino, "PDF", resolution=ppp)


# --------------------------------------------------------------------------
# Escenas de prueba
# --------------------------------------------------------------------------

def _fondo(rng: np.random.Generator) -> np.ndarray:
    """Calle generica de 1280x720: asfalto con textura, no un color liso."""
    base = rng.normal(95, 22, (720, 1280, 3)).clip(0, 255).astype(np.uint8)
    base = cv2.GaussianBlur(base, (0, 0), 5)
    base[:300] = cv2.GaussianBlur(rng.normal(150, 30, (300, 1280, 3)).clip(0, 255).astype(np.uint8), (0, 0), 9)
    return base


def _trasera(ancho_placa: int, rng: np.random.Generator) -> np.ndarray:
    """Parte trasera de un vehiculo a la MISMA escala que la placa (la placa
    mide ~17 % del ancho de un auto): al alejarse se encogen juntas. El
    detector se apoya en ese contexto; una placa flotando sola en la imagen
    se detecta mucho peor (medido)."""
    w = int(ancho_placa / 0.17)
    h = int(w * 0.55)
    color = tuple(int(c) for c in rng.integers(40, 170, 3))
    auto = np.zeros((h, w, 3), np.uint8)
    auto[:] = color
    cv2.rectangle(auto, (0, int(h * 0.72)), (w, h), (35, 35, 35), -1)                      # defensa
    cv2.rectangle(auto, (int(w * 0.04), int(h * 0.30)), (int(w * 0.20), int(h * 0.48)), (30, 30, 200), -1)
    cv2.rectangle(auto, (int(w * 0.80), int(h * 0.30)), (int(w * 0.96), int(h * 0.48)), (30, 30, 200), -1)
    cv2.rectangle(auto, (int(w * 0.10), 0), (int(w * 0.90), int(h * 0.22)), (60, 60, 60), -1)  # medallon
    return auto


def escena(placa_bgr: np.ndarray, ancho_px: int, angulo: float = 0.0, desenfoque: float = 0.0,
           luz: float = 1.0, semilla: int = 0) -> np.ndarray:
    rng = np.random.default_rng(semilla)
    fondo = _fondo(rng)
    auto = _trasera(ancho_px, rng)
    alto_px = ancho_px // 2
    p = cv2.resize(placa_bgr, (ancho_px, alto_px), interpolation=cv2.INTER_AREA)
    ah, aw = auto.shape[:2]
    px0, py0 = (aw - ancho_px) // 2, int(ah * 0.40)
    auto[py0:py0 + alto_px, px0:px0 + ancho_px] = p

    # De lado: perspectiva sobre el auto completo (la placa va pegada a el).
    comp = math.cos(math.radians(angulo))
    lejos = 1.0 - 0.25 * math.sin(math.radians(angulo))
    wd = max(int(aw * comp), 1)
    origen = np.float32([[0, 0], [aw, 0], [aw, ah], [0, ah]])
    destino = np.float32([[0, 0], [wd, ah * (1 - lejos) / 2], [wd, ah * (1 + lejos) / 2], [0, ah]])
    m = cv2.getPerspectiveTransform(origen, destino)
    warp = cv2.warpPerspective(auto, m, (wd, ah))
    mascara = cv2.warpPerspective(np.full((ah, aw), 255, np.uint8), m, (wd, ah))

    cx, cy = ((640, 470), (520, 500), (760, 440))[semilla % 3]
    x0, y0 = max(0, cx - wd // 2), max(0, cy - ah // 2)
    x1, y1 = min(1280, x0 + wd), min(720, y0 + ah)
    region = fondo[y0:y1, x0:x1]
    sub_m = mascara[: y1 - y0, : x1 - x0] > 0
    region[sub_m] = warp[: y1 - y0, : x1 - x0][sub_m]
    if desenfoque:
        fondo = cv2.GaussianBlur(fondo, (0, 0), desenfoque)
    if luz != 1.0:
        fondo = (fondo.astype(np.float32) * luz).clip(0, 255).astype(np.uint8)
        fondo = (fondo + rng.normal(0, 6, fondo.shape)).clip(0, 255).astype(np.uint8)
    return fondo


SEMILLAS = 4
CONDICIONES = (
    [("De frente", px, 0, 0, 1.0) for px in (300, 240, 200, 160, 140, 120, 100)]
    + [("De lado 30°", 220, 30, 0, 1.0), ("De lado 45°", 220, 45, 0, 1.0),
       ("Desenfocada", 220, 0, 1.6, 1.0), ("Poca luz", 220, 0, 0, 0.35)]
)
ANCHO_IMPRESO_M = 0.28


def validar(placa_bgr: np.ndarray, texto: str, destino_csv: Path) -> list[dict]:
    from tools.calibrar_distancia import Calibrador

    from edge.config import load_config

    # El umbral de deteccion de produccion (PLATE_CONF), no uno permisivo.
    cal = Calibrador(umbral_deteccion=load_config().plate_conf)
    esperado = limpiar(texto)
    filas = []
    print(f"{'Condicion':14} {'placa':>7} {'detecta':>8} {'lectura':>10} {'conf':>5}  resultado")
    for i, (nombre, px, angulo, blur, luz) in enumerate(CONDICIONES):
        aciertos, lecturas = 0, []
        for semilla in range(SEMILLAS):           # varias escenas por condicion
            r = cal.analizar(escena(placa_bgr, px, angulo, blur, luz, semilla=i * 10 + semilla))
            # Igual que el worker: la lectura se corrige por formato mexicano
            # (un 2 en posicion de letra es Z) antes de compararla.
            corregida = corregir_placa(r["texto"] or "")
            leido = corregida[0] if corregida else (r["texto"] or "")
            lecturas.append((r["detecta"], leido, r.get("conf_ocr", 0.0)))
            aciertos += int(leido == esperado)
        detecta = sum(1 for d, _, _ in lecturas if d)
        ultima = next((t for _, t, _ in reversed(lecturas) if t), "")
        conf = max(c for _, _, c in lecturas)
        resultado = f"{aciertos}/{SEMILLAS}"
        filas.append({"condicion": nombre, "placa_px": px, "angulo": angulo, "desenfoque": blur,
                      "luz": luz, "detecta": f"{detecta}/{SEMILLAS}", "lecturas_correctas": resultado,
                      "aciertos": aciertos,
                      "ejemplo_lectura": ultima, "conf_ocr_max": round(conf, 2)})
        print(f"{nombre:14} {px:>5}px {detecta:>6}/{SEMILLAS} {ultima:>10} {conf:>5.2f}  {resultado} correctas")
    with destino_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0]))
        w.writeheader()
        w.writerows(filas)
    return filas


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--placa", default="ZTP-482-A")
    p.add_argument("--sin-validar", action="store_true")
    args = p.parse_args()

    info = analizar_placa(limpiar(args.placa))
    if info is None or info.extranjera:
        print(f"[x] '{args.placa}' no tiene formato de placa mexicana.")
        return 1
    if info.entidad:
        print(f"[!] La serie de {info.legible} corresponde a {info.entidad}: podria coincidir con un "
              "vehiculo real. Mejor una serie sin entidad (ZTP, ZKP...).")

    carpeta = RAIZ / "demo"
    carpeta.mkdir(exist_ok=True)
    placa = dibujar_placa(info.legible)
    png = carpeta / f"placa_{info.texto}.png"
    pdf = carpeta / f"placa_{info.texto}.pdf"
    placa.save(png)
    hoja_para_imprimir(placa, pdf)
    print(f"[OK] {png.relative_to(RAIZ)}  y  {pdf.relative_to(RAIZ)}")
    print(f"     Para la lista negra: {info.legible} ({info.tipo})")
    if args.sin_validar:
        return 0

    print("\nValidando con el detector y el OCR del worker (imagen generada, no foto)...\n")
    bgr = cv2.cvtColor(np.array(placa), cv2.COLOR_RGB2BGR)
    evidencias = RAIZ / "docs" / "evidencias"
    evidencias.mkdir(parents=True, exist_ok=True)
    filas = validar(bgr, info.legible, evidencias / f"validacion_placa_{info.texto}.csv")
    frente = [f for f in filas if f["angulo"] == 0 and f["desenfoque"] == 0 and f["luz"] == 1.0]
    # Umbral: el tamano mas chico desde el que TODOS los mas grandes se leen
    # en al menos la mitad de las escenas.
    umbral = None
    for f in sorted(frente, key=lambda x: -x["placa_px"]):
        if f["aciertos"] * 2 >= SEMILLAS:
            umbral = f["placa_px"]
        else:
            break
    if umbral is None:
        print("\n[!] El diseno no se lee ni de cerca: no imprimas esta placa, revisa el diseno.")
        return 1
    from shared.instalacion import LENTES_HFOV, alcance_m

    print(f"\nLa placa impresa se lee desde ~{umbral} px de ancho en el cuadro analizado.")
    print(f"Distancia maxima para sostenerla frente a la camara (sub-stream de 1280 px, "
          f"placa impresa de {ANCHO_IMPRESO_M * 100:.0f} cm):")
    for mm, hfov in LENTES_HFOV.items():
        print(f"   lente {mm:>3} mm: {alcance_m(1280, hfov, ANCHO_IMPRESO_M, umbral):.1f} m")
    print("Consejos: pegala en un carton oscuro con forma de defensa, de frente y quieta 2-3 s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
