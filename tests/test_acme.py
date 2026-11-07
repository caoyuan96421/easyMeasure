"""Let's Encrypt certificates (ACME, HTTP-01) for the remote gateway, against a
stand-in ACME server (tests/acme_fake.py)."""

import socket
import ssl
import time

import pytest
from cryptography import x509

from easymeasure.remote import acme
from easymeasure.remote import config as rconfig
from easymeasure.remote.tlsutil import cert_info, server_context

from acme_fake import FakeAcme

pytest.importorskip("aiohttp")
from test_remote import GW, gw_factory, home, server  # noqa: E402,F401  (fixtures)

DOMAIN = "lab.example.org"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def ca():
    f = FakeAcme(free_port())
    yield f
    f.close()


def get(home, ca, **kw):
    kw.setdefault("agree_tos", True)
    return acme.obtain(home, kw.pop("domain", DOMAIN), kw.pop("email", "me@example.org"),
                       directory=ca.directory, http_port=kw.pop("http_port", ca.http01_port), **kw)


def test_certificate_obtained_and_configured(tmp_path, ca):
    st = get(tmp_path, ca, domain="Lab.Example.ORG.")
    assert st["state"] == "ok", st["log"]
    assert "badNonce" in ca.events                       # retried with a fresh nonce
    assert "http-01 valid" in ca.events and "account ['mailto:me@example.org']" in ca.events
    cfg = rconfig.load(tmp_path)
    assert cfg["acme"] == {"domain": DOMAIN, "email": "me@example.org", "staging": False,
                           "directory": ca.directory, "http_port": ca.http01_port}
    chain = x509.load_pem_x509_certificates(open(cfg["certfile"], "rb").read())
    assert len(chain) == 2 and chain[1].subject == ca.ca_cert.subject
    assert chain[0].extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) == [DOMAIN]
    server_context(cfg["certfile"], cfg["keyfile"])      # key and chain belong together
    info = cert_info(cfg["certfile"])
    assert info["names"] == [DOMAIN] and "Fake ACME CA" in info["issuer"]
    assert st["expires"] and acme.read_status(tmp_path)["state"] == "ok"
    assert any("verified" in line for line in st["log"])
    # the account key is kept: the next request reuses the account
    keys = list(acme.acme_dir(tmp_path).glob("account-*.key"))
    assert len(keys) == 1
    n_acc = sum(e.startswith("account") for e in ca.events)
    assert get(tmp_path, ca)["state"] == "ok"
    assert list(acme.acme_dir(tmp_path).glob("account-*.key")) == keys
    assert sum(e.startswith("account") for e in ca.events) == n_acc + 1   # same key: same account


def test_renewal_when_due(tmp_path):
    short = FakeAcme(free_port(), days=10)                # expires within 30 days: due
    try:
        assert get(tmp_path, short)["state"] == "ok"
        cert = rconfig.load(tmp_path)["certfile"]
        assert acme.due(cert)
        serial = x509.load_pem_x509_certificate(open(cert, "rb").read()).serial_number
        st = acme.renew_if_due(tmp_path)
        assert st["state"] == "ok"
        assert x509.load_pem_x509_certificate(open(cert, "rb").read()).serial_number != serial
    finally:
        short.close()
    long_ = FakeAcme(free_port(), days=90)
    try:
        cfg = rconfig.load(tmp_path)
        cfg["acme"].update(directory=long_.directory, http_port=long_.http01_port)
        rconfig.save(cfg, tmp_path)
        assert acme.renew_if_due(tmp_path)["state"] == "ok"       # still due (10 days)
        assert not acme.due(cert)
        assert acme.renew_if_due(tmp_path) is None                # 90 days left: nothing to do
        assert acme.renew_if_due(tmp_path, force=True)["state"] == "ok"
    finally:
        long_.close()
    assert acme.renew_if_due(tmp_path / "elsewhere") is None      # nothing configured


def test_failures_are_explained_and_change_nothing(tmp_path, ca):
    st = get(tmp_path, ca, agree_tos=False)
    assert st["state"] == "error" and "terms of service" in st["message"]
    st = get(tmp_path, ca, http_port=free_port())              # the CA reaches nobody
    assert st["state"] == "error"
    assert "could not be verified" in st["message"] and "nothing reached port" in st["message"]
    assert "http-01 invalid" in ca.events
    busy = socket.socket()
    busy.bind(("", 0))
    busy.listen()
    try:
        st = get(tmp_path, ca, http_port=busy.getsockname()[1])
        assert st["state"] == "error" and "cannot listen on port" in st["message"]
    finally:
        busy.close()
    st = get(tmp_path, ca, domain="192.168.1.5")
    assert st["state"] == "error" and "not a domain name" in st["message"]
    st = acme.obtain(tmp_path, DOMAIN, agree_tos=True, directory="http://127.0.0.1:1/dir")
    assert st["state"] == "error" and "cannot reach" in st["message"]
    assert rconfig.load(tmp_path)["certfile"] is None and rconfig.load(tmp_path)["acme"] is None


def test_domain_check_and_firewall_command():
    assert acme.check_domain(" Lab.Example.org. ") == "lab.example.org"
    for bad in ("localhost", "10.0.0.1", "-a.example.org", "a..b", "", "a b.org"):
        with pytest.raises(acme.AcmeError):
            acme.check_domain(bad)
    cmd = acme.windows_firewall_command([443, 80, 443])
    assert 'delete rule name="easyMeasure remote access"' in cmd
    assert 'add rule name="easyMeasure remote access" dir=in action=allow protocol=TCP ' \
        'localport=80,443' in cmd


def _served_cert(port, cafile=None, name=None):
    """The certificate a new TLS connection gets (verified against *cafile* if given)."""
    if cafile:
        ctx = ssl.create_default_context(cafile=cafile)
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        with ctx.wrap_socket(s, server_hostname=name or "127.0.0.1") as t:
            return x509.load_der_x509_certificate(t.getpeercert(binary_form=True))


def test_gateway_switches_certificate_without_restart(gw_factory, home, ca, tmp_path):
    g = gw_factory(cert_check_s=0.2)
    first = _served_cert(g.port)
    assert "self-signed" in first.subject.rfc4514_string()
    assert get(home, ca)["state"] == "ok"
    cafile = tmp_path / "ca.pem"
    from cryptography.hazmat.primitives import serialization
    cafile.write_bytes(ca.ca_cert.public_bytes(serialization.Encoding.PEM))
    t0 = time.time()
    while g.gw.self_signed and time.time() - t0 < 10:
        time.sleep(0.1)
    # a browser that trusts the CA now connects by name without any warning
    cert = _served_cert(g.port, cafile=str(cafile), name=DOMAIN)
    assert cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) == [DOMAIN]
    import json
    st = json.loads(rconfig.status_path(home).read_text())
    assert st["self_signed"] is False and st["cert"].endswith("fullchain.pem")
    # a broken file is not taken: the gateway keeps serving the good one
    cfg = rconfig.load(home)
    good = cfg["certfile"]
    open(good, "a").write("garbage")
    bad_key = tmp_path / "other.key"
    from cryptography.hazmat.primitives.asymmetric import ec
    acme._write_private(bad_key, ec.generate_private_key(ec.SECP256R1()))
    cfg["keyfile"] = str(bad_key)                       # a key that does not match
    rconfig.save(cfg, home)
    assert g.call(_async(g.gw.check_certificate)) is False
    assert _served_cert(g.port, cafile=str(cafile), name=DOMAIN).serial_number == cert.serial_number
    # back to self-signed
    cfg.update(certfile=None, keyfile=None, acme=None)
    rconfig.save(cfg, home)
    t0 = time.time()
    while not g.gw.self_signed and time.time() - t0 < 10:
        time.sleep(0.1)
    assert "self-signed" in _served_cert(g.port).subject.rfc4514_string()


async def _async(fn):
    return fn()


def test_backend_certificate_commands(demo, home, ca, monkeypatch):
    """remote_cert_request runs the request in its own process and remote_status reports
    progress and the result; remote_cert_use switches to files / self-signed."""
    be = demo
    st = be.remote_status()
    assert st["cert"]["mode"] == "self-signed" and st["cert"]["request"] == {}
    from easymeasure.common.errors import ChannelError
    with pytest.raises(ChannelError, match="Subscriber Agreement"):
        be.remote_cert_request(DOMAIN, agree_tos=False)
    with pytest.raises(ChannelError, match="not a domain name"):
        be.remote_cert_request("10.1.2.3", agree_tos=True)
    be._acme_extra_args = ["--directory", ca.directory]
    st = be.remote_cert_request(DOMAIN, "me@example.org", agree_tos=True,
                                http_port=ca.http01_port)
    assert st["cert"]["request"]["state"] == "running"
    with pytest.raises(ChannelError, match="already running"):
        be.remote_cert_request(DOMAIN, agree_tos=True, http_port=ca.http01_port)
    t0 = time.time()
    while be.remote_status()["cert"]["request"].get("state") == "running" and time.time() - t0 < 60:
        time.sleep(0.2)
    st = be.remote_status()
    assert st["cert"]["request"]["state"] == "ok", st["cert"]["request"]
    assert st["cert"]["mode"] == "letsencrypt" and st["cert"]["names"] == [DOMAIN]
    assert st["urls"][0] == "https://%s:%s" % (DOMAIN, st["port"])
    le = st["cert"]["certfile"], st["cert"]["keyfile"]
    st = be.remote_cert_use("self-signed")
    assert st["cert"]["mode"] == "self-signed" and rconfig.load(home)["acme"] is None
    with pytest.raises(ChannelError, match="cannot be used"):
        be.remote_cert_use("files", le[0], str(home / "nonexistent.key"))
    st = be.remote_cert_use("files", *le)
    assert st["cert"]["mode"] == "files" and st["cert"]["certfile"] == le[0]
    # the port chosen in the dialog is remembered
    be.remote_start(port=free_port(), persist=False)
    port = rconfig.load(home)["port"]
    be.remote_stop(persist=False)
    assert be.remote_status()["port"] == port
