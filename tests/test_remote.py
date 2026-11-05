"""Remote access: gateway security, robustness and performance.

The gateway runs on its own asyncio loop in a thread (in-process, for speed);
crash isolation and supervision are tested with real subprocesses.
"""

import asyncio
import base64
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

aiohttp = pytest.importorskip("aiohttp")

from easymeasure.backend.server import Server
from easymeasure.client import BackendClient
from easymeasure.client.remote import RemoteClient
from easymeasure.common import codec
from easymeasure.common.errors import ExpressionError, RemoteError
from easymeasure.common.exprs import Expression
from easymeasure.remote import gateway as gwmod
from easymeasure.remote import policy
from easymeasure.remote.config import users_path
from easymeasure.remote.gateway import Gateway
from easymeasure.remote.users import UserStore, check_password, hash_password

from conftest import EXAMPLES, ROOT

PW = {"ada": "admin-password-1", "olga": "operator-password-1", "vic": "viewer-password-1"}
ROLE = {"ada": "admin", "olga": "operator", "vic": "viewer"}
FAST_CFG = {"host": "127.0.0.1", "port": 0, "lockout_s": 1.0, "heartbeat_s": 2.0}


# ----------------------------------------------------------------- fixtures --
class GW:
    """A gateway on its own event loop thread."""

    def __init__(self, home, runtime_file, cfg):
        self.loop = asyncio.new_event_loop()
        self.t = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.t.start()
        self.gw = Gateway(home, dict(FAST_CFG, **cfg), runtime_file=runtime_file, console=False)
        self.call(self.gw.start())
        self.port = self.gw.port
        self.url = "https://127.0.0.1:%d" % self.port
        self.fp = self.gw.fingerprint
        # wait for the control link (data directory for path checks)
        t0 = time.time()
        while not self.gw.data_roots and time.time() - t0 < 10:
            time.sleep(0.05)

    def call(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self):
        if getattr(self, "_stopped", False):
            return
        self._stopped = True
        try:
            self.call(self.gw.stop())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.t.join(5)


@pytest.fixture
def home(_isolated_home):
    users = UserStore(users_path(_isolated_home))
    for u, pw in PW.items():
        users.set_user(u, pw, ROLE[u])
    return _isolated_home


@pytest.fixture
def server(demo, home):
    srv = Server(demo, port=0, runtime_file=home / "backend.json")
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def gw_factory(home, server):
    made = []

    def make(**cfg):
        g = GW(home, home / "backend.json", cfg)
        made.append(g)
        return g
    yield make
    for g in made:
        g.stop()


@pytest.fixture
def gw(gw_factory):
    return gw_factory()


def rc(gw, user, **kw):
    return RemoteClient(gw.url, user, PW[user], fingerprint=gw.fp, **kw)


def enc(msg):
    return codec.encode(msg)[4:]      # WebSocket messages: frames without length prefix


dec = codec.decode_body


def insecure():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def run(coro):
    return asyncio.run(coro)


async def _login(session, gw, user, pw=None, token=True, **kw):
    r = await session.post(gw.url + "/api/login", json={"user": user, "password": pw or PW[user],
                                                         "token": token}, ssl=insecure(), **kw)
    return r, await r.json(content_type=None)


def raw_ws(port, token=None, origin=None, cookie=None, rcvbuf=None):
    """Hand-made WebSocket client (lets tests misbehave at the protocol level)."""
    s = socket.socket()
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    s.connect(("127.0.0.1", port))
    ss = insecure().wrap_socket(s, server_hostname="localhost")
    key = base64.b64encode(os.urandom(16)).decode()
    hdr = ["GET /ws HTTP/1.1", "Host: 127.0.0.1:%d" % port, "Upgrade: websocket",
           "Connection: Upgrade", "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13"]
    if token:
        hdr.append("Authorization: Bearer " + token)
    if origin:
        hdr.append("Origin: " + origin)
    if cookie:
        hdr.append("Cookie: " + cookie)
    ss.sendall(("\r\n".join(hdr) + "\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = ss.recv(4096)
        if not d:
            break
        buf += d
    status = int(buf.split(b" ", 2)[1]) if buf else 0
    return ss, status


def ws_frame(payload: bytes, opcode: int = 2) -> bytes:
    mask = os.urandom(4)
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def api_token(gw, user):
    async def go():
        async with aiohttp.ClientSession() as s:
            r, d = await _login(s, gw, user)
            assert r.status == 200, d
            return d["token"]
    return run(go())


# ------------------------------------------------------------- unit level ----
def test_password_hashing_and_store(tmp_path):
    rec = hash_password("correct horse battery")
    assert check_password("correct horse battery", rec)
    assert not check_password("correct horse batterY", rec)
    assert not check_password("x", {"salt": "!!", "hash": "??", "n": 2, "r": 1, "p": 1})
    # absurd (DoS) parameters are refused, not computed
    assert not check_password("x", dict(rec, n=1 << 30))
    store = UserStore(tmp_path / "u.json")
    with pytest.raises(ValueError):
        store.set_user("bob", "short", "viewer")
    with pytest.raises(ValueError):
        store.set_user("bad name!", "long enough password", "viewer")
    with pytest.raises(ValueError):
        store.set_user("bob", "aaaaaaaaaaaaaaa", "viewer")            # too simple
    store.set_user("bob", "long enough password", "viewer")
    assert store.verify("bob", "long enough password")["role"] == "viewer"
    assert store.verify("bob", "wrong") is None
    assert store.verify("nobody", "long enough password") is None
    assert store.verify("../../etc", "x") is None
    if os.name == "posix":
        assert (tmp_path / "u.json").stat().st_mode & 0o077 == 0
    gen = store.generation("bob")
    other = UserStore(tmp_path / "u.json")                       # another process' view
    other.set_user("bob", None, "operator")
    time.sleep(0.01)
    assert store.generation("bob") != gen                        # picked up without restart
    other.remove("bob")
    assert store.get("bob") is None
    # timing: unknown users cost about as much as known ones (no enumeration)
    store.set_user("eve", "long enough password", "viewer")
    t0 = time.perf_counter(); store.verify("eve", "nope"); t1 = time.perf_counter()
    store.verify("ghost", "nope"); t2 = time.perf_counter()
    assert 0.3 < (t2 - t1) / (t1 - t0) < 3.0


def test_policy_matrix(tmp_path):
    data = tmp_path / "data"
    (data / "2026").mkdir(parents=True)
    f = data / "2026" / "00001_x.emj"
    f.write_text("x")
    roots = [str(data)]
    ok = lambda role, cmd, args=None: policy.check(role, cmd, args or {}, roots)
    # viewer
    ok("viewer", "read_channel", {"name": "Vg"})
    ok("viewer", "load_run", {"path": str(f)})
    for cmd in ("write_channel", "run_action", "queue_add", "abort", "set_settings", "raw_write"):
        with pytest.raises(policy.Denied):
            ok("viewer", cmd)
    # operator: no code, no setup / limit changes, no files outside the data directory
    ok("operator", "write_channel", {"name": "Vg", "value": 0.1})
    ok("operator", "queue_add", {"item": {"kind": "sweep", "definition": {"pre": {"hook": "  "}}}})
    for cmd, args in (("run_script_now", {"code": "1"}),
                      ("queue_add", {"item": {"kind": "script", "code": "import os"}}),
                      ("queue_add", {"item": {"kind": "sweep", "definition": {"post": {"hook": "x=1"}}}}),
                      ("queue_add", {"item": {"kind": "weird"}}),
                      ("define_alias", {"name": "Vg", "max": 100}), ("raw_write", {}),
                      ("load_setup", {}), ("set_settings", {}), ("shutdown", {}),
                      ("save_setup", {"path": "/tmp/evil.json"}),
                      ("make_ppt", {"ppt_path": "/tmp/x.pptx"}),
                      ("load_run", {"path": str(data / ".." / "secret.emj")}),
                      ("load_run", {"path": "/etc/passwd"}),
                      ("read_run_header", {"path": str(tmp_path)}),
                      ("load_run", {"path": 5}), ("load_run", {})):
        with pytest.raises(policy.Denied):
            ok("operator", cmd, args)
    if os.name == "posix":                     # a symlink out of the data directory
        (data / "link.emj").symlink_to("/etc/hostname")
        with pytest.raises(policy.Denied):
            ok("operator", "load_run", {"path": str(data / "link.emj")})
    assert ok("operator", "monitor_add", {"channel": "I", "interval": 0})["interval"] == \
        policy.MIN_MONITOR_INTERVAL
    # admin: everything except local-only commands
    ok("admin", "run_script_now", {"code": "1"})
    ok("admin", "load_run", {"path": "/anything"})
    for cmd in ("start_tray", "remote_start", "remote_stop", "remote_status", "remote_set_user"):
        with pytest.raises(policy.Denied):
            ok("admin", cmd)
    with pytest.raises(policy.Denied):
        ok("admin", "set_settings", {"remote_access": False})
    with pytest.raises(policy.Denied):
        ok("admin", "no_such_command")
    with pytest.raises(policy.Denied):
        ok("intruder", "ping")
    # every backend command is classified
    from easymeasure.backend.service import Backend
    for cmd in Backend.COMMANDS:
        assert policy.required_level(cmd) is not None or cmd in policy.LOCAL_ONLY, cmd


def test_expression_sandbox():
    good = {"start + (stop-start)*t**2": 1.5, "(Vx-Vy)/2": 0.25, "x if x > 0 else -x": 2,
            "np.sin(0) + sqrt(4) + abs(-1) + max(1, 2)": 5.0}
    vars_ = {"start": 1, "stop": 3, "t": 0.5, "Vx": 1, "Vy": 0.5, "x": -2}
    for src, want in good.items():
        assert Expression(src).evaluate(vars_) == pytest.approx(want)
    assert Expression("x*np.sin(np.linspace(0, 2*np.pi, 128))").evaluate({"x": 1}).shape == (128,)
    evil = ["__import__('os').system('id')", "().__class__.__bases__[0].__subclasses__()",
            "np.load('x')", "np.save('x', 1)", "np.savetxt('x', [1])", "np.fromfile('x')",
            "np.lib.npyio", "np.ctypeslib", "open('x')", "eval('1')", "exec('1')",
            "getattr(np, 'load')", "type(1)", "[c for c in ()]", "lambda: 0", "f'{x}'",
            "x.__class__", "np.sin.__self__", "'{0.__class__}'.format(1)", "'%s' % 'x'",
            "9**9**9", "'a'*10**9", "1 << 10**8", "np.zeros(10**10)", "np.ones((10**5,) * 3)",
            "np.linspace(0, 1, 10**9)", "np.arange(1e12)", "np.tile(1, 10**9)",
            "np.sum(x, dtype='O')", "(1).bit_length()", "x" * 5000]
    for src in evil:
        t = time.time()
        with pytest.raises(ExpressionError):
            Expression(src).evaluate({"x": np.arange(3)})
        assert time.time() - t < 1.0, src


def test_sweep_size_limits(demo):
    from easymeasure.common.errors import SweepDefinitionError
    big = {"name": "huge", "dimensions": [
        {"points": 100_000, "sweep": [{"channel": "Vg", "start": 0, "stop": 1}]},
        {"points": 100_000, "sweep": [{"channel": "Vb", "start": 0, "stop": 0.1}]}]}
    with pytest.raises(SweepDefinitionError):
        demo.validate_sweep(big)
    with pytest.raises(SweepDefinitionError):
        demo.validate_sweep({"dimensions": [{"points": 10**9}]})
    with pytest.raises(SweepDefinitionError):
        demo.validate_sweep({"dimensions": [{"points": 1000, "sweep": [
            {"channel": "Vg", "start": 0, "stop": 1, "mapping": "np.zeros(1000000)"}]}]})


# ----------------------------------------------------------------- transport --
def test_tls_only_and_certificate(gw):
    # plain HTTP on the TLS port gets nothing useful
    s = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
    s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    try:
        data = s.recv(100)
    except (ConnectionResetError, socket.timeout):
        data = b""
    assert not data.startswith(b"HTTP/1.1 200")
    s.close()
    # old protocol versions are refused
    ctx = insecure()
    try:
        import warnings
        warnings.simplefilter("ignore", DeprecationWarning)
        ctx.minimum_version = ssl.TLSVersion.TLSv1
        ctx.maximum_version = ssl.TLSVersion.TLSv1_1
        ctx.set_ciphers("ALL:@SECLEVEL=0")
    except (ValueError, ssl.SSLError):
        pass
    else:
        with pytest.raises((ssl.SSLError, ConnectionResetError, OSError)):
            with socket.create_connection(("127.0.0.1", gw.port), timeout=5) as raw:
                ctx.wrap_socket(raw, server_hostname="localhost")
    # a modern client negotiates TLS 1.2+ with an AEAD cipher; the certificate covers localhost
    with socket.create_connection(("127.0.0.1", gw.port), timeout=5) as raw:
        with insecure().wrap_socket(raw, server_hostname="localhost") as t:
            assert t.version() in ("TLSv1.2", "TLSv1.3")
            assert any(c in t.cipher()[0] for c in ("GCM", "CHACHA20"))
    from easymeasure.remote.tlsutil import cert_info
    info = cert_info(gw.gw.cert)
    assert "127.0.0.1" in info["names"] and "localhost" in info["names"]
    assert info["fingerprint"] == gw.fp
    # pinning: the right fingerprint connects, a wrong one is refused
    with rc(gw, "vic") as c:
        assert c.call("ping")["pong"]
    with pytest.raises(Exception):
        RemoteClient(gw.url, "vic", PW["vic"], fingerprint="00" * 32)
    with pytest.raises(Exception):        # default verification rejects the self-signed cert
        RemoteClient(gw.url, "vic", PW["vic"])


def test_security_headers_and_static_files(gw):
    async def go():
        async with aiohttp.ClientSession() as s:
            r = await s.get(gw.url + "/", ssl=insecure())
            assert r.status == 200
            body = await r.text()
            assert "easyMeasure" in body
            h = r.headers
            assert "script-src 'self'" in h["Content-Security-Policy"]
            assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
            assert h["X-Content-Type-Options"] == "nosniff"
            assert h["X-Frame-Options"] == "DENY"
            assert h["Referrer-Policy"] == "no-referrer"
            assert "max-age" in h["Strict-Transport-Security"]
            assert "aiohttp" not in h.get("Server", "") and "Python" not in h.get("Server", "")
            r = await s.get(gw.url + "/assets/js/app.js", ssl=insecure(),
                            headers={"Accept-Encoding": "gzip"})
            assert r.status == 200 and r.headers["Content-Type"].startswith("text/javascript")
            assert r.headers.get("Content-Encoding") == "gzip"
            etag = r.headers["ETag"]
            r = await s.get(gw.url + "/assets/js/app.js", ssl=insecure(),
                            headers={"If-None-Match": etag})
            assert r.status == 304
            for bad in ("/assets/../remote/users.json", "/assets/%2e%2e/%2e%2e/remote/users.json",
                        "/assets/js/../../../../etc/passwd", "/users.json", "/assets/js/nope.js",
                        "/assets/..%2fremote%2fusers.json", "/remote/users.json", "/.git/config"):
                r = await s.get(gw.url + bad, ssl=insecure())
                assert r.status in (403, 404), bad
                assert "scrypt" not in await r.text()
            r = await s.get(gw.url + "/api/session", ssl=insecure())
            assert (await r.json())["user"] is None
            assert r.headers["Cache-Control"] == "no-store"
    run(go())


def test_login_cookie_and_generic_errors(gw):
    async def go():
        async with aiohttp.ClientSession() as s:
            r, d = await _login(s, gw, "olga", token=False)
            assert r.status == 200 and d["role"] == "operator" and "token" not in d
            ck = r.headers["Set-Cookie"]
            assert ck.startswith("__Host-em=")
            for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
                assert flag.lower() in ck.lower(), flag
            r1, d1 = await _login(s, gw, "olga", "wrong-password")
            r2, d2 = await _login(s, gw, "nobody", "wrong-password")
            assert r1.status == r2.status == 401 and d1 == d2      # no user enumeration
            r = await s.post(gw.url + "/api/login", data="user=olga&password=x", ssl=insecure(),
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
            assert r.status == 415                                  # no form-based CSRF login
            r, _ = await _login(s, gw, "olga", headers={"Origin": "https://evil.example"})
            assert r.status == 403
            r = await s.post(gw.url + "/api/login", data=b"x" * 100_000, ssl=insecure(),
                             headers={"Content-Type": "application/json"})
            assert r.status in (400, 413)
            r = await s.post(gw.url + "/api/login", data=b"{bad json", ssl=insecure(),
                             headers={"Content-Type": "application/json"})
            assert r.status == 400
    run(go())


def test_brute_force_lockout(gw):
    async def go():
        async with aiohttp.ClientSession() as s:
            for _ in range(5):
                r, _ = await _login(s, gw, "vic", "guess-guess-guess")
                assert r.status == 401
            r, d = await _login(s, gw, "vic")                       # right password, locked
            assert r.status == 429 and "Retry-After" in r.headers
            r, d = await _login(s, gw, "olga")                      # whole address locked
            assert r.status == 429
            await asyncio.sleep(1.2)                                # lockout_s = 1 in tests
            for _ in range(5):                                      # second lockout is longer
                r, _ = await _login(s, gw, "vic", "guess-guess-guess")
                assert r.status == 401
            r, _ = await _login(s, gw, "vic")
            assert r.status == 429
            await asyncio.sleep(1.2)
            r, _ = await _login(s, gw, "vic")
            assert r.status == 429                                  # 2 s this time
            await asyncio.sleep(1.2)
            r, d = await _login(s, gw, "vic")
            assert r.status == 200
            # the account itself is protected against attempts from many addresses
            th = gw.gw.throttle
            for i in range(10):
                th.failure("10.0.0.%d" % i, "olga")
            assert th.blocked("10.9.9.9", "olga") > 0
    run(go())


def test_websocket_auth_and_origin(gw):
    tok = api_token(gw, "vic")
    for kw, want in ((dict(), 401), (dict(token="x" * 43), 401),
                     (dict(token=tok, origin="https://evil.example"), 401),
                     (dict(token=tok), 101), (dict(token=tok, origin="https://127.0.0.1:%d" % gw.port), 101)):
        s, st = raw_ws(gw.port, **kw)
        s.close()
        assert st == want, kw

    async def go():
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
            r, _ = await _login(s, gw, "vic", token=False)
            assert r.status == 200
            # cookie without Origin (not a browser) and cookie with a foreign Origin: refused
            for hdr in ({}, {"Origin": "https://evil.example"}):
                with pytest.raises(aiohttp.WSServerHandshakeError) as ei:
                    await s.ws_connect(gw.url + "/ws", ssl=insecure(), headers=hdr)
                assert ei.value.status == 401
            ws = await s.ws_connect(gw.url + "/ws", ssl=insecure(),
                                    headers={"Origin": "https://127.0.0.1:%d" % gw.port})
            w = dec((await ws.receive()).data)
            assert w["type"] == "welcome" and w["role"] == "viewer"
            # logging out ends the live connection too
            r = await s.post(gw.url + "/api/logout", ssl=insecure(),
                             headers={"Origin": "https://127.0.0.1:%d" % gw.port})
            assert r.status == 200
            msg = await asyncio.wait_for(ws.receive(), 5)
            while msg.type == aiohttp.WSMsgType.BINARY:
                msg = await asyncio.wait_for(ws.receive(), 5)
            assert msg.type == aiohttp.WSMsgType.CLOSE and msg.data == 4401   # the reason
    run(go())


def test_roles_enforced_end_to_end(gw, demo):
    with rc(gw, "vic") as v, rc(gw, "olga") as o, rc(gw, "ada") as a:
        assert v.role == "viewer" and "write_channel" not in v.allowed
        assert v.call("read_channel", name="Vg") == 0.0
        with pytest.raises(RemoteError) as ei:
            v.call("write_channel", name="Vg", value=0.1)
        assert ei.value.kind == "PermissionError"
        assert o.call("write_channel", name="Vg", value=0.1, ramp=False) == pytest.approx(0.1)
        with pytest.raises(RemoteError) as ei:                  # backend limits still apply
            o.call("write_channel", name="Vg", value=50)
        assert ei.value.kind == "LimitError"
        o.call("run_action", ref="dmm.reset")
        sweep = {"name": "s", "dimensions": [{"points": 3, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}],
                                              "record": ["I"]}]}
        o.call("queue_add", item={"kind": "sweep", "definition": sweep}, start=True)
        for cmd, args in (("run_script_now", {"code": "open('/tmp/pwned', 'w')"}),
                          ("queue_add", {"item": {"kind": "sweep", "definition": dict(sweep, pre={"hook": "import os"})}}),
                          ("raw_write", {"instrument": "src", "command": "*RST"}),
                          ("define_alias", {"name": "Vg", "target": "src.V1", "max": 100, "replace": True}),
                          ("set_settings", {"data_dir": "/"}), ("shutdown", {}),
                          ("load_run", {"path": "/etc/passwd"}), ("save_setup", {"path": "/tmp/x.json"})):
            with pytest.raises(RemoteError) as ei:
                o.call(cmd, **args)
            assert ei.value.kind == "PermissionError", cmd
        assert not os.path.exists("/tmp/pwned")
        assert demo.channels.info("Vg")["max"] == 2.0           # limits unchanged
        for cmd in ("start_tray", "remote_stop", "remote_set_user"):
            with pytest.raises(RemoteError) as ei:
                a.call(cmd)
            assert ei.value.kind == "PermissionError"
        assert a.call("set_settings", fsync_interval=0.1)["fsync_interval"] == 0.1
        # the audit log names the user for every change
        time.sleep(0.2)
        audit = (gw.gw.home / "logs" / "remote.log").read_text()
        assert "cmd web:olga@127.0.0.1 write_channel" in audit
        assert "denied web:olga@127.0.0.1 run_script_now" in audit
        with pytest.raises(RemoteError):
            a.call("remote_set_user", name="mallory", password="secret-password-123", role="admin")
        time.sleep(0.2)
        audit = (gw.gw.home / "logs" / "remote.log").read_text()
        assert "secret-password-123" not in audit and '"password":"***"' in audit
        assert PW["olga"] not in audit and PW["ada"] not in audit


def test_account_changes_close_sessions(gw, home):
    users = UserStore(users_path(home))
    codes = {}
    for who, change in (("olga", lambda: users.set_user("olga", "a-new-password-1")),
                        ("vic", lambda: users.set_user("vic", None, "operator")),
                        ("ada", lambda: users.remove("ada"))):
        c = rc(gw, who)
        c.call("ping")
        change()
        t0 = time.time()
        while not c.closed and time.time() - t0 < 8:
            time.sleep(0.1)
        assert c.closed, who
        codes[who] = c.close_code
        c.close()
    assert set(codes.values()) == {4403}
    with pytest.raises(PermissionError):
        RemoteClient(gw.url, "olga", PW["olga"], fingerprint=gw.fp)     # old password
    with rc(gw, "vic") as v:
        assert v.role == "operator"


def test_malformed_traffic_is_contained(gw_factory):
    gw = gw_factory(max_msg_bytes=64 * 1024)
    tok = api_token(gw, "olga")

    def attempt(payload_frames, want_close=True):
        s, st = raw_ws(gw.port, token=tok)
        assert st == 101
        s.settimeout(10)
        for f in payload_frames:
            try:
                s.sendall(f)
            except OSError:
                break
        data = b""
        try:
            while True:
                d = s.recv(65536)
                if not d:
                    break
                data += d
        except (socket.timeout, ConnectionResetError, ssl.SSLError):
            pass
        s.close()
        return data

    deep = b"[" * 100_000 + b"]" * 100_000
    bad_json = struct.pack("<II", 4 + len(deep), len(deep)) + deep
    cases = [
        [ws_frame(b"hello", opcode=1)],                                   # text frame
        [ws_frame(os.urandom(1000))],                                     # garbage binary
        [ws_frame(enc([1, 2, 3]))],                              # not an object
        [ws_frame(enc({"type": "req", "id": "x", "cmd": "ping"}))],
        [ws_frame(enc({"type": "req", "id": True, "cmd": "ping"}))],
        [ws_frame(enc({"type": "hello", "token": "steal"}))],    # no backend hello
        [ws_frame(enc({"type": "subscribe", "events": [1] * 1000}))],
        [ws_frame(bad_json[:60000])],                                     # truncated
        [ws_frame(enc({"type": "req", "id": 1, "cmd": "ping", "args": {"x": "y" * 70000}}))],
        [os.urandom(5000)],                                               # not even WebSocket
        [ws_frame(b"\x00" * 10, opcode=9) * 1000],                        # ping flood
    ]
    for frames in cases:
        attempt(frames)
    # requests with wrong shapes get an error reply, the connection survives
    with rc(gw, "olga") as c:
        for args in ({"name": 5}, {"name": ["a"]}, {"bogus": 1}):
            with pytest.raises(RemoteError):
                c.call("read_channel", **args)
        with pytest.raises(RemoteError):
            c.call("x" * 100)
        assert c.call("ping")["pong"]
    # the gateway and the backend are fine
    with rc(gw, "vic") as c:
        assert c.call("read_channel", name="Vg") == 0.0
    assert not gw.gw.bridges or all(not b.ws.closed for b in gw.gw.bridges)


def test_rate_limit_and_in_flight_limit(gw_factory):
    gw = gw_factory(rate=50.0, burst=50.0, max_in_flight=4)
    tok = api_token(gw, "olga")

    async def go():
        async with aiohttp.ClientSession() as s:
            ws = await s.ws_connect(gw.url + "/ws", ssl=insecure(), headers={"Authorization": "Bearer " + tok})
            await ws.receive()
            n = 400
            for i in range(n):
                await ws.send_bytes(enc({"type": "req", "id": i + 1, "cmd": "ping", "args": {}}))
            got, limited, inflight = {}, 0, 0
            while len(got) < n:
                m = dec((await asyncio.wait_for(ws.receive(), 20)).data)
                if m.get("type") != "resp":
                    continue
                got[m["id"]] = m
                if not m["ok"]:
                    limited += m.get("kind") == "RateLimitError" and "rate" in m["error"]
                    inflight += "unanswered" in m["error"]
            assert set(got) == set(range(1, n + 1))          # every request is answered
            assert limited > 0 and inflight > 0
            assert not ws.closed
            await asyncio.sleep(1.5)                          # budget refills
            await ws.send_bytes(enc({"type": "req", "id": 9999, "cmd": "ping", "args": {}}))
            while True:
                m = dec((await ws.receive()).data)
                if m.get("type") == "resp":
                    assert m["id"] == 9999 and m["ok"]
                    break
            await ws.close()
    run(go())


def test_connection_limits(gw_factory):
    gw = gw_factory(max_ws_per_user=2, max_conn_per_ip=8)
    tok = api_token(gw, "vic")
    a, sa = raw_ws(gw.port, token=tok)
    b, sb = raw_ws(gw.port, token=tok)
    c, sc = raw_ws(gw.port, token=tok)
    assert (sa, sb, sc) == (101, 101, 429)
    for x in (a, b, c):
        x.close()
    socks = [socket.create_connection(("127.0.0.1", gw.port)) for _ in range(12)]
    time.sleep(0.5)
    with pytest.raises(Exception):             # the 9th+ connections from one address are cut
        for s in socks[8:]:
            insecure().wrap_socket(s, server_hostname="localhost").sendall(b"GET / HTTP/1.1\r\n\r\n")
            s.recv(10) or (_ for _ in ()).throw(ConnectionError())
    for s in socks:
        s.close()
    time.sleep(0.5)
    with rc(gw, "vic") as v:                   # a normal client still gets in
        assert v.call("ping")


def test_slowloris_connections_are_closed(gw_factory):
    gw = gw_factory(tls_handshake_timeout=1.0, first_request_timeout=1.0)
    raw = socket.create_connection(("127.0.0.1", gw.port))          # never starts TLS
    tls = insecure().wrap_socket(socket.create_connection(("127.0.0.1", gw.port)),
                                 server_hostname="localhost")       # never sends a request
    half = insecure().wrap_socket(socket.create_connection(("127.0.0.1", gw.port)),
                                  server_hostname="localhost")
    half.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")                  # never finishes headers
    t0 = time.time()
    for s in (raw, tls, half):
        s.settimeout(15)
        try:
            while s.recv(1024):
                pass
        except (ConnectionResetError, ssl.SSLError, OSError):
            pass
    assert time.time() - t0 < 12
    for s in (raw, tls, half):
        s.close()


def test_idle_connection_survives(gw):
    c = rc(gw, "vic")
    time.sleep(7)                    # several heartbeat periods (2 s in tests), no traffic
    assert not c.closed
    assert c.call("ping")["pong"]
    c.close()


def test_slow_consumer_is_bounded(gw, demo, monkeypatch):
    monkeypatch.setattr(gwmod, "SEND_STALL_S", 4.0)
    tok = api_token(gw, "vic")
    demo.monitor.add("image", 0.05)            # 2-D camera frames, 20 per second
    demo.monitor.add("spectrum", 0.05)
    s, st = raw_ws(gw.port, token=tok, rcvbuf=4096)
    assert st == 101
    s.sendall(ws_frame(enc({"type": "subscribe", "events": "*"})))
    # ... and never read again
    with rc(gw, "olga") as good:
        lat = []
        t0 = time.time()
        while time.time() - t0 < 12:
            t = time.perf_counter()
            good.call("read_channel", name="Vg")
            lat.append(time.perf_counter() - t)
            time.sleep(0.1)
            if all(b.session.user != "vic" for b in gw.gw.bridges) and time.time() - t0 > 5:
                break
        assert all(b.session.user != "vic" for b in gw.gw.bridges), "stalled client not dropped"
        assert max(lat) < 2.0, max(lat)        # the other client never noticed
    s.close()
    demo.monitor.clear()


def test_backend_restart_and_session_persistence(home, demo, gw_factory, server):
    gw = gw_factory()
    tok = api_token(gw, "olga")
    c = RemoteClient(gw.url, token=tok, fingerprint=gw.fp)
    assert c.call("ping")
    server.stop()                                  # backend goes away
    t0 = time.time()
    while not c.closed and time.time() - t0 < 10:
        time.sleep(0.1)
    assert c.closed and c.close_code in (1012, 1013)
    with pytest.raises(Exception):
        RemoteClient(gw.url, token=tok, fingerprint=gw.fp).call("ping")
    srv2 = Server(demo, port=0, runtime_file=home / "backend.json")   # new port and token
    srv2.start()
    try:
        c2 = RemoteClient(gw.url, token=tok, fingerprint=gw.fp)       # same session, no login
        assert c2.call("read_channel", name="Vg") == 0.0
        c2.close()
        # a gateway restart keeps the session (only token hashes are on disk)
        gw.stop()
        sessions = (home / "remote" / "sessions.json").read_text()
        assert tok not in sessions
        gw2 = gw_factory(port=gw.port)
        c3 = RemoteClient(gw2.url, token=tok, fingerprint=gw2.fp)
        assert c3.call("ping")
        c3.close()
    finally:
        srv2.stop()


def test_file_download(gw, demo):
    with rc(gw, "olga") as c:
        sweep = {"name": "dl", "dimensions": [{"points": 4, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}],
                                               "record": ["I"]}]}
        c.call("queue_add", item={"kind": "sweep", "definition": sweep}, start=True)
        t0 = time.time()
        while time.time() - t0 < 20:
            h = c.call("queue_state")["history"]
            if h and h[-1]["status"] != "running" and h[-1].get("path"):
                break
            time.sleep(0.2)
        path = h[-1]["path"]
        snap = c.call("load_run", path=path)
        assert snap["data"]["I"].shape == (4,)
    tok = api_token(gw, "vic")

    async def go():
        async with aiohttp.ClientSession() as s:
            auth = {"Authorization": "Bearer " + tok}
            r = await s.get(gw.url + "/api/file", params={"path": path}, ssl=insecure(), headers=auth)
            assert r.status == 200 and len(await r.read()) == os.path.getsize(path)
            assert "attachment" in r.headers["Content-Disposition"]
            for bad in ("/etc/passwd", path + "/../../../../etc/passwd", str(Path(path).parent),
                        str(Path(path).with_suffix(".json"))):
                r = await s.get(gw.url + "/api/file", params={"path": bad}, ssl=insecure(), headers=auth)
                assert r.status in (403, 404), bad
            r = await s.get(gw.url + "/api/file", params={"path": path}, ssl=insecure())
            assert r.status == 401
    run(go())


def test_gateway_process_isolation_and_supervision(demo, home):
    """Killing the gateway never affects the backend; the backend restarts it."""
    srv = Server(demo, port=0, runtime_file=home / "backend.json")
    srv.start()
    try:
        from easymeasure.remote import config as rconfig
        cfg = rconfig.load(home)
        cfg.update(host="127.0.0.1", port=_free_port())
        rconfig.save(cfg, home)
        st = demo.remote_start(persist=False)
        t0 = time.time()
        while not st["running"] and time.time() - t0 < 20:
            time.sleep(0.3)
            st = demo.remote_status()
        assert st["running"], st
        fp = st["fingerprint"]
        url = "https://127.0.0.1:%d" % st["port"]
        c = RemoteClient(url, "olga", PW["olga"], fingerprint=fp)
        demo.queue.add({"kind": "sweep", "definition": {"name": "bg", "dimensions": [
            {"points": 60, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.2}], "record": ["I"],
             "pre_delay": 0.05}]}}, start=True)
        pid = st["pid"]
        os.kill(pid, 9)                                           # hard crash of the gateway
        local = BackendClient("127.0.0.1", srv.port, srv.token)
        assert local.call("ping")                                 # backend unaffected
        t0 = time.time()
        while time.time() - t0 < 30:
            st = demo.remote_status()
            if st["running"] and st["pid"] != pid:
                break
            time.sleep(0.3)
        assert st["running"] and st["pid"] != pid                 # restarted by the backend
        c2 = RemoteClient(url, "olga", PW["olga"], fingerprint=fp)
        assert c2.call("current_run") is not None or c2.call("queue_state")
        c2.close()
        c.close()
        local.close()
        demo.queue.abort_all()
        demo.remote_stop(persist=False)
        assert not demo.remote_status()["running"]
    finally:
        srv.stop()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_performance(gw, demo, capsys):
    with rc(gw, "olga", events="*") as c:
        lat = []
        for _ in range(300):
            t = time.perf_counter()
            c.call("ping")
            lat.append((time.perf_counter() - t) * 1e3)
        lat.sort()
        p50, p99 = lat[150], lat[296]
        # a 512 x 512 float64 map (2 MB), as a running sweep snapshot would carry
        big = np.random.default_rng(0).normal(size=(512, 512))
        demo.last_dataset = None
        from easymeasure.backend.storage import DataSet
        ds = DataSet({"shape": [512, 512], "dims": [{"record": ["Z"]}, {"record": []}], "name": "perf"})
        ds.data["Z"] = big
        demo.last_dataset = ds
        t = time.perf_counter()
        for _ in range(5):
            snap = c.call("run_snapshot")
        dt = (time.perf_counter() - t) / 5
        assert np.array_equal(snap["data"]["Z"], big)
        # partially filled maps (NaN) compress very well
        half = big.copy()
        half[256:] = np.nan
        ds.data["Z"] = half
        t = time.perf_counter()
        snap = c.call("run_snapshot")
        dt_half = time.perf_counter() - t
        assert np.isnan(snap["data"]["Z"][300, 0])
    with capsys.disabled():
        print("\n  WSS ping p50 %.2f ms, p99 %.2f ms; 2 MB map %.0f ms (%.0f MB/s); half-NaN map %.0f ms"
              % (p50, p99, dt * 1e3, 2.1 / dt, dt_half * 1e3))
    assert p50 < 10 and dt < 2.0


def test_event_subscriptions_over_the_internet(gw, demo):
    with rc(gw, "vic") as c:                     # default: status events only
        got = []
        c.on_event = lambda k, d: got.append(k)
        demo.monitor.add("I", 0.1)
        with rc(gw, "olga") as o:
            o.call("queue_add", item={"kind": "sweep", "definition": {"name": "ev", "dimensions": [
                {"points": 3, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}], "record": ["I"]}]}},
                start=True)
            t0 = time.time()
            while "sweep_end" not in got and time.time() - t0 < 20:
                time.sleep(0.1)
        assert "sweep_end" in got and "monitor" not in got and "sweep_point" not in got
        c.subscribe(["monitor"])
        t0 = time.time()
        while "monitor" not in got and time.time() - t0 < 5:
            time.sleep(0.1)
        assert "monitor" in got
        demo.monitor.clear()
