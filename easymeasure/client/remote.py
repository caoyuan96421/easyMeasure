"""Blocking client for a backend reached over the internet through the gateway.

Same interface as :class:`easymeasure.client.BackendClient`::

    from easymeasure.client import RemoteClient
    c = RemoteClient("https://lab-pc.example.org:8443", "alice", getpass.getpass(),
                     fingerprint="87:68:0C:...")       # from: python -m easymeasure.remote cert
    c.call("read_channel", name="Vg")
    c.on_event = lambda kind, data: print(kind)
    c.close()

Certificate checking: give ``fingerprint`` (pins the gateway's self-signed
certificate - the usual case) or ``cafile`` (a private CA), or neither for a
certificate from a public CA.  ``verify=False`` disables checking and makes the
connection vulnerable to interception; only use it on a trusted network.

The connection is TLS-encrypted and compressed (permessage-deflate).  An idle
client stays connected indefinitely; requests are limited by the gateway's rate
limits and your account's role (``c.role``, ``c.allowed``).
"""

from __future__ import annotations

import asyncio
import itertools
import ssl
import threading
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from ..common import codec
from ..common.errors import ProtocolError, RemoteError


def _body(msg) -> bytes:
    # WebSocket messages are codec frames without the 4-byte length prefix
    return codec.encode(msg)[4:]


class RemoteClient:
    def __init__(self, url: str, user: Optional[str] = None, password: Optional[str] = None, *,
                 token: Optional[str] = None, fingerprint: Optional[str] = None,
                 cafile: Optional[str] = None, verify: bool = True, events=None,
                 timeout: float = 15.0, logout_on_close: Optional[bool] = None):
        import aiohttp
        parts = urlsplit(url if "://" in url else "https://" + url)
        if parts.scheme not in ("https", "wss"):
            raise ValueError("remote connections must use https://")
        self.base = "https://%s" % parts.netloc
        self.user = user
        self._aiohttp = aiohttp
        if fingerprint:
            digest = bytes.fromhex(fingerprint.replace(":", "").replace(" ", ""))
            self._ssl: Any = aiohttp.Fingerprint(digest)
        elif cafile:
            self._ssl = ssl.create_default_context(cafile=cafile)
        elif not verify:
            self._ssl = False
        else:
            self._ssl = ssl.create_default_context()
        self._ids = itertools.count(1)
        self._pending: Dict[int, list] = {}
        self._lock = threading.Lock()
        self.on_event: Optional[Callable[[str, Any], None]] = None
        self.closed = False
        self.welcome: Optional[dict] = None
        self._token = token
        self._logout = (token is None) if logout_on_close is None else logout_on_close
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="remote-client",
                                        daemon=True)
        self._thread.start()
        try:
            fut = asyncio.run_coroutine_threadsafe(self._connect(user, password, events, timeout),
                                                   self._loop)
            fut.result(timeout + 5)
        except BaseException:
            self._shutdown_loop()
            raise

    # -- properties ------------------------------------------------------------------
    @property
    def role(self) -> Optional[str]:
        return (self.welcome or {}).get("role")

    @property
    def allowed(self) -> list:
        return list((self.welcome or {}).get("allowed") or [])

    # -- connection ----------------------------------------------------------------
    async def _connect(self, user, password, events, timeout):
        aiohttp = self._aiohttp
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None,
                                                                            sock_connect=timeout))
        if self._token is None:
            if not user or password is None:
                raise ValueError("give user and password (or a token)")
            async with self._session.post(self.base + "/api/login", ssl=self._ssl,
                                          json={"user": user, "password": password,
                                                "token": True},
                                          timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                data = await r.json(content_type=None)
                if r.status != 200:
                    raise PermissionError("login failed: %s" % (data or {}).get("error", r.status))
                self._token = data["token"]
        self._ws = await self._session.ws_connect(
            self.base + "/ws", ssl=self._ssl, headers={"Authorization": "Bearer " + self._token},
            compress=15, max_msg_size=0, heartbeat=20.0, autoping=True)
        msg = await asyncio.wait_for(self._ws.receive(), timeout)
        if msg.type != aiohttp.WSMsgType.BINARY:
            raise ConnectionError("gateway closed the connection: %s" % (self._ws.close_code,))
        w = codec.decode_body(msg.data)
        if not isinstance(w, dict) or w.get("type") != "welcome":
            raise ProtocolError("unexpected first message from the gateway")
        self.welcome = w
        if events is None:
            from ..backend.server import DEFAULT_EVENTS
            events = list(DEFAULT_EVENTS)
        await self._ws.send_bytes(_body(
            {"type": "subscribe", "events": events if events == "*" else list(events)}))
        self._rx = asyncio.ensure_future(self._read_loop())

    async def _read_loop(self):
        aiohttp = self._aiohttp
        T = aiohttp.WSMsgType
        self.close_code = self.close_reason = None
        try:
            while True:
                m = await self._ws.receive()
                if m.type == T.CLOSE:
                    # the code in the CLOSE frame is the server's reason (aiohttp may later
                    # report 1006 if its reply races the server's TCP close)
                    self.close_code, self.close_reason = m.data, m.extra
                    break
                if m.type in (T.CLOSED, T.CLOSING, T.ERROR):
                    break
                if m.type != T.BINARY:
                    continue
                try:
                    msg = codec.decode_body(m.data)
                except ProtocolError:
                    continue
                t = msg.get("type") if isinstance(msg, dict) else None
                if t == "resp":
                    with self._lock:
                        slot = self._pending.pop(msg.get("id"), None)
                    if slot is not None:
                        slot[1] = msg
                        slot[0].set()
                elif t == "event" and self.on_event is not None:
                    try:
                        self.on_event(msg.get("event"), msg.get("data"))
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            self.closed = True
            if self.close_code is None:
                self.close_code = self._ws.close_code
            with self._lock:
                pend = list(self._pending.values())
                self._pending.clear()
            for slot in pend:
                slot[1] = {"ok": False, "error": "connection closed (%s%s)" % (
                    self.close_code, ": %s" % self.close_reason if self.close_reason else ""),
                           "kind": "ConnectionError"}
                slot[0].set()

    def _run(self, coro, timeout: float = 30.0):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    # -- API -----------------------------------------------------------------------------
    def subscribe(self, events) -> None:
        """``"*"`` for every event or a list of names (default: status events only)."""
        self._run(self._ws.send_bytes(_body(
            {"type": "subscribe", "events": events if events == "*" else list(events)})))

    def call(self, cmd: str, _timeout: float = 120.0, **args) -> Any:
        if self.closed:
            raise ConnectionError("connection to the gateway closed")
        rid = next(self._ids)
        slot = [threading.Event(), None]
        with self._lock:
            self._pending[rid] = slot
        try:
            self._run(self._ws.send_bytes(_body({"type": "req", "id": rid, "cmd": cmd,
                                                 "args": args})), 30.0)
        except Exception as exc:
            with self._lock:
                self._pending.pop(rid, None)
            raise ConnectionError("cannot send to the gateway: %s" % exc) from None
        if not slot[0].wait(_timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise TimeoutError("no answer to %s within %.0fs" % (cmd, _timeout))
        resp = slot[1]
        if not resp.get("ok"):
            raise RemoteError(resp.get("error", "error"), resp.get("kind", "RemoteError"),
                              resp.get("tb"))
        return resp.get("result")

    def ping_ms(self) -> float:
        t = time.perf_counter()
        self.call("ping")
        return (time.perf_counter() - t) * 1e3

    async def _aclose(self):
        try:
            if not self._ws.closed:
                await self._ws.close()
        except Exception:
            pass
        if self._logout and self._token:
            try:
                async with self._session.post(self.base + "/api/logout", ssl=self._ssl,
                                              headers={"Authorization": "Bearer " + self._token},
                                              timeout=self._aiohttp.ClientTimeout(total=5)):
                    pass
            except Exception:
                pass
        await self._session.close()

    def _shutdown_loop(self):
        try:
            if hasattr(self, "_session"):
                self._run(self._session.close(), 5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)

    def close(self) -> None:
        if getattr(self, "_closed_done", False):
            return
        self._closed_done = True
        try:
            self._run(self._aclose(), 10)
        except Exception:
            pass
        self.closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
