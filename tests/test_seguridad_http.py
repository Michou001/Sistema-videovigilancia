"""La CSP no debe incorporar directivas desde el encabezado Host."""

from api.seguridad_http import politica_csp


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
