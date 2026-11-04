"""Non-blocking, Qt-native connection to the backend.

Everything happens through Qt signals on the GUI thread; nothing here ever
waits.  Requests take callbacks::

    conn.request("read_channel", {"name": "Vg"}, on_ok=lambda v: ..., on_err=lambda e: ...)

Events from the backend are re-emitted as ``event(kind, data)``.  If the backend
goes away the connection retries in the background and emits ``connected``
again once it is back (e.g. after a backend restart).
"""

from __future__ import annotations

import itertools
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket

from ..common import codec
from ..common.errors import ProtocolError

OkCb = Optional[Callable[[Any], None]]
ErrCb = Optional[Callable[[str], None]]


class BackendConnection(QObject):
    connected = Signal(dict)          # welcome message
    disconnected = Signal()
    status = Signal(str)              # human-readable connection status
    event = Signal(str, object)       # (kind, data)
    request_failed = Signal(str, str)  # (cmd, error) for requests without on_err

    def __init__(self, runtime_file: Optional[Path] = None, parent=None,
                 client_name: str = "gui", events="*"):
        super().__init__(parent)
        self.client_name = client_name
        self.events = events if events == "*" else list(events)
        self._runtime_file = runtime_file
        self._sock = QTcpSocket(self)
        self._sock.readyRead.connect(self._on_ready_read)
        self._sock.connected.connect(self._on_socket_connected)
        self._sock.disconnected.connect(self._on_socket_disconnected)
        self._sock.errorOccurred.connect(self._on_error)
        self._reader = codec.FrameReader()
        self._ids = itertools.count(1)
        self._pending: Dict[int, tuple] = {}
        self._token = ""
        self.is_connected = False
        self.info: Optional[dict] = None
        self._retry = QTimer(self)
        self._retry.setInterval(2000)
        self._retry.timeout.connect(self._try_connect)
        self._timeouts = QTimer(self)
        self._timeouts.setInterval(1000)
        self._timeouts.timeout.connect(self._check_timeouts)
        self._timeouts.start()
        self._auto = True

    # -- connection management -------------------------------------------------
    def start(self) -> None:
        """Begin (re)connecting in the background."""
        self._auto = True
        self._try_connect()
        self._retry.start()

    def stop(self) -> None:
        self._auto = False
        self._retry.stop()
        if self._sock.state() != QAbstractSocket.UnconnectedState:
            try:
                self._sock.write(codec.encode({"type": "bye"}))
                self._sock.flush()
            except Exception:
                pass
            self._sock.abort()

    def _try_connect(self) -> None:
        if self._sock.state() != QAbstractSocket.UnconnectedState:
            return
        from ..backend.server import read_runtime_file
        info = read_runtime_file(self._runtime_file)
        if not info:
            self.status.emit("no backend running")
            return
        self.info = info
        self._token = info.get("token", "")
        self.status.emit("connecting to %s:%s ..." % (info.get("host"), info.get("port")))
        self._reader = codec.FrameReader()
        self._sock.connectToHost(info.get("host", "127.0.0.1"), int(info["port"]))

    def _on_socket_connected(self) -> None:
        self._send({"type": "hello", "token": self._token, "client": self.client_name,
                    "events": self.events})

    def _on_socket_disconnected(self) -> None:
        was = self.is_connected
        self.is_connected = False
        self._fail_all("connection lost")
        if was:
            self.disconnected.emit()
        self.status.emit("disconnected")
        if self._auto and not self._retry.isActive():
            self._retry.start()

    def _on_error(self, err) -> None:
        if not self.is_connected:
            self.status.emit("backend not reachable (%s)" % self._sock.errorString())

    # -- I/O -------------------------------------------------------------------
    def _send(self, msg: dict) -> bool:
        if self._sock.state() != QAbstractSocket.ConnectedState:
            return False
        try:
            self._sock.write(codec.encode(msg))
            return True
        except Exception:
            return False

    def _on_ready_read(self) -> None:
        data = bytes(self._sock.readAll())
        try:
            for msg in self._reader.feed(data):
                self._dispatch(msg)
        except ProtocolError as exc:
            self.status.emit("protocol error: %s" % exc)
            self._sock.abort()

    def _dispatch(self, msg) -> None:
        if not isinstance(msg, dict):
            return
        t = msg.get("type")
        if t == "resp":
            entry = self._pending.pop(msg.get("id"), None)
            if entry is None:
                return
            cmd, on_ok, on_err, _ = entry
            if msg.get("ok"):
                if on_ok is not None:
                    self._safe(on_ok, msg.get("result"))
            else:
                err = msg.get("error", "error")
                if on_err is not None:
                    self._safe(on_err, err)
                else:
                    self.request_failed.emit(cmd, err)
        elif t == "event":
            try:
                self.event.emit(msg.get("event", ""), msg.get("data"))
            except Exception:
                traceback.print_exc()
        elif t == "welcome":
            self.is_connected = True
            self._retry.stop()
            self.status.emit("connected (backend pid %s)" % msg.get("pid"))
            self.connected.emit(msg)
        elif t == "error":
            self.status.emit("backend refused connection: %s" % msg.get("error"))

    @staticmethod
    def _safe(fn, arg) -> None:
        try:
            fn(arg)
        except Exception:
            traceback.print_exc()

    # -- requests ----------------------------------------------------------------
    def request(self, cmd: str, args: Optional[dict] = None, on_ok: OkCb = None,
                on_err: ErrCb = None, timeout: float = 120.0) -> Optional[int]:
        """Send a request; callbacks run later on the GUI thread.  Never blocks."""
        if not self.is_connected:
            if on_err is not None:
                QTimer.singleShot(0, lambda: self._safe(on_err, "not connected to backend"))
            else:
                self.request_failed.emit(cmd, "not connected to backend")
            return None
        rid = next(self._ids)
        self._pending[rid] = (cmd, on_ok, on_err, time.monotonic() + timeout)
        if not self._send({"type": "req", "id": rid, "cmd": cmd, "args": args or {}}):
            self._pending.pop(rid, None)
            if on_err is not None:
                self._safe(on_err, "send failed")
            return None
        return rid

    def _check_timeouts(self) -> None:
        now = time.monotonic()
        for rid, (cmd, _, on_err, deadline) in list(self._pending.items()):
            if now > deadline:
                self._pending.pop(rid, None)
                msg = "backend did not answer %s in time" % cmd
                if on_err is not None:
                    self._safe(on_err, msg)
                else:
                    self.request_failed.emit(cmd, msg)

    def _fail_all(self, reason: str) -> None:
        pend, self._pending = self._pending, {}
        for cmd, _, on_err, _ in pend.values():
            if on_err is not None:
                self._safe(on_err, reason)
