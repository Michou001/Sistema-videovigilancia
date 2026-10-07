"""Pruebas de la limpieza de archivos huerfanos de la purga de retencion.

    python tests/test_retencion_huerfanas.py

REGRESION: unas pruebas corrian la purga con una base de datos temporal pero
con las carpetas reales de evidencia. Para esa base ninguna foto tenia
registro y se borraron las fotos y clips de una prueba con la camara real.
Lo mismo pasaria en produccion al apuntar DATABASE_URL a una base nueva.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

_TMP = Path(tempfile.mkdtemp())
from bd_prueba import borrar as _borrar_bd  # noqa: E402
from bd_prueba import url_temporal  # noqa: E402

os.environ["DATABASE_URL"] = url_temporal(_TMP, "huerfanas")
os.environ["API_TOKEN"] = "token-de-prueba-del-worker"

from sqlmodel import Session  # noqa: E402

from api.config import BASE_DIR, get_config  # noqa: E402
from api.database import engine, init_db  # noqa: E402
from api.models import Event  # noqa: E402
from api.retention import Politica, purgar  # noqa: E402


def _foto(nombre: str, vieja: bool = True) -> Path:
    ruta = get_config().snapshot_dir / nombre
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(b"\xff\xd8\xff")
    if vieja:
        hace = time.time() - 3600
        os.utime(ruta, (hace, hace))
    return ruta


def _evento_con_foto(nombre: str) -> None:
    with Session(engine) as s:
        s.add(Event(event_id=str(uuid.uuid4()), dedupe_key=str(uuid.uuid4()), camera_id="cam-h",
                    type="plate", value="ABC123",
                    confidence=0.9, ts=datetime.now(timezone.utc), severity="info",
                    snapshot_path=(get_config().snapshot_dir / nombre).relative_to(BASE_DIR).as_posix()))
        s.commit()


def _limpiar_carpeta() -> None:
    for f in get_config().snapshot_dir.glob("*.jpg"):
        f.unlink()


def test_las_pruebas_no_usan_la_carpeta_real():
    reales = [(BASE_DIR / "data" / "snapshots").resolve(), (BASE_DIR / "data" / "clips").resolve()]
    for carpeta in (get_config().snapshot_dir.resolve(), get_config().clips_dir.resolve()):
        assert not any(carpeta == r or carpeta.is_relative_to(r) for r in reales), carpeta


def test_huerfanas_sueltas_se_borran():
    init_db()
    _limpiar_carpeta()
    con_registro = [f"{uuid.uuid4()}.jpg" for _ in range(25)]
    for n in con_registro:
        _foto(n)
        _evento_con_foto(n)
    sueltas = [_foto(f"{uuid.uuid4()}.jpg") for _ in range(3)]
    with Session(engine) as s:
        cuenta = purgar(s, Politica())
    assert cuenta["huerfanas"] == 3
    assert not any(f.exists() for f in sueltas)
    assert all((get_config().snapshot_dir / n).exists() for n in con_registro)


def test_base_de_datos_ajena_no_borra_nada():
    """Casi todas las fotos sin registro: la base de datos es otra."""
    init_db()
    _limpiar_carpeta()
    _evento_con_foto("conocida.jpg")
    _foto("conocida.jpg")
    ajenas = [_foto(f"{uuid.uuid4()}.jpg") for _ in range(30)]
    with Session(engine) as s:
        cuenta = purgar(s, Politica())
    assert cuenta["huerfanas"] == 0
    assert all(f.exists() for f in ajenas)


def test_foto_recien_escrita_no_es_huerfana():
    init_db()
    _limpiar_carpeta()
    reciente = _foto(f"{uuid.uuid4()}.jpg", vieja=False)
    with Session(engine) as s:
        purgar(s, Politica())
    assert reciente.exists()


def main() -> int:
    pruebas = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    fallos = 0
    try:
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
    finally:
        engine.dispose()
        _borrar_bd(os.environ["DATABASE_URL"])
        shutil.rmtree(_TMP, ignore_errors=True)

    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
