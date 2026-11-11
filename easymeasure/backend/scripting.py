"""Execution of user scripts (queue items, pre/post sweep hooks).

Scripts are plain Python run in the backend with a small API in their namespace:

=====================  =========================================================
``get(name)``          read an alias / composite / ``instrument.channel``
``set(name, value)``   protected (limits, ramp) write to an alias / composite
``set_many({n: v})``   set several together (e.g. variables of one composite)
``set_raw(ref, v)``    unprotected write to ``instrument.channel``
``sleep(s)``           abortable sleep (always use this instead of time.sleep)
``wait_until(f, timeout, interval)``  poll ``f()`` until true
``check()``            honour pause / abort
``log(msg)``/``print`` write to the backend log
``instrument(name)``   guarded instrument proxy: ``.raw_query``, ``.raw_write``, ``.call``,
                       ``.action``
``action(ref)``        run an instrument action, e.g. ``action('dmm.reset')``
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

from ..common.errors import AbortRequested, EasyMeasureError, EndSweep, SkipLine, SkipPoint
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

    def action(self, name: str) -> None:
        self._h.run_action(name)

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
        self._compiled: Dict[tuple, Any] = {}

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

        def _set_many(values, ramp=True):
            ctl.check()
            return self.channels.set_many(values, ramp=ramp, abort_check=ctl.check)

        def _get(n):
            ctl.check()
            return self.channels.get(n)

        def _set_raw(ref, v):
            ctl.check()
            self.channels.set_raw(ref, v)

        def _instrument(n):
            return _InstrumentProxy(self.instruments.get(n))

        def _action(ref):
            ctl.check()
            if not isinstance(ref, str) or "." not in ref:
                raise ScriptError("action() needs 'instrument.action'")
            inst, name = ref.split(".", 1)
            try:
                self.instruments.get(inst).run_action(name)
            finally:
                self.channels.forget_instrument(inst)

        def _point_only(fn):
            def f():
                raise ScriptError("%s() only works in a sweep's per-point actions" % fn)
            return f

        def _run_sweep(defn):
            if self.run_sweep is None:
                raise ScriptError("run_sweep not available here")
            return self.run_sweep(defn)

        return {
            "__name__": "__easymeasure_script__",
            "get": _get, "set": _set, "set_many": _set_many, "set_raw": _set_raw, "sleep": ctl.sleep,
            "check": ctl.check, "wait_until": wait_until, "log": slog.info,
            "warn": slog.warning, "print": _print, "instrument": _instrument,
            "run_sweep": _run_sweep, "action": _action, "np": np, "math": math, "time": time,
            "state": self.state, "AbortRequested": AbortRequested, "skip_point": _point_only("skip_point"), "skip_line": _point_only("skip_line"),
            "end_sweep": _point_only("end_sweep"),
        }

    def run(self, code: str, name: str = "script", extra: Optional[dict] = None) -> None:
        """Execute *code* in the calling thread.  Raises on error/abort.

        *extra* adds names to the script's namespace (the per-point sweep hooks get
        the point's indices and setpoints this way)."""
        filename = _SCRIPT_PREFIX + name + ">"
        key = (code, filename)
        compiled = self._compiled.get(key)
        if compiled is None:
            try:
                compiled = compile(code, filename, "exec")
            except SyntaxError as exc:
                raise ScriptError("syntax error in %s line %s: %s"
                                  % (name, exc.lineno, exc.msg)) from None
            if len(self._compiled) > 64:        # per-point hooks run thousands of times
                self._compiled.clear()
            self._compiled[key] = compiled
        ns = self.namespace(name)
        if extra:
            ns.update(extra)
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
        except (AbortRequested, SkipPoint, SkipLine, EndSweep):
            raise
        except SystemExit as exc:
            raise ScriptError("%s called exit(%s)" % (name, exc.code)) from None
        except BaseException as exc:  # noqa: B902
            tb = traceback.format_exc(limit=8)
            log.error("script %s failed:\n%s", name, tb)
            raise ScriptError("%s: %s: %s" % (name, type(exc).__name__, exc)) from None
        finally:
            sys.settrace(old)
