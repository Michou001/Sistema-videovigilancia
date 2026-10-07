"""Metricas reales del sistema para el portafolio de evidencias.

    python tools/metricas.py --muestrear 300        # 5 min en vivo, cada 5 s
    python tools/metricas.py --resumen              # lo que dice la base de datos
    python tools/metricas.py --resumen --desde 2026-10-07

Nada se estima: todo sale de lo que el sistema midio.

--muestrear (con la API y los workers corriendo)
    Cada N segundos anota, por camara: fps procesados y de la camara, frames
    descartados, reconexiones, segundos sin imagen, milisegundos de inferencia
    por detector (del latido del worker, medidos en sus ultimos ~15 s, sin el
    calentamiento del arranque); y del equipo: latencia de la API, GPU, VRAM,
    CPU y RAM. Sale a docs/evidencias/metricas-AAAAMMDD-HHMM.csv. El primer
    minuto despues de arrancar un worker no es representativo.

--resumen (no necesita la API corriendo)
    De la base de datos: eventos por tipo, alertas por severidad y estado,
    tiempo hasta que un operador atiende una alerta, lecturas de placa que un
    operador tuvo que corregir, caidas de camara y cuanto tardaron en
    recuperarse. Sale a docs/evidencias/resumen-AAAAMMDD-HHMM.md.

Lee la base de datos directo (solo lectura de conteos): no hace falta
usuario ni contrasena del dashboard.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

SALIDA = RAIZ / "docs" / "evidencias"


def _gpu() -> dict:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        nombre, uso, usada, total = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
        return {"gpu": nombre, "gpu_uso_pct": float(uso), "vram_usada_mb": float(usada),
                "vram_total_mb": float(total)}
    except Exception:  # noqa: BLE001 - sin GPU NVIDIA o sin nvidia-smi
        return {}


def _latencia_api(url: str) -> float | None:
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/health", timeout=5) as r:
            r.read()
        return round((time.perf_counter() - t0) * 1000, 1)
    except Exception:  # noqa: BLE001
        return None


def _camaras() -> list[dict]:
    from sqlmodel import Session, select

    from api.database import engine
    from api.models import Camera

    ahora = datetime.now(timezone.utc)
    salida = []
    with Session(engine) as s:
        # Solo las columnas que hacen falta: funciona aunque la base de datos
        # todavia no tenga las migraciones mas nuevas.
        for camera_id, visto, status_json in s.exec(
                select(Camera.camera_id, Camera.last_heartbeat, Camera.status_json)).all():
            if visto is not None and visto.tzinfo is None:
                visto = visto.replace(tzinfo=timezone.utc)
            try:
                salud = json.loads(status_json) if status_json else {}
            except ValueError:
                salud = {}
            salida.append({"camara": camera_id,
                           "en_linea": bool(visto and (ahora - visto).total_seconds() < 60
                                            and salud.get("connected", True) is not False),
                           "salud": salud})
    return salida


def muestrear(segundos: float, cada: float, api_url: str) -> Path:
    import psutil

    SALIDA.mkdir(parents=True, exist_ok=True)
    destino = SALIDA / f"metricas-{datetime.now():%Y%m%d-%H%M}.csv"
    filas: list[dict] = []
    fin = time.monotonic() + segundos
    print(f"[i] Muestreando {segundos:.0f} s cada {cada:.0f} s -> {destino.relative_to(RAIZ)}")
    while True:
        momento = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        equipo = {"latencia_api_ms": _latencia_api(api_url), "cpu_pct": psutil.cpu_percent(interval=None),
                  "ram_usada_mb": round(psutil.virtual_memory().used / 2**20), **_gpu()}
        camaras = _camaras() or [{"camara": "-", "en_linea": False, "salud": {}}]
        for c in camaras:
            s = c["salud"]
            fila = {"fecha_hora": momento, "camara": c["camara"], "en_linea": c["en_linea"],
                    "fps_procesados": s.get("fps_recientes"), "fps_camara": s.get("measured_fps"),
                    "frames_descartados": s.get("frames_dropped"), "reconexiones": s.get("reconnects"),
                    "segundos_sin_frame": s.get("seconds_since_frame"), "eventos_worker": s.get("eventos"),
                    **{f"ms_{k}": v for k, v in (s.get("ms_inferencia") or {}).items()}, **equipo}
            filas.append(fila)
        print(f"  {momento}  " + "  ".join(
            f"{f['camara']}: {f['fps_procesados'] or 0} fps" for f in filas[-len(camaras):])
            + f"  API {equipo['latencia_api_ms']} ms  VRAM {equipo.get('vram_usada_mb', '?')} MB")
        if time.monotonic() >= fin:
            break
        time.sleep(cada)

    columnas: list[str] = []
    for f in filas:
        columnas += [k for k in f if k not in columnas]
    with destino.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columnas)
        w.writeheader()
        w.writerows(filas)

    print("\nResumen (promedio / minimo / maximo):")
    for col in columnas:
        valores = [f[col] for f in filas if isinstance(f.get(col), (int, float))
                   and not isinstance(f.get(col), bool)]
        if valores and col not in ("vram_total_mb",):
            print(f"  {col:22} {statistics.mean(valores):9.1f} {min(valores):9.1f} {max(valores):9.1f}")
    return destino


def _a_utc(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def resumen(desde: datetime | None) -> Path:
    from sqlmodel import Session, col, func, select

    from api.database import engine
    from api.models import Alert, Event

    with Session(engine) as s:
        filtro_ev = [col(Event.ts) >= desde] if desde else []
        filtro_al = [col(Alert.created_at) >= desde] if desde else []
        por_tipo = dict(s.exec(select(Event.type, func.count()).where(*filtro_ev).group_by(Event.type)).all())
        por_sev = dict(s.exec(select(Alert.severity, func.count()).where(*filtro_al)
                              .group_by(Alert.severity)).all())
        por_estado = dict(s.exec(select(Alert.status, func.count()).where(*filtro_al)
                                 .group_by(Alert.status)).all())
        atendidas = s.exec(select(Alert.created_at, Alert.acknowledged_at)
                           .where(col(Alert.acknowledged_at).is_not(None), *filtro_al)).all()
        tiempos = [(_a_utc(b) - _a_utc(a)).total_seconds() for a, b in atendidas if a and b]
        placas = s.exec(select(func.count()).select_from(Event).where(Event.type == "plate", *filtro_ev)).one()
        corregidas = s.exec(select(func.count()).select_from(Event)
                            .where(Event.type == "plate", Event.corregido == True, *filtro_ev)).one()  # noqa: E712
        coincidencias = s.exec(select(func.count()).select_from(Event)
                               .where(Event.type == "plate", Event.match_kind != "none", *filtro_ev)).one()
        camara_ev = s.exec(select(Event.camera_id, Event.value, Event.ts)
                           .where(Event.type == "camera", *filtro_ev).order_by(Event.ts)).all()
        rango = s.exec(select(func.min(Event.ts), func.max(Event.ts)).where(*filtro_ev)).one()

    caidas: dict[str, datetime] = {}
    recuperaciones = []
    for camara, valor, ts in camara_ev:
        if valor == "sin_senal":
            caidas[camara] = _a_utc(ts)
        elif valor == "senal_recuperada" and camara in caidas:
            recuperaciones.append((camara, (_a_utc(ts) - caidas.pop(camara)).total_seconds()))

    lineas = [
        "# Resumen de operación (datos de la base de datos)",
        "",
        f"Generado: {datetime.now():%Y-%m-%d %H:%M}",
        f"Periodo con eventos: {rango[0]:%Y-%m-%d %H:%M} a {rango[1]:%Y-%m-%d %H:%M} (UTC)"
        if rango and rango[0] else "Periodo con eventos: sin eventos",
        "",
        "## Eventos por tipo",
        "",
        "| Tipo | Eventos |",
        "|---|---|",
        *[f"| {t} | {n} |" for t, n in sorted(por_tipo.items(), key=lambda x: -x[1])],
        "",
        "## Alertas",
        "",
        f"- Por severidad: {', '.join(f'{k} {v}' for k, v in por_sev.items()) or 'ninguna'}",
        f"- Por estado: {', '.join(f'{k} {v}' for k, v in por_estado.items()) or 'ninguna'}",
    ]
    if tiempos:
        lineas.append(f"- Tiempo hasta que un operador atiende: mediana {statistics.median(tiempos):.0f} s, "
                      f"mínimo {min(tiempos):.0f} s, máximo {max(tiempos):.0f} s ({len(tiempos)} alertas)")
    lineas += [
        "",
        "## Placas",
        "",
        f"- Lecturas: {placas}",
        f"- Corregidas por un operador: {corregidas}"
        + (f" ({corregidas / placas:.1%} de las lecturas)" if placas else ""),
        f"- Con coincidencia en lista negra: {coincidencias}",
        "",
        "## Cámaras caídas",
        "",
    ]
    if recuperaciones:
        lineas += [f"- {cam}: recuperada en {seg:.0f} s" for cam, seg in recuperaciones]
    else:
        lineas.append("- Sin caídas con recuperación registradas en el periodo.")
    if caidas:
        lineas += [f"- {cam}: caída desde {ts:%Y-%m-%d %H:%M} UTC, sin recuperación registrada"
                   for cam, ts in caidas.items()]

    SALIDA.mkdir(parents=True, exist_ok=True)
    destino = SALIDA / f"resumen-{datetime.now():%Y%m%d-%H%M}.md"
    destino.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    print("\n".join(lineas))
    return destino


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--muestrear", type=float, metavar="SEGUNDOS", help="Muestrear en vivo este tiempo")
    p.add_argument("--cada", type=float, default=5.0, help="Segundos entre muestras (default 5)")
    p.add_argument("--api", default="http://127.0.0.1:8000", help="URL de la API")
    p.add_argument("--resumen", action="store_true", help="Resumen desde la base de datos")
    p.add_argument("--desde", help="Fecha AAAA-MM-DD para el resumen")
    args = p.parse_args()

    from edge.config import leer_env, ruta_env

    for clave, valor in leer_env(ruta_env()).items():
        os.environ.setdefault(clave, valor)

    if not args.muestrear and not args.resumen:
        p.print_help()
        return 1
    if args.muestrear:
        print(f"[OK] {muestrear(args.muestrear, args.cada, args.api).relative_to(RAIZ)}")
    if args.resumen:
        desde = None
        if args.desde:
            desde = datetime.strptime(args.desde, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        print(f"\n[OK] {resumen(desde).relative_to(RAIZ)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
