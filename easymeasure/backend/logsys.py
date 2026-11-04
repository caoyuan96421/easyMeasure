"""Logging system.

* Everything goes to a rotating log file (``~/.easymeasure/logs/backend.log``).
* The most recent records are kept in a ring buffer so a (re)connecting frontend
  can show recent history.
* Listeners (the IPC server) are notified of every record; a failing listener is
  never allowed to break logging.
"""

from __future__ import annotations

import collections
import logging
import logging.handlers
import threading
import time
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional

ROOT = "easymeasure"
_FMT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str) -> logging.Logger:
    if not name.startswith(ROOT):
        name = ROOT + "." + name
    return logging.getLogger(name)


class RingBufferHandler(logging.Handler):
    """Keeps the last *capacity* records (as dicts) and fans them out."""

    def __init__(self, capacity: int = 5000):
        super().__init__(level=logging.DEBUG)
        self._records: Deque[Dict] = collections.deque(maxlen=capacity)
        self._listeners: List[Callable[[Dict], None]] = []
        self._lock = threading.Lock()
        self._seq = 0

    _tls = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        # A listener that logs (directly or indirectly) must not recurse forever.
        if getattr(self._tls, "busy", False):
            return
        self._tls.busy = True
        try:
            self._emit(record)
        finally:
            self._tls.busy = False

    def _emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # bad format args must never propagate
            msg = str(record.msg)
        if record.exc_info:
            try:
                msg += "\n" + logging.Formatter().formatException(record.exc_info)
            except Exception:
                pass
        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "t": record.created,
                "level": record.levelname,
                "levelno": record.levelno,
                "name": record.name,
                "thread": record.threadName,
                "msg": msg,
            }
            self._records.append(entry)
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(entry)
            except Exception:
                pass

    def recent(self, n: int = 500, min_level: int = 0) -> List[Dict]:
        with self._lock:
            items = [r for r in self._records if r["levelno"] >= min_level]
        return items[-n:]

    def add_listener(self, cb: Callable[[Dict], None]) -> None:
        with self._lock:
            self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[Dict], None]) -> None:
        with self._lock:
            try:
                self._listeners.remove(cb)
            except ValueError:
                pass


_ring: Optional[RingBufferHandler] = None
_setup_lock = threading.Lock()


def setup_logging(log_dir: Optional[Path] = None, level: int = logging.DEBUG,
                  console: bool = True) -> RingBufferHandler:
    """Configure the ``easymeasure`` logger tree (idempotent)."""
    global _ring
    with _setup_lock:
        root = logging.getLogger(ROOT)
        if _ring is not None:
            return _ring
        root.setLevel(level)
        root.propagate = False
        fmt = logging.Formatter(_FMT, _DATEFMT)
        if log_dir is not None:
            log_dir = Path(log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_dir / "backend.log", maxBytes=10_000_000, backupCount=10, encoding="utf-8",
                delay=False)
            fh.setFormatter(fmt)
            fh.setLevel(logging.DEBUG)
            root.addHandler(fh)
        if console:
            ch = logging.StreamHandler()
            ch.setFormatter(fmt)
            ch.setLevel(logging.INFO)
            root.addHandler(ch)
        _ring = RingBufferHandler()
        root.addHandler(_ring)
        logging.captureWarnings(True)
        return _ring


def ring() -> RingBufferHandler:
    """Return the ring buffer, setting up minimal logging if needed."""
    global _ring
    if _ring is None:
        return setup_logging(None, console=False)
    return _ring


class RateLimitedLogger:
    """Log a repeated message at most once every *period* seconds (per key)."""

    def __init__(self, logger: logging.Logger, period: float = 10.0):
        self.logger = logger
        self.period = period
        self._last: Dict[str, float] = {}
        self._suppressed: Dict[str, int] = {}
        self._lock = threading.Lock()

    def log(self, level: int, key: str, msg: str, *args) -> None:
        now = time.monotonic()
        with self._lock:
            last = self._last.get(key, -1e18)
            if now - last < self.period:
                self._suppressed[key] = self._suppressed.get(key, 0) + 1
                return
            n = self._suppressed.pop(key, 0)
            self._last[key] = now
        if n:
            msg = msg + " (%d similar messages suppressed)" % n
        self.logger.log(level, msg, *args)
