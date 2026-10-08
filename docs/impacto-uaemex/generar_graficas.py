r"""Genera figuras de investigación; no importa la app ni accede a data/.

Desde la raíz: venv\Scripts\python.exe docs/impacto-uaemex/generar_graficas.py
Solo escribe sus salidas en la carpeta graficas/ contigua a este archivo.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "graficas"
BACKGROUND = "#f6f7f9"
INK = "#152f39"
GREEN = "#087f72"
MUTED = "#586875"
AMBER = "#af670d"


def base(title: str, subtitle: str, height: float = 5.7):
    fig, ax = plt.subplots(figsize=(11.8, height))
    fig.set_facecolor(BACKGROUND)
    ax.set_facecolor(BACKGROUND)
    fig.subplots_adjust(left=0.28, right=0.93, top=0.72, bottom=0.32)
    fig.text(0.055, 0.94, "GOSS IP  /  UTILIDAD PARA LA UAEMÉX", color=GREEN,
             fontsize=10, weight="bold")
    fig.text(0.055, 0.865, title, color=INK, fontsize=20, weight="bold")
    fig.text(0.055, 0.795, subtitle, color=MUTED, fontsize=11)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color("#c6d0d5")
    ax.tick_params(axis="both", length=0, colors=INK, labelsize=11)
    ax.xaxis.grid(True, color="#dce2e6", linewidth=0.7)
    ax.set_axisbelow(True)
    return fig, ax


def save(fig, name: str, footer: str):
    fig.text(0.055, 0.105, footer, color=MUTED, fontsize=10, linespacing=1.6)
    fig.text(0.055, 0.035, "Revisión: 7 oct 2026  ·  Fuentes y supuestos en docs/impacto-uaemex/",
             color=MUTED, fontsize=9)
    fig.savefig(OUT / f"{name}.png", dpi=170, facecolor=fig.get_facecolor())
    fig.savefig(OUT / f"{name}.svg", facecolor=fig.get_facecolor())
    plt.close(fig)


def main():
    OUT.mkdir(exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "svg.fonttype": "none"})

    fig, ax = base("Lo que respalda la evidencia externa",
                   "CCTV frente a controles · No son resultados ni pronósticos de GOSS IP")
    labels = ["Conjunto de entornos\n76 evaluaciones", "Estacionamientos\n8 evaluaciones"]
    ax.barh(labels, [13, 37], color=[GREEN, "#35a599"], height=0.48)
    ax.invert_yaxis()
    ax.set_xlim(0, 45)
    ax.set_xlabel("Reducción relativa aproximada de delitos (%)", color=INK, labelpad=12)
    for y, value in enumerate([13, 37]):
        ax.text(value + 0.8, y, f"{value} %", va="center", color=INK, fontsize=16, weight="bold")
    save(fig, "01-evidencia-externa",
         "Fuente: Piza et al. (2019), DOI 10.1111/1745-9133.12419; resumen de los autores.\n"
         "Los subgrupos se superponen: no sumar porcentajes. No mide el beneficio adicional de la IA.")

    fig, ax = base("Qué tiempos podríamos mejorar",
                   "EJEMPLOS HIPOTÉTICOS · No medidos en campus ni en la demo", height=6.1)
    labels = ["Revisión del aviso\n120 → 60 segundos", "Llegada de apoyo\n240 → 120 segundos",
              "Búsqueda de evidencia\n20 → 5 minutos"]
    ax.barh(labels, [50, 50, 75], color=AMBER, height=0.49)
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.set_xlabel("Disminución del tiempo respecto al valor inicial (%)", color=INK, labelpad=12)
    for y, value in enumerate([50, 50, 75]):
        ax.text(value + 1, y, f"{value} %", va="center", color=INK, fontsize=15, weight="bold")
    save(fig, "02-ejemplos-operativos",
         "Cálculo: 100 × (tiempo inicial − tiempo final) / tiempo inicial.\n"
         "Son ejemplos matemáticos; cada tiempo depende de funciones y responsables diferentes.")

    reductions = [-10, 0, 5, 10, 15]
    remaining = [40 * (1 - value / 100) for value in reductions]
    fig, ax = base("Probar viabilidad también sin mejora",
                   "SENSIBILIDAD ILUSTRATIVA · Base ficticia: 40 incidentes en un año", height=6.5)
    labels = ["Empeora 10 %", "Sin cambio", "Disminuye 5 %", "Disminuye 10 %", "Disminuye 15 %"]
    ax.barh(labels, remaining, color=["#b25449", "#8a969d", GREEN, GREEN, GREEN], height=0.57)
    ax.invert_yaxis()
    ax.set_xlim(0, 50)
    ax.axvline(40, color=INK, linestyle="--", linewidth=1)
    ax.set_xlabel("Incidentes anuales en el escenario ficticio", color=INK, labelpad=12)
    for y, value in enumerate(remaining):
        ax.text(value + 0.6, y, f"{value:.0f}", va="center", color=INK, fontsize=13, weight="bold")
    save(fig, "03-escenarios",
         "Escenarios elegidos para explorar sensibilidad, sin probabilidades asignadas.\n"
         "No son datos de Toluca, Tianguistenco ni otra facultad. No derivan del metaanálisis.")

    source = ROOT.parent / "evidencias" / "validacion_placa_ZTP482A_umbral060.csv"
    with source.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    detected = total = correct = 0
    for row in rows:
        d, n = map(int, row["detecta"].split("/"))
        c, nc = map(int, row["lecturas_correctas"].split("/"))
        if n != nc or not 0 <= c <= d <= n:
            raise ValueError("Denominadores o conteos inconsistentes en evidencia de placas")
        detected += d
        total += n
        correct += c
    if not total or not detected:
        raise ValueError("No se pueden calcular ambas métricas con denominadores vacíos")
    whole = 100 * correct / total
    conditional = 100 * correct / detected
    fig, ax = base("El denominador cambia la conclusión",
                   "ENSAYO SINTÉTICO EXISTENTE · Una referencia, varias condiciones · Umbral 0.60")
    labels = [f"Todos los intentos\n{correct}/{total} correctos",
              f"Solo lo detectado\n{correct}/{detected} correctos"]
    ax.barh(labels, [whole, conditional], color=[GREEN, "#8a969d"], height=0.48)
    ax.invert_yaxis()
    ax.set_xlim(0, 118)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xlabel("Lecturas correctas con el denominador indicado (%)", color=INK, labelpad=12)
    for y, value in enumerate([whole, conditional]):
        ax.text(value + 1.1, y, f"{value:.1f} %", va="center", color=INK, fontsize=15, weight="bold")
    save(fig, "04-denominadores-demo",
         "Fuente: docs/evidencias/validacion_placa_ZTP482A_umbral060.csv.\n"
         "No es rendimiento de campus ni de vehículos reales. Repeticiones no independientes.")

    ratio = (34 / 40) / (45 / 50)
    se = math.sqrt(1 / 34 + 1 / 40 + 1 / 45 + 1 / 50)
    lower_ratio = math.exp(math.log(ratio) - 1.96 * se)
    upper_ratio = math.exp(math.log(ratio) + 1.96 * se)
    calculations = {
        "fecha_revision": "2026-10-07",
        "resultados_campus": None,
        "evidencia_externa": {"doi": "10.1111/1745-9133.12419", "reduccion_agregada_pct": 13,
                              "reduccion_estacionamientos_pct": 37, "aplicable_como_prediccion_uaemex": False},
        "escenarios_ficticios": {"base": 40, "reducciones_pct": reductions, "incidentes": remaining},
        "ensayo_sintetico_existente": {"fuente": source.name,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "oportunidades": total, "detectadas": detected, "correctas": correct,
            "lecturas_correctas_global_pct": whole, "ocr_condicionado_pct": conditional},
        "ejemplo_control_ficticio": {"intervencion": [40, 34], "control": [50, 45],
            "cociente_cambios": ratio, "reduccion_ajustada_pct": 100 * (1 - ratio),
            "ic95_poisson_aproximado_reduccion_pct": [100 * (1 - upper_ratio), 100 * (1 - lower_ratio)]},
        "ejemplo_cero_errores": {"negativos": 20, "fp": 0,
            "limite_superior_unilateral95_pct": 100 * (1 - 0.05 ** (1 / 20))},
        "ejemplo_baja_prevalencia": {"positivos": 10, "negativos": 9990,
            "sensibilidad": 0.90, "fpr": 0.01, "precision_pct": 100 * 9 / (9 + 9990 * 0.01)},
    }
    (OUT / "calculos.json").write_text(json.dumps(calculations, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
    print(f"4 gráficas PNG/SVG y cálculos generados en {OUT}")
    print(f"Ensayo existente: {correct}/{total} global; {correct}/{detected} entre detecciones")


if __name__ == "__main__":
    main()
