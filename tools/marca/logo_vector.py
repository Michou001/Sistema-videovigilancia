"""Lee el logo vectorial de GOSS IP (traza VTracer) y lo dibuja por capas.

Cada forma del SVG trae su color y un translate(); el trazo usa M, C, L y Z.
Se dibuja con matplotlib (regla nonzero, igual que SVG) a cualquier tamano.
"""
from __future__ import annotations

import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import PathPatch  # noqa: E402
from matplotlib.path import Path as MPath  # noqa: E402

AQUI = Path(__file__).parent
SVG = AQUI / "logo-goss-ip.svg"
VIEWBOX = (63.0, 51.0, 466.0, 451.0)          # x, y, ancho, alto


def _numeros(s: str) -> list[float]:
    return [float(x) for x in re.findall(r"-?\d*\.?\d+(?:e-?\d+)?", s)]


def _trazo(d: str, tx: float, ty: float) -> tuple[list, list]:
    """Vertices y codigos de matplotlib para un atributo d con M, L, C y Z."""
    verts: list = []
    codes: list = []
    for cmd, args in re.findall(r"([MCLZ])([^MCLZ]*)", d):
        n = _numeros(args)
        pares = [(x + tx, y + ty) for x, y in zip(n[0::2], n[1::2])]
        if cmd == "M":
            verts += pares
            codes += [MPath.MOVETO] + [MPath.LINETO] * (len(pares) - 1)
        elif cmd == "L":
            verts += pares
            codes += [MPath.LINETO] * len(pares)
        elif cmd == "C":
            verts += pares
            codes += [MPath.CURVE4] * len(pares)
        elif cmd == "Z":
            verts.append(verts[-1])
            codes.append(MPath.CLOSEPOLY)
    return verts, codes


def leer() -> list[dict]:
    texto = SVG.read_text(encoding="utf-8")
    formas = []
    for m in re.finditer(r'<path d="([^"]+)" fill="(#[0-9A-Fa-f]{6})" transform="translate\(([^)]+)\)"/>', texto):
        d, color, tr = m.groups()
        tx, ty = _numeros(tr)
        verts, codes = _trazo(d, tx, ty)
        v = np.array(verts)
        formas.append({"color": color, "verts": v, "codes": codes,
                       "bbox": (v[:, 0].min(), v[:, 1].min(), v[:, 0].max(), v[:, 1].max())})
    return formas


def dibujar(formas: list[dict], ancho_px: int, *, ventana=VIEWBOX, fondo=None) -> np.ndarray:
    """RGBA uint8 de las formas dadas, en la ventana (x, y, w, h) del viewBox."""
    x, y, w, h = ventana
    alto_px = int(round(ancho_px * h / w))
    fig = plt.figure(figsize=(ancho_px / 100, alto_px / 100), dpi=100)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(x, x + w)
    ax.set_ylim(y + h, y)
    ax.axis("off")
    fig.patch.set_alpha(0 if fondo is None else 1)
    if fondo is not None:
        fig.patch.set_facecolor(fondo)
    for f in formas:
        ax.add_patch(PathPatch(MPath(f["verts"], f["codes"]), facecolor=f["color"],
                               edgecolor=f["color"], linewidth=0.15, antialiased=True))
    fig.canvas.draw()
    img = np.asarray(fig.canvas.buffer_rgba()).copy()
    plt.close(fig)
    return img


if __name__ == "__main__":
    formas = leer()
    for i, f in enumerate(formas, 1):
        x0, y0, x1, y1 = f["bbox"]
        print(f"{i:2} {f['color']}  centro ({(x0+x1)/2:6.1f},{(y0+y1)/2:6.1f})  tam {x1-x0:5.1f} x {y1-y0:5.1f}")
    from PIL import Image
    Image.fromarray(dibujar(formas, 900, fondo="#222222")).save("logo_prueba.png")
