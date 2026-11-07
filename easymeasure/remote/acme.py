"""Certificates from Let's Encrypt (or any ACME server, RFC 8555) for the gateway.

``obtain()`` proves control of a domain name with the **HTTP-01** challenge: for
the few seconds of the check it answers
``http://<domain>/.well-known/acme-challenge/<token>`` on port 80 of this
computer, so the domain must point to this computer and port 80 must be
reachable from the internet (router / firewall).  The result is a certificate
chain and key in ``<home>/remote/tls/acme/<domain>/`` that the gateway uses (and
reloads when they change); :func:`renew_if_due` renews it 30 days before it
expires.

Only the standard library and ``cryptography`` are used.  The account key
(ECDSA P-256, ``ES256``) is kept per ACME server in ``<home>/remote/tls/acme/``.

Command line: ``python -m easymeasure.remote letsencrypt DOMAIN --email ADDR
--agree-tos [--staging]`` and ``... letsencrypt --renew``; progress goes to
``<home>/remote/acme.json`` (shown in Backend → Remote access).
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional

LETSENCRYPT = "https://acme-v02.api.letsencrypt.org/directory"
LETSENCRYPT_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
TOS_URL = "https://letsencrypt.org/repository/"
RENEW_DAYS = 30
USER_AGENT = "easyMeasure-acme/1"
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")


class AcmeError(Exception):
    pass


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def check_domain(domain: str) -> str:
    d = (domain or "").strip().lower().rstrip(".")
    if not _DOMAIN_RE.match(d) or d.replace(".", "").isdigit():
        raise AcmeError("%r is not a domain name (a certificate needs a DNS name such as "
                        "lab.example.org, not an IP address)" % domain)
    return d


# ---------------------------------------------------------------------------
# HTTP-01 responder
# ---------------------------------------------------------------------------
class ChallengeServer:
    """Answers ``/.well-known/acme-challenge/<token>`` on a port (80) while it runs."""

    def __init__(self, port: int = 80, host: str = ""):
        self.tokens: Dict[str, str] = {}
        self.hits: List[str] = []
        tokens, hits = self.tokens, self.hits

        class Handler(BaseHTTPRequestHandler):
            server_version = "easyMeasure"
            sys_version = ""

            def do_GET(self):
                m = re.fullmatch(r"/\.well-known/acme-challenge/([A-Za-z0-9_-]+)", self.path)
                body = tokens.get(m.group(1)) if m else None
                hits.append("%s %s" % (self.client_address[0], self.path))
                if body is None:
                    self.send_error(404)
                    return
                data = body.encode("ascii")
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        try:
            self.httpd = ThreadingHTTPServer((host, port), Handler)
        except OSError as exc:
            raise AcmeError(
                "cannot listen on port %d for the Let's Encrypt check (%s). Another program "
                "uses it - on Windows often IIS / 'World Wide Web Publishing Service' or "
                "Skype; stop it for the moment, or run as administrator if access is denied"
                % (port, exc.strerror or exc)) from None
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="acme-http01",
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


# ---------------------------------------------------------------------------
# ACME client
# ---------------------------------------------------------------------------
class AcmeClient:
    def __init__(self, directory: str, account_key_path: Path,
                 log: Callable[[str], None] = lambda m: None,
                 ssl_context: Optional[ssl.SSLContext] = None):
        self.directory_url = directory
        self.log = log
        self.ctx = ssl_context
        self.key = self._load_key(Path(account_key_path))
        nums = self.key.public_key().public_numbers()
        self.jwk = {"crv": "P-256", "kty": "EC", "x": b64u(nums.x.to_bytes(32, "big")),
                    "y": b64u(nums.y.to_bytes(32, "big"))}
        self.thumbprint = b64u(hashlib.sha256(json.dumps(
            self.jwk, sort_keys=True, separators=(",", ":")).encode()).digest())
        self.kid: Optional[str] = None
        self.nonce: Optional[str] = None
        self.dir = self._get_json(directory)

    @staticmethod
    def _load_key(path: Path):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        if path.exists():
            return serialization.load_pem_private_key(path.read_bytes(), None)
        key = ec.generate_private_key(ec.SECP256R1())
        _write_private(path, key)
        return key

    # -- HTTP --------------------------------------------------------------------------
    def _open(self, req):
        try:
            return urllib.request.urlopen(req, timeout=30, context=self.ctx)
        except urllib.error.HTTPError as exc:
            return exc                          # an ACME problem document: read below
        except (urllib.error.URLError, OSError) as exc:
            raise AcmeError("cannot reach %s: %s" % (req.full_url, getattr(exc, "reason", exc))) \
                from None

    def _get_json(self, url):
        r = self._open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}))
        body = r.read()
        if r.status >= 400:
            raise AcmeError(self._problem(body, r.status))
        return json.loads(body)

    def _new_nonce(self) -> str:
        r = self._open(urllib.request.Request(self.dir["newNonce"], method="HEAD",
                                              headers={"User-Agent": USER_AGENT}))
        n = r.headers.get("Replay-Nonce")
        if not n:
            raise AcmeError("the ACME server gave no nonce")
        return n

    @staticmethod
    def _problem(body: bytes, status: int) -> str:
        try:
            p = json.loads(body)
            text = p.get("detail") or p.get("type") or str(p)
            for sub in p.get("subproblems") or []:
                text += "; %s" % sub.get("detail", "")
            return text
        except Exception:
            return "HTTP %d: %s" % (status, body[:200])

    def _sign(self, url: str, payload) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        protected = {"alg": "ES256", "nonce": self.nonce or self._new_nonce(), "url": url}
        if self.kid:
            protected["kid"] = self.kid
        else:
            protected["jwk"] = self.jwk
        p64 = b64u(json.dumps(protected).encode())
        pl64 = "" if payload is None else b64u(json.dumps(payload).encode())
        der = self.key.sign(("%s.%s" % (p64, pl64)).encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        self.nonce = None
        return json.dumps({"protected": p64, "payload": pl64, "signature": b64u(sig)}).encode()

    def post(self, url: str, payload=None, accept: str = "application/json"):
        """Signed POST (payload None = POST-as-GET); returns (response, body)."""
        for attempt in range(3):
            req = urllib.request.Request(url, data=self._sign(url, payload), method="POST",
                                         headers={"Content-Type": "application/jose+json",
                                                  "Accept": accept, "User-Agent": USER_AGENT})
            r = self._open(req)
            body = r.read()
            self.nonce = r.headers.get("Replay-Nonce")
            if r.status < 400:
                return r, body
            if b"badNonce" in body and attempt < 2:
                continue                        # stale nonce: sign again with the fresh one
            raise AcmeError(self._problem(body, r.status))

    def post_json(self, url, payload=None):
        r, body = self.post(url, payload)
        return r, (json.loads(body) if body else {})

    # -- protocol -----------------------------------------------------------------------
    def register(self, email: str, agree_tos: bool) -> None:
        if not agree_tos:
            raise AcmeError("the certificate authority's terms of service must be agreed to")
        payload = {"termsOfServiceAgreed": True}
        if email:
            payload["contact"] = ["mailto:" + email]
        r, _ = self.post_json(self.dir["newAccount"], payload)
        self.kid = r.headers["Location"]

    def _poll(self, url: str, done=("valid",), bad=("invalid",), timeout: float = 120.0) -> dict:
        t0 = time.time()
        while True:
            _, obj = self.post_json(url)
            st = obj.get("status")
            if st in done:
                return obj
            if st in bad:
                err = obj.get("error") or next((c.get("error") for c in obj.get("challenges", [])
                                                if c.get("error")), None) or {}
                raise AcmeError(err.get("detail") or "%s is %s" % (url, st))
            if time.time() - t0 > timeout:
                raise AcmeError("the ACME server did not finish in %d s (%s)" % (timeout, st))
            time.sleep(1.0 if time.time() - t0 < 10 else 3.0)

    def issue(self, domains: List[str], responder: ChallengeServer, key) -> str:
        """Order a certificate for *domains* with *key*; returns the PEM chain."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.x509.oid import NameOID
        r, order = self.post_json(self.dir["newOrder"],
                                  {"identifiers": [{"type": "dns", "value": d} for d in domains]})
        order_url = r.headers["Location"]
        for authz_url in order["authorizations"]:
            _, authz = self.post_json(authz_url)
            name = authz["identifier"]["value"]
            if authz.get("status") == "valid":
                self.log("%s: already validated" % name)
                continue
            ch = next((c for c in authz["challenges"] if c["type"] == "http-01"), None)
            if ch is None:
                raise AcmeError("the server offers no HTTP-01 check for %s" % name)
            responder.tokens[ch["token"]] = "%s.%s" % (ch["token"], self.thumbprint)
            self.log("%s: the certificate authority checks http://%s/.well-known/acme-challenge/…"
                     % (name, name))
            self.post_json(ch["url"], {})
            try:
                self._poll(authz_url)
            except AcmeError as exc:
                raise AcmeError("%s could not be verified: %s%s" % (
                    name, exc, "" if responder.hits else
                    " - nothing reached port %d of this computer: check that %s points to "
                    "this computer's public address and that port 80 is open in the firewall "
                    "and forwarded by the router" % (responder.port, name))) from None
            self.log("%s: verified" % name)
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domains[0])]))
               .add_extension(x509.SubjectAlternativeName([x509.DNSName(d) for d in domains]),
                              critical=False)
               .sign(key, hashes.SHA256()))
        self.post_json(order["finalize"],
                       {"csr": b64u(csr.public_bytes(serialization.Encoding.DER))})
        order = self._poll(order_url, bad=("invalid",))
        _, pem = self.post(order["certificate"], accept="application/pem-certificate-chain")
        self.log("certificate issued")
        return pem.decode("ascii")


# ---------------------------------------------------------------------------
# Files, status, renewal
# ---------------------------------------------------------------------------
def acme_dir(home: Optional[Path] = None) -> Path:
    from . import config as rconfig
    d = rconfig.tls_dir(home) / "acme"
    d.mkdir(parents=True, exist_ok=True)
    return d


def status_file(home: Optional[Path] = None) -> Path:
    from . import config as rconfig
    return rconfig.remote_dir(home) / "acme.json"


def read_status(home: Optional[Path] = None) -> dict:
    try:
        return json.loads(status_file(home).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_status(home, st: dict) -> None:
    p = status_file(home)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _write_private(path: Path, key) -> None:
    from cryptography.hazmat.primitives import serialization
    path.parent.mkdir(parents=True, exist_ok=True)
    data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def expires(cert_path) -> Optional[datetime.datetime]:
    from cryptography import x509
    try:
        c = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    except Exception:
        return None
    na = getattr(c, "not_valid_after_utc", None)
    return na if na is not None else c.not_valid_after.replace(tzinfo=datetime.timezone.utc)


def due(cert_path, days: int = RENEW_DAYS) -> bool:
    e = expires(cert_path)
    return e is None or e - datetime.datetime.now(datetime.timezone.utc) < \
        datetime.timedelta(days=days)


def obtain(home: Optional[Path], domain: str, email: str = "", agree_tos: bool = False,
           staging: bool = False, directory: Optional[str] = None, http_port: int = 80,
           ssl_context: Optional[ssl.SSLContext] = None, configure: bool = True) -> dict:
    """Get a certificate for *domain* and (with *configure*) make the gateway use it.
    Progress and the outcome are written to ``acme.json``; returns the final status."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from . import config as rconfig
    directory = directory or (LETSENCRYPT_STAGING if staging else LETSENCRYPT)
    st = {"state": "running", "domain": domain, "staging": bool(staging), "directory": directory,
          "started": time.time(), "log": [], "pid": os.getpid()}

    def log(msg):
        st["log"].append("%s %s" % (time.strftime("%H:%M:%S"), msg))
        st["message"] = msg
        _write_status(home, st)
    try:
        domain = st["domain"] = check_domain(domain)
        log("requesting a %scertificate for %s from %s" % (
            "TEST " if staging else "", domain, directory))
        try:
            socket.getaddrinfo(domain, 80)
        except OSError:
            log("warning: %s does not resolve here - it must point to this computer" % domain)
        tag = hashlib.sha256(directory.encode()).hexdigest()[:10]
        client = AcmeClient(directory, acme_dir(home) / ("account-%s.key" % tag), log, ssl_context)
        client.register(email, agree_tos)
        key = ec.generate_private_key(ec.SECP256R1())
        with ChallengeServer(http_port) as responder:
            pem = client.issue([domain], responder, key)
        out = acme_dir(home) / domain
        out.mkdir(parents=True, exist_ok=True)
        cert, keyf = out / "fullchain.pem", out / "privkey.pem"
        # key first: the gateway reloads when the chain changes and needs the matching key
        _write_private(keyf, key)
        tmp = cert.with_name("fullchain.pem.tmp")
        tmp.write_text(pem, encoding="ascii")
        os.replace(tmp, cert)
        if configure:
            cfg = rconfig.load(home)
            cfg.update(certfile=str(cert), keyfile=str(keyf),
                       acme={"domain": domain, "email": email, "staging": bool(staging),
                             "directory": directory, "http_port": int(http_port)})
            rconfig.save(cfg, home)
        e = expires(cert)
        st.update(state="ok", cert=str(cert), key=str(keyf),
                  expires=e.isoformat() if e else None)
        log("saved %s" % cert)
        log("done: valid until %s - the gateway uses it now and renews it %d days before"
            % (e.strftime("%Y-%m-%d") if e else "?", RENEW_DAYS))
    except Exception as exc:
        st["state"] = "error"
        log("failed: %s" % exc)
    finally:
        st["finished"] = time.time()
        _write_status(home, st)
    return st


def renew_if_due(home: Optional[Path] = None, force: bool = False,
                 ssl_context: Optional[ssl.SSLContext] = None) -> Optional[dict]:
    """Renew the configured Let's Encrypt certificate when it is due (None: nothing to do)."""
    from . import config as rconfig
    cfg = rconfig.load(home)
    a = cfg.get("acme")
    if not a or not cfg.get("certfile"):
        return None
    if not force and not due(cfg["certfile"]):
        return None
    return obtain(home, a["domain"], a.get("email", ""), agree_tos=True,
                  staging=a.get("staging", False), directory=a.get("directory"),
                  http_port=int(a.get("http_port", 80)), ssl_context=ssl_context)


def windows_firewall_command(ports, name: str = "easyMeasure remote access") -> str:
    """``cmd /c`` arguments that (re)create one inbound Windows-firewall rule for *ports*
    (run elevated)."""
    plist = ",".join(str(int(p)) for p in sorted(set(ports)))
    return ('/c netsh advfirewall firewall delete rule name="{n}" >nul 2>&1 & '
            'netsh advfirewall firewall add rule name="{n}" dir=in action=allow '
            'protocol=TCP localport={p} profile=any').format(n=name, p=plist)
