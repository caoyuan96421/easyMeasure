"""Generic multi-dimensional sweep definition and execution engine.

Definition (JSON-compatible dict)::

    {
      "name": "gate_bias_map",
      "dimensions": [                       # dimension 0 = innermost / fastest
        {"points": 101,
         "sweep":  [{"channel": "Vg", "start": -0.5, "stop": 0.5},
                    {"channel": "Vc", "start": 0, "stop": 1, "mapping": "start+(stop-start)*t**2"},
                    {"channel": "wave", "mapping": "np.sin(np.linspace(0, 2*np.pi, 64))*t"},
                    {"channel": "Vx", "values": [0, 0.1, 0.3, ...]}],
         "record": ["I", "spectrum"],
         "pre_delay": 0.05,                 # after setting, before recording
         "post_delay": 0.0,                 # after recording
         "snake": false,                    # alternate direction on each pass
         "record_after_inner": false,       # (outer dims) record after the inner loop
         "hooks": {"pre_record": {"enabled": true, "code": "action('dmm.trigger')"}}},
        {"points": 21, "sweep": [{"channel": "Vb", "start": -0.3, "stop": 0.3}],
         "record": ["T"]}
      ],
      "pre":  {"hook": "log('starting')", "constants": [{"channel": "mode", "value": "AC"}],
               "record": ["T"]},
      "post": {"record": ["T"], "constants": [{"channel": "Vg", "value": 0}], "hook": ""},
      "on_read_error": "nan",               # or "abort"
      "on_set_error": "abort",              # or "skip"
      "run_post_on_abort": true,
      "metadata": {"sample": "D17", "comment": "..."}
    }

Every point of a dimension runs, in this order::

    pre_sweep hook -> set the swept channels -> post_sweep hook -> pre_delay
      -> pre_record hook -> record -> post_record hook -> post_delay
      (-> the inner dimensions; with record_after_inner they run before pre_record)

The four hooks are optional Python snippets (like the pre/post-sweep hook, see
:mod:`.scripting`), each switched on with ``enabled``.  Besides the script API they
see ``k`` (dimension), ``i`` (index in it), ``index`` (indices of this and the outer
dimensions), ``setpoints`` (the dimension's channel -> value at this point) and,
in ``post_record``, ``values`` (what was just recorded).

Three special calls change the flow (think of nested ``for`` loops):

``skip_point()`` (``continue``) ends the point at once: the rest of its sequence
    (and, unless they already ran, its inner dimensions) is skipped, the data
    recorded at this point stay NaN, and the sweep continues with the next point
    of the same dimension.
``skip_line()`` (``break``) does the same for this point *and all remaining
    points of this dimension's line*; the outer dimension continues (for the
    outermost dimension the sweep is done).
``end_sweep()`` (``exit``) stops the whole sweep: status ``terminated``, the
    post-sweep phase still runs, unvisited points stay NaN.  Called in
    ``post_record`` it keeps the point just recorded (e.g. stop when a threshold
    is crossed and see that point); the skips discard it.

Variables of one multivariate composite are set together.  When they are swept in
different dimensions (``n`` inner, ``D`` outer), the outer ones are not written on
their own: they are set together with the inner ones at every inner point, so the
hardware only ever sees whole combinations.  A combination that would put a target
of the composite out of range is skipped (``skip_point()`` before anything is set)
when the composite's ``out_of_range`` is ``"skip"``; validation counts such points
and, for ``"error"``, refuses the sweep.

Mapping expressions may use ``i`` (index), ``N`` (points), ``t`` = i/(N-1),
``start``, ``stop``, ``x`` (= linear value between start and stop) and numpy.
A mapping is mandatory for vector/tensor channels.

Recorded data of a channel recorded in dimension ``k`` is assembled into an
array of shape ``(N_k, ..., N_{D-1}) + value_shape``.  Special record channel:
``@time`` (unix time of the recording event), ``@elapsed`` (seconds since start).
"""

from __future__ import annotations

import copy
from pathlib import Path
import math
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from ..common.errors import (AbortRequested, ChannelError, EasyMeasureError, InstrumentFault,
                             LimitError,
                             EndSweep, OutOfRange, SkipLine, SkipPoint, SweepDefinitionError)
from ..common.exprs import Expression
from .logsys import get_logger
from .storage import EVENT, FOOTER, HEADER, PHASE, POINT, DataSet, JournalWriter

log = get_logger("sweep")


# ---------------------------------------------------------------------------
# Definition handling
# ---------------------------------------------------------------------------
#: size limits (protect the backend's memory against absurd or hostile definitions)
MAX_POINTS_PER_DIM = 10_000_000
MAX_TOTAL_POINTS = 100_000_000
MAX_SETPOINT_ELEMENTS = 20_000_000
#: combinations of composite variables checked in advance by validation: all at once
#: (vectorised) when the composite drives aliases directly, else one by one
MAX_COMBINATIONS_VECTOR = 5_000_000
MAX_COMBINATIONS_CHECKED = 250_000
#: per-point hooks of a dimension, in the order they run
POINT_HOOKS = ("pre_sweep", "post_sweep", "pre_record", "post_record")


def _normalize_hooks(hooks, k: int) -> dict:
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise SweepDefinitionError("dimension %d: 'hooks' must be a dict" % k)
    unknown = [h for h in hooks if h not in POINT_HOOKS]
    if unknown:
        raise SweepDefinitionError("dimension %d: unknown hook(s) %s (use %s)"
                                   % (k, ", ".join(map(str, unknown)), ", ".join(POINT_HOOKS)))
    out = {}
    for name in POINT_HOOKS:
        h = hooks.get(name)
        if h is None:
            h = {}
        elif isinstance(h, str):
            h = {"enabled": bool(h.strip()), "code": h}
        if not isinstance(h, dict):
            raise SweepDefinitionError("dimension %d: hook %s must be code or "
                                       "{'enabled': ..., 'code': ...}" % (k, name))
        code = h.get("code", "")
        if not isinstance(code, str):
            raise SweepDefinitionError("dimension %d: hook %s code must be text" % (k, name))
        out[name] = {"enabled": h.get("enabled", False) is True, "code": code}
    return out


def active_hooks(dim: dict) -> Dict[str, str]:
    """``name -> code`` of the hooks of a (normalised) dimension that will run."""
    return {n: h["code"] for n, h in (dim.get("hooks") or {}).items()
            if h.get("enabled") and h.get("code", "").strip()}


def normalize_definition(d: dict) -> dict:
    """Fill defaults and check structure (without consulting the instruments)."""
    if not isinstance(d, dict):
        raise SweepDefinitionError("sweep definition must be a dict")
    d = copy.deepcopy(d)
    d.setdefault("name", "sweep")
    d["name"] = str(d["name"]) or "sweep"
    dims = d.get("dimensions")
    if not isinstance(dims, list) or not dims:
        raise SweepDefinitionError("sweep needs at least one dimension")
    for k, dim in enumerate(dims):
        if not isinstance(dim, dict):
            raise SweepDefinitionError("dimension %d must be a dict" % k)
        sweep = dim.setdefault("sweep", [])
        dim.setdefault("record", [])
        if "points" not in dim:
            # infer from explicit values lists
            lens = [len(s["values"]) for s in sweep if isinstance(s, dict) and "values" in s]
            if not lens:
                raise SweepDefinitionError("dimension %d: 'points' missing" % k)
            dim["points"] = lens[0]
        try:
            dim["points"] = int(dim["points"])
        except (TypeError, ValueError):
            raise SweepDefinitionError("dimension %d: points must be an integer" % k) from None
        if dim["points"] < 1:
            raise SweepDefinitionError("dimension %d: points must be >= 1" % k)
        if dim["points"] > MAX_POINTS_PER_DIM:
            raise SweepDefinitionError("dimension %d: at most %d points per dimension"
                                       % (k, MAX_POINTS_PER_DIM))
        for key in ("pre_delay", "post_delay"):
            try:
                v = float(dim.get(key, 0.0) or 0.0)
            except (TypeError, ValueError):
                raise SweepDefinitionError("dimension %d: %s must be a number" % (k, key)) from None
            if v < 0 or not math.isfinite(v):
                raise SweepDefinitionError("dimension %d: %s must be >= 0" % (k, key))
            dim[key] = v
        dim["snake"] = bool(dim.get("snake", False))
        dim["record_after_inner"] = bool(dim.get("record_after_inner", False))
        dim.setdefault("label", "")
        dim["hooks"] = _normalize_hooks(dim.get("hooks"), k)
        if not isinstance(sweep, list):
            raise SweepDefinitionError("dimension %d: 'sweep' must be a list" % k)
        for j, s in enumerate(sweep):
            if not isinstance(s, dict) or not s.get("channel"):
                raise SweepDefinitionError("dimension %d sweep entry %d needs a 'channel'" % (k, j))
            has_lin = "start" in s and "stop" in s
            if not (has_lin or "values" in s or s.get("mapping")):
                raise SweepDefinitionError(
                    "dimension %d: %s needs start/stop, values or mapping" % (k, s["channel"]))
        if not isinstance(dim["record"], list) or not all(isinstance(r, str) for r in dim["record"]):
            raise SweepDefinitionError("dimension %d: 'record' must be a list of names" % k)
    total = 1
    for dim in dims:
        total *= dim["points"]
    if total > MAX_TOTAL_POINTS:
        raise SweepDefinitionError("sweep has %d points; the limit is %d" % (total, MAX_TOTAL_POINTS))
    for ph in ("pre", "post"):
        p = d.setdefault(ph, {}) or {}
        d[ph] = p
        p.setdefault("hook", "")
        p.setdefault("constants", [])
        p.setdefault("record", [])
        for c in p["constants"]:
            if not isinstance(c, dict) or "channel" not in c:
                raise SweepDefinitionError("%s constants need a channel (and a value, but for "
                                           "an action)" % ph)
            c.setdefault("value", None)
    if d.setdefault("on_read_error", "nan") not in ("nan", "abort"):
        raise SweepDefinitionError("on_read_error must be 'nan' or 'abort'")
    if d.setdefault("on_set_error", "abort") not in ("abort", "skip"):
        raise SweepDefinitionError("on_set_error must be 'abort' or 'skip'")
    d["run_post_on_abort"] = bool(d.get("run_post_on_abort", True))
    d.setdefault("metadata", {})
    d.setdefault("ppt", True)
    # every recorded name must be unique across the sweep
    seen = {}
    for k, dim in enumerate(dims):
        for r in dim["record"]:
            if r in seen:
                raise SweepDefinitionError("channel %r recorded in dimensions %d and %d"
                                           % (r, seen[r], k))
            seen[r] = k
    return d


def compute_setpoints(d: dict) -> Dict[int, Dict[str, Any]]:
    """Evaluate all setpoints up front -> ``{dim: {channel: array or list}}``."""
    out: Dict[int, Dict[str, Any]] = {}
    for k, dim in enumerate(d["dimensions"]):
        n = dim["points"]
        out[k] = {}
        for s in dim["sweep"]:
            ch = s["channel"]
            if "values" in s and not s.get("mapping"):
                vals = list(s["values"])
                if len(vals) != n:
                    raise SweepDefinitionError("%s: %d values given but dimension %d has %d points"
                                               % (ch, len(vals), k, n))
                if all(isinstance(v, str) for v in vals):
                    raise SweepDefinitionError("%s: string channels cannot be swept" % ch)
                try:
                    arr = np.asarray(vals, dtype=float)
                except (TypeError, ValueError):
                    raise SweepDefinitionError("%s: values must be numeric" % ch) from None
                out[k][ch] = arr
                continue
            start = s.get("start", 0.0)
            stop = s.get("stop", 0.0)
            try:
                start_f, stop_f = float(start), float(stop)
            except (TypeError, ValueError):
                start_f, stop_f = 0.0, 0.0
            if s.get("mapping"):
                expr = Expression(s["mapping"])
                rows = []
                for i in range(n):
                    t = i / (n - 1) if n > 1 else 0.0
                    x = start_f + (stop_f - start_f) * t
                    v = expr.evaluate({"i": i, "N": n, "t": t, "start": start, "stop": stop,
                                       "x": x})
                    if isinstance(v, str):
                        raise SweepDefinitionError("%s: mapping returned a string" % ch)
                    if np.size(v) * n > MAX_SETPOINT_ELEMENTS:
                        raise SweepDefinitionError("%s: mapping produces more than %d values"
                                                   % (ch, MAX_SETPOINT_ELEMENTS))
                    try:
                        rows.append(np.asarray(v, dtype=float))
                    except (TypeError, ValueError):
                        raise SweepDefinitionError("%s: mapping must return numbers" % ch) from None
                shapes = {r.shape for r in rows}
                if len(shapes) != 1:
                    raise SweepDefinitionError("%s: mapping returns inconsistent shapes %s"
                                               % (ch, sorted(shapes)))
                if rows and rows[0].size * len(rows) > MAX_SETPOINT_ELEMENTS:
                    raise SweepDefinitionError("%s: mapping produces more than %d values"
                                               % (ch, MAX_SETPOINT_ELEMENTS))
                out[k][ch] = np.stack(rows)
            else:
                if not all(np.isfinite([start_f, stop_f])):
                    raise SweepDefinitionError("%s: start/stop must be finite" % ch)
                out[k][ch] = np.linspace(start_f, stop_f, n)
            if not np.all(np.isfinite(out[k][ch])):
                raise SweepDefinitionError("%s: setpoints contain NaN/inf" % ch)
    return out


def _instruments_of(channels, name: str) -> set:
    """Instruments behind an alias / composite / raw reference (best effort)."""
    try:
        return channels.instruments_behind(name)
    except Exception:
        return set()


def _changes_instrument(channels, ch: str) -> Optional[str]:
    """The instrument a constant writes directly (a channel or action of it), which
    may change its settings and channel list; None otherwise."""
    try:
        k = channels.kind(ch)
    except Exception:
        k = None
    if k in ("hardware", "config", "action"):
        return ch.split(".", 1)[0]
    return None


def _pre_configured(d: dict, channels) -> set:
    """Instruments whose settings the pre-sweep constants change (written directly)."""
    out = set()
    for c in d["pre"]["constants"]:
        inst = _changes_instrument(channels, c["channel"])
        if inst:
            out.add(inst)
    return out


def composite_dims(d: dict, channels) -> Dict[str, Dict[int, List[str]]]:
    """For every multivariate composite whose variables are swept: dimension ->
    the variables swept there."""
    out: Dict[str, Dict[int, List[str]]] = {}
    if not hasattr(channels, "group_of"):
        return out
    for k, dim in enumerate(d["dimensions"]):
        for s in dim["sweep"]:
            try:
                g = channels.group_of(s["channel"])
            except Exception:
                g = None
            if g is not None:
                out.setdefault(g, {}).setdefault(k, []).append(s["channel"])
    return out


def validate_against(d: dict, channels, after_pre: bool = False,
                     abort_check=None) -> List[str]:
    """Check channel existence/direction and limits for every setpoint.

    Returns a list of warnings; raises :class:`SweepDefinitionError` on errors.

    A pre-sweep constant may change an instrument's settings and with them its
    channel list (e.g. switch a source-measure unit from voltage to current
    source).  Problems with channels of such an instrument are then only warnings
    here; the runner checks again with ``after_pre=True`` once the pre-sweep
    constants are applied, and stops before the first point if they remain.
    """
    warnings: List[str] = []
    errors: List[str] = []
    setpoints = compute_setpoints(d)
    deferred = set() if after_pre else _pre_configured(d, channels)
    comps = composite_dims(d, channels)
    skipped: Dict[str, int] = {}          # composite -> points of the sweep it will skip
    total_points = int(np.prod([dim["points"] for dim in d["dimensions"]]))

    def problem(name: str, text: str) -> None:
        insts = _instruments_of(channels, name) & deferred
        if insts:
            warnings.append("%s (checked again after the pre-sweep constants change the "
                            "settings of %s)" % (text, ", ".join(sorted(insts))))
        else:
            errors.append(text)

    for k, dim in enumerate(d["dimensions"]):
        for s in dim["sweep"]:
            ch = s["channel"]
            try:
                info = channels.info(ch)
            except Exception as exc:
                problem(ch, "sweep channel %s: %s" % (ch, exc))
                continue
            if not info["writable"]:
                problem(ch, "sweep channel %s is read-only" % ch)
                continue
            if info["kind"] in ("action", "user_action"):
                errors.append("sweep channel %s is an action: it has no value to sweep (run it "
                              "as a constant or from a point action)" % ch)
                continue
            if info["dtype"] == "str":
                errors.append("string channel %s cannot be swept" % ch)
                continue
            sp = setpoints[k][ch]
            shape = info.get("shape")
            if shape not in (None, []) and sp.ndim == 1:
                errors.append("%s is a vector/tensor channel: a mapping is required" % ch)
                continue
            if shape == [] and sp.ndim > 1:
                errors.append("%s is a scalar channel but its mapping returns arrays" % ch)
                continue
            if info["kind"] == "variable" and \
                    sum(len(v) for v in comps.get(info["composite"], {}).values()) > 1:
                continue          # checked together with the other variables below
            try:
                for v in sp:
                    # partial: a variable of the same composite that is not swept
                    # may not be known yet
                    try:
                        channels.check_value(ch, v if np.ndim(v) else float(v), partial=True)
                    except OutOfRange as exc:
                        if exc.mode != "skip":
                            raise
                        # every point of the other dimensions with this value is lost
                        skipped[exc.composite] = skipped.get(exc.composite, 0) + \
                            total_points // dim["points"]
            except LimitError as exc:
                errors.append(str(exc))
            except ChannelError as exc:
                problem(ch, str(exc))
        for name, code in active_hooks(dim).items():
            try:
                compile(code, "<%s-hook>" % name, "exec")
            except SyntaxError as exc:
                errors.append("dimension %d %s hook: syntax error line %s: %s"
                              % (k, name, exc.lineno, exc.msg))
        for r in dim["record"]:
            if r in ("@time", "@elapsed"):
                continue
            try:
                info = channels.info(r)
                if not info["readable"]:
                    problem(r, "record channel %s is not readable" % r)
            except Exception as exc:
                problem(r, "record channel %s: %s" % (r, exc))
    # variables of one composite swept together (in one or several dimensions): every
    # combination that the sweep will set is checked
    for g, by_dim in comps.items():
        names = [n for k in sorted(by_dim) for n in by_dim[k]]
        if len(names) < 2:
            continue
        ks = sorted(by_dim)
        sizes = [d["dimensions"][k]["points"] for k in ks]
        total = int(np.prod(sizes))
        lost_per = total_points // total     # sweep points per combination
        mask = None
        if total <= MAX_COMBINATIONS_VECTOR and hasattr(channels, "grid_out_of_range"):
            grids = {}
            for j, k in enumerate(ks):
                sh = [1] * len(ks)
                sh[j] = sizes[j]
                for n in by_dim[k]:
                    a = np.asarray(setpoints[k][n], dtype=float)
                    if a.ndim != 1:
                        grids = None
                        break
                    grids[n] = np.broadcast_to(a.reshape(sh), sizes)
                if grids is None:
                    break
            if grids:
                try:
                    mask = channels.grid_out_of_range(grids)
                except Exception:
                    mask = None
            if mask is not None and mask.any() and channels.out_of_range_mode(g) != "skip":
                mask = None                  # one by one, for the exact error
        if mask is not None:
            n_bad = int(mask.sum())
            if n_bad:
                skipped[g] = skipped.get(g, 0) + n_bad * lost_per
            continue
        if total > MAX_COMBINATIONS_CHECKED:
            warnings.append("composite %s: %d combinations of %s are too many to check in "
                            "advance; each is checked when it is reached" % (g, total,
                                                                             ", ".join(names)))
            continue
        try:
            for flat in range(total):
                if abort_check is not None and flat % 1000 == 0:
                    abort_check()
                idx = np.unravel_index(flat, sizes)
                values = {n: float(setpoints[k][n][i]) for k, i in zip(ks, idx)
                          for n in by_dim[k]}
                try:
                    channels.check_many(values, partial=True)
                except OutOfRange as exc:
                    if exc.mode != "skip":
                        raise
                    # every point of the other dimensions with this combination is lost
                    skipped[g] = skipped.get(g, 0) + lost_per
        except LimitError as exc:
            errors.append(str(exc))
        except ChannelError as exc:
            problem(names[0], str(exc))
    for g, n in sorted(skipped.items()):
        warnings.append("composite %s: %d of %d points out of range, will be skipped"
                        % (g, n, total_points))
    for ph in (("post",) if after_pre else ("pre", "post")):
        # a constant may depend on an earlier one of the same phase (mode first, then
        # the compliance of that mode): those are only checked when they are applied
        changed = set(deferred) if ph == "post" else set()
        for c in d[ph]["constants"]:
            ch = c["channel"]
            try:
                channels.check_value(ch, c["value"])
            except LimitError as exc:
                errors.append("%s constant %s: %s" % (ph, ch, exc))
            except Exception as exc:
                text = "%s constant %s: %s" % (ph, ch, exc)
                if _instruments_of(channels, ch) & changed:
                    warnings.append(text + " (checked when applied: an earlier constant changes "
                                    "the settings of that instrument)")
                else:
                    errors.append(text)
            inst = _changes_instrument(channels, ch)
            if inst:
                changed.add(inst)
        for r in d[ph]["record"]:
            if not channels.exists(r):
                text = "%s record channel %s does not exist" % (ph, r)
                if _instruments_of(channels, r) & changed or \
                        (("." in r) and r.split(".", 1)[0] in changed):
                    warnings.append(text + " (yet)")
                else:
                    errors.append(text)
            elif not channels.info(r)["readable"]:
                errors.append("%s record channel %s cannot be read" % (ph, r))
        if d[ph]["hook"]:
            try:
                compile(d[ph]["hook"], "<%s-hook>" % ph, "exec")
            except SyntaxError as exc:
                errors.append("%s hook: syntax error line %s: %s" % (ph, exc.lineno, exc.msg))
    if errors:
        raise SweepDefinitionError("; ".join(errors))
    total = int(np.prod([dim["points"] for dim in d["dimensions"]]))
    if total > 10_000_000:
        warnings.append("very large sweep (%d points)" % total)
    return warnings


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
class RunControl:
    """Pause / abort flags shared between the queue and running work."""

    USER = "aborted by user"

    def __init__(self):
        self._abort = threading.Event()
        self._running = threading.Event()
        self._running.set()          # set = not paused
        self.pause_requested = False
        self.reason = self.USER

    def abort(self, reason: Optional[str] = None) -> None:
        """Abort; *reason* (default: by the user) is what the run reports."""
        if reason or not self._abort.is_set():
            self.reason = reason or self.USER
        self._abort.set()
        self._running.set()

    def pause(self) -> None:
        self.pause_requested = True
        self._running.clear()

    def resume(self) -> None:
        self.pause_requested = False
        self._running.set()

    def reset(self) -> None:
        self._abort.clear()
        self.reason = self.USER
        self.resume()

    def check_abort(self) -> None:
        """Non-blocking: raise AbortRequested if an abort was requested."""
        if self._abort.is_set():
            raise AbortRequested(self.reason)

    @property
    def aborted(self) -> bool:
        return self._abort.is_set()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def check(self, on_pause: Optional[Callable[[bool], None]] = None) -> None:
        """Raise AbortRequested if aborted; block while paused."""
        if self._abort.is_set():
            raise AbortRequested(self.reason)
        if not self._running.is_set():
            if on_pause:
                on_pause(True)
            while not self._running.wait(0.1):
                if self._abort.is_set():
                    break
            if on_pause:
                on_pause(False)
            if self._abort.is_set():
                raise AbortRequested(self.reason)

    def sleep(self, seconds: float) -> None:
        """Abortable sleep (pause does not stop the clock of a delay)."""
        end = time.monotonic() + max(0.0, seconds)
        while True:
            if self._abort.is_set():
                raise AbortRequested(self.reason)
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            self._abort.wait(min(remaining, 0.05))


class _OutOfRangeSkip(SkipPoint):
    """A combination of composite variables out of range (``out_of_range="skip"``)."""


def _skip_point() -> None:
    """In a per-point action: skip the rest of this point (its data stay NaN)."""
    raise SkipPoint()


def _skip_line() -> None:
    """In a per-point action: skip the rest of this dimension's line."""
    raise SkipLine()


def _end_sweep() -> None:
    """In a per-point action: stop the sweep (status 'terminated')."""
    raise EndSweep()


_FLOW = {"skip_point": _skip_point, "skip_line": _skip_line, "end_sweep": _end_sweep}


class SweepRunner:
    """Executes one sweep definition, streaming data to a journal and listeners."""

    def __init__(self, definition: dict, channels, control: RunControl, path,
                 run_number: int, emit: Callable[[str, dict], None],
                 script_runner: Optional[Callable[[str, str], None]] = None,
                 fsync_interval: float = 0.5, extra_header: Optional[dict] = None):
        self.d = normalize_definition(definition)
        self.channels = channels
        self.control = control
        self.path = path
        self.run_number = run_number
        self.emit = emit
        self.script_runner = script_runner
        self.fsync_interval = fsync_interval
        self.extra_header = extra_header or {}
        self.setpoints = compute_setpoints(self.d)
        # swept channels in order; variables of one multivariate composite are set
        # together at the place of the first of them.  Each entry: (channels,
        # composite or None, deferred): a deferred group belongs to a composite also
        # swept in an inner dimension and is set together with that one
        self._set_groups: List[List[tuple]] = []
        comps = composite_dims(self.d, channels)
        innermost = {g: min(by_dim) for g, by_dim in comps.items()}
        for k, dim in enumerate(self.d["dimensions"]):
            groups: List[tuple] = []
            where: Dict[str, List[str]] = {}
            for s in dim["sweep"]:
                ch = s["channel"]
                g = channels.group_of(ch) if hasattr(channels, "group_of") else None
                if g is not None and g in where:
                    where[g].append(ch)
                    continue
                names = [ch]
                groups.append((names, g, g is not None and innermost[g] < k))
                if g is not None:
                    where[g] = names
            self._set_groups.append(groups)
        self._pending: Dict[str, Dict[str, Any]] = {}   # composite -> outer variables
        self.shape = [dim["points"] for dim in self.d["dimensions"]]
        self.dataset: Optional[DataSet] = None
        self.journal: Optional[JournalWriter] = None
        self.t_start = 0.0
        self.status = "pending"
        self.error = ""
        self.read_failures = 0
        self.storage_errors = 0
        self._pass_counts = [0] * len(self.shape)
        self._hooks = [active_hooks(dim) for dim in self.d["dimensions"]]
        self.skipped_points = 0
        self.out_of_range_skipped = 0
        self._ended_at: Optional[dict] = None
        if any(self._hooks) and script_runner is None:
            raise EasyMeasureError("this sweep has point hooks but no script runner")

    # -- helpers ------------------------------------------------------------------
    def _emit(self, kind: str, payload: dict) -> None:
        try:
            self.emit(kind, payload)
        except Exception:
            pass

    def _journal(self, ftype: int, obj: dict, sync: bool = False) -> None:
        if self.journal is None:
            return
        try:
            self.journal.write(ftype, obj, sync=sync)
        except Exception as exc:
            # Never lose the measurement because of storage: the journal keeps the
            # frame and retries; the sweep goes on with the data also in memory.
            self.storage_errors += 1
            if self.storage_errors == 1 or self.storage_errors % 100 == 0:
                log.error("DATA STORAGE PROBLEM for run %d (%d so far): %s",
                          self.run_number, self.storage_errors, exc)
                self._emit("sweep_warning", {"run": self.run_number,
                                             "message": "storage problem: %s" % exc})

    def _close_journal(self) -> None:
        j = self.journal
        if j is None:
            return
        try:
            j.close()
        except Exception as exc:
            log.error("run %d: closing journal failed: %s", self.run_number, exc)
        lost = j.pending or j.dropped
        if not lost and self.storage_errors:
            # frames may have failed before reaching the journal (e.g. encoding):
            # verify the file against the in-memory data
            try:
                from .storage import DataSet as _DS
                lost = _DS.load(self.path).points < self.dataset.points
            except Exception:
                lost = True
        if lost:
            # some frames never reached the journal: save the complete in-memory
            # dataset elsewhere so nothing is lost
            from .storage import export_hdf5
            import os
            import tempfile
            candidates = [Path(self.path).with_name(Path(self.path).stem + "_recovered.h5"),
                          Path(tempfile.gettempdir()) / ("easymeasure_run%05d_recovered.h5"
                                                         % self.run_number)]
            for target in candidates:
                try:
                    export_hdf5(self.dataset, target)
                    log.error("run %d: %d frame(s) could not be written to %s; the complete "
                              "data was saved to %s", self.run_number, j.pending + j.dropped,
                              self.path, target)
                    self._emit("sweep_warning", {"run": self.run_number,
                                                 "message": "data saved to %s" % target})
                    break
                except Exception as exc:
                    log.error("run %d: recovery save to %s failed: %s", self.run_number,
                              target, exc)

    def _read(self, name: str):
        if name == "@time":
            return time.time()
        if name == "@elapsed":
            return time.time() - self.t_start
        return self.channels.get(name)

    def _record(self, names: List[str]) -> tuple:
        values, errors = {}, {}
        # composite variables of an outer dimension, set with the inner ones: their
        # value is the setpoint about to be set, not the one the hardware still has
        pending = {n: v for g in self._pending.values() for n, v in g.items()}
        for name in names:
            self.control.check()
            if name in pending:
                values[name] = pending[name]
                continue
            try:
                v = self._read(name)
                values[name] = v
            except (AbortRequested, InstrumentFault):
                raise
            except Exception as exc:
                self.read_failures += 1
                errors[name] = str(exc)
                log.warning("run %d: reading %s failed: %s", self.run_number, name, exc)
                if self.d["on_read_error"] == "abort":
                    raise
        return values, errors

    def _set_many(self, values: dict) -> None:
        try:
            self.channels.set_many(values, ramp=True, abort_check=self.control.check)
        except (AbortRequested, InstrumentFault):
            raise
        except Exception as exc:
            if self.d["on_set_error"] == "skip":
                log.warning("run %d: setting %s failed (skipped): %s", self.run_number,
                            ", ".join(values), exc)
                return
            raise

    def _set(self, name: str, value) -> None:
        try:
            self.channels.set(name, value, ramp=True, abort_check=self.control.check)
        except (AbortRequested, InstrumentFault):
            raise
        except Exception as exc:
            if self.d["on_set_error"] == "skip":
                log.warning("run %d: setting %s failed (skipped): %s", self.run_number, name, exc)
                return
            raise

    def _hook(self, phase: str) -> None:
        code = self.d[phase].get("hook") or ""
        if code.strip():
            if self.script_runner is None:
                raise EasyMeasureError("no script runner for hooks")
            log.info("run %d: running %s-sweep hook", self.run_number, phase)
            self.script_runner(code, "%s-hook" % phase)

    def _point_hook(self, k: int, name: str, ctx: dict) -> None:
        code = self._hooks[k].get(name)
        if code is not None:
            try:
                self.script_runner(code, "dim%d-%s" % (k, name), dict(ctx, **_FLOW))
            except EndSweep:
                # where the sweep was ended: logged, journaled and kept in the footer
                self._ended_at = {"dimension": k + 1, "action": name,
                                  "index": [int(x) for x in ctx["index"]]}
                raise

    def _phase(self, phase: str) -> None:
        p = self.d[phase]
        if phase == "pre":
            self._hook("pre")
        if phase == "post":
            vals, errs = self._record(p["record"]) if p["record"] else ({}, {})
            self._store_phase("post", vals, errs)
        for c in p["constants"]:
            self.control.check()
            if c["value"] is None:
                log.info("run %d: %s: %s", self.run_number, phase, c["channel"])
            else:
                log.info("run %d: %s constant %s = %r", self.run_number, phase, c["channel"],
                         c["value"])
            self._set(c["channel"], c["value"])
        if phase == "pre":
            vals, errs = self._record(p["record"]) if p["record"] else ({}, {})
            self._store_phase("pre", vals, errs)
        if phase == "post":
            self._hook("post")

    def _value_axes(self) -> None:
        """Coordinates of the value axes of the recorded array channels (VNA frequency
        axis …), read once before the first point and stored as the ``axes`` phase."""
        axes = {}
        for dim in self.d["dimensions"]:
            for n in dim["record"]:
                if n.startswith("@"):
                    continue
                try:
                    ax = self.channels.value_axes(n)
                except Exception as exc:
                    log.warning("value axes of %s: %s", n, exc)
                    continue
                if ax:
                    axes[n] = ax
        if axes:
            self._store_phase("axes", axes, {})

    def _store_phase(self, phase, vals, errs):
        if not vals and not errs:
            return
        self._journal(PHASE, {"phase": phase, "v": vals, "e": errs}, sync=True)
        if self.dataset is not None:
            self.dataset.apply_frame(PHASE, {"phase": phase, "v": vals})
        self._emit("sweep_phase", {"run": self.run_number, "phase": phase, "v": vals, "e": errs})

    # -- main loop -------------------------------------------------------------------
    def _parameters_snapshot(self) -> dict:
        if not hasattr(self.channels, "describe_parameters"):
            return {}
        return {p["name"]: {"value": p["current"], "unit": p.get("unit", ""),
                            "formula": p.get("formula") or None}
                for p in self.channels.describe_parameters()}

    def build_header(self) -> dict:
        dims = []
        for k, dim in enumerate(self.d["dimensions"]):
            dims.append({"points": dim["points"], "sweep": [s["channel"] for s in dim["sweep"]],
                         "record": list(dim["record"]), "label": dim.get("label", "")})
        units = {}
        for k, dim in enumerate(self.d["dimensions"]):
            for n in [s["channel"] for s in dim["sweep"]] + list(dim["record"]):
                try:
                    units[n] = self.channels.info(n).get("unit", "") if not n.startswith("@") else "s"
                except Exception:
                    units[n] = ""
        hdr = {
            "format": "easyMeasure-journal-1",
            "run": self.run_number,
            "name": self.d["name"],
            "definition": self.d,
            "shape": self.shape,
            "dims": dims,
            "setpoints": {str(k): v for k, v in self.setpoints.items()},
            "units": units,
            # the setup's parameters at the start: {name: {value, unit, formula}}
            "parameters": self._parameters_snapshot(),
            "started": time.time(),
            "status": "running",
        }
        hdr.update(self.extra_header)
        return hdr

    def run(self) -> str:
        """Run the sweep; returns final status (finished/aborted/error)."""
        self.t_start = time.time()
        header = self.build_header()
        self.dataset = DataSet(header)
        try:
            self.journal = JournalWriter(self.path, self.fsync_interval)
        except Exception as exc:
            log.error("cannot create data file %s: %s", self.path, exc)
            self.status, self.error = "error", "cannot create data file: %s" % exc
            self._emit("sweep_end", {"run": self.run_number, "status": self.status,
                                     "error": self.error, "path": str(self.path)})
            return self.status
        self._journal(HEADER, header, sync=True)
        self._emit("sweep_start", {"run": self.run_number, "path": str(self.path),
                                   "header": header})
        log.info("run %d (%s) started: shape %s -> %s", self.run_number, self.d["name"],
                 self.shape, self.path)
        self.status = "running"
        main_error: Optional[BaseException] = None
        try:
            self._phase("pre")
            if _pre_configured(self.d, self.channels):
                # the pre-sweep constants changed instrument settings (and maybe their
                # channel lists): make sure the sweep still makes sense before point 1
                validate_against(self.d, self.channels, after_pre=True,
                                 abort_check=self.control.check)
            self._value_axes()
            self._loop(len(self.shape) - 1, ())
            self.status = "finished"
        except EndSweep:
            # a normal end: everything is saved as usual, unvisited points stay NaN
            self.status = "terminated"
            self.error = ""
            at = self._ended_at or {}
            log.warning("run %d terminated by end_sweep() in the %s action of dimension %s at "
                        "point %s: %d of %d points measured, the rest are NaN",
                        self.run_number, str(at.get("action", "?")).replace("_", "-"),
                        at.get("dimension", "?"), at.get("index", "?"), self.dataset.points,
                        self.dataset.total_points)
            self._journal(EVENT, dict(event="terminated", t=time.time(), **at), sync=True)
        except AbortRequested as exc:
            self.status = "aborted"
            why = str(exc) or RunControl.USER
            if why != RunControl.USER:
                self.error = why
            log.warning("run %d %s", self.run_number,
                        why if why == RunControl.USER else "aborted: %s" % why)
        except Exception as exc:
            main_error = exc
            self.status = "error"
            self.error = "%s: %s" % (type(exc).__name__, exc)
            log.error("run %d failed: %s\n%s", self.run_number, self.error,
                      traceback.format_exc(limit=6))
        # post phase: always for errors, optionally on abort
        if self.status != "aborted" or self.d["run_post_on_abort"]:
            was_aborted = self.control.aborted
            reason = self.control.reason
            if was_aborted:
                self.control.reset()  # let the de-init run to completion
            try:
                self._phase("post")
            except AbortRequested:
                log.warning("run %d: post-sweep actions aborted", self.run_number)
            except Exception as exc:
                log.error("run %d: post-sweep actions failed: %s", self.run_number, exc)
                if self.status in ("finished", "terminated"):
                    self.status, self.error = "error", "post-sweep: %s" % exc
            finally:
                if was_aborted:
                    self.control.abort(reason)
        footer = {"status": self.status, "error": self.error, "ended": time.time(),
                  "points": self.dataset.points, "read_failures": self.read_failures,
                  "skipped_points": self.skipped_points,
                  "out_of_range_skipped": self.out_of_range_skipped}
        if self.out_of_range_skipped:
            log.info("run %d: %d point%s skipped because a composite would be out of range",
                     self.run_number, self.out_of_range_skipped,
                     "" if self.out_of_range_skipped == 1 else "s")
        if self.status == "terminated":
            footer["terminated_at"] = self._ended_at
        self._journal(FOOTER, footer, sync=True)
        self.dataset.footer = footer
        self.dataset.fill_missing()
        self._close_journal()
        self._emit("sweep_end", {"run": self.run_number, "status": self.status,
                                 "error": self.error, "path": str(self.path), "footer": footer})
        log.info("run %d %s (%d/%d points, %d read failures)", self.run_number, self.status,
                 self.dataset.points, self.dataset.total_points, self.read_failures)
        return self.status

    def _loop(self, k: int, outer: tuple) -> None:
        dim = self.d["dimensions"][k]
        n = dim["points"]
        reverse = dim["snake"] and self._pass_counts[k] % 2 == 1
        self._pass_counts[k] += 1
        order = range(n - 1, -1, -1) if reverse else range(n)
        sweep_names = [s["channel"] for s in dim["sweep"]]
        hooks = self._hooks[k]
        order = list(order)
        for pos, i in enumerate(order):
            self.control.check(on_pause=self._on_pause)
            idx = (i,) + outer
            sp = {name: self.setpoints[k][name][i] for name in sweep_names}
            ctx = {"k": k, "i": i, "index": idx,
                   "setpoints": {n: (float(v) if np.ndim(v) == 0 else v) for n, v in sp.items()}}
            try:
                if hooks:
                    self._point_hook(k, "pre_sweep", ctx)
                todo = []
                for names, g, outer_var in self._set_groups[k]:
                    values = {n: ctx["setpoints"][n] for n in names}
                    if outer_var:          # set with the inner variables of g
                        self._pending.setdefault(g, {}).update(values)
                        continue
                    if g is not None:
                        values = dict(self._pending.get(g, {}), **values)
                        try:
                            reason = self.channels.check_point(values)
                        except Exception:
                            reason = None   # setting it fails, with the usual handling
                        if reason is not None:
                            if not self.out_of_range_skipped:
                                log.info("run %d: point %s skipped: %s (further points out "
                                         "of range are skipped quietly)", self.run_number,
                                         list(idx), reason)
                            raise _OutOfRangeSkip()
                    todo.append(values)
                for values in todo:
                    if len(values) == 1:
                        name, value = next(iter(values.items()))
                        self._set(name, value)
                    else:                 # variables of one composite: set together
                        self._set_many(values)
                if hooks:
                    self._point_hook(k, "post_sweep", ctx)
                if dim["pre_delay"]:
                    self.control.sleep(dim["pre_delay"])
                if not dim["record_after_inner"] or k == 0:
                    self._record_event(k, idx, dim, ctx)
                if k > 0:
                    self._loop(k - 1, idx)      # a skip in there belongs to the inner dim
                if dim["record_after_inner"] and k > 0:
                    self._record_event(k, idx, dim, ctx)
            except _OutOfRangeSkip:
                self.out_of_range_skipped += 1
                self._skip_point(k, idx, quiet=True)
            except SkipPoint:
                self._skip_point(k, idx)
            except SkipLine:
                rest = order[pos:]
                log.info("run %d: dimension %d line %s skipped from index %d (%d points)",
                         self.run_number, k + 1, list(outer), i, len(rest))
                for j in rest:
                    self._skip_point(k, (j,) + outer, quiet=True)
                break

    def _skip_point(self, k: int, idx: tuple, quiet: bool = False) -> None:
        """skip_point() was called: store the point as skipped (its data stay NaN, its
        inner points that did not run count as done) and go on with the next one."""
        self.skipped_points += 1
        if not quiet:
            log.info("run %d: dimension %d point %s skipped", self.run_number, k + 1, list(idx))
        frame = {"k": k, "i": list(idx), "v": {}, "e": {}, "t": time.time(), "skip": True}
        self._journal(POINT, frame)
        try:
            self.dataset.apply_point(k, idx, {}, {}, skip=True)
        except Exception as exc:
            log.error("run %d: cannot mark point %s skipped: %s", self.run_number, idx, exc)
        progress = self.progress()
        self._emit("sweep_point", {"run": self.run_number, "k": k, "i": list(idx), "v": {},
                                   "e": {}, "skip": True, "progress": progress})
        self._emit("sweep_progress", {"run": self.run_number, "progress": progress})

    def _record_event(self, k: int, idx: tuple, dim: dict, ctx: Optional[dict] = None) -> None:
        hooks = self._hooks[k]
        if hooks:
            self._point_hook(k, "pre_record", ctx)
        values, errors = self._record(dim["record"])
        ending = None
        if hooks:
            try:
                self._point_hook(k, "post_record", dict(ctx, values=values))
            except EndSweep as exc:
                ending = exc        # exit keeps what was just measured (skips discard it)
        frame = {"k": k, "i": list(idx), "v": values, "e": errors, "t": time.time()}
        self._journal(POINT, frame)
        try:
            self.dataset.apply_point(k, idx, values, errors)
        except Exception as exc:
            log.error("run %d: cannot assemble point %s: %s", self.run_number, idx, exc)
        progress = self.progress()
        self._emit("sweep_point", {"run": self.run_number, "k": k, "i": list(idx),
                                   "v": values, "e": errors, "progress": progress})
        # tiny event for clients that only show progress (tray, scripts)
        self._emit("sweep_progress", {"run": self.run_number, "progress": progress})
        if ending is not None:
            raise ending
        if dim["post_delay"]:
            self.control.sleep(dim["post_delay"])

    def _on_pause(self, paused: bool) -> None:
        if paused:
            self._journal(EVENT, {"event": "paused", "t": time.time()}, sync=True)
        else:
            self._journal(EVENT, {"event": "resumed", "t": time.time()})
        self._emit("sweep_paused" if paused else "sweep_resumed", {"run": self.run_number})

    def progress(self) -> dict:
        ds = self.dataset
        done = ds.points if ds is not None else 0
        total = ds.total_points if ds is not None else 0
        elapsed = time.time() - self.t_start if self.t_start else 0.0
        eta = elapsed / done * (total - done) if done else None
        return {"done": done, "total": total, "elapsed": elapsed, "eta": eta}
