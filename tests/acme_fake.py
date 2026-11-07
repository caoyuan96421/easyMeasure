"""A small ACME (RFC 8555) server for the tests: checks the JWS signatures, nonces
and URLs like Let's Encrypt does, validates HTTP-01 by fetching the challenge
from 127.0.0.1:<http01_port>, and issues certificates from its own CA."""

from __future__ import annotations

import base64
import datetime
import hashlib
import itertools
import json
import secrets
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import NameOID


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class FakeAcme:
    def __init__(self, http01_port: int, days: int = 90, reject_first_nonce: bool = True):
        self.http01_port = http01_port
        self.days = days
        self.reject_nonce = reject_first_nonce
        self.nonces = set()
        self.accounts = {}                      # kid -> jwk
        self.orders, self.authzs, self.challs = {}, {}, {}
        self.ids = itertools.count(1)
        self.lock = threading.Lock()
        self.events = []
        self.ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fake ACME CA")])
        now = datetime.datetime.now(datetime.timezone.utc)
        self.ca_cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                        .public_key(self.ca_key.public_key()).serial_number(1)
                        .not_valid_before(now - datetime.timedelta(days=1))
                        .not_valid_after(now + datetime.timedelta(days=3650))
                        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
                        .add_extension(x509.KeyUsage(True, False, False, False, False, True, True,
                                                     False, False), True)
                        .add_extension(x509.SubjectKeyIdentifier.from_public_key(
                            self.ca_key.public_key()), False)
                        .sign(self.ca_key, hashes.SHA256()))
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/dir":
                    b = fake.base
                    return self._send(200, {"newNonce": b + "/nonce", "newAccount": b + "/acct",
                                            "newOrder": b + "/order",
                                            "meta": {"termsOfService": b + "/tos"}})
                self._send(404, {"type": "urn:ietf:params:acme:error:malformed"})

            def do_HEAD(self):
                self.send_response(200 if self.path == "/nonce" else 404)
                self.send_header("Replay-Nonce", fake.nonce())
                self.end_headers()

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                try:
                    status, obj, headers = fake.handle(self.path, json.loads(body))
                except _Problem as p:
                    status, obj, headers = p.status, {"type": "urn:ietf:params:acme:error:" + p.kind,
                                                      "detail": p.detail}, {}
                self._send(status, obj, headers)

            def _send(self, status, obj, headers=None):
                if isinstance(obj, bytes):
                    data, ctype = obj, "application/pem-certificate-chain"
                else:
                    data = json.dumps(obj).encode()
                    ctype = "application/problem+json" if status >= 400 else "application/json"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Replay-Nonce", fake.nonce())
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.directory = self.base + "/dir"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def nonce(self):
        n = secrets.token_urlsafe(12)
        with self.lock:
            self.nonces.add(n)
        return n

    # -- JWS ------------------------------------------------------------------------------
    def verify(self, path, jws):
        prot = json.loads(b64d(jws["protected"]))
        with self.lock:
            ok = prot.get("nonce") in self.nonces
            self.nonces.discard(prot.get("nonce"))
            if ok and self.reject_nonce:
                self.reject_nonce, ok = False, False
                self.events.append("badNonce")
        if not ok:
            raise _Problem(400, "badNonce", "bad nonce")
        if prot.get("url") != self.base + path:
            raise _Problem(400, "unauthorized", "url mismatch")
        if prot.get("alg") != "ES256":
            raise _Problem(400, "badSignatureAlgorithm", "ES256 only")
        if "jwk" in prot:
            if path != "/acct":
                raise _Problem(400, "malformed", "kid required")
            jwk = prot["jwk"]
        else:
            jwk = self.accounts.get(prot.get("kid"))
            if jwk is None:
                raise _Problem(400, "accountDoesNotExist", "no account")
        pub = ec.EllipticCurvePublicNumbers(int.from_bytes(b64d(jwk["x"]), "big"),
                                            int.from_bytes(b64d(jwk["y"]), "big"),
                                            ec.SECP256R1()).public_key()
        sig = b64d(jws["signature"])
        der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        try:
            pub.verify(der, ("%s.%s" % (jws["protected"], jws["payload"])).encode(),
                       ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            raise _Problem(400, "malformed", "bad signature")
        payload = json.loads(b64d(jws["payload"])) if jws["payload"] else None
        return jwk, prot, payload

    @staticmethod
    def thumbprint(jwk):
        j = json.dumps({k: jwk[k] for k in ("crv", "kty", "x", "y")}, sort_keys=True,
                       separators=(",", ":"))
        return b64u(hashlib.sha256(j.encode()).digest())

    # -- resources ------------------------------------------------------------------------
    def handle(self, path, jws):
        jwk, prot, payload = self.verify(path, jws)
        b = self.base
        if path == "/acct":
            if not (payload or {}).get("termsOfServiceAgreed"):
                raise _Problem(403, "userActionRequired", "agree to the terms")
            kid = b + "/acct/%d" % next(self.ids)
            self.accounts[kid] = jwk
            self.events.append("account %s" % payload.get("contact"))
            return 201, {"status": "valid"}, {"Location": kid}
        if path == "/order":
            oid = next(self.ids)
            auths = []
            for ident in payload["identifiers"]:
                aid, cid = next(self.ids), next(self.ids)
                self.challs[cid] = {"type": "http-01", "url": b + "/chall/%d" % cid,
                                    "token": secrets.token_urlsafe(16), "status": "pending",
                                    "authz": aid, "kid": prot["kid"]}
                self.authzs[aid] = {"status": "pending", "identifier": ident,
                                    "challenges": [{"type": "dns-01", "url": b + "/x",
                                                    "token": "x", "status": "pending"},
                                                   self.challs[cid]]}
                auths.append(b + "/authz/%d" % aid)
            self.orders[oid] = {"status": "pending", "identifiers": payload["identifiers"],
                                "authorizations": auths, "finalize": b + "/finalize/%d" % oid}
            return 201, self._pub(self.orders[oid]), {"Location": b + "/orders/%d" % oid}
        kind, _, num = path.strip("/").partition("/")
        num = int(num) if num.isdigit() else -1
        if kind == "orders":
            return 200, self._pub(self.orders[num]), {}
        if kind == "authz":
            return 200, self._pub(self.authzs[num]), {}
        if kind == "chall":
            ch = self.challs[num]
            self._validate(ch)
            return 200, self._pub(ch), {}
        if kind == "finalize":
            return 200, self._finalize(num, payload), {}
        if kind == "cert":
            return 200, self.orders[num]["pem"].encode(), {}
        raise _Problem(404, "malformed", "no such resource")

    @staticmethod
    def _pub(obj):
        return {k: v for k, v in obj.items() if k not in ("authz", "kid", "pem")}

    def _validate(self, ch):
        authz = self.authzs[ch["authz"]]
        want = "%s.%s" % (ch["token"], self.thumbprint(self.accounts[ch["kid"]]))
        url = "http://127.0.0.1:%d/.well-known/acme-challenge/%s" % (self.http01_port, ch["token"])
        try:
            got = urllib.request.urlopen(url, timeout=3).read().decode()
            ok = got == want
            err = "the key authorization did not match"
        except Exception as exc:
            ok, err = False, "Connection refused / timeout fetching %s: %s" % (url, exc)
        ch["status"] = authz["status"] = "valid" if ok else "invalid"
        if not ok:
            ch["error"] = {"type": "urn:ietf:params:acme:error:connection", "detail": err}
        self.events.append("http-01 %s" % ch["status"])

    def _finalize(self, oid, payload):
        order = self.orders[oid]
        if any(self.authzs[int(a.rsplit("/", 1)[1])]["status"] != "valid"
               for a in order["authorizations"]):
            raise _Problem(403, "orderNotReady", "authorizations are not valid")
        csr = x509.load_der_x509_csr(b64d(payload["csr"]))
        names = sorted(csr.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName))
        if names != sorted(i["value"] for i in order["identifiers"]):
            raise _Problem(400, "badCSR", "names differ from the order")
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(csr.subject)
                .issuer_name(self.ca_cert.subject).public_key(csr.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=1))
                .not_valid_after(now + datetime.timedelta(days=self.days))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    self.ca_key.public_key()), False)
                .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                               False)
                .sign(self.ca_key, hashes.SHA256()))
        order["pem"] = (cert.public_bytes(serialization.Encoding.PEM) +
                        self.ca_cert.public_bytes(serialization.Encoding.PEM)).decode()
        order["status"] = "valid"
        order["certificate"] = self.base + "/cert/%d" % oid
        return self._pub(order)


class _Problem(Exception):
    def __init__(self, status, kind, detail):
        super().__init__(detail)
        self.status, self.kind, self.detail = status, kind, detail
