"""La CSP no debe incorporar directivas desde el encabezado Host.

    python tests/test_seguridad_http.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.seguridad_http import politica_csp  # noqa: E402


def test_csp_acepta_hosts_de_despliegue():
    for host in ("localhost:8000", "camaras.ejemplo.mx", "[::1]:8000"):
        csp = politica_csp(host)
        assert f"ws://{host}" in csp
        assert f"wss://{host}" in csp


def test_csp_descarta_host_malformado():
    for host in ("servidor; script-src *", "servidor\nscript-src *", "servidor:99999"):
        csp = politica_csp(host)
        assert "connect-src 'self';" in csp
        assert "ws://" not in csp
        assert "wss://" not in csp


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
