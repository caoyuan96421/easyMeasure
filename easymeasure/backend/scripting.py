"""Execution of user scripts (queue items, pre/post sweep hooks).

Scripts are plain Python run in the backend with a small API in their namespace:

=====================  =========================================================
``get(name)``          read any readable channel (``Vg``, ``gates.eps``,
                       ``dmm.V``, a parameter, ``@time``)
``set(name, value)``   write any writable channel: an alias with its limits and
                       ramp, ``src.V1`` as it is, a parameter; ``set('dmm.reset')``
                       or ``set('zero_gates')`` runs an action
``set_many({n: v})``   write several together (e.g. variables of one composite)
``sleep(s)``           abortable sleep (always use this instead of time.sleep)
``wait_until(f, timeout, interval)``  poll ``f()`` until true
``check()``            honour pause / abort
``log(msg)``/``print`` write to the backend log
``instrument(name)``   guarded instrument proxy: ``.raw_query``, ``.raw_write``, ``.call``
``run_sweep(defn)``    run a sweep definition synchronously; returns its DataSet
                       (``.status``, ``.data``, ``.setpoints``, ``.path``)
``load_sweep(path)``   a sweep definition (dict) from a JSON file saved by the sweep
                       editor, or from a data file
``params``             the setup's parameters: ``params.Cb``, ``params['Cb']``; each
                       parameter is also available by its name (value at the start)
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

    @property
    def state(self) -> str:
        return self._h.state

    def __repr__(self):
        return "<instrument %s>" % self._h.name


def load_sweep(path) -> dict:
    """A sweep definition from *path*: a ``.json`` file written by the sweep editor's
    *Save…*, or a data file (``.emj``, ``.h5``), whose header holds the definition of
    the sweep that made it.  Returns a fresh dict, free to change before
    ``run_sweep()``."""
    import json
    from pathlib import Path
    p = Path(path).expanduser()
    if p.suffix.lower() == ".json":
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("definition", d) if isinstance(d, dict) else d
    from .storage import read_header
    return dict(read_header(p)["definition"])


class _Params:
    """The setup's parameters in scripts and actions: ``params.Cb`` or
    ``params['Cb']`` (the present value), ``dict(params)``.  ``params.Cb = 0.7``
    sets a value parameter (like ``set('Cb', 0.7)``)."""

    def __init__(self, channels):
        object.__setattr__(self, "_ch", channels)

    def _vals(self) -> dict:
        f = getattr(self._ch, "parameter_values", None)
        return f() if f is not None else {}

    def __getitem__(self, name):
        v = self._vals()
        if name not in v:
            raise KeyError("no parameter %r" % name)
        return v[name]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(str(exc)) from None

    def __setattr__(self, name, value):
        if self._ch.kind(name) != "parameter":
            raise ScriptError("no parameter %r" % name)
        self._ch.set(name, value)

    __setitem__ = __setattr__

    def __contains__(self, name):
        return name in self._vals()

    def __iter__(self):
        return iter(self._vals())

    def keys(self):
        return self._vals().keys()

    def __len__(self):
        return len(self._vals())

    def __repr__(self):
        return "params(%s)" % ", ".join("%s=%g" % kv for kv in self._vals().items())


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

        def _set(n, v=None, ramp=True):
            ctl.check()
            return self.channels.set(n, v, ramp=ramp, abort_check=ctl.check)

        def _set_many(values, ramp=True):
            ctl.check()
            return self.channels.set_many(values, ramp=ramp, abort_check=ctl.check)

        def _get(n):
            ctl.check()
            return self.channels.get(n)

        def _instrument(n):
            return _InstrumentProxy(self.instruments.get(n))

        def _point_only(fn):
            def f():
                raise ScriptError("%s() only works in a sweep's per-point actions" % fn)
            return f

        def _run_sweep(defn):
            if self.run_sweep is None:
                raise ScriptError("run_sweep not available here")
            return self.run_sweep(defn)

        ns = {
            "__name__": "__easymeasure_script__", "load_sweep": load_sweep,
            "get": _get, "set": _set, "set_many": _set_many, "sleep": ctl.sleep,
            "check": ctl.check, "wait_until": wait_until, "log": slog.info,
            "warn": slog.warning, "print": _print, "instrument": _instrument,
            "run_sweep": _run_sweep, "np": np, "math": math, "time": time,
            "state": self.state, "AbortRequested": AbortRequested, "skip_point": _point_only("skip_point"), "skip_line": _point_only("skip_line"),
            "end_sweep": _point_only("end_sweep"),
            "params": _Params(self.channels),
        }
        # parameters by their name too (values at the start), unless the name is taken
        import builtins
        for n, v in self._param_values().items():
            if n not in ns and not hasattr(builtins, n):
                ns[n] = v
        return ns

    def _param_values(self) -> dict:
        f = getattr(self.channels, "parameter_values", None)
        return f() if f is not None else {}

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


#: script functions whose first argument is a channel name
_CHANNEL_FUNCS = {"get": "r", "set": "w"}


def check_code(code: str, channels, what: str = "code") -> dict:
    """Check Python code for the code fields (scripts, user actions, point actions)
    without running it: ``{ok, errors, warnings}``.  An error is a syntax error;
    a warning is a quoted channel that does not exist (``get('Vxx')``,
    ``set_many({'nD.n': 0})``) - it may be defined before the code runs - or that
    cannot be read / written that way (``get('dmm.reset')``, ``set('Rxx', 1)``)."""
    import ast
    errors, warnings = [], []
    try:
        tree = ast.parse(code or "", "<%s>" % what, "exec")
        compile(tree, "<%s>" % what, "exec")
    except SyntaxError as exc:
        line = (exc.text or "").strip()
        errors.append("line %s: %s%s" % (exc.lineno, exc.msg, (": %s" % line) if line else ""))
        return {"ok": False, "errors": errors, "warnings": warnings}
    seen = set()

    def chan(node, line, need):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            n = node.value
            if not n or (n, need) in seen:
                return
            seen.add((n, need))
            if not channels.exists(n):
                warnings.append("line %d: no channel %r (yet)" % (line, n))
                return
            try:
                acc = channels.info(n)["access"]
            except Exception:
                return
            if need not in acc:
                warnings.append("line %d: %s is %s: it cannot be %s" % (
                    line, n, "read-only" if acc == "r" else "write-only" if acc == "w"
                    else "not accessible", "read" if need == "r" else "written"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fn = node.func.id
            if fn in _CHANNEL_FUNCS and node.args:
                chan(node.args[0], node.lineno, _CHANNEL_FUNCS[fn])
            elif fn == "set_many" and node.args and isinstance(node.args[0], ast.Dict):
                for k in node.args[0].keys:
                    if k is not None:
                        chan(k, node.lineno, "w")
    return {"ok": True, "errors": errors, "warnings": warnings}
