"""Pruebas de placas mexicanas: formatos de la NOM-001-SCT-2-2016 y anteriores,
letras prohibidas, entidad por serie, marca F/T y placas extranjeras.

    python tests/test_placas_mexicanas.py

No necesitan modelos: el detector se arma sin cargar el OCR y se le dan las
lecturas que habria producido.
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import numpy as np  # noqa: E402

from shared.plates import (  # noqa: E402
    FORMATOS,
    LETRAS_PROHIBIDAS_NOM,
    SERIES_AUTOMOVIL_2016,
    analizar_placa,
    buscar_coincidencia,
    corregir_placa,
    describir,
    elegir_mejor_lectura,
    entidad_por_serie,
    es_placa_valida,
    formatear,
    pais_por_votos,
)

# --------------------------------------------------------------------------
# Formatos

def test_particular_vigente():
    assert es_placa_valida("PZW-123-A") == (True, "PZW123A", "Automóvil particular")
    assert formatear("pzw123a") == "PZW-123-A"
    info = analizar_placa("PZW123A")
    assert info.norma == "NOM-001-SCT-2-2016" and info.pais == "México"


def test_formatos_por_tipo_de_vehiculo():
    casos = {
        "A01-AAA": ("Automóvil particular", "A01-AAA", "Ciudad de México"),
        "A-123-BC": ("Camión particular", "A-123-BC", None),
        "01-AB-2C": ("Autotransporte federal", "01-AB-2C", None),
        "N01-AB": ("Motocicleta", "N01-AB", None),
        "B-123-CDE": ("Taxi / servicio público", "B-123-CDE", None),
        "L-0001-A": ("Taxi / servicio público", "L-0001-A", "Ciudad de México"),
        "AM-123-BC": ("Ambulancia", "AM-123-BC", None),
        "12-PC-345": ("Protección civil", "12-PC-345", None),
        "CD-1234": ("Cuerpo diplomático", "CD-1234", None),
        "ABC-12-34": ("Automóvil particular (formato anterior)", "ABC-12-34", None),
        "D-12-345": ("Convertidor (dolly) federal", "D-12-345", None),
    }
    for placa, (tipo, legible, entidad) in casos.items():
        info = analizar_placa(placa)
        assert info is not None, placa
        assert (info.tipo, info.legible, info.entidad) == (tipo, legible, entidad), (placa, info)


def test_cada_formato_se_reconoce_a_si_mismo():
    """Una placa armada con la mascara de cada formato tiene que ser valida."""
    for f in FORMATOS:
        muestra = "".join("B" if c == "L" else "5" if c == "D" else c[1] for c in f.clases)
        valida, limpio, _ = es_placa_valida(muestra)
        assert valida, f"{f.mascara} -> {muestra}"
        assert len(limpio) == len(f.clases)


# --------------------------------------------------------------------------
# Letras prohibidas (I, Ñ, O, Q) en la norma vigente

def test_letra_prohibida_se_corrige_a_d():
    # En LLL-DDD-L no existe la O: una O leida ahi es una D (o un 0 mal leido).
    assert not es_placa_valida("POW-123-A")[0]
    placa, tipo, n = corregir_placa("POW-123-A")
    assert (placa, tipo, n) == ("PDW123A", "Automóvil particular", 1)


def test_cero_en_posicion_de_letra_es_d():
    # "0BC-123-A" encaja tal cual como remolque (1AB-234-C), pero un
    # particular con la D mal leida es mucho mas probable.
    placa, tipo, n = corregir_placa("0BC-123-A")
    assert (placa, tipo, n) == ("DBC123A", "Automóvil particular", 1)
    # "PZW-123-0" en cambio es valida tal cual: particular anterior PZW-12-30.
    assert corregir_placa("PZW-123-0") == ("PZW1230", "Automóvil particular (formato anterior)", 0)


def test_uno_en_posicion_de_letra_no_se_inventa():
    # "1" donde va una letra: la I no existe en la norma vigente y ninguna
    # letra permitida se le parece lo suficiente para adivinarla.
    resultado = corregir_placa("PZW-123-1")
    assert resultado is None or resultado[0] != "PZW123I"


def test_series_no_usan_letras_prohibidas():
    for desde, hasta, _ in SERIES_AUTOMOVIL_2016:
        assert not (set(desde + hasta) & LETRAS_PROHIBIDAS_NOM), (desde, hasta)


# --------------------------------------------------------------------------
# Entidad por serie

def test_entidad_por_serie():
    assert entidad_por_serie("AAA-001-A") == "Aguascalientes"
    assert entidad_por_serie("AFZ-999-Z") == "Aguascalientes"
    assert entidad_por_serie("AGA-001-A") == "Baja California"
    assert entidad_por_serie("JHK-123-A") == "Jalisco"
    assert entidad_por_serie("MNP-128-J") == "Estado de México"
    assert entidad_por_serie("RKA-001-B") == "Nuevo León"
    assert entidad_por_serie("ZHZ-999-Z") == "Zacatecas"
    # Fuera de las series asignadas, o en otro formato: no se adivina.
    assert entidad_por_serie("ZZZ-123-A") is None
    assert entidad_por_serie("ABC-12-34") is None


def test_series_ordenadas_sin_huecos_ni_traslapes():
    anterior = None
    for desde, hasta, entidad in SERIES_AUTOMOVIL_2016:
        assert desde <= hasta, entidad
        if anterior is not None:
            assert desde > anterior, f"{entidad} se traslapa con la serie anterior"
        anterior = hasta


def test_describir_para_la_alerta():
    assert describir("JHK-123-A") == "Automóvil particular de Jalisco"
    assert describir("A01-AAA") == "Automóvil particular de Ciudad de México"
    assert describir("A-123-BC") == "Camión particular"
    assert describir("7ABC123", "United States") == "Placa de Estados Unidos"


# --------------------------------------------------------------------------
# Marca F/T (placa delantera / trasera)

def test_marca_de_posicion_leida_de_mas():
    assert corregir_placa("PZW123AT")[0] == "PZW123A"
    assert corregir_placa("FPZW123A")[0] == "PZW123A"
    # Una T que SI es parte de la placa no se quita.
    assert corregir_placa("PZW-123-T") == ("PZW123T", "Automóvil particular", 0)


# --------------------------------------------------------------------------
# Placas extranjeras

def test_placa_extranjera_no_se_fuerza_al_formato_mexicano():
    info = analizar_placa("7ABC123", "United States")
    assert info.extranjera and info.pais == "Estados Unidos" and info.legible == "7ABC123"
    # Sin pais, la misma lectura no es placa mexicana.
    assert analizar_placa("7ABC123") is None


def test_pais_por_votos():
    assert pais_por_votos([("United States", 0.9)] * 4 + [("Mexico", 0.5)]) == "United States"
    assert pais_por_votos([("United States", 0.9), ("Mexico", 0.9)]) is None, "votos repartidos"
    assert pais_por_votos([("Unknown", 1.0), (None, 1.0)]) is None
    assert pais_por_votos([]) is None


def test_consenso_prefiere_formatos_comunes():
    # "6ZW123" encaja en "demostracion" (raro); "PZW123A" en particular.
    lecturas = [("6ZW123", 0.9), ("PZW123A", 0.6)]
    assert elegir_mejor_lectura(lecturas)[0] == "PZW123A"


def test_cruce_con_lista_negra_ignora_separadores():
    lista = [("PZW-123-A", 1)]
    assert buscar_coincidencia("PZW123A", lista).exacta
    assert buscar_coincidencia("P2W-I23-A", lista).exacta


# --------------------------------------------------------------------------
# Detector (sin cargar modelos)

def _detector():
    from edge.config import EdgeConfig
    from edge.detectors.plates import PlateDetector

    det = PlateDetector.__new__(PlateDetector)
    det.cfg = EdgeConfig()
    det.cfg.plate_dedupe_s = 0
    det._emitidas = {}
    det._duplicados_suprimidos = 0
    det._eventos_emitidos = 0
    det.snapshot_hd = None
    det._forma_frame = (720, 1280)
    det._hd_usados = 0
    return det


def _track(lecturas, regiones=None, crudas=None, conf=0.9):
    from edge.tracking import Track

    t = Track(track_id=7, bbox=(100, 100, 220, 160), confidence=0.8, first_seen=1000.0,
              last_seen=1002.0, hits=12)
    t.state["lecturas"] = lecturas
    t.state["regiones"] = regiones or []
    t.state["crudas"] = crudas or [(x, c) for x, c in lecturas]
    return t


def test_detector_arma_evento_mexicano_con_tipo_y_entidad():
    det = _detector()
    ev = det._construir_evento(_track([("JHK123A", 0.95), ("JHK123A", 0.9), ("JHK123A", 0.95)],
                                      regiones=[("United States", 0.9)] * 2))
    assert ev.value == "JHK-123-A"
    assert ev.meta["tipo_placa"] == "Automóvil particular"
    assert ev.meta["entidad"] == "Jalisco" and ev.meta["pais"] == "México"
    # El OCR dijo "Estados Unidos", pero el formato es mexicano valido.
    assert ev.meta["pais_ocr"] == "Estados Unidos"


def test_detector_reporta_placa_extranjera_que_antes_descartaba():
    det = _detector()
    track = _track([("7ABC123", 0.9)], regiones=[("United States", 0.95)] * 3,
                   crudas=[("7ABC123", 0.9)] * 3)
    ev = det._construir_evento(track)
    assert ev is not None, "una placa de EUA bien leida no debe perderse"
    assert ev.value == "7ABC123" and ev.meta["pais"] == "Estados Unidos"
    assert ev.meta["tipo_placa"] == "Placa extranjera"


def test_detector_descarta_formato_raro_dudoso():
    det = _detector()
    assert det._construir_evento(_track([("6ZW123", 0.7)])) is None
    assert det._construir_evento(_track([("6ZW123", 0.95)] * 3)) is not None


def test_detector_guarda_donde_esta_la_placa_en_la_evidencia():
    det = _detector()
    track = _track([("JHK123A", 0.95)] * 3)
    track.state["recorte"] = np.zeros((40, 120, 3), np.uint8)
    ev = det._construir_evento(track)
    try:
        assert ev.meta["placa_en_evidencia"] == [0.0, 0.0, 1.0, 1.0]
        assert ev.snapshot_path
    finally:
        if ev and ev.snapshot_path:
            (RAIZ / ev.snapshot_path).unlink(missing_ok=True)


# --------------------------------------------------------------------------

def test_descarta_caja_real_717_y_lectura_unica():
    det = _detector()
    caja = _track([("717", 0.4013)], regiones=[("United States", 0.95)], crudas=[("717", 0.4013)])
    caja.confidence = 0.4809
    assert det._construir_evento(caja) is None
    assert det._construir_evento(_track([("JHK123A", 0.99)])) is None
    # Variantes generadas por una sola pasada no cuentan como tres votos.
    assert det._construir_evento(_track([("JHK123A", 0.99)] * 5, crudas=[("JHK123A", 0.99)])) is None
    assert det._construir_evento(_track([("JHK123A", 0.95)] * 3)) is not None


# --------------------------------------------------------------------------
# Emision en cuanto la placa queda confirmada (sin esperar a que se vaya)

class _FuturoPendiente:
    def __init__(self, listo=False):
        self.listo = listo

    def done(self):
        return self.listo

    def result(self, timeout=None):
        return None


def test_emite_en_cuanto_se_confirma_sin_esperar_que_se_vaya():
    det = _detector()
    det.cfg.plate_min_readings = 2
    track = _track([("JHK123A", 0.95), ("JHK123A", 0.96)])
    ev = det._emitir_si_confirmada(track, 1002.0)
    assert ev is not None and ev.value == "JHK-123-A"
    # Una sola vez por track: ni en el siguiente frame ni al caducar.
    assert det._emitir_si_confirmada(track, 1003.0) is None
    assert track.state["emitido"]


def test_no_emite_con_una_sola_lectura():
    det = _detector()
    det.cfg.plate_min_readings = 2
    track = _track([("JHK123A", 0.99)])
    assert det._emitir_si_confirmada(track, 1002.0) is None
    assert not track.state.get("emitido")
    # Llega la segunda lectura que coincide: ahora si.
    track.state["lecturas"].append(("JHK123A", 0.97))
    track.state["crudas"].append(("JHK123A", 0.97))
    assert det._emitir_si_confirmada(track, 1003.0) is not None


def test_espera_la_foto_hd_un_momento_y_luego_emite_sin_ella():
    det = _detector()
    det.cfg.plate_min_readings = 2
    track = _track([("JHK123A", 0.95), ("JHK123A", 0.96)])
    track.state["hd_future"] = _FuturoPendiente()
    track.state["hd_pedido_ts"] = 1001.5
    assert det._emitir_si_confirmada(track, 1002.0) is None, "aun puede llegar la HD"
    assert det._emitir_si_confirmada(track, 1001.5 + det.ESPERA_HD_S + 0.1) is not None


def test_duplicado_suprimido_no_se_reintenta_cada_frame():
    det = _detector()
    det.cfg.plate_min_readings = 2
    det.cfg.plate_dedupe_s = 60
    primero = _track([("JHK123A", 0.95), ("JHK123A", 0.96)])
    assert det._emitir_si_confirmada(primero, 1002.0) is not None
    segundo = _track([("JHK123A", 0.95), ("JHK123A", 0.96)])
    segundo.track_id = 8
    assert det._emitir_si_confirmada(segundo, 1003.0) is None
    assert segundo.state["emitido"] and det._duplicados_suprimidos == 1
    assert det._emitir_si_confirmada(segundo, 1004.0) is None
    assert det._duplicados_suprimidos == 1


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
