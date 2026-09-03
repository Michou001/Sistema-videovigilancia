"""Las fechas que salen de la API siempre llevan zona horaria.

Se corre sin dependencias:  python tests/test_fechas.py

Existe por un bug concreto. SQLite no tiene tipo con zona horaria: se guarda un
datetime en UTC y al leerlo vuelve NAIVE. Sin zona, FastAPI lo serializa como
"2026-09-03T15:23:18" y el navegador lo interpreta como hora LOCAL, asi que en
Mexico (UTC-6) el operador veia un evento de las 09:23 fechado a las 15:23.
Seis horas en el futuro, en un sistema donde la hora de una deteccion es parte
de la evidencia.

Y era inconsistente: los eventos que llegan por WebSocket no pasan por SQLite y
si llevaban zona, asi que en la misma lista convivian la hora buena y la
desplazada. La pista de que algo estaba mal, y la razon de este archivo.

El arreglo es el mixin `FechasEnUtc`. La trampa que estas pruebas vigilan es
que hay DOS familias de modelos que salen por la API -- las tablas y los
esquemas de `response_model`, que revalidan la fila y pierden un serializador
puesto solo en la tabla -- asi que es facil añadir un esquema nuevo y volver a
introducir el bug en un solo endpoint.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.models import Alert, BlacklistFace, BlacklistPlate, Camera, Event  # noqa: E402
from api.routers.alerts import AlertaLeida, EventoLeido  # noqa: E402
from api.routers.blacklist import PlacaLeida  # noqa: E402
from api.routers.faces import RostroLeido  # noqa: E402

# Como sale de SQLite: el valor correcto, pero sin etiqueta de zona.
NAIVE = datetime(2026, 9, 3, 15, 23, 18)


def _fechas_serializadas(modelo) -> dict[str, str]:
    volcado = modelo.model_dump(mode="json")
    return {
        campo: valor
        for campo, valor in volcado.items()
        if isinstance(valor, str) and valor.startswith("2026-09-03T15:23:18")
    }


def _exigir_zona(modelo, *campos_esperados):
    fechas = _fechas_serializadas(modelo)
    for campo in campos_esperados:
        assert campo in fechas, f"{type(modelo).__name__}.{campo} no se serializo"
        valor = fechas[campo]
        assert valor.endswith("Z") or "+00:00" in valor, (
            f"{type(modelo).__name__}.{campo} sale sin zona: {valor!r} -- "
            "el navegador lo leera como hora local"
        )


# --------------------------------------------------------------------------
# Modelos de tabla
# --------------------------------------------------------------------------

def test_evento_lleva_zona():
    _exigir_zona(Event(event_id="e", dedupe_key="d", camera_id="cam-01", ts=NAIVE,
                       received_at=NAIVE, type="plate", value="ABC-123",
                       confidence=0.9), "ts", "received_at")


def test_alerta_lleva_zona():
    _exigir_zona(Alert(event_id="e", camera_id="cam-01", type="plate",
                       severity="critical", title="t", created_at=NAIVE),
                 "created_at")


def test_camara_lleva_zona():
    _exigir_zona(Camera(camera_id="cam-01", name="Entrada",
                        last_heartbeat=NAIVE, created_at=NAIVE),
                 "last_heartbeat", "created_at")


def test_lista_negra_lleva_zona():
    _exigir_zona(BlacklistPlate(plate="ABC-123", plate_normalized="ABC123",
                                reason="robo", created_at=NAIVE, expires_at=NAIVE),
                 "created_at", "expires_at")
    _exigir_zona(BlacklistFace(label="X", reason="orden", embedding=b"", dim=512,
                               created_at=NAIVE),
                 "created_at")


# --------------------------------------------------------------------------
# Esquemas de respuesta: revalidan la fila, asi que necesitan el mixin aparte
# --------------------------------------------------------------------------

def test_evento_leido_lleva_zona():
    _exigir_zona(EventoLeido(event_id="e", camera_id="cam-01", ts=NAIVE,
                             type="plate", value="ABC-123", confidence=0.9,
                             severity="info", observations=5, snapshot_path=None),
                 "ts")


def test_alerta_leida_lleva_zona():
    _exigir_zona(AlertaLeida(id=1, event_id="e", camera_id="cam-01", type="plate",
                             severity="critical", title="t", detail=None,
                             match_kind="exact", match_score=None,
                             snapshot_path=None, status="new",
                             acknowledged_by=None, created_at=NAIVE),
                 "created_at")


def test_placa_leida_lleva_zona():
    _exigir_zona(PlacaLeida(id=1, plate="ABC-123", plate_normalized="ABC123",
                            reason="robo", severity="critical", notes=None,
                            active=True, created_by=None,
                            created_at=NAIVE, expires_at=NAIVE),
                 "created_at", "expires_at")


def test_rostro_leido_lleva_zona():
    _exigir_zona(RostroLeido(id=1, label="X", reason="orden", legal_basis=None,
                             severity="critical", photo_path=None, active=True,
                             created_by=None, created_at=NAIVE),
                 "created_at")


# --------------------------------------------------------------------------

def test_no_altera_una_fecha_que_ya_trae_zona():
    """Solo etiqueta lo que viene sin zona; no reinterpreta ni desplaza nada."""
    con_zona = datetime(2026, 9, 3, 15, 23, 18, tzinfo=timezone.utc)
    volcado = Event(event_id="e", dedupe_key="d", camera_id="c", ts=con_zona,
                    type="plate", value="V", confidence=0.9).model_dump(mode="json")
    assert volcado["ts"].endswith("Z") or "+00:00" in volcado["ts"]
    assert "15:23:18" in volcado["ts"], "la hora no debe desplazarse"


def test_los_campos_no_fecha_pasan_intactos():
    """El serializador es comodin ('*'): no debe tocar nada mas."""
    e = Event(event_id="e", dedupe_key="d", camera_id="cam-01", ts=NAIVE,
              type="plate", value="ABC-123", confidence=0.94, observations=7)
    v = e.model_dump(mode="json")
    assert v["value"] == "ABC-123"
    assert v["confidence"] == 0.94
    assert v["observations"] == 7
    assert v["camera_id"] == "cam-01"


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
