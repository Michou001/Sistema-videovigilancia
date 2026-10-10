"""Foto de evidencia con contexto: la escena completa y un acercamiento.

Un recorte de la placa o del rostro se lee bien, pero no dice donde ni en que
situacion se tomo; la escena sola da el contexto, pero la placa queda chica.
La evidencia lleva las dos cosas: el frame completo con el objeto marcado y,
en la esquina mas alejada de el, el recorte ampliado.
"""
from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

ANCHO_MAX = 1920          # la escena HD (3200 px) se reduce; el acercamiento no
FRACCION_DETALLE = 0.34   # ancho del acercamiento respecto del de la escena
ALTO_MAX_DETALLE = 0.45   # y como maximo esta fraccion del alto
BORDE = 3
MARGEN = 12

Caja = tuple[int, int, int, int]


def componer(escena: np.ndarray, caja, detalle: Optional[np.ndarray],
             color: tuple[int, int, int] = (255, 180, 0)
             ) -> tuple[np.ndarray, Optional[Caja], Caja]:
    """Escena con `caja` marcada y `detalle` ampliado en una esquina.

    Devuelve la imagen, donde quedo el acercamiento (None si no cupo sin tapar
    el objeto) y donde quedo el objeto, ambos en pixeles de la imagen final.
    Los marcos se dibujan por fuera del objeto y del acercamiento: un recorte
    que se saque despues de esas regiones no lleva lineas encima.
    """
    alto, ancho = escena.shape[:2]
    escala = min(1.0, ANCHO_MAX / ancho)
    if escala < 1.0:
        vista = cv2.resize(escena, (round(ancho * escala), round(alto * escala)),
                           interpolation=cv2.INTER_AREA)
    else:
        vista = escena.copy()
    hv, av = vista.shape[:2]

    x1, y1, x2, y2 = (int(round(float(v) * escala)) for v in caja)
    x1, y1 = max(0, min(av - 1, x1)), max(0, min(hv - 1, y1))
    x2, y2 = max(x1 + 1, min(av, x2)), max(y1 + 1, min(hv, y2))
    objeto = (x1, y1, x2, y2)
    g = BORDE + 2
    cv2.rectangle(vista, (max(0, x1 - g), max(0, y1 - g)),
                  (min(av - 1, x2 + g), min(hv - 1, y2 + g)), color, BORDE)

    if detalle is None or detalle.size == 0:
        return vista, None, objeto

    hd, wd = detalle.shape[:2]
    ancho_det = max(1, int(av * FRACCION_DETALLE))
    alto_det = max(1, round(hd * ancho_det / wd))
    if alto_det > hv * ALTO_MAX_DETALLE:
        alto_det = max(1, int(hv * ALTO_MAX_DETALLE))
        ancho_det = max(1, round(wd * alto_det / hd))
    interp = cv2.INTER_CUBIC if ancho_det > wd else cv2.INTER_AREA
    ampliado = cv2.resize(detalle, (ancho_det, alto_det), interpolation=interp)

    # Esquinas de la mas lejana a la mas cercana al objeto; se usa la primera
    # en la que el acercamiento, con su marco, no lo tapa.
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    esquinas = [(MARGEN, MARGEN), (av - ancho_det - MARGEN, MARGEN),
                (MARGEN, hv - alto_det - MARGEN), (av - ancho_det - MARGEN, hv - alto_det - MARGEN)]
    esquinas.sort(key=lambda p: -((p[0] + ancho_det / 2 - cx) ** 2 + (p[1] + alto_det / 2 - cy) ** 2))
    for ex, ey in esquinas:
        if ex < BORDE or ey < BORDE:
            continue
        separado = (ex + ancho_det + BORDE < x1 - g or ex - BORDE > x2 + g
                    or ey + alto_det + BORDE < y1 - g or ey - BORDE > y2 + g)
        if separado:
            cv2.rectangle(vista, (ex - BORDE, ey - BORDE),
                          (ex + ancho_det + BORDE - 1, ey + alto_det + BORDE - 1), color, -1)
            vista[ey:ey + alto_det, ex:ex + ancho_det] = ampliado
            return vista, (ex, ey, ex + ancho_det, ey + alto_det), objeto
    return vista, None, objeto
