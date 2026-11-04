"""Execution of user scripts (queue items, pre/post sweep hooks).

Scripts are plain Python run in the backend with a small API in their namespace:

=====================  =========================================================
``get(name)``          read an alias / composite / ``instrument.channel``
``set(name, value)``   protected (limits, ramp) write to an alias / composite
``set_raw(ref, v)``    unprotected write to ``instrument.channel``
``sleep(s)``           abortable sleep (always use this instead of time.sleep)
``wait_until(f, timeout, interval)``  poll ``f()`` until true
``check()``            honour pause / abort
``log(msg)``/``print`` write to the backend log
``instrument(name)``   guarded instrument proxy: ``.raw_query``, ``.raw_write``, ``.call``
``run_sweep(defn)``    run a sweep definition synchronously (returns status)
``np``, ``math``, ``time``  modules
``state``              a dict persisting between scripts in the same backend
=====================  =========================================================

Abort/pause are honoured not only inside API calls but on every executed line
of the script (via a thread-local trace function), so even ``while True: pass``
can be aborted.
"""

from __future__ import annotations

import math
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, Optional

import numpy as np

from ..common.errors import AbortRequested, EasyMeasureError
from .logsys import get_logger

log = get_logger("script")

_SCRIPT_PREFIX = "<script:"


class ScriptError(EasyMeasureError):
    pass


class _InstrumentProxy:
    def __init__(self, handle):
        self._h = handle

    def raw_query(self, cmd: str) -> str:
        return self._h.raw_query(cmd)

    def raw_write(self, cmd: str) -> None:
        self._h.raw_write(cmd)

    def call(self, method: str, *args, **kwargs):
        return self._h.call(method, *args, **kwargs)

    def read(self, channel: str):
        return self._h.read(channel)

    @property
    def state(self) -> str:
        return self._h.state

    def __repr__(self):
        return "<instrument %s>" % self._h.name


class ScriptRunner:
    def __init__(self, channels, instruments, control,
                 run_sweep: Optional[Callable[[dict], str]] = None):
        self.channels = channels
        self.instruments = instruments
        self.control = control
        self.run_sweep = run_sweep
        self.state: Dict[str, Any] = {}

    def namespace(self, name: str) -> dict:
        ctl = self.control
        slog = get_logger("script." + name.replace(".", "_"))

        def _print(*args, sep=" ", end="", **_):
            slog.info(sep.join(str(a) for a in args) + (end if end != "\n" else ""))

        def wait_until(cond, timeout: float = 60.0, interval: float = 0.2) -> bool:
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                ctl.check()
                if cond():
                    return True
                ctl.sleep(interval)
            return False

        def _set(n, v, ramp=True):
            ctl.check()
            return self.channels.set(n, v, ramp=ramp, abort_check=ctl.check)

        def _get(n):
            ctl.check()
            return self.channels.get(n)

        def _set_raw(ref, v):
            ctl.check()
            self.channels.set_raw(ref, v)

        def _instrument(n):
            return _InstrumentProxy(self.instruments.get(n))

        def _run_sweep(defn):
            if self.run_sweep is None:
                raise ScriptError("run_sweep not available here")
            return self.run_sweep(defn)

        return {
            "__name__": "__easymeasure_script__",
            "get": _get, "set": _set, "set_raw": _set_raw, "sleep": ctl.sleep,
            "check": ctl.check, "wait_until": wait_until, "log": slog.info,
            "warn": slog.warning, "print": _print, "instrument": _instrument,
            "run_sweep": _run_sweep, "np": np, "math": math, "time": time,
            "state": self.state, "AbortRequested": AbortRequested,
        }

    def run(self, code: str, name: str = "script") -> None:
        """Execute *code* in the calling thread.  Raises on error/abort."""
        filename = _SCRIPT_PREFIX + name + ">"
        try:
            compiled = compile(code, filename, "exec")
        except SyntaxError as exc:
            raise ScriptError("syntax error in %s line %s: %s" % (name, exc.lineno, exc.msg)) from None
        ns = self.namespace(name)
        ctl = self.control

        def local_trace(frame, event, arg):
            if event == "line":
                ctl.check()
            return local_trace

        def global_trace(frame, event, arg):
            if frame.f_code.co_filename.startswith(_SCRIPT_PREFIX):
                return local_trace
            return None

        old = sys.gettrace()
        sys.settrace(global_trace)
        try:
            exec(compiled, ns)  # noqa: S102 - user scripts are the point
        except AbortRequested:
            raise
        except SystemExit as exc:
            raise ScriptError("%s called exit(%s)" % (name, exc.code)) from None
        except BaseException as exc:  # noqa: B902
            tb = traceback.format_exc(limit=8)
            log.error("script %s failed:\n%s", name, tb)
            raise ScriptError("%s: %s: %s" % (name, type(exc).__name__, exc)) from None
        finally:
            sys.settrace(old)
