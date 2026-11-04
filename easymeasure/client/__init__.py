"""Thread-based client for the backend (scripts, notebooks, tests).

    from easymeasure.client import BackendClient
    c = BackendClient.connect()            # uses ~/.easymeasure/backend.json
    c.call("read_channel", name="Vg")
    c.on_event = lambda kind, data: print(kind)   # status events (queue, sweep start/end ...)
    c.subscribe(["monitor"])                      # ... or ask for more, "*" for everything

An idle client is never disconnected.  A client is only dropped when the backend
has data waiting for it and its socket has accepted nothing for 60 s (a frozen
GUI); the reader thread here always drains the socket, so that cannot happen.

The GUI uses its own Qt-native, non-blocking client
(:mod:`easymeasure.frontend.connection`); this one blocks the calling thread.
"""

from __future__ import annotations

import itertools
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ..common import codec, paths
from ..common.errors import ProtocolError, RemoteError


class BackendClient:
    def __init__(self, host: str, port: int, token: str, name: str = "client",
                 timeout: float = 5.0, events=None):
        self.host, self.port, self.token = host, int(port), token
        self.sock = socket.create_connection((host, self.port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(None)
        self._ids = itertools.count(1)
        self._pending: Dict[int, list] = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self.on_event: Optional[Callable[[str, Any], None]] = None
        self.closed = False
        self._welcome = threading.Event()
        self.welcome: Optional[dict] = None
        self._rx = threading.Thread(target=self._read_loop, name="client-rx", daemon=True)
        self._rx.start()
        hello = {"type": "hello", "token": token, "client": name}
        if events is not None:
            hello["events"] = events if events == "*" else list(events)
        self._send(hello)
        if not self._welcome.wait(timeout):
            self.close()
            raise ProtocolError("no welcome from backend")
        if self.welcome is None:
            self.close()
            raise ProtocolError("authentication failed")

    def subscribe(self, events) -> None:
        """Choose the pushed events: ``"*"`` for all (monitor values, sweep data, log
        lines ...) or a list of names.  The default is status events only."""
        self._send({"type": "subscribe", "events": events if events == "*" else list(events)})

    @classmethod
    def connect(cls, runtime_file: Optional[Path] = None, name: str = "client",
                events=None) -> "BackendClient":
        from ..backend.server import read_runtime_file
        info = read_runtime_file(runtime_file)
        if not info:
            raise ConnectionError("no running backend found (%s)" % (runtime_file or paths.runtime_file()))
        return cls(info.get("host", "127.0.0.1"), info["port"], info["token"], name=name,
                   events=events)

    def _send(self, msg: dict) -> None:
        frame = codec.encode(msg)
        with self._send_lock:
            self.sock.sendall(frame)

    def _read_loop(self):
        reader = codec.FrameReader()
        try:
            while True:
                data = self.sock.recv(1 << 16)
                if not data:
                    break
                for msg in reader.feed(data):
                    t = msg.get("type")
                    if t == "welcome":
                        self.welcome = msg
                        self._welcome.set()
                    elif t == "error":
                        self._welcome.set()
                    elif t == "resp":
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
        except (OSError, ProtocolError):
            pass
        finally:
            self.closed = True
            self._welcome.set()
            with self._lock:
                pend = list(self._pending.values())
                self._pending.clear()
            for slot in pend:
                slot[1] = {"ok": False, "error": "connection closed", "kind": "ConnectionError"}
                slot[0].set()

    def call(self, cmd: str, _timeout: float = 120.0, **args) -> Any:
        if self.closed:
            raise ConnectionError("connection to backend closed")
        rid = next(self._ids)
        slot = [threading.Event(), None]
        with self._lock:
            self._pending[rid] = slot
        self._send({"type": "req", "id": rid, "cmd": cmd, "args": args})
        if not slot[0].wait(_timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise TimeoutError("backend did not answer %s within %.0fs" % (cmd, _timeout))
        resp = slot[1]
        if not resp.get("ok"):
            raise RemoteError(resp.get("error", "error"), resp.get("kind", "RemoteError"),
                              resp.get("tb"))
        return resp.get("result")

    def close(self) -> None:
        try:
            self._send({"type": "bye"})
        except Exception:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def start_backend_detached(extra_args=None, home: Optional[str] = None) -> subprocess.Popen:
    """Start ``python -m easymeasure.backend`` fully detached from this process,
    so that closing or killing the caller (e.g. the GUI) never affects it."""
    args = [sys.executable, "-m", "easymeasure.backend", "--quiet"] + list(extra_args or [])
    env = dict(os.environ)
    if home:
        env["EASYMEASURE_HOME"] = home
    pkg_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
    kwargs: Dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, env=env, close_fds=True)
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


def wait_for_backend(timeout: float = 15.0, runtime_file: Optional[Path] = None) -> Optional[dict]:
    from ..backend.server import backend_alive, read_runtime_file
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        info = read_runtime_file(runtime_file)
        if info and backend_alive(info, 0.5):
            return info
        time.sleep(0.2)
    return None
