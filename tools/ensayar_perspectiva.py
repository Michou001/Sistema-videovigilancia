"""Mide el rendimiento de una camara ya montada sin copiar imagenes ni placas.

Ejemplo: venv\\Scripts\\python.exe tools/ensayar_perspectiva.py --camara cam-01
         --tipo face --distancia 3 --segundos 30
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from shared.plates import normalizar  # noqa: E402

UMBRAL_INSTALACION_PX = {"face": 100, "plate": 72}
CSV_CAMPOS = ("camara", "tipo", "distancia_m_declarada", "desde_utc", "hasta_utc",
              "eventos", "con_caja", "con_foto", "coincidencias_lista",
              "umbral_instalacion_px", "con_margen", "ancho_min_px", "ancho_p10_px",
              "ancho_mediana_px", "ancho_p90_px", "lecturas_correctas")


def _ahora_bd() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")


def _percentil(valores: list[int], fraccion: float) -> int:
    ordenados = sorted(valores)
    return ordenados[round((len(ordenados) - 1) * fraccion)]


def medir(db: Path, camara: str, tipo: str, desde: str, hasta: str,
          esperado: str | None = None) -> dict:
    """Solo SELECT: las fotos y los valores leidos no salen del proceso."""
    ruta = db.resolve()
    with sqlite3.connect(f"file:{ruta.as_posix()}?mode=ro", uri=True) as conexion:
        filas = conexion.execute(
            "SELECT bbox_x1, bbox_x2, confidence, value, snapshot_path, match_kind "
            "FROM events WHERE camera_id = ? AND type = ? AND ts >= ? AND ts <= ? "
            "ORDER BY ts", (camara, tipo, desde, hasta),
        ).fetchall()
    anchos = [int(x2 - x1) for x1, x2, *_ in filas
              if x1 is not None and x2 is not None and x2 > x1]
    umbral = UMBRAL_INSTALACION_PX[tipo]
    resultado = {
        "camara": camara, "tipo": tipo, "desde_utc": desde, "hasta_utc": hasta,
        "eventos": len(filas), "con_caja": len(anchos),
        "con_foto": sum(bool(f[4]) for f in filas),
        "coincidencias_lista": sum(f[5] in {"exact", "fuzzy", "biometric"} for f in filas),
        "umbral_instalacion_px": umbral,
        "con_margen": sum(ancho >= umbral for ancho in anchos),
    }
    if anchos:
        resultado.update(ancho_min_px=min(anchos), ancho_p10_px=_percentil(anchos, 0.1),
                         ancho_mediana_px=median(anchos), ancho_p90_px=_percentil(anchos, 0.9))
    if esperado is not None and tipo == "plate":
        ref = normalizar(esperado)
        resultado["lecturas_correctas"] = sum(normalizar(f[3] or "") == ref for f in filas)
    return resultado


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camara", required=True, help="cam-01 o cam-02")
    parser.add_argument("--tipo", required=True, choices=("face", "plate"))
    parser.add_argument("--distancia", type=float,
                        help="distancia horizontal marcada en el piso, en metros")
    periodo = parser.add_mutually_exclusive_group(required=True)
    periodo.add_argument("--segundos", type=int, help="espera mientras haces las pasadas")
    periodo.add_argument("--minutos", type=int, help="analiza detecciones recientes")
    parser.add_argument("--esperado", help="placa sintetica esperada; no se imprime")
    parser.add_argument("--db", type=Path, default=RAIZ / "data" / "vigilancia.db")
    parser.add_argument("--csv", type=Path, help="anexa solo metricas agregadas")
    args = parser.parse_args()
    if (args.distancia is not None and args.distancia <= 0) \
            or (args.segundos is not None and args.distancia is None) \
            or (args.segundos is not None and args.segundos <= 0) \
            or (args.minutos is not None and args.minutos <= 0):
        parser.error("la distancia es obligatoria en un ensayo nuevo; valores mayores que cero")
    if not args.db.is_file():
        parser.error("no existe la base de datos indicada")

    if args.segundos is not None:
        desde = _ahora_bd()
        print(f"Ensayo {args.camara} / {args.tipo} / {args.distancia:g} m: "
              f"haz al menos 3 pasadas y sal del cuadro entre ellas.", flush=True)
        try:
            time.sleep(args.segundos)
        except KeyboardInterrupt:
            pass
        hasta = _ahora_bd()
    else:
        fin = datetime.now(timezone.utc).replace(tzinfo=None)
        desde = (fin - timedelta(minutes=args.minutos)).isoformat(sep=" ")
        hasta = fin.isoformat(sep=" ")

    resultado = medir(args.db, args.camara, args.tipo, desde, hasta, args.esperado)
    resultado["distancia_m_declarada"] = args.distancia if args.segundos is not None else None
    print(f"Eventos: {resultado['eventos']} | con foto: {resultado['con_foto']} "
          f"| coincidencias: {resultado['coincidencias_lista']}")
    if resultado["con_caja"]:
        print(f"Ancho del objetivo: p10 {resultado['ancho_p10_px']} px | "
              f"mediana {resultado['ancho_mediana_px']:g} px | "
              f"p90 {resultado['ancho_p90_px']} px")
        print(f"Margen de instalacion (>= {resultado['umbral_instalacion_px']} px): "
              f"{resultado['con_margen']}/{resultado['con_caja']}")
    else:
        print("No hubo detecciones con caja medible en este intervalo.")
    if "lecturas_correctas" in resultado:
        print(f"Lecturas correctas de la placa de prueba: "
              f"{resultado['lecturas_correctas']}/{resultado['eventos']}")
    if resultado["eventos"] < 3:
        print("Muestra insuficiente: repite con al menos 3 pasadas separadas.")
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        existe = args.csv.exists()
        with args.csv.open("a", newline="", encoding="utf-8") as archivo:
            escritor = csv.DictWriter(archivo, fieldnames=CSV_CAMPOS, extrasaction="ignore")
            if not existe:
                escritor.writeheader()
            escritor.writerow(resultado)
        print(f"Metricas guardadas en {args.csv}")


if __name__ == "__main__":
    main()
