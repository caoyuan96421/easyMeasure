"""Monitor: continuously polls selected channels and streams the values.

Each monitored channel has its own interval.  Channels are polled by a small
pool of worker threads (one per instrument, so a slow/hung instrument never
delays the others).  Values are kept in a per-channel ring buffer (so a
reconnecting frontend gets recent history) and pushed to listeners.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Callable, Deque, Dict, List, Optional

import numpy as np

from .logsys import RateLimitedLogger, get_logger

log = get_logger("monitor")


class _Entry:
    def __init__(self, name: str, interval: float, history: int):
        self.name = name
        self.interval = max(0.05, float(interval))
        self.history: Deque = collections.deque(maxlen=history)
        self.next_due = 0.0
        self.busy = False
        self.last_error = ""
        self.last_value = None
        self.last_time = 0.0
        self.errors = 0
        self.enabled = True


class Monitor:
    def __init__(self, channels, history: int = 2000):
        self.channels = channels
        self.history_len = history
        self._entries: Dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pool: Dict[str, "_PollWorker"] = {}
        self.listeners: List[Callable[[dict], None]] = []
        self._rl = RateLimitedLogger(log, 30.0)

    # -- configuration -----------------------------------------------------------
    def add(self, name: str, interval: float = 1.0) -> None:
        with self._lock:
            e = self._entries.get(name)
            interval = max(0.05, float(interval))
            if e is None:
                self._entries[name] = _Entry(name, interval, self.history_len)
            else:
                e.interval = max(0.05, float(interval))
                e.enabled = True
        self._wake.set()
        log.info("monitoring %s every %.2fs", name, interval)

    def remove(self, name: str) -> None:
        with self._lock:
            self._entries.pop(name, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def config(self) -> List[dict]:
        with self._lock:
            return [{"channel": e.name, "interval": e.interval} for e in self._entries.values()]

    def status(self) -> List[dict]:
        with self._lock:
            return [{"channel": e.name, "interval": e.interval, "last_value": e.last_value,
                     "last_time": e.last_time, "last_error": e.last_error, "errors": e.errors}
                    for e in self._entries.values()]

    def history(self, name: Optional[str] = None) -> Dict[str, list]:
        with self._lock:
            items = [self._entries[name]] if name and name in self._entries else \
                list(self._entries.values()) if name is None else []
            return {e.name: list(e.history) for e in items}

    # -- running -------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(2.0)
        for w in list(self._pool.values()):
            w.stop()
        self._pool.clear()

    def _group_of(self, name: str) -> str:
        """Poll channels of the same instrument on the same worker."""
        try:
            info = self.channels.info(name)
            target = info.get("target") or (name if info.get("kind") in ("hardware", "config") else None)
            if target:
                return target.split(".", 1)[0]
            if info.get("kind") in ("variable", "map", "read"):
                return "composite:" + (info.get("composite") or name)
        except Exception:
            pass
        return "misc"

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                next_wake = now + 0.5
                with self._lock:
                    entries = list(self._entries.values())
                for e in entries:
                    if not e.enabled:
                        continue
                    if not e.busy and now >= e.next_due:
                        group = self._group_of(e.name)
                        w = self._pool.get(group)
                        if w is None or not w.alive:
                            w = _PollWorker(group, self)
                            self._pool[group] = w
                        if w.submit(e):
                            e.busy = True
                            e.next_due = now + e.interval
                    next_wake = min(next_wake, max(e.next_due, now + 0.02))
                self._wake.wait(max(0.01, next_wake - time.monotonic()))
                self._wake.clear()
            except Exception as exc:  # the monitor loop must never die
                self._rl.log(40, "loop", "monitor loop error: %s", exc)
                time.sleep(0.5)

    def _poll(self, e: _Entry) -> None:
        try:
            v = self.channels.get(e.name)
            t = time.time()
            e.last_value = v if np.ndim(v) == 0 else None
            e.last_time = t
            e.last_error = ""
            if np.ndim(v) == 0:
                e.history.append((t, v))
            else:
                e.history.clear()
                e.history.append((t, v))  # only keep the latest array
            msg = {"channel": e.name, "t": t, "value": v}
        except Exception as exc:
            e.errors += 1
            e.last_error = str(exc)
            self._rl.log(30, e.name, "monitor: reading %s failed: %s", e.name, exc)
            msg = {"channel": e.name, "t": time.time(), "error": str(exc)}
        finally:
            e.busy = False
            self._wake.set()
        for cb in list(self.listeners):
            try:
                cb(msg)
            except Exception:
                pass


class _PollWorker:
    """One thread per instrument group; a hung instrument only blocks itself."""

    def __init__(self, group: str, monitor: Monitor):
        self.group = group
        self.monitor = monitor
        self._q: "collections.deque" = collections.deque()
        self._ev = threading.Event()
        self._stop = False
        self._t = threading.Thread(target=self._run, name="monitor-%s" % group, daemon=True)
        self._t.start()

    @property
    def alive(self) -> bool:
        return self._t.is_alive()

    def submit(self, e: _Entry) -> bool:
        if len(self._q) > 50:
            return False
        self._q.append(e)
        self._ev.set()
        return True

    def stop(self) -> None:
        self._stop = True
        self._ev.set()

    def _run(self) -> None:
        while not self._stop:
            self._ev.wait(0.5)
            self._ev.clear()
            while self._q and not self._stop:
                e = self._q.popleft()
                self.monitor._poll(e)
