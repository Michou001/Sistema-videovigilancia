"""Genera un certificado TLS autofirmado para servir el dashboard por HTTPS.

    python tools/generar_certificado.py
    python tools/generar_certificado.py --nombre vigilancia.local --ip 192.168.1.20

Crea data/tls/servidor.crt y data/tls/servidor.key (la llave no sale de la
maquina: data/ esta en .gitignore). Despues agrega al .env:

    SSL_CERTFILE=data/tls/servidor.crt
    SSL_KEYFILE=data/tls/servidor.key

y arranca con `python -m api` (o iniciar_api.bat).

El navegador va a advertir que el certificado no es de una autoridad
reconocida. Para quitar la advertencia en las PCs del centro de monitoreo,
importa servidor.crt como "Entidad de certificacion raiz de confianza"
(Windows: certmgr.msc) -- una vez por PC. Si la organizacion tiene su propia
autoridad certificadora o un dominio publico, usa ese certificado en su lugar.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import socket
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _ips_locales() -> list[str]:
    ips = {"127.0.0.1"}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # No manda nada: solo pregunta al sistema que interfaz usaria.
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(ips)


def generar(nombres: list[str], ips: list[str], dias: int, destino: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    llave = ec.generate_private_key(ec.SECP256R1())
    sujeto = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, nombres[0]),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "GOSS IP - videovigilancia"),
    ])
    alternativos = [x509.DNSName(n) for n in nombres] + \
                   [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    ahora = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(sujeto)
        .issuer_name(sujeto)
        .public_key(llave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(ahora - dt.timedelta(minutes=5))
        .not_valid_after(ahora + dt.timedelta(days=dias))
        .add_extension(x509.SubjectAlternativeName(alternativos), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                       critical=False)
        .sign(llave, hashes.SHA256())
    )

    destino.mkdir(parents=True, exist_ok=True)
    ruta_cert = destino / "servidor.crt"
    ruta_llave = destino / "servidor.key"
    ruta_llave.write_bytes(llave.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    try:
        ruta_llave.chmod(0o600)
    except OSError:
        pass
    ruta_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return ruta_cert, ruta_llave


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--nombre", action="append", default=[],
                   help="Nombre DNS del servidor (repetible). Por defecto: el de esta maquina y localhost")
    p.add_argument("--ip", action="append", default=[],
                   help="IP del servidor (repetible). Por defecto: las de esta maquina")
    p.add_argument("--dias", type=int, default=825, help="Vigencia (default 825, el maximo que aceptan los navegadores)")
    p.add_argument("--destino", default=str(RAIZ / "data" / "tls"))
    args = p.parse_args()

    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("[x] Falta el paquete 'cryptography': pip install cryptography", file=sys.stderr)
        return 1

    nombres = args.nombre or [socket.gethostname(), "localhost"]
    ips = args.ip or _ips_locales()
    cert, llave = generar(nombres, ips, args.dias, Path(args.destino))
    print(f"  [+] Certificado: {cert}")
    print(f"  [+] Llave      : {llave}   (no la compartas)")
    print(f"      Valido para: {', '.join(nombres + ips)}")
    print()
    print("  Agrega al .env:")
    print(f"      SSL_CERTFILE={cert.relative_to(RAIZ).as_posix() if cert.is_relative_to(RAIZ) else cert}")
    print(f"      SSL_KEYFILE={llave.relative_to(RAIZ).as_posix() if llave.is_relative_to(RAIZ) else llave}")
    print("  y arranca con:  python -m api")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
