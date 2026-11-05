"""Internet-facing gateway: HTTPS + secure WebSocket in front of the backend.

::

    browser / RemoteClient ──TLS 1.2+ (WSS, permessage-deflate)──▶ gateway ──localhost TCP──▶ backend

The gateway runs in its own process.  Nothing that arrives from the network is
parsed inside the backend process before the gateway has authenticated the user,
validated the message and checked the user's role, and a crash, hang or memory
blow-up caused by a hostile peer can only take down the gateway (which the
backend restarts), never the instrument backend or a running measurement.

Each live remote connection gets its own backend connection, so the backend's
per-client machinery (event subscriptions, droppable events for slow clients,
stall detection) works unchanged.  Frames from the backend are forwarded
byte-for-byte (no re-encoding); WebSocket flow control propagates back-pressure
from a slow browser to the backend, which then drops monitor/sweep-point events
for that client instead of buffering them.

Security measures (see :mod:`easymeasure.remote.policy` for the roles):

* TLS only, 1.2+ with forward-secret AEAD ciphers; no plaintext port.
* Accounts with scrypt-hashed passwords; login brute-force lock-out per address
  and per account; generic error messages; constant-time checks.
* Sessions: 256-bit random tokens (only their SHA-256 kept in memory), cookie
  ``__Host-em`` (Secure, HttpOnly, SameSite=Strict) or ``Authorization: Bearer``
  for scripts; idle and absolute expiry; revoked when the account changes.
* Cross-site WebSocket hijacking blocked by an Origin check; strict CSP and
  other security headers; only files of the bundled web app are served.
* Limits: connections per address, sessions per user, live connections, message
  size, request rate, unanswered requests, TLS-handshake and first-request
  timeouts.  Idle connections are never dropped (heartbeats only detect dead
  peers).
* Audit log ``<home>/logs/remote.log``: logins, lock-outs, denied commands and
  every command that changes something.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import logging.handlers
import os
import random
import secrets
import struct
import time
from pathlib import Path
from typing import Dict, Optional, Set

from aiohttp import WSMsgType, web

from .. import CODE_STAMP, __version__
from ..common import codec, paths
from ..common.errors import ProtocolError
from . import config as rconfig
from . import policy
from .throttle import LoginThrottle, TokenBucket
from .tlsutil import cert_info, ensure_certificate, server_context
from .users import UserStore

COOKIE = "__Host-em"
_HDR = struct.Struct("<I")
_RESP_PREFIX = b'{"m":{"type":"resp","id":'
WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_LOGIN_BODY = 4096
#: a client that accepts no data for this long is disconnected
SEND_STALL_S = 90.0
_CT = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
       ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
       ".ico": "image/x-icon", ".json": "application/json", ".woff2": "font/woff2"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")
#: commands worth an audit-log line even though they change nothing
_AUDIT_READS = {"load_run", "read_run_header", "save_setup", "get_setup"}

log = logging.getLogger("easymeasure.remote")


def _setup_logging(home: Path, console: bool) -> None:
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    d = home / "logs"
    d.mkdir(parents=True, exist_ok=True)
    target = str(d / "remote.log")
    for h in list(log.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler) and h.baseFilename != target:
            log.removeHandler(h)
            h.close()
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in log.handlers):
        fh = logging.handlers.RotatingFileHandler(target, maxBytes=5_000_000,
                                                  backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    if console and not any(type(h) is logging.StreamHandler for h in log.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)


def _summary(args, limit: int = 160) -> str:
    try:
        s = json.dumps(codec.to_jsonable(args), separators=(",", ":"), default=str)
    except Exception:
        s = repr(args)
    return s if len(s) <= limit else s[:limit] + "…"


# ---------------------------------------------------------------------------
class Session:
    __slots__ = ("key", "user", "role", "generation", "ip", "created", "last_seen", "kind",
                 "bridges")

    def __init__(self, key, user, role, generation, ip, kind):
        self.key, self.user, self.role, self.generation = key, user, role, generation
        self.ip, self.kind = ip, kind
        self.created = self.last_seen = time.time()
        self.bridges: Set["Bridge"] = set()


class _GuardProto(asyncio.Protocol):
    """Wraps aiohttp's protocol to enforce per-address connection limits and a
    first-request deadline (slow-loris protection)."""

    def __init__(self, inner, gw: "Gateway"):
        self._inner, self._gw = inner, gw
        self._ip = None

    def connection_made(self, transport):
        peer = transport.get_extra_info("peername")
        self._ip = peer[0] if peer else "?"
        self._inner.connection_made(transport)
        if not self._gw._conn_open(self._ip, transport):
            transport.abort()

    def data_received(self, data):
        self._inner.data_received(data)

    def eof_received(self):
        return self._inner.eof_received()

    def connection_lost(self, exc):
        self._gw._conn_closed(self._ip)
        self._inner.connection_lost(exc)

    def pause_writing(self):
        self._inner.pause_writing()

    def resume_writing(self):
        self._inner.resume_writing()


# ---------------------------------------------------------------------------
class Bridge:
    """One remote WebSocket <-> one backend connection."""

    def __init__(self, gw: "Gateway", ws: web.WebSocketResponse, session: Session, ip: str,
                 request: web.Request):
        self.gw, self.ws, self.session, self.ip = gw, ws, session, ip
        self.request = request
        cfg = gw.cfg
        self.bucket = TokenBucket(cfg["rate"], cfg["burst"])
        self.in_flight: Set[int] = set()
        self.rejected = 0
        self.sent_frames = 0
        self.sent_bytes = 0
        self.recv_msgs = 0
        self._send_lock = asyncio.Lock()
        self._writer: Optional[asyncio.StreamWriter] = None
        self._close_code, self._close_reason = 1000, ""
        self.started = time.time()
        self.name = "web:%s@%s" % (session.user, ip)

    # -- helpers -----------------------------------------------------------------
    async def _send(self, data: bytes) -> None:
        async with self._send_lock:
            try:
                await asyncio.wait_for(self.ws.send_bytes(data), SEND_STALL_S)
            except asyncio.TimeoutError:
                # the peer has accepted nothing for minutes (frozen or malicious)
                log.warning("%s: not reading for %.0f s, dropping the connection",
                            self.name, SEND_STALL_S)
                self.abort()
                raise ConnectionError("client stalled") from None
        self.sent_frames += 1
        self.sent_bytes += len(data)

    async def _reply(self, rid, error: str, kind: str = "PermissionError") -> None:
        await self._send(codec.encode({"type": "resp", "id": rid, "ok": False, "error": error,
                                       "kind": kind}))

    def abort(self) -> None:
        tr = self.request.transport
        if tr is not None:
            tr.abort()
        if self._writer is not None:
            self._writer.close()

    def kick(self, code: int, reason: str) -> None:
        self._close_code, self._close_reason = code, reason
        asyncio.ensure_future(self.ws.close(code=code, message=reason.encode()[:120]))
        if self._writer is not None:
            self._writer.close()

    # -- main --------------------------------------------------------------------
    async def run(self) -> None:
        gw, s = self.gw, self.session
        info = gw.backend_info()
        if not info:
            await self.ws.close(code=1013, message=b"backend not running")
            return
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(info.get("host", "127.0.0.1"), int(info["port"]),
                                        limit=1 << 20), 5.0)
        except Exception:
            await self.ws.close(code=1013, message=b"backend not reachable")
            return
        self._writer = writer
        try:
            writer.write(codec.encode({"type": "hello", "token": info["token"],
                                       "client": self.name,
                                       "events": list(gw.initial_events)}))
            await writer.drain()
            frame = await asyncio.wait_for(self._read_frame(reader), 10.0)
            welcome = codec.decode(frame)
            if not isinstance(welcome, dict) or welcome.get("type") != "welcome":
                raise ProtocolError("backend refused the gateway")
            await self._send(codec.encode({
                "type": "welcome", "gateway": __version__, "version": welcome.get("version"),
                "code_stamp": welcome.get("code_stamp"), "user": s.user, "role": s.role,
                "allowed": policy.allowed_commands(s.role),
                "session_expires": s.created + gw.cfg["session_max_days"] * 86400,
                "limits": {"max_msg_bytes": gw.cfg["max_msg_bytes"], "rate": gw.cfg["rate"],
                           "max_in_flight": gw.cfg["max_in_flight"]},
                "server_time": time.time()}))
        except Exception as exc:
            (log.info if self.ws.closed else log.warning)(
                "%s: backend handshake failed: %s", self.name, exc)
            writer.close()
            await self.ws.close(code=1011, message=b"backend handshake failed")
            return
        log.info("connect %s (%s, %s)", self.name, s.role, s.kind)
        up = asyncio.ensure_future(self._from_backend(reader))
        down = asyncio.ensure_future(self._from_client(writer))
        try:
            done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc is not None and not isinstance(exc, (asyncio.CancelledError,
                                                           ConnectionError)):
                    log.warning("%s: %s: %s", self.name, type(exc).__name__, exc)
            if up in done and not self.ws.closed:
                # the backend went away (restart / shutdown): tell the client to retry
                await self.ws.close(code=1012, message=b"backend restarting")
        finally:
            try:
                writer.close()
            except Exception:
                pass
            if not self.ws.closed:
                await self.ws.close(code=self._close_code, message=self._close_reason.encode()[:120])
            log.info("disconnect %s after %.0f s (%d msgs in, %d frames / %.1f MB out, %d rejected)",
                     self.name, time.time() - self.started, self.recv_msgs, self.sent_frames,
                     self.sent_bytes / 1e6, self.rejected)

    @staticmethod
    async def _read_frame(reader: asyncio.StreamReader) -> bytes:
        hdr = await reader.readexactly(4)
        (n,) = _HDR.unpack(hdr)
        if n < 4 or n > codec.MAX_FRAME:
            raise ProtocolError("bad frame from backend")
        return hdr + await reader.readexactly(n)

    async def _from_backend(self, reader: asyncio.StreamReader) -> None:
        while True:
            frame = await self._read_frame(reader)
            if self.in_flight and frame[8:8 + len(_RESP_PREFIX)] == _RESP_PREFIX:
                try:
                    (jlen,) = _HDR.unpack_from(frame, 4)
                    rid = json.loads(frame[8:8 + jlen])["m"].get("id")
                    self.in_flight.discard(rid)
                except Exception:
                    pass
            await self._send(frame)       # flow control: waits while the client is slow

    async def _from_client(self, writer: asyncio.StreamWriter) -> None:
        cfg = self.gw.cfg
        async for msg in self.ws:
            if msg.type == WSMsgType.BINARY:
                self.recv_msgs += 1
                self.session.last_seen = time.time()
                await self._handle(msg.data, writer, cfg)
            elif msg.type == WSMsgType.TEXT:
                self._close_code, self._close_reason = 1003, "binary frames only"
                return
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                return
            elif msg.type == WSMsgType.ERROR:
                exc = self.ws.exception()
                if exc is not None and "too big" in str(exc).lower():
                    log.warning("%s: message too large", self.name)
                return

    async def _handle(self, data: bytes, writer: asyncio.StreamWriter, cfg: dict) -> None:
        try:
            msg = codec.decode(data)
        except ProtocolError as exc:
            log.warning("%s: malformed message: %s", self.name, exc)
            self._close_code, self._close_reason = 1007, "malformed message"
            raise ConnectionError("malformed message")
        if not isinstance(msg, dict):
            self._close_code, self._close_reason = 1007, "message must be an object"
            raise ConnectionError("bad message")
        t = msg.get("type")
        if t == "req":
            rid = msg.get("id")
            if not isinstance(rid, int) or isinstance(rid, bool) or not 0 <= rid < 2 ** 53:
                self._close_code, self._close_reason = 1007, "request id must be an integer"
                raise ConnectionError("bad id")
            cmd, args = msg.get("cmd"), msg.get("args")
            if args is None:
                args = {}
            if not isinstance(cmd, str) or len(cmd) > 64 or not isinstance(args, dict):
                await self._reply(rid, "malformed request", "ProtocolError")
                return
            if not self.bucket.take():
                self.rejected += 1
                if self.rejected % 500 == 1:
                    log.warning("%s: rate limit exceeded (%d rejected)", self.name, self.rejected)
                await self._reply(rid, "rate limit exceeded; slow down", "RateLimitError")
                return
            if len(self.in_flight) >= cfg["max_in_flight"] or rid in self.in_flight:
                self.rejected += 1
                await self._reply(rid, "too many unanswered requests" if rid not in self.in_flight
                                  else "duplicate request id", "RateLimitError")
                return
            try:
                args = policy.check(self.session.role, cmd, args, self.gw.data_roots)
            except policy.Denied as exc:
                log.warning("denied %s %s %s: %s", self.name, cmd, _summary(args), exc)
                await self._reply(rid, "permission denied: %s" % exc)
                return
            level = policy.required_level(cmd)
            if level and level >= 1 or cmd in _AUDIT_READS:
                log.info("cmd %s %s %s", self.name, cmd, _summary(args))
            try:
                frame = codec.encode({"type": "req", "id": rid, "cmd": cmd, "args": args})
            except ProtocolError as exc:
                await self._reply(rid, str(exc), "ProtocolError")
                return
            self.in_flight.add(rid)
            writer.write(frame)
            await writer.drain()
        elif t == "subscribe":
            ev = msg.get("events")
            if ev != "*" and not (isinstance(ev, list) and len(ev) <= 100 and
                                  all(isinstance(e, str) and len(e) <= 64 for e in ev)):
                self._close_code, self._close_reason = 1007, "bad subscription"
                raise ConnectionError("bad subscription")
            writer.write(codec.encode({"type": "subscribe", "events": ev}))
            await writer.drain()
        elif t == "bye":
            self._close_code, self._close_reason = 1000, "bye"
            raise ConnectionError("bye")
        else:
            self._close_code, self._close_reason = 1007, "unknown message type"
            raise ConnectionError("unknown message type")


# ---------------------------------------------------------------------------
class Gateway:
    def __init__(self, home: Optional[Path] = None, cfg: Optional[dict] = None,
                 runtime_file: Optional[Path] = None, console: bool = True):
        self.home = Path(home) if home else paths.home_dir()
        self.cfg = rconfig.load(self.home, cfg)
        self.runtime_file = Path(runtime_file) if runtime_file else self.home / "backend.json"
        _setup_logging(self.home, console)
        self.users = UserStore(rconfig.users_path(self.home))
        self.throttle = LoginThrottle(self.cfg["login_max_failures_ip"],
                                      self.cfg["login_max_failures_user"],
                                      self.cfg["lockout_s"], self.cfg["lockout_max_s"])
        self.sessions: Dict[str, Session] = {}
        self.bridges: Set[Bridge] = set()
        self.data_roots: list = []
        self.settings: dict = {}
        self.initial_events = ("queue", "changed", "settings")
        self._ip_conns: Dict[str, int] = collections.Counter()
        self._fresh: Dict[object, float] = {}
        self._static: Dict[str, tuple] = {}
        self._tasks: list = []
        self._server = None
        self._runner = None
        self.port = None
        self.cert = self.key = None
        self.self_signed = False
        self.started = time.time()
        self.login_sem: Optional[asyncio.Semaphore] = None
        self._sessions_dirty = False
        self._sessions_saved = 0.0
        self._load_sessions()

    # -- backend discovery -----------------------------------------------------
    def backend_info(self) -> Optional[dict]:
        try:
            info = json.loads(self.runtime_file.read_text())
            if "port" in info and "token" in info:
                return info
        except Exception:
            pass
        return None

    async def _control_link(self) -> None:
        """Keep the data directory (for path checks) and settings up to date."""
        while True:
            info = self.backend_info()
            writer = None
            try:
                if not info:
                    raise ConnectionError("no backend")
                reader, writer = await asyncio.wait_for(asyncio.open_connection(
                    info.get("host", "127.0.0.1"), int(info["port"]), limit=1 << 20), 5.0)
                writer.write(codec.encode({"type": "hello", "token": info["token"],
                                           "client": "gateway", "events": ["settings"]}))
                writer.write(codec.encode({"type": "req", "id": 1, "cmd": "get_settings",
                                           "args": {}}))
                await writer.drain()
                while True:
                    msg = codec.decode(await Bridge._read_frame(reader))
                    st = None
                    if msg.get("type") == "resp" and msg.get("ok"):
                        st = msg.get("result")
                    elif msg.get("type") == "event" and msg.get("event") == "settings":
                        st = msg.get("data")
                    if isinstance(st, dict):
                        self.settings = st
                        self.data_roots = [r for r in [st.get("data_dir")] if r]
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                if writer is not None:
                    writer.close()
            await asyncio.sleep(2.0)

    # -- connection guard --------------------------------------------------------
    def _conn_open(self, ip, transport) -> bool:
        self._ip_conns[ip] += 1
        if self._ip_conns[ip] > self.cfg["max_conn_per_ip"]:
            log.warning("too many connections from %s", ip)
            return False
        self._fresh[transport] = time.monotonic()
        return True

    def _conn_closed(self, ip) -> None:
        n = self._ip_conns.get(ip, 0) - 1
        if n <= 0:
            self._ip_conns.pop(ip, None)
        else:
            self._ip_conns[ip] = n

    # -- HTTP ----------------------------------------------------------------------
    def _client_ip(self, request: web.Request) -> str:
        if self.cfg["trust_proxy"]:
            xff = request.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[-1].strip()[:64]
        return request.remote or "?"

    def _same_origin(self, request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        if origin is None:
            return True
        if origin in self.cfg["allowed_origins"]:
            return True
        return origin == "https://%s" % request.host

    @web.middleware
    async def _mw(self, request: web.Request, handler):
        self._fresh.pop(request.transport, None)
        try:
            resp = await handler(request)
        except web.HTTPException as exc:
            resp = exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:          # never leak internals
            log.error("error serving %s: %s: %s", request.path, type(exc).__name__, exc)
            resp = web.json_response({"error": "internal error"}, status=500)
        h = resp.headers
        h["Content-Security-Policy"] = CSP
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "no-referrer"
        h["Strict-Transport-Security"] = "max-age=31536000"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Cross-Origin-Resource-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), usb=(), serial=()"
        if request.path.startswith("/api/") or request.path == "/":
            h["Cache-Control"] = "no-store"
        return resp

    @staticmethod
    async def _on_prepare(request, response) -> None:
        response.headers["Server"] = "easyMeasure"     # no software/version disclosure

    def _load_static(self) -> None:
        import gzip
        self._static.clear()
        for root, _, files in os.walk(WEB_DIR):
            for f in files:
                p = Path(root) / f
                rel = p.relative_to(WEB_DIR).as_posix()
                ct = _CT.get(p.suffix.lower())
                if ct is None:
                    continue
                data = p.read_bytes()
                gz = gzip.compress(data, 9) if ct.startswith(("text/", "image/svg", "application/json")) else None
                etag = '"%s"' % hashlib.sha256(data).hexdigest()[:20]
                self._static[rel] = (data, gz, ct, etag)

    async def _serve_static(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info.get("name") or "index.html"
        ent = self._static.get(name)
        if ent is None:
            raise web.HTTPNotFound()
        data, gz, ct, etag = ent
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag})
        headers = {"Content-Type": ct, "ETag": etag,
                   "Cache-Control": "no-cache" if name == "index.html" else "max-age=300"}
        if gz is not None and "gzip" in request.headers.get("Accept-Encoding", ""):
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
            return web.Response(body=gz, headers=headers)
        return web.Response(body=data, headers=headers)

    # -- sessions ------------------------------------------------------------------
    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()

    def _session_for(self, request: web.Request, need_origin: bool) -> Optional[Session]:
        token, via_cookie = None, False
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
        else:
            token = request.cookies.get(COOKIE)
            via_cookie = True
        if not token or len(token) > 200:
            return None
        if (via_cookie or need_origin) and not self._same_origin(request):
            return None
        if via_cookie and need_origin and request.headers.get("Origin") is None:
            return None                     # browsers always send Origin for WebSockets
        s = self.sessions.get(self._key(token))
        if s is None or not self._valid(s):
            return None
        return s

    def _valid(self, s: Session, now: Optional[float] = None) -> bool:
        now = now or time.time()
        if now - s.created > self.cfg["session_max_days"] * 86400:
            return False
        if not s.bridges and now - s.last_seen > self.cfg["session_idle_hours"] * 3600:
            return False
        return self.users.generation(s.user) == s.generation

    def _drop_session(self, s: Session, code: int, reason: str) -> None:
        self.sessions.pop(s.key, None)
        self._sessions_dirty = True
        for b in list(s.bridges):
            b.kick(code, reason)

    # Sessions survive gateway restarts (e.g. when the backend is restarted after a
    # code update).  Only SHA-256 hashes of the tokens are stored, owner-only.
    def _save_sessions(self) -> None:
        p = rconfig.remote_dir(self.home) / "sessions.json"
        data = [{"key": x.key, "user": x.user, "role": x.role, "generation": x.generation,
                 "ip": x.ip, "created": x.created, "last_seen": x.last_seen, "kind": x.kind}
                for x in self.sessions.values()]
        tmp = p.with_suffix(".tmp")
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, p)
            self._sessions_dirty = False
            self._sessions_saved = time.time()
        except OSError as exc:
            log.warning("cannot save sessions: %s", exc)

    def _load_sessions(self) -> None:
        p = rconfig.remote_dir(self.home) / "sessions.json"
        try:
            data = json.loads(p.read_text())
        except Exception:
            return
        now = time.time()
        for d in data if isinstance(data, list) else []:
            try:
                x = Session(str(d["key"]), str(d["user"]), str(d["role"]), d["generation"],
                            str(d["ip"]), str(d["kind"]))
                x.created, x.last_seen = float(d["created"]), float(d["last_seen"])
            except Exception:
                continue
            if len(x.key) == 64 and self._valid(x, now):
                self.sessions[x.key] = x

    async def _sweeper(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            try:
                now = time.time()
                self.users.reload()
                for s in list(self.sessions.values()):
                    if s.bridges:
                        s.last_seen = now
                    if not self._valid(s, now):
                        changed = self.users.generation(s.user) != s.generation
                        log.info("session of %s from %s ended (%s)", s.user, s.ip,
                                 "account changed or removed" if changed else "expired")
                        self._drop_session(s, 4403 if changed else 4401,
                                           "account changed" if changed else "session expired")
                limit = self.cfg["first_request_timeout"]
                tnow = time.monotonic()
                for tr, t0 in list(self._fresh.items()):
                    if tnow - t0 > limit:
                        self._fresh.pop(tr, None)
                        try:
                            tr.abort()
                        except Exception:
                            pass
                self._write_status()
                if self._sessions_dirty or time.time() - self._sessions_saved > 60:
                    self._save_sessions()
            except Exception as exc:
                log.error("sweeper: %s", exc)

    # -- API -------------------------------------------------------------------------
    async def _login(self, request: web.Request) -> web.Response:
        ip = self._client_ip(request)
        if not self._same_origin(request):
            return web.json_response({"error": "cross-origin request refused"}, status=403)
        if request.content_type != "application/json":
            return web.json_response({"error": "JSON body required"}, status=415)
        if (request.content_length or 0) > MAX_LOGIN_BODY:
            return web.json_response({"error": "request too large"}, status=413)
        try:
            body = await request.content.read(MAX_LOGIN_BODY + 1)
            if len(body) > MAX_LOGIN_BODY:
                raise ValueError
            data = json.loads(body)
            user = data.get("user")
            pw = data.get("password")
            if not isinstance(user, str) or not isinstance(pw, str) or len(user) > 64:
                raise ValueError
        except Exception:
            return web.json_response({"error": "user and password required"}, status=400)
        wait = self.throttle.blocked(ip, user)
        if wait > 0:
            log.warning("login refused for %r from %s: locked out for %.0f s", user, ip, wait)
            return web.json_response({"error": "too many failed attempts; try again in %d s"
                                      % (int(wait) + 1)}, status=429,
                                     headers={"Retry-After": str(int(wait) + 1)})
        async with self.login_sem:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self.users.verify, user, pw)
        if res is None:
            self.throttle.failure(ip, user)
            log.warning("login failed for %r from %s", user, ip)
            await asyncio.sleep(random.uniform(0.2, 0.5))
            return web.json_response({"error": "invalid user name or password"}, status=401)
        self.throttle.success(ip, user)
        token = secrets.token_urlsafe(32)
        kind = "api" if data.get("token") else "browser"
        s = Session(self._key(token), user, res["role"], res["generation"], ip, kind)
        mine = sorted((x for x in self.sessions.values() if x.user == user),
                      key=lambda x: x.last_seen)
        while len(mine) >= self.cfg["max_sessions_per_user"]:
            self._drop_session(mine.pop(0), 4401, "too many sessions")
        self.sessions[s.key] = s
        self._save_sessions()
        log.info("login %s (%s) from %s [%s]", user, res["role"], ip, kind)
        out = {"user": user, "role": res["role"],
               "expires": s.created + self.cfg["session_max_days"] * 86400}
        if kind == "api":
            out["token"] = token
        resp = web.json_response(out)
        if kind == "browser":
            resp.set_cookie(COOKIE, token, max_age=int(self.cfg["session_max_days"] * 86400),
                            path="/", secure=True, httponly=True, samesite="Strict")
        return resp

    async def _logout(self, request: web.Request) -> web.Response:
        if not self._same_origin(request):
            return web.json_response({"error": "cross-origin request refused"}, status=403)
        s = self._session_for(request, need_origin=False)
        if s is not None:
            log.info("logout %s from %s", s.user, self._client_ip(request))
            self._drop_session(s, 4401, "logged out")
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="Strict")
        return resp

    async def _whoami(self, request: web.Request) -> web.Response:
        s = self._session_for(request, need_origin=False)
        if s is None:
            return web.json_response({"user": None})
        return web.json_response({"user": s.user, "role": s.role,
                                  "expires": s.created + self.cfg["session_max_days"] * 86400,
                                  "allowed": policy.allowed_commands(s.role)})

    def _download(self, path: Optional[str], allowed_suffixes, roots) -> web.StreamResponse:
        if not path or not isinstance(path, str):
            raise web.HTTPBadRequest()
        if Path(path).suffix.lower() not in allowed_suffixes or not policy._inside(path, roots):
            raise web.HTTPForbidden()
        p = Path(os.path.realpath(path))
        if not p.is_file():
            raise web.HTTPNotFound()
        resp = web.FileResponse(p, chunk_size=256 * 1024)
        resp.headers["Content-Disposition"] = 'attachment; filename="%s"' % \
            p.name.replace('"', "").replace("\r", "").replace("\n", "")
        resp.headers["Content-Type"] = "application/octet-stream"
        return resp

    async def _file(self, request: web.Request) -> web.StreamResponse:
        s = self._session_for(request, need_origin=False)
        if s is None:
            raise web.HTTPUnauthorized()
        path = request.query.get("path")
        log.info("download %s by %s from %s", path, s.user, self._client_ip(request))
        return self._download(path, (".emj", ".h5", ".hdf5"), self.data_roots)

    async def _ppt(self, request: web.Request) -> web.StreamResponse:
        s = self._session_for(request, need_origin=False)
        if s is None:
            raise web.HTTPUnauthorized()
        ppt = self.settings.get("ppt_path") or ""
        if not ppt:
            raise web.HTTPNotFound()
        log.info("download %s by %s from %s", ppt, s.user, self._client_ip(request))
        return self._download(ppt, (".pptx",), [os.path.dirname(os.path.realpath(ppt))])

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        ip = self._client_ip(request)
        s = self._session_for(request, need_origin=True)
        if s is None:
            log.warning("websocket refused from %s (no valid session or wrong origin)", ip)
            raise web.HTTPUnauthorized()
        if len(self.bridges) >= self.cfg["max_ws_total"]:
            raise web.HTTPServiceUnavailable(text="too many connections")
        if sum(len(x.bridges) for x in self.sessions.values() if x.user == s.user) \
                >= self.cfg["max_ws_per_user"]:
            raise web.HTTPTooManyRequests(text="too many connections for this user")
        ws = web.WebSocketResponse(heartbeat=self.cfg["heartbeat_s"], compress=True,
                                   max_msg_size=int(self.cfg["max_msg_bytes"]), autoping=True)
        await ws.prepare(request)
        b = Bridge(self, ws, s, ip, request)
        self.bridges.add(b)
        s.bridges.add(b)
        try:
            await b.run()
        finally:
            self.bridges.discard(b)
            s.bridges.discard(b)
            s.last_seen = time.time()
        return ws

    # -- lifecycle -----------------------------------------------------------------
    def _write_status(self) -> None:
        st = {"pid": os.getpid(), "port": self.port, "host": self.cfg["host"],
              "started": self.started, "self_signed": self.self_signed,
              "fingerprint": getattr(self, "fingerprint", None),
              "connections": [{"user": b.session.user, "role": b.session.role, "ip": b.ip,
                               "since": b.started} for b in list(self.bridges)],
              "sessions": len(self.sessions), "users": len(self.users), "t": time.time()}
        p = rconfig.status_path(self.home)
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(st))
            os.replace(tmp, p)
        except OSError:
            pass

    async def start(self) -> None:
        self.login_sem = asyncio.Semaphore(4)
        self.cert, self.key, self.self_signed = ensure_certificate(
            rconfig.tls_dir(self.home), self.cfg.get("certfile"), self.cfg.get("keyfile"),
            self.cfg.get("cert_names") or ())
        ctx = server_context(self.cert, self.key)
        self.fingerprint = cert_info(self.cert)["fingerprint"]
        self._load_static()
        app = web.Application(middlewares=[self._mw], client_max_size=64 * 1024)
        app.on_response_prepare.append(self._on_prepare)
        app.router.add_get("/", self._serve_static)
        app.router.add_get("/assets/{name:[A-Za-z0-9_\\-]+(/[A-Za-z0-9_\\-]+)*\\.[a-z0-9]+}",
                           self._serve_static_asset)
        app.router.add_post("/api/login", self._login)
        app.router.add_post("/api/logout", self._logout)
        app.router.add_get("/api/session", self._whoami)
        app.router.add_get("/api/file", self._file)
        app.router.add_get("/api/ppt", self._ppt)
        app.router.add_get("/ws", self._ws)
        self._runner = web.AppRunner(app, access_log=None, keepalive_timeout=20,
                                     handle_signals=False, max_line_size=8190,
                                     max_field_size=8190)
        await self._runner.setup()
        handler = self._runner.server
        loop = asyncio.get_running_loop()
        self._server = await loop.create_server(
            lambda: _GuardProto(handler(), self), self.cfg["host"] or None, int(self.cfg["port"]),
            ssl=ctx, ssl_handshake_timeout=float(self.cfg["tls_handshake_timeout"]),
            backlog=256, reuse_address=True)
        self.port = self._server.sockets[0].getsockname()[1]
        self._tasks = [asyncio.ensure_future(self._control_link()),
                       asyncio.ensure_future(self._sweeper())]
        self._write_status()
        log.info("gateway listening on https://%s:%d (%s certificate, SHA-256 %s); %d user(s)",
                 self.cfg["host"], self.port, "self-signed" if self.self_signed else "configured",
                 self.fingerprint, len(self.users))
        if not len(self.users):
            log.warning("no user accounts yet: add one with "
                        "'python -m easymeasure.remote adduser NAME --role admin'")

    async def _serve_static_asset(self, request: web.Request) -> web.StreamResponse:
        return await self._serve_static(request)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._server is not None:
            self._server.close()
        for b in list(self.bridges):
            b.kick(1001, "gateway stopping")
        await asyncio.sleep(0.2)
        if self._runner is not None:
            await self._runner.cleanup()
        self._save_sessions()
        try:
            rconfig.status_path(self.home).unlink()
        except OSError:
            pass
        log.info("gateway stopped")


def run(home: Optional[Path] = None, cfg: Optional[dict] = None, console: bool = True,
        stop_event=None) -> None:
    """Run a gateway until interrupted (or until ``stop_event`` is set)."""
    async def main():
        gw = Gateway(home, cfg, console=console)
        await gw.start()
        try:
            if stop_event is None:
                await asyncio.Event().wait()
            else:
                while not stop_event.is_set():
                    await asyncio.sleep(0.2)
        finally:
            await gw.stop()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
