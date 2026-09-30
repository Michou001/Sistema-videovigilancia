"""Pruebas de lectura y cruce de placas (shared/plates.py).

    python tests/test_placas.py

Cubren los tres pasos que hay entre la salida cruda de EasyOCR y el evento:
unir fragmentos, corregir por posicion segun el formato de placa, y decidir
entre varias lecturas del mismo vehiculo.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.plates import (  # noqa: E402
    analizar_placa,
    buscar_coincidencia,
    corregir_placa,
    elegir_mejor_lectura,
    es_placa_valida,
    formatear,
    lecturas_de_ocr,
    normalizar,
)


def _caja(x, y, ancho, alto):
    """Caja en el formato de EasyOCR: 4 puntos en sentido horario."""
    return [[x, y], [x + ancho, y], [x + ancho, y + alto], [x, y + alto]]


# --------------------------------------------------------------------------
# Correccion por posicion

def test_lectura_valida_no_se_toca():
    assert corregir_placa("ABC-123") == ("ABC123", "Automóvil (formato anterior)", 0)
    assert corregir_placa("PZW-123-A") == ("PZW123A", "Automóvil particular", 0)


def test_digito_en_posicion_de_letra():
    placa, _, n = corregir_placa("A8C-123")
    assert placa == "ABC123" and n == 1


def test_letra_en_posicion_de_digito():
    placa, _, n = corregir_placa("ABC-I2O")
    assert placa == "ABC120" and n == 2


def test_formato_edomex():
    placa, formato, _ = corregir_placa("MNP 12B J")
    assert placa == "MNP128J" and formato == "Automóvil particular"
    # La serie MNP esta dentro de LGA-PEZ, asignada al Estado de Mexico.
    assert analizar_placa(placa).entidad == "Estado de México"


def test_demasiadas_correcciones_se_rechaza():
    # Ninguna plantilla encaja sin cambiar la mitad de los caracteres: eso ya
    # no es una placa mal leida.
    assert corregir_placa("SSSSSSS") is None
    assert corregir_placa("ZZZZZZZZZ") is None


def test_texto_sin_forma_de_placa():
    assert corregir_placa("HOLA") is None
    assert corregir_placa("") is None


# --------------------------------------------------------------------------
# Fragmentos de EasyOCR

def test_placa_partida_en_dos_fragmentos():
    """REGRESION: EasyOCR devolvia 'ABC' y '123' por separado y ningun
    fragmento suelto pasaba el patron: el vehiculo se quedaba sin evento."""
    resultados = [
        (_caja(10, 20, 60, 30), "ABC", 0.91),
        (_caja(80, 21, 60, 30), "123", 0.88),
    ]
    lecturas = dict(lecturas_de_ocr(resultados))
    assert "ABC123" in lecturas


def test_se_descarta_el_nombre_del_estado():
    resultados = [
        (_caja(20, 2, 100, 10), "GUANAJUATO", 0.95),
        (_caja(10, 20, 60, 30), "GTO", 0.90),
        (_caja(80, 20, 30, 30), "12", 0.90),
        (_caja(115, 20, 30, 30), "34", 0.90),
    ]
    lecturas = dict(lecturas_de_ocr(resultados))
    assert "GTO1234" in lecturas
    assert not any("GUANAJUATO" in t for t in lecturas)


def test_fragmentos_bajo_umbral_no_cuentan():
    resultados = [(_caja(10, 20, 120, 30), "ABC123", 0.10)]
    assert lecturas_de_ocr(resultados, min_conf=0.35) == []


def test_correccion_penaliza_la_confianza():
    limpia = lecturas_de_ocr([(_caja(0, 0, 100, 30), "ABC123", 0.9)])
    corregida = lecturas_de_ocr([(_caja(0, 0, 100, 30), "A8C123", 0.9)])
    assert limpia[0][0] == corregida[0][0] == "ABC123"
    assert corregida[0][1] < limpia[0][1]


def test_sin_placa_devuelve_fragmentos_crudos():
    resultados = [(_caja(0, 0, 40, 30), "XY", 0.8)]
    assert lecturas_de_ocr(resultados) == [("XY", 0.8)]


# --------------------------------------------------------------------------
# Consenso entre lecturas del mismo vehiculo

def test_consenso_pesa_confianza_no_solo_conteo():
    lecturas = [("ABC123", 0.9), ("ABC123", 0.85), ("ABC123", 0.9),
                ("XYZ789", 0.2), ("XYZ789", 0.2), ("XYZ789", 0.2), ("XYZ789", 0.2)]
    assert normalizar(elegir_mejor_lectura(lecturas)[0]) == normalizar("ABC123")


def test_fusion_por_posicion_recupera_la_placa():
    """Ningun frame leyo la placa completa sin error, pero cada posicion la
    leyo bien la mayoria de las veces."""
    lecturas = [("ABC124", 0.8), ("ABD123", 0.8), ("XBC123", 0.8), ("ABC723", 0.8)]
    texto, _ = elegir_mejor_lectura(lecturas)
    assert formatear(texto) == "ABC-123", texto


def test_lectura_unica():
    assert elegir_mejor_lectura([("ABC123", 0.7)]) == ("ABC123", 0.7)
    assert elegir_mejor_lectura([]) is None


# --------------------------------------------------------------------------
# Cruce contra lista negra

def test_cruce_exacto_y_difuso():
    lista = [("ABC-123", 1)]
    assert buscar_coincidencia("A8C-I23", lista).exacta
    difusa = buscar_coincidencia("ABX-123", lista)
    assert difusa is not None and not difusa.exacta and difusa.distancia == 1
    assert buscar_coincidencia("QWE-987", lista) is None


def test_validacion_de_alta():
    assert es_placa_valida("abc 123")[0]
    assert not es_placa_valida("HOLA-MUNDO")[0]


# --------------------------------------------------------------------------

def main() -> int:
    pruebas = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    fallos = 0
    for nombre, fn in pruebas:
        try:
            fn()
            print(f"  [OK]    {nombre}")
        except AssertionError as e:
            fallos += 1
            print(f"  [FALLA] {nombre}: {e}")
        except Exception as e:  # noqa: BLE001
            fallos += 1
            print(f"  [ERROR] {nombre}: {type(e).__name__}: {e}")

    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
