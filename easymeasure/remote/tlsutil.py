"""TLS for the remote gateway.

Without a configured certificate a self-signed ECDSA P-256 certificate is created
on first start (``<home>/remote/tls``).  Browsers will warn about it once; Python
clients pin its SHA-256 fingerprint (``python -m easymeasure.remote cert``).  For
a browser without warnings, configure a real certificate (e.g. Let's Encrypt)
with ``certfile`` / ``keyfile`` in the remote config.

Only TLS 1.2+ with forward-secret AEAD ciphers is accepted; TLS compression and
renegotiation are disabled.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import os
import socket
import ssl
from pathlib import Path
from typing import List, Optional, Tuple

CIPHERS = "ECDHE+AESGCM:ECDHE+CHACHA20:!aNULL:!eNULL:!MD5:!SHA1:!DSS:!RC4:!3DES"


def _local_names() -> Tuple[List[str], List[str]]:
    names = {"localhost"}
    ips = {"127.0.0.1", "::1"}
    try:
        h = socket.gethostname()
        if h:
            names.add(h)
            fq = socket.getfqdn()
            if fq:
                names.add(fq)
            for info in socket.getaddrinfo(h, None):
                ips.add(info[4][0].split("%")[0])
    except OSError:
        pass
    return sorted(names), sorted(ips)


def generate_self_signed(cert_path: Path, key_path: Path, extra_names=(), days: int = 825) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    names, ips = _local_names()
    for n in extra_names:
        try:
            ips.append(str(ipaddress.ip_address(n)))
        except ValueError:
            names.append(n)
    host = names[-1] if names else "localhost"
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "easyMeasure %s" % host[:40]),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "easyMeasure (self-signed)")])
    san = [x509.DNSName(n) for n in dict.fromkeys(names)]
    for ip in dict.fromkeys(ips):
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False,
                                         content_commitment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=False, crl_sign=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    kb = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption())
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(kb)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def ensure_certificate(tls_dir: Path, certfile: Optional[str] = None,
                       keyfile: Optional[str] = None, extra_names=()) -> Tuple[Path, Path, bool]:
    """Return (cert, key, self_signed)."""
    if certfile:
        if not keyfile:
            raise ValueError("keyfile is required with certfile")
        return Path(certfile), Path(keyfile), False
    cert, key = Path(tls_dir) / "cert.pem", Path(tls_dir) / "key.pem"
    if not (cert.exists() and key.exists()):
        generate_self_signed(cert, key, extra_names)
    return cert, key, True


def server_context(cert: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers(CIPHERS)           # TLS 1.3 suites are always AEAD + forward secret
    ctx.options |= ssl.OP_NO_COMPRESSION | ssl.OP_CIPHER_SERVER_PREFERENCE
    ctx.options |= getattr(ssl, "OP_NO_RENEGOTIATION", 0)
    ctx.options |= getattr(ssl, "OP_SINGLE_ECDH_USE", 0)
    ctx.load_cert_chain(str(cert), str(key))
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def fingerprint(cert: Path) -> str:
    """SHA-256 fingerprint of the certificate, ``AB:CD:...``."""
    der = ssl.PEM_cert_to_DER_cert(Path(cert).read_text())
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def cert_info(cert: Path) -> dict:
    from cryptography import x509
    c = x509.load_pem_x509_certificate(Path(cert).read_bytes())
    try:
        san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(n.value) for n in san]
    except Exception:
        names = []
    end = getattr(c, "not_valid_after_utc", None) or c.not_valid_after
    return {"subject": c.subject.rfc4514_string(), "names": names,
            "expires": end.isoformat(), "fingerprint": fingerprint(cert)}
