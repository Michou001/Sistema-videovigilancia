"""Loop del logo de GOSS IP para el monitor del stand (1920x1080, 12 s, sin corte).

    python tools/marca/loop_logo.py 3_mercadotecnia/loop/GOSS_IP_Loop_Stand.mp4
    python tools/marca/loop_logo.py salida.mp4 --cuadros 0,150,359   # solo PNG de revision

La frase, la linea de identidad y el pie estan en FRASE, IDENTIDAD y PIE.
Con imageio-ffmpeg instalado codifica con x264 (~3 MB); sin el, usa el H.264
de OpenCV (mismo video, archivo mucho mas pesado). El logo sale de
logo-goss-ip.svg, la traza vectorial del logo del press kit.

Escenas:
  0.3-2.4 s  el ojo enfoca (desenfoque -> nitido) y el iris gira como diafragma
  1.9-4.2 s  aparecen "Goss IP", la frase y la linea de identidad
  ~7.0 s     parpadeo
  11.0-12 s  todo se desvanece; el cuadro final empata con el inicial
La reticula de fondo (anillos, escala y arcos de escaneo) gira en multiplos
exactos de su simetria, asi que el corte del loop no se nota.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, str(Path(__file__).parent))
from logo_vector import dibujar, leer  # noqa: E402

W, H, FPS, DUR = 1920, 1080, 30, 12.0
FRASE = "El ojo que no se distrae: ve, entiende y avisa a tiempo."
IDENTIDAD = "VIDEOVIGILANCIA INTELIGENTE  ·  PROCESADA EN EL SITIO  ·  CON EVIDENCIA"
PIE = "Reto InnovaTIC's UAEMéx 2026"
FUENTES = Path("C:/Windows/Fonts")

# ------------------------------------------------------------------ capas
formas = leer()
IRIS = {4, 13, 18, 19, 23, 24, 25, 27}          # segmentos y pupila (giran)
TEXTO = {12, 15, 16, 20, 21, 22}                 # "Goss IP"
ojo_f = [f for i, f in enumerate(formas, 1) if i not in IRIS | TEXTO]
iris_f = [f for i, f in enumerate(formas, 1) if i in IRIS]
texto_f = [f for i, f in enumerate(formas, 1) if i in TEXTO]

K = 640 / 466.0                     # px por unidad del SVG
OJO_C = (296.0, 197.8)              # centro de la almendra
IRIS_C = (297.8, 205.8)             # centro del anillo azul marino
CX, CY = W / 2, 330                 # centro del ojo en pantalla
KM = K * 0.74                       # el texto del logo, algo mas chico que en el original


def a_px(ux, uy):
    return CX + (ux - OJO_C[0]) * K, CY + (uy - OJO_C[1]) * K


def capa(formas_, ventana):
    x, y, w, h = ventana
    img = Image.fromarray(dibujar(formas_, int(round(w * K)), ventana=ventana))
    px, py = a_px(x, y)
    return img, (px, py)


def capa_marca(formas_, ventana, top):
    x, y, w, h = ventana
    img = Image.fromarray(dibujar(formas_, int(round(w * KM)), ventana=ventana))
    # Centrado por la caja real de las letras, no por la del logo completo.
    xs = [v for f in formas_ for v in (f['bbox'][0], f['bbox'][2])]
    centro = (min(xs) + max(xs)) / 2
    return img, (W / 2 - (centro - x) * KM, top)


ojo_img, ojo_pos = capa(ojo_f, (58, 26, 476, 344))
lado = 116.0
iris_img, iris_pos = capa(iris_f, (IRIS_C[0] - lado / 2, IRIS_C[1] - lado / 2, lado, lado))
marca_img, marca_pos = capa_marca(texto_f, (63, 396, 466, 112), 636)


# ------------------------------------------------------------- reticula
def suave_lineas(dibujo, tam, escala=3):
    """Dibuja a 3x y reduce: lineas finas con antialias."""
    grande = Image.new("RGBA", (tam * escala, tam * escala), (0, 0, 0, 0))
    dibujo(ImageDraw.Draw(grande), escala)
    return grande.resize((tam, tam), Image.LANCZOS)


R_ANILLOS = [(330, 70, 2), (372, 34, 1), (470, 44, 1), (620, 26, 1), (820, 16, 1), (1040, 10, 1)]
R_MARCAS, R_ARCOS = 372, 470


def anillos(d, e):
    c = 1100 * e
    for r, a, g in R_ANILLOS:
        d.ellipse([c - r * e, c - r * e, c + r * e, c + r * e], outline=(74, 143, 224, a), width=g * e)
    # Almendra enorme del ojo y cruz punteada, como en el acceso.
    pts = []
    for t in np.linspace(0, 1, 120):
        x = (1 - t) ** 2 * -880 + 2 * (1 - t) * t * 0 + t ** 2 * 880
        y = 2 * (1 - t) * t * -760
        pts.append((c + x * e, c + y * e))
    pts += [(c - x + c, c - y + c) for x, y in pts]
    d.line(pts, fill=(74, 143, 224, 24), width=2 * e)
    for s in range(-1060, -360, 22):
        for x0, y0, x1, y1 in ((s, 0, s + 6, 0), (-s - 6, 0, -s, 0)):
            d.line([c + x0 * e, c + y0 * e, c + x1 * e, c + y1 * e], fill=(74, 143, 224, 30), width=e)


def marcas(d, e):
    c = (R_MARCAS + 40) * e
    for g in range(0, 360, 4):
        largo = 22 if g % 30 == 0 else 9
        a = math.radians(g)
        d.line([c + R_MARCAS * e * math.cos(a), c + R_MARCAS * e * math.sin(a),
                c + (R_MARCAS + largo) * e * math.cos(a), c + (R_MARCAS + largo) * e * math.sin(a)],
               fill=(74, 143, 224, 80), width=e)


def arcos(d, e):
    c = (R_ARCOS + 20) * e
    caja = [c - R_ARCOS * e, c - R_ARCOS * e, c + R_ARCOS * e, c + R_ARCOS * e]
    for a0, a1, alfa in ((-70, -14, 170), (110, 140, 90)):
        d.arc(caja, a0, a1, fill=(111, 211, 255, alfa), width=3 * e)
    r2 = 300
    caja2 = [c - r2 * e, c - r2 * e, c + r2 * e, c + r2 * e]
    d.arc(caja2, 200, 250, fill=(143, 224, 255, 90), width=2 * e)


fondo_ret = suave_lineas(anillos, 2200)
_m = np.clip(1 - (np.arange(H, dtype=np.float32) - 590) / 120, 0.12, 1.0)
MASCARA = np.repeat(_m[:, None], W, axis=1)


def enmascarar(img_rgba):
    a = np.asarray(img_rgba).copy()
    a[..., 3] = (a[..., 3] * MASCARA).astype(np.uint8)
    return Image.fromarray(a, "RGBA")
marcas_img = suave_lineas(marcas, 2 * (R_MARCAS + 40))
arcos_img = suave_lineas(arcos, 2 * (R_ARCOS + 20))

# Fondo: degradado azul noche con un poco de ruido para que el H.264 no
# dibuje escalones en el degradado.
yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
dist = np.sqrt(((xx - CX) / (W * 0.55)) ** 2 + ((yy - CY) / (H * 0.75)) ** 2)
base = np.stack([np.interp(dist, [0, 1.2], [c0, c1]) for c0, c1 in ((16, 7), (44, 16), (86, 34))], -1)
rng = np.random.default_rng(7)
base = np.clip(base + rng.normal(0, 1.1, base.shape), 0, 255).astype(np.uint8)
fondo = Image.fromarray(base, "RGB").convert("RGBA")
_ret = Image.new("RGBA", (W, H), (0, 0, 0, 0))
_ret.alpha_composite(fondo_ret.crop((int(1100 - CX), int(1100 - CY), int(1100 - CX) + W, int(1100 - CY) + H)))
fondo.alpha_composite(enmascarar(_ret))

# Resplandor detras del ojo.
g = np.clip(1 - np.sqrt(((xx - CX) / 520) ** 2 + ((yy - CY) / 380) ** 2), 0, 1) ** 2
resplandor = np.zeros((H, W, 4), np.uint8)
resplandor[..., 0], resplandor[..., 1], resplandor[..., 2] = 40, 130, 240
resplandor[..., 3] = (g * 90).astype(np.uint8)
resplandor = Image.fromarray(resplandor, "RGBA")


# ------------------------------------------------------------------ texto
def texto_espaciado(texto, fuente, color, espacio=0):
    ancho = sum(fuente.getlength(ch) + espacio for ch in texto) - espacio
    asc, desc = fuente.getmetrics()
    img = Image.new("RGBA", (int(ancho) + 4, asc + desc + 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = 2
    for ch in texto:
        d.text((x, 2), ch, font=fuente, fill=color)
        x += fuente.getlength(ch) + espacio
    return img


frase_img = texto_espaciado(FRASE, ImageFont.truetype(str(FUENTES / "GOTHIC.TTF"), 46), (226, 239, 252, 255))
ident_img = texto_espaciado(IDENTIDAD, ImageFont.truetype(str(FUENTES / "segoeuisl.ttf"), 19), (111, 168, 220, 255), 3)
pie_img = texto_espaciado(PIE, ImageFont.truetype(str(FUENTES / "segoeuil.ttf"), 18), (90, 125, 165, 255), 1)


# ------------------------------------------------------------- utilidades
def tramo(t, a, b):
    return min(1.0, max(0.0, (t - a) / (b - a)))


def sale(k):            # ease-out cubica
    return 1 - (1 - k) ** 3


def con_alfa(img, alfa):
    if alfa >= 0.999:
        return img
    r, g_, b, a = img.split()
    return Image.merge("RGBA", (r, g_, b, a.point(lambda v: int(v * alfa))))


def pegar(lienzo, img, x, y, alfa=1.0):
    if alfa <= 0.002:
        return
    lienzo.alpha_composite(con_alfa(img, alfa), (int(round(x)), int(round(y))))


def girada(img, grados):
    return img.rotate(-grados, resample=Image.BICUBIC, expand=False)


ojo_cache: dict = {}


def ojo_completo(giro):
    """Ojo con el iris girado, en el marco de ojo_img."""
    clave = round(giro, 1)
    if clave in ojo_cache:
        return ojo_cache[clave]
    img = ojo_img.copy()
    iris = girada(iris_img, giro) if abs(giro) > 0.05 else iris_img
    img.alpha_composite(iris, (int(round(iris_pos[0] - ojo_pos[0])), int(round(iris_pos[1] - ojo_pos[1]))))
    if abs(giro) < 0.05:
        ojo_cache[clave] = img
    return img


def cuadro(n):
    t = n / FPS
    lienzo = fondo.copy()

    # Reticula viva: marcas 24 grados por vuelta (simetria de 4), arcos 2 vueltas.
    viva = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    m = girada(marcas_img, 24 * t / DUR)
    viva.alpha_composite(m, (int(CX - m.width / 2), int(CY - m.height / 2)))
    a = girada(arcos_img, 720 * t / DUR)
    viva.alpha_composite(a, (int(CX - a.width / 2), int(CY - a.height / 2)))
    lienzo.alpha_composite(enmascarar(viva))

    entrada = sale(tramo(t, 0.3, 1.6))
    salida = 1 - tramo(t, 11.0, 11.85)
    visible = entrada * salida
    pegar(lienzo, resplandor, 0, 0, visible * (0.75 + 0.25 * math.sin(2 * math.pi * t / 6)))

    # Ojo: enfoca al entrar, parpadea a los 7 s y se aleja al salir.
    giro = -150 * (1 - sale(tramo(t, 0.3, 2.4)))
    ojo = ojo_completo(giro)
    escala = 1.08 - 0.08 * sale(tramo(t, 0.3, 1.8)) - 0.04 * tramo(t, 11.0, 11.85)
    desenfoque = 9 * (1 - sale(tramo(t, 0.3, 1.6))) + 8 * tramo(t, 11.0, 11.85)
    parpadeo = 1.0
    if 7.0 <= t <= 7.34:
        parpadeo = 1 - 0.93 * math.sin(math.pi * (t - 7.0) / 0.34)
    if visible > 0.002:
        w = int(ojo.width * escala)
        h = max(2, int(ojo.height * escala * parpadeo))
        o = ojo.resize((w, h), Image.LANCZOS)
        if desenfoque > 0.3:
            o = o.filter(ImageFilter.GaussianBlur(desenfoque))
        ox = CX - (OJO_C[0] - 58) * K * escala
        oy = CY - (OJO_C[1] - 26) * K * escala * parpadeo
        pegar(lienzo, o, ox, oy, visible)

    # Textos.
    k1 = sale(tramo(t, 1.9, 2.7))
    pegar(lienzo, marca_img, marca_pos[0], marca_pos[1] + 24 * (1 - k1), k1 * salida)
    k2 = sale(tramo(t, 2.8, 3.6))
    pegar(lienzo, frase_img, CX - frase_img.width / 2, 846 + 18 * (1 - k2), k2 * salida)
    k3 = sale(tramo(t, 3.4, 4.2))
    pegar(lienzo, ident_img, CX - ident_img.width / 2, 926 + 12 * (1 - k3), k3 * salida)
    pegar(lienzo, pie_img, CX - pie_img.width / 2, 1022, sale(tramo(t, 3.8, 4.6)) * salida)
    return lienzo


if __name__ == "__main__":
    salida_mp4 = Path(sys.argv[1])
    total = int(DUR * FPS)
    if "--cuadros" in sys.argv:
        for n in map(int, sys.argv[sys.argv.index("--cuadros") + 1].split(",")):
            cuadro(n).convert("RGB").save(salida_mp4.with_name(f"cuadro_{n:03d}.png"))
        sys.exit()
    import subprocess

    try:
        import imageio_ffmpeg
    except ImportError:
        vw = cv2.VideoWriter(str(salida_mp4), cv2.VideoWriter_fourcc(*"avc1"), FPS, (W, H))
        assert vw.isOpened(), "No se pudo abrir el codificador H.264"
        for n in range(total):
            vw.write(cv2.cvtColor(np.asarray(cuadro(n).convert("RGB")), cv2.COLOR_RGB2BGR))
        vw.release()
        sys.exit(print("ok (OpenCV)", salida_mp4))
    # x264 con CRF 18 (visualmente sin perdidas), yuv420p y perfil high para
    # que lo abra cualquier TV, proyector o reproductor; faststart para que
    # arranque al instante en el navegador.
    proc = subprocess.Popen([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
                             "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                             "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-tune", "animation",
                             "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
                             "-g", str(FPS * 2), "-movflags", "+faststart", str(salida_mp4)],
                            stdin=subprocess.PIPE)
    for n in range(total):
        proc.stdin.write(np.asarray(cuadro(n).convert("RGB")).tobytes())
        if n % 60 == 0:
            print(f"  {n}/{total}", flush=True)
    proc.stdin.close()
    assert proc.wait() == 0, "ffmpeg fallo"
    print("ok", salida_mp4)
