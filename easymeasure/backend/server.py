"""TCP server exposing the :class:`Backend` to frontends.

Design goals: the backend must never be slowed down or blocked by a frontend,
and frontends may connect / disconnect / crash at any time.

* Each client has a reader thread and a writer thread with a bounded queue.
* Commands are executed on a thread pool, so a slow instrument call never blocks
  other requests (or other clients).
* Events (monitor values, sweep points, log lines) are *droppable*: if a client
  is too slow its queue overflows, events are dropped and the client receives a
  ``resync`` event telling it to re-request a snapshot.  Replies are never dropped.
* Authentication: a random token, advertised in ``~/.easymeasure/backend.json``.

Messages (all codec frames)::

    client -> server  {"type": "hello", "token": "...", "client": "gui"}
    server -> client  {"type": "welcome", "version": ..., "pid": ...}
    client -> server  {"type": "req", "id": 7, "cmd": "read_channel", "args": {...}}
    server -> client  {"type": "resp", "id": 7, "ok": true, "result": ...}
                      {"type": "resp", "id": 7, "ok": false, "error": "...", "kind": "..."}
    server -> client  {"type": "event", "event": "monitor", "data": {...}}
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import queue
import secrets
import socket
import threading
import time
import traceback
from pathlib import Path
from typing import Dict, Optional

from .. import __version__
from ..common import codec, paths
from ..common.errors import EasyMeasureError, ProtocolError
from .logsys import get_logger
from .service import Backend

log = get_logger("server")

DEFAULT_PORT = 47811
MAX_QUEUE_BYTES = 256 * 1024 * 1024
MAX_QUEUE_MSGS = 20000
_DROPPABLE = {"monitor", "log", "sweep_point"}


class _Client:
    def __init__(self, server: "Server", sock: socket.socket, addr):
        self.server = server
        self.sock = sock
        self.addr = addr
        self.id = "%s:%s" % addr[:2] if isinstance(addr, tuple) else str(addr)
        self.authed = False
        self.alive = True
        self._q: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._qbytes = 0
        self._qlock = threading.RLock()
        self._dropping = False
        self.dropped = 0
        self.name = "?"
        self._reader = threading.Thread(target=self._read_loop, name="client-rx-%s" % self.id,
                                        daemon=True)
        self._writer = threading.Thread(target=self._write_loop, name="client-tx-%s" % self.id,
                                        daemon=True)

    def start(self):
        self._reader.start()
        self._writer.start()

    # -- sending ---------------------------------------------------------------
    def send(self, msg: dict, droppable: bool = False) -> None:
        if not self.alive:
            return
        try:
            frame = codec.encode(msg)
        except Exception as exc:
            log.error("cannot encode message for %s: %s", self.id, exc)
            if msg.get("type") == "resp":
                frame = codec.encode({"type": "resp", "id": msg.get("id"), "ok": False,
                                      "error": "result not serialisable: %s" % exc,
                                      "kind": "ProtocolError"})
            else:
                return
        # NB: never log while holding _qlock - log records are themselves sent to
        # clients and would re-enter here.
        started_dropping = hopeless = False
        with self._qlock:
            over = self._qbytes + len(frame) > MAX_QUEUE_BYTES or self._q.qsize() > MAX_QUEUE_MSGS
            if droppable and (over or self._dropping):
                self.dropped += 1
                if not self._dropping:
                    self._dropping = True
                    started_dropping = True
            elif over and not droppable and self._qbytes > 2 * MAX_QUEUE_BYTES:
                hopeless = True
            else:
                self._qbytes += len(frame)
                self._q.put(frame)
        if started_dropping:
            log.warning("client %s is too slow, dropping events", self.id)
        if hopeless:
            log.error("client %s hopelessly behind, disconnecting", self.id)
            self.close()

    def _write_loop(self):
        try:
            while self.alive:
                frame = self._q.get()
                if frame is None:
                    break
                with self._qlock:
                    self._qbytes -= len(frame)
                    resync = self._dropping and self._qbytes < MAX_QUEUE_BYTES // 4 and \
                        self._q.qsize() < MAX_QUEUE_MSGS // 4
                    if resync:
                        self._dropping = False
                self.sock.sendall(frame)
                if resync:
                    self.sock.sendall(codec.encode({"type": "event", "event": "resync",
                                                    "data": {"dropped": self.dropped}}))
        except OSError:
            pass
        except Exception as exc:
            log.warning("writer for %s failed: %s", self.id, exc)
        finally:
            self.close()

    # -- receiving -------------------------------------------------------------
    def _read_loop(self):
        reader = codec.FrameReader()
        try:
            self.sock.settimeout(None)
            while self.alive:
                data = self.sock.recv(1 << 16)
                if not data:
                    break
                for msg in reader.feed(data):
                    self._handle(msg)
        except ProtocolError as exc:
            log.warning("protocol error from %s: %s", self.id, exc)
        except OSError:
            pass
        except Exception as exc:
            log.error("reader for %s failed: %s\n%s", self.id, exc, traceback.format_exc())
        finally:
            self.close()

    def _handle(self, msg):
        if not isinstance(msg, dict):
            raise ProtocolError("message must be a dict")
        t = msg.get("type")
        if not self.authed:
            if t != "hello" or not secrets.compare_digest(str(msg.get("token", "")),
                                                          self.server.token):
                self.send({"type": "error", "error": "authentication failed"})
                log.warning("client %s failed authentication", self.id)
                time.sleep(0.1)
                raise ProtocolError("authentication failed")
            self.authed = True
            self.name = str(msg.get("client", "?"))
            self.send({"type": "welcome", "version": __version__, "pid": os.getpid(),
                       "started": self.server.backend.started})
            log.info("client %s (%s) connected", self.id, self.name)
            self.server._attach(self)
            return
        if t == "req":
            self.server.submit(self, msg)
        elif t == "bye":
            self.close()
        elif t == "ping":
            self.send({"type": "pong", "t": time.time()})

    def close(self):
        if not self.alive:
            return
        self.alive = False
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.server._detach(self)


class Server:
    def __init__(self, backend: Backend, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 token: Optional[str] = None, runtime_file: Optional[Path] = None,
                 workers: int = 16):
        self.backend = backend
        self.host = host
        self.token = token or secrets.token_hex(16)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.bind((host, port))
        except OSError:
            if port == 0:
                raise
            log.warning("port %d busy, using an ephemeral port", port)
            self._sock.bind((host, 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self.runtime_file = Path(runtime_file) if runtime_file else paths.runtime_file()
        self._clients: Dict[int, _Client] = {}
        self._clients_lock = threading.RLock()
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers,
                                                           thread_name_prefix="cmd")
        self._stop = threading.Event()
        self._accept_thread = threading.Thread(target=self._accept_loop, name="accept", daemon=True)
        backend.add_listener(self._on_event)
        backend.set_shutdown_callback(self.stop)

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        self._write_runtime_file()
        self._accept_thread.start()
        log.info("backend server listening on %s:%d", self.host, self.port)

    def _write_runtime_file(self) -> None:
        info = {"host": self.host if self.host not in ("0.0.0.0", "") else "127.0.0.1",
                "port": self.port, "token": self.token, "pid": os.getpid(),
                "started": time.time(), "version": __version__}
        self.runtime_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.runtime_file.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(info, f)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.runtime_file)

    def serve_forever(self) -> None:
        self.start()
        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            log.warning("interrupted")
        finally:
            self.stop()

    def stop(self) -> None:
        if self._stop.is_set() and not self._accept_thread.is_alive():
            return
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        with self._clients_lock:
            clients = list(self._clients.values())
        for c in clients:
            c.close()
        self.backend.remove_listener(self._on_event)
        self._pool.shutdown(wait=False, cancel_futures=True)
        try:
            info = json.loads(self.runtime_file.read_text())
            if info.get("pid") == os.getpid() and info.get("port") == self.port:
                self.runtime_file.unlink()
        except Exception:
            pass
        log.info("server stopped")

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock, addr = self._sock.accept()
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.1)
                continue
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                c = _Client(self, sock, addr)
                c.start()
            except Exception as exc:
                log.error("cannot start client: %s", exc)
                try:
                    sock.close()
                except OSError:
                    pass

    def _attach(self, c: _Client) -> None:
        with self._clients_lock:
            self._clients[id(c)] = c

    def _detach(self, c: _Client) -> None:
        with self._clients_lock:
            removed = self._clients.pop(id(c), None) is not None
        if removed:
            log.info("client %s (%s) disconnected", c.id, c.name)

    def clients(self) -> list:
        with self._clients_lock:
            return [{"id": c.id, "name": c.name, "dropped": c.dropped} for c in self._clients.values()]

    # -- requests / events -------------------------------------------------------
    def submit(self, client: _Client, msg: dict) -> None:
        rid = msg.get("id")
        cmd = msg.get("cmd")
        args = msg.get("args") or {}

        def run():
            try:
                if not isinstance(args, dict):
                    raise ProtocolError("args must be a dict")
                result = self.backend.dispatch(cmd, args)
                client.send({"type": "resp", "id": rid, "ok": True, "result": result})
            except BaseException as exc:  # noqa: B902 - report everything to the client
                tb = None if isinstance(exc, EasyMeasureError) else traceback.format_exc(limit=6)
                if tb:
                    log.error("command %s failed: %s\n%s", cmd, exc, tb)
                client.send({"type": "resp", "id": rid, "ok": False,
                             "error": str(exc) or type(exc).__name__,
                             "kind": type(exc).__name__, "tb": tb})
        try:
            self._pool.submit(run)
        except RuntimeError:
            client.send({"type": "resp", "id": rid, "ok": False, "error": "server shutting down",
                         "kind": "RuntimeError"})

    def _on_event(self, kind: str, payload) -> None:
        msg = {"type": "event", "event": kind, "data": payload}
        droppable = kind in _DROPPABLE
        with self._clients_lock:
            clients = list(self._clients.values())
        if not clients:
            return
        try:
            frame_msg = msg
            for c in clients:
                c.send(frame_msg, droppable=droppable)
        except Exception:
            pass


def read_runtime_file(path: Optional[Path] = None) -> Optional[dict]:
    p = Path(path) if path else paths.runtime_file()
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def backend_alive(info: Optional[dict], timeout: float = 1.0) -> bool:
    if not info:
        return False
    try:
        with socket.create_connection((info.get("host", "127.0.0.1"), int(info["port"])),
                                      timeout=timeout):
            return True
    except OSError:
        return False
