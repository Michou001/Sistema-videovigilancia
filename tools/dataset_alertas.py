"""Arma un dataset YOLO con las alertas que los operadores ya revisaron.

    python tools/dataset_alertas.py                      # todas las camaras de este worker
    python tools/dataset_alertas.py --tipos weapon       # solo armas
    python tools/dataset_alertas.py --env .env.cam2 --salida armas.zip

Se corre en la maquina del WORKER, que es donde estan los cuadros limpios
(DATASET_ENABLED=true, ver edge/dataset.py). Pregunta a la API el veredicto
de cada alerta y arma:

    dataset/
      data.yaml              clases y rutas, listo para `yolo train`
      images/{train,val}/    cuadros originales, sin nada dibujado
      labels/{train,val}/    YOLO: clase cx cy w h (normalizado)
      resumen.csv            evento, camara, clase, veredicto
      LEEME.txt

  "Atendida"       -> la deteccion era correcta: imagen con su caja.
  "Falso positivo" -> imagen de FONDO (etiqueta vacia): le ensena al modelo
                      que ahi no hay nada. Es lo que mas baja las falsas alarmas.
  Sin revisar      -> no entra.

Ver docs/reentrenamiento.md para entrenar y poner el modelo nuevo.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))


@dataclass
class Resumen:
    confirmadas: int = 0
    falsos_positivos: int = 0
    sin_revisar: int = 0
    sin_alerta: int = 0
    clases: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.confirmadas + self.falsos_positivos


def clase_de(datos: dict) -> str:
    """Nombre de la clase YOLO de un cuadro."""
    if datos.get("tipo") == "zone":
        return datos.get("clase") or "persona"
    return str(datos.get("valor") or datos.get("tipo"))


def etiqueta_yolo(datos: dict, indice: int) -> str:
    x1, y1, x2, y2 = datos["bbox"]
    ancho, alto = float(datos["ancho"]), float(datos["alto"])
    x1, x2 = sorted((max(0.0, min(ancho, x1)), max(0.0, min(ancho, x2))))
    y1, y2 = sorted((max(0.0, min(alto, y1)), max(0.0, min(alto, y2))))
    cx, cy = (x1 + x2) / 2 / ancho, (y1 + y2) / 2 / alto
    w, h = (x2 - x1) / ancho, (y2 - y1) / alto
    return f"{indice} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n"


def es_validacion(event_id: str, fraccion: float = 0.2) -> bool:
    """Reparto estable: el mismo evento cae siempre en el mismo lado."""
    return int(hashlib.sha1(event_id.encode()).hexdigest()[:8], 16) % 1000 < fraccion * 1000


def leer_cuadros(carpeta: Path, tipos: set[str] | None = None) -> list[tuple[dict, Path]]:
    cuadros = []
    for meta in sorted(carpeta.glob("*/*.json")):
        imagen = meta.with_suffix(".jpg")
        if not imagen.exists():
            continue
        try:
            datos = json.loads(meta.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if tipos and datos.get("tipo") not in tipos:
            continue
        cuadros.append((datos, imagen))
    return cuadros


def construir(cuadros: list[tuple[dict, Path]], veredictos: dict[str, dict], salida,
              fraccion_val: float = 0.2) -> Resumen:
    resumen = Resumen()
    elegidos = []
    for datos, imagen in cuadros:
        v = veredictos.get(datos["event_id"])
        if v is None:
            resumen.sin_alerta += 1
        elif v["estado"] == "acknowledged":
            resumen.confirmadas += 1
            elegidos.append((datos, imagen, True, v))
        elif v["estado"] == "dismissed":
            resumen.falsos_positivos += 1
            elegidos.append((datos, imagen, False, v))
        else:
            resumen.sin_revisar += 1

    nombres = sorted({clase_de(d) for d, _, confirmada, _ in elegidos if confirmada})
    indice = {n: i for i, n in enumerate(nombres)}
    tabla = io.StringIO()
    escritor = csv.writer(tabla)
    escritor.writerow(["event_id", "camara", "fecha", "clase", "veredicto", "motivo", "particion"])
    with zipfile.ZipFile(salida, "w", zipfile.ZIP_DEFLATED) as z:
        for datos, imagen, confirmada, v in elegidos:
            particion = "val" if es_validacion(datos["event_id"], fraccion_val) else "train"
            nombre = datos["event_id"]
            z.write(imagen, f"dataset/images/{particion}/{nombre}.jpg")
            clase = clase_de(datos)
            texto = etiqueta_yolo(datos, indice[clase]) if confirmada else ""
            z.writestr(f"dataset/labels/{particion}/{nombre}.txt", texto)
            if confirmada:
                resumen.clases[clase] = resumen.clases.get(clase, 0) + 1
            escritor.writerow([nombre, datos.get("camera_id"), datos.get("ts"), clase,
                               "confirmada" if confirmada else "falso positivo", v.get("motivo") or "",
                               particion])
        yaml = ["# Dataset de alertas revisadas por los operadores (tools/dataset_alertas.py)",
                "path: .", "train: images/train", "val: images/val", "names:"]
        yaml += [f"  {i}: {n}" for i, n in enumerate(nombres)] or ["  0: objeto"]
        z.writestr("dataset/data.yaml", "\n".join(yaml) + "\n")
        z.writestr("dataset/resumen.csv", tabla.getvalue())
        z.writestr("dataset/LEEME.txt", _leeme(resumen, nombres))
    return resumen


def _leeme(r: Resumen, nombres: list[str]) -> str:
    return f"""Dataset de alertas revisadas ({datetime.now():%Y-%m-%d %H:%M})

Confirmadas por un operador: {r.confirmadas}   Falsos positivos (fondo): {r.falsos_positivos}
Clases: {', '.join(nombres) or '(ninguna confirmada todavia)'}

Afinar un modelo YOLO con estos datos (GPU recomendada):

    unzip dataset.zip && cd dataset
    yolo detect train data=data.yaml model=yolo11s.pt epochs=60 imgsz=640 patience=15

Para no olvidar lo que el modelo ya sabe, conviene mezclar estas imagenes con
las del dataset original con el que se entreno el modelo que se esta usando.
El resultado queda en runs/detect/train/weights/best.pt: ponlo en el .env del
worker (WEAPON_MODEL=... para armas) y compara las falsas alarmas de una semana
antes y despues.

Son imagenes de personas reales: guarda este archivo cifrado y borralo al
terminar (docs/privacidad.md).
"""


def pedir_veredictos(api_url: str, token: str, ids: Iterable[str]) -> dict[str, dict]:
    import httpx

    ids = list(ids)
    resultado: dict[str, dict] = {}
    with httpx.Client(timeout=30.0, headers={"X-API-Token": token}) as c:
        for i in range(0, len(ids), 500):
            r = c.post(f"{api_url.rstrip('/')}/api/events/veredictos", json={"event_ids": ids[i:i + 500]})
            r.raise_for_status()
            resultado.update(r.json()["veredictos"])
    return resultado


def main(argv=None, obtener: Callable | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", help="Archivo de entorno del worker (default .env)")
    p.add_argument("--carpeta", default=str(RAIZ / "data" / "entrenamiento"))
    p.add_argument("--tipos", help="weapon,anomaly,zone (default: todos)")
    p.add_argument("--salida", default=f"dataset-alertas-{datetime.now():%Y%m%d-%H%M}.zip")
    p.add_argument("--val", type=float, default=0.2, help="Fraccion para validacion")
    args = p.parse_args(argv)

    carpeta = Path(args.carpeta)
    tipos = {t.strip() for t in args.tipos.split(",")} if args.tipos else None
    cuadros = leer_cuadros(carpeta, tipos)
    if not cuadros:
        print(f"  [!] No hay cuadros en {carpeta}. Activa DATASET_ENABLED=true en el .env del worker")
        print("      y espera a que haya alertas revisadas.")
        return 1
    if obtener is None:
        from edge.config import load_config

        cfg = load_config(args.env)
        if not (cfg.api_url and cfg.api_token):
            print("  [x] Falta API_URL / API_TOKEN en el .env del worker")
            return 1

        def obtener(ids):
            return pedir_veredictos(cfg.api_url, cfg.api_token, ids)

    veredictos = obtener([d["event_id"] for d, _ in cuadros])
    salida = Path(args.salida)
    with salida.open("wb") as f:
        r = construir(cuadros, veredictos, f, args.val)
    print(f"  [+] {salida}: {r.total} imagenes ({r.confirmadas} confirmadas, {r.falsos_positivos} falsos "
          f"positivos como fondo). Sin revisar: {r.sin_revisar}.")
    if r.clases:
        print("      Clases: " + ", ".join(f"{k} ({v})" for k, v in sorted(r.clases.items())))
    print("      Son imagenes de personas: guarda el archivo cifrado y borralo al terminar.")
    return 0 if r.total else 1


if __name__ == "__main__":
    raise SystemExit(main())
