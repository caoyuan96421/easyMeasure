"""Run queue: the backend state machine.

States::

    idle  --start-->  running  --pause-->  paused  --resume--> running
      ^                  |  \\---abort--> aborting --(item ends)--+
      +---- queue empty / stop / abort_all -----------------------+

Items are either sweeps (``{"kind": "sweep", "definition": {...}}``) or scripts
(``{"kind": "script", "code": "..."}``) and are executed strictly sequentially
on one dedicated thread.  Whatever an item does, the queue thread survives it.
"""

from __future__ import annotations

import collections
import copy
import itertools
import threading
import time
import traceback
from typing import Callable, Deque, Dict, List, Optional

from ..common.errors import AbortRequested, ChannelError, EasyMeasureError
from .logsys import get_logger
from .resources import PRIO_MEASUREMENT, set_thread_priority
from .sweep import RunControl, normalize_definition

log = get_logger("queue")

IDLE, RUNNING, PAUSED, ABORTING = "idle", "running", "paused", "aborting"


class RunQueue:
    def __init__(self, execute_sweep: Callable[[dict, dict], dict],
                 execute_script: Callable[[str, str], None], control: RunControl,
                 emit: Callable[[str, dict], None]):
        self._execute_sweep = execute_sweep
        self._execute_script = execute_script
        self.control = control
        self._emit = emit
        self._items: List[dict] = []
        self._history: Deque[dict] = collections.deque(maxlen=200)
        self._current: Optional[dict] = None
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._processing = False
        self._stop_after_current = False
        self._ids = itertools.count(1)
        self._shutdown = False
        self.stop_on_error = False
        self._thread = threading.Thread(target=self._loop, name="run-queue", daemon=True)
        self._thread.start()

    # -- state ---------------------------------------------------------------------
    @property
    def state(self) -> str:
        with self._lock:
            if self._current is not None:
                if self.control.aborted:
                    return ABORTING
                if self.control.paused:
                    return PAUSED
                return RUNNING
            return RUNNING if self._processing and self._items else IDLE

    def describe(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "processing": self._processing,
                "stop_on_error": self.stop_on_error,
                "current": _public(self._current),
                "queued": [_public(i) for i in self._items],
                "history": [_public(i) for i in list(self._history)[-50:]],
            }

    def _changed(self) -> None:
        try:
            self._emit("queue", self.describe())
        except Exception:
            pass

    # -- editing ---------------------------------------------------------------------
    def add(self, item: dict, start: bool = False, position: Optional[int] = None) -> int:
        item = copy.deepcopy(item)
        kind = item.get("kind")
        if kind == "sweep":
            item["definition"] = normalize_definition(item.get("definition") or {})
            item.setdefault("name", item["definition"]["name"])
        elif kind == "script":
            code = item.get("code")
            if not isinstance(code, str):
                raise ChannelError("script item needs 'code'")
            try:
                compile(code, "<queued script>", "exec")
            except SyntaxError as exc:
                raise ChannelError("script syntax error line %s: %s" % (exc.lineno, exc.msg)) from None
            item.setdefault("name", "script")
        else:
            raise ChannelError("queue item kind must be 'sweep' or 'script'")
        with self._cv:
            item["id"] = next(self._ids)
            item["status"] = "queued"
            item["added"] = time.time()
            if position is None:
                self._items.append(item)
            else:
                self._items.insert(max(0, int(position)), item)
            if start:
                self._processing = True
                self._stop_after_current = False
            self._cv.notify_all()
        log.info("queued %s #%d (%s)", kind, item["id"], item["name"])
        self._changed()
        return item["id"]

    def remove(self, item_id: int) -> None:
        with self._lock:
            for i, it in enumerate(self._items):
                if it["id"] == item_id:
                    del self._items[i]
                    break
            else:
                raise ChannelError("no queued item %d" % item_id)
        self._changed()

    def move(self, item_id: int, position: int) -> None:
        with self._lock:
            for i, it in enumerate(self._items):
                if it["id"] == item_id:
                    del self._items[i]
                    self._items.insert(max(0, min(int(position), len(self._items))), it)
                    break
            else:
                raise ChannelError("no queued item %d" % item_id)
        self._changed()

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
        self._changed()

    def get_item(self, item_id: int) -> dict:
        with self._lock:
            for it in itertools.chain(self._items, [self._current] if self._current else [],
                                      self._history):
                if it and it["id"] == item_id:
                    return copy.deepcopy(it)
        raise ChannelError("no item %d" % item_id)

    # -- control ---------------------------------------------------------------------
    def start(self) -> None:
        with self._cv:
            self._processing = True
            self._stop_after_current = False
            self._cv.notify_all()
        log.info("queue started")
        self._changed()

    def stop(self) -> None:
        """Finish the current item, then go idle."""
        with self._cv:
            self._stop_after_current = True
            self._processing = False
            self._cv.notify_all()
        self._changed()

    def pause(self) -> None:
        if self._current is None:
            return
        self.control.pause()
        log.info("pause requested")
        self._changed()

    def resume(self) -> None:
        self.control.resume()
        log.info("resumed")
        self._changed()

    def abort(self) -> None:
        """Abort the current item; the queue continues with the next one."""
        with self._lock:
            if self._current is None:
                return
        self.control.abort()
        log.warning("abort requested for current item")
        self._changed()

    def abort_all(self) -> None:
        """Abort the current item and stop processing the queue."""
        with self._cv:
            self._processing = False
            self._stop_after_current = True
            cur = self._current
        if cur is not None:
            self.control.abort()
        log.warning("abort all requested")
        self._changed()

    def shutdown(self) -> None:
        with self._cv:
            self._shutdown = True
            self._processing = False
            self._cv.notify_all()
        self.control.abort()
        self._thread.join(5.0)

    def wait_idle(self, timeout: float = 60.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                if self._current is None and (not self._processing or not self._items):
                    return True
            time.sleep(0.02)
        return False

    # -- worker ----------------------------------------------------------------------
    def _loop(self) -> None:
        set_thread_priority(PRIO_MEASUREMENT)
        while True:
            try:
                with self._cv:
                    while not self._shutdown and not (self._processing and self._items):
                        self._cv.wait(0.5)
                    if self._shutdown:
                        return
                    item = self._items.pop(0)
                    self._current = item
                    item["status"] = "running"
                    item["started"] = time.time()
                    self.control.reset()
                self._changed()
                self._run_item(item)
            except BaseException as exc:  # noqa: B902 - the queue thread must survive
                log.critical("queue worker error (recovered): %s\n%s", exc, traceback.format_exc())
                time.sleep(0.2)
            finally:
                with self._cv:
                    if self._current is not None:
                        cur = self._current
                        cur.setdefault("ended", time.time())
                        if cur["status"] == "running":
                            cur["status"] = "error"
                        self._history.append(cur)
                        self._current = None
                        failed = cur["status"] == "error"
                        if self._stop_after_current or (failed and self.stop_on_error):
                            self._processing = False
                            self._stop_after_current = False
                        if not self._items:
                            self._processing = False
                    self.control.reset()
                if not self._shutdown:
                    self._changed()

    def _run_item(self, item: dict) -> None:
        name = item.get("name", "")
        try:
            if item["kind"] == "sweep":
                result = self._execute_sweep(item["definition"], item)
                item["status"] = result.get("status", "error")
                item["run"] = result.get("run")
                item["path"] = result.get("path")
                item["error"] = result.get("error", "")
            else:
                self._execute_script(item["code"], name)
                item["status"] = "finished"
        except AbortRequested:
            item["status"] = "aborted"
            log.warning("%s #%d aborted", item["kind"], item["id"])
        except EasyMeasureError as exc:
            item["status"] = "error"
            item["error"] = str(exc)
            log.error("%s #%d failed: %s", item["kind"], item["id"], exc)
        except BaseException as exc:  # noqa: B902
            item["status"] = "error"
            item["error"] = "%s: %s" % (type(exc).__name__, exc)
            log.error("%s #%d crashed: %s\n%s", item["kind"], item["id"], exc,
                      traceback.format_exc(limit=8))
        item["ended"] = time.time()
        self._emit("item_end", _public(item))


def _public(item: Optional[dict]) -> Optional[dict]:
    if item is None:
        return None
    out = {k: v for k, v in item.items() if k not in ("code", "definition")}
    if item.get("kind") == "script":
        out["code"] = item.get("code", "")
    if item.get("kind") == "sweep":
        d = item.get("definition") or {}
        out["shape"] = [dim.get("points") for dim in d.get("dimensions", [])]
        out["definition"] = d
    return out
