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
         "record_after_inner": false},      # (outer dims) record after the inner loop
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

from ..common.errors import (AbortRequested, ChannelError, EasyMeasureError, LimitError,
                             SweepDefinitionError)
from ..common.exprs import Expression
from .logsys import get_logger
from .storage import EVENT, FOOTER, HEADER, PHASE, POINT, DataSet, JournalWriter

log = get_logger("sweep")


# ---------------------------------------------------------------------------
# Definition handling
# ---------------------------------------------------------------------------
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
    for ph in ("pre", "post"):
        p = d.setdefault(ph, {}) or {}
        d[ph] = p
        p.setdefault("hook", "")
        p.setdefault("constants", [])
        p.setdefault("record", [])
        for c in p["constants"]:
            if not isinstance(c, dict) or "channel" not in c or "value" not in c:
                raise SweepDefinitionError("%s constants need channel and value" % ph)
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
                    try:
                        rows.append(np.asarray(v, dtype=float))
                    except (TypeError, ValueError):
                        raise SweepDefinitionError("%s: mapping must return numbers" % ch) from None
                shapes = {r.shape for r in rows}
                if len(shapes) != 1:
                    raise SweepDefinitionError("%s: mapping returns inconsistent shapes %s"
                                               % (ch, sorted(shapes)))
                out[k][ch] = np.stack(rows)
            else:
                if not all(np.isfinite([start_f, stop_f])):
                    raise SweepDefinitionError("%s: start/stop must be finite" % ch)
                out[k][ch] = np.linspace(start_f, stop_f, n)
            if not np.all(np.isfinite(out[k][ch])):
                raise SweepDefinitionError("%s: setpoints contain NaN/inf" % ch)
    return out


def validate_against(d: dict, channels) -> List[str]:
    """Check channel existence/direction and limits for every setpoint.

    Returns a list of warnings; raises :class:`SweepDefinitionError` on errors.
    """
    warnings: List[str] = []
    errors: List[str] = []
    setpoints = compute_setpoints(d)
    for k, dim in enumerate(d["dimensions"]):
        for s in dim["sweep"]:
            ch = s["channel"]
            try:
                info = channels.info(ch)
            except Exception as exc:
                errors.append("sweep channel %s: %s" % (ch, exc))
                continue
            if info["kind"] == "raw":
                errors.append("sweep channel %s must be an alias or composite" % ch)
                continue
            if not info["writable"]:
                errors.append("sweep channel %s is not writable" % ch)
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
            try:
                for v in sp:
                    channels.check_value(ch, v if np.ndim(v) else float(v))
            except (LimitError, ChannelError) as exc:
                errors.append(str(exc))
        for r in dim["record"]:
            if r in ("@time", "@elapsed"):
                continue
            try:
                info = channels.info(r)
                if not info["readable"]:
                    errors.append("record channel %s is not readable" % r)
            except Exception as exc:
                errors.append("record channel %s: %s" % (r, exc))
    for ph in ("pre", "post"):
        for c in d[ph]["constants"]:
            try:
                channels.check_value(c["channel"], c["value"])
            except Exception as exc:
                errors.append("%s constant %s: %s" % (ph, c["channel"], exc))
        for r in d[ph]["record"]:
            if not channels.exists(r):
                errors.append("%s record channel %s does not exist" % (ph, r))
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

    def __init__(self):
        self._abort = threading.Event()
        self._running = threading.Event()
        self._running.set()          # set = not paused
        self.pause_requested = False

    def abort(self) -> None:
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
        self.resume()

    def check_abort(self) -> None:
        """Non-blocking: raise AbortRequested if an abort was requested."""
        if self._abort.is_set():
            raise AbortRequested("aborted by user")

    @property
    def aborted(self) -> bool:
        return self._abort.is_set()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def check(self, on_pause: Optional[Callable[[bool], None]] = None) -> None:
        """Raise AbortRequested if aborted; block while paused."""
        if self._abort.is_set():
            raise AbortRequested("aborted by user")
        if not self._running.is_set():
            if on_pause:
                on_pause(True)
            while not self._running.wait(0.1):
                if self._abort.is_set():
                    break
            if on_pause:
                on_pause(False)
            if self._abort.is_set():
                raise AbortRequested("aborted by user")

    def sleep(self, seconds: float) -> None:
        """Abortable sleep (pause does not stop the clock of a delay)."""
        end = time.monotonic() + max(0.0, seconds)
        while True:
            if self._abort.is_set():
                raise AbortRequested("aborted by user")
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            self._abort.wait(min(remaining, 0.05))


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
        self.shape = [dim["points"] for dim in self.d["dimensions"]]
        self.dataset: Optional[DataSet] = None
        self.journal: Optional[JournalWriter] = None
        self.t_start = 0.0
        self.status = "pending"
        self.error = ""
        self.read_failures = 0
        self.storage_errors = 0
        self._pass_counts = [0] * len(self.shape)

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
        for name in names:
            self.control.check()
            try:
                v = self._read(name)
                values[name] = v
            except AbortRequested:
                raise
            except Exception as exc:
                self.read_failures += 1
                errors[name] = str(exc)
                log.warning("run %d: reading %s failed: %s", self.run_number, name, exc)
                if self.d["on_read_error"] == "abort":
                    raise
        return values, errors

    def _set(self, name: str, value) -> None:
        try:
            self.channels.set(name, value, ramp=True, abort_check=self.control.check)
        except AbortRequested:
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

    def _phase(self, phase: str) -> None:
        p = self.d[phase]
        if phase == "pre":
            self._hook("pre")
        if phase == "post":
            vals, errs = self._record(p["record"]) if p["record"] else ({}, {})
            self._store_phase("post", vals, errs)
        for c in p["constants"]:
            self.control.check()
            log.info("run %d: %s constant %s = %r", self.run_number, phase, c["channel"], c["value"])
            self._set(c["channel"], c["value"])
        if phase == "pre":
            vals, errs = self._record(p["record"]) if p["record"] else ({}, {})
            self._store_phase("pre", vals, errs)
        if phase == "post":
            self._hook("post")

    def _store_phase(self, phase, vals, errs):
        if not vals and not errs:
            return
        self._journal(PHASE, {"phase": phase, "v": vals, "e": errs}, sync=True)
        if self.dataset is not None:
            self.dataset.apply_frame(PHASE, {"phase": phase, "v": vals})
        self._emit("sweep_phase", {"run": self.run_number, "phase": phase, "v": vals, "e": errs})

    # -- main loop -------------------------------------------------------------------
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
            self._loop(len(self.shape) - 1, ())
            self.status = "finished"
        except AbortRequested:
            self.status = "aborted"
            log.warning("run %d aborted by user", self.run_number)
        except Exception as exc:
            main_error = exc
            self.status = "error"
            self.error = "%s: %s" % (type(exc).__name__, exc)
            log.error("run %d failed: %s\n%s", self.run_number, self.error,
                      traceback.format_exc(limit=6))
        # post phase: always for errors, optionally on abort
        if self.status != "aborted" or self.d["run_post_on_abort"]:
            was_aborted = self.control.aborted
            if was_aborted:
                self.control.reset()  # let the de-init run to completion
            try:
                self._phase("post")
            except AbortRequested:
                log.warning("run %d: post-sweep actions aborted", self.run_number)
            except Exception as exc:
                log.error("run %d: post-sweep actions failed: %s", self.run_number, exc)
                if self.status == "finished":
                    self.status, self.error = "error", "post-sweep: %s" % exc
            finally:
                if was_aborted:
                    self.control.abort()
        footer = {"status": self.status, "error": self.error, "ended": time.time(),
                  "points": self.dataset.points, "read_failures": self.read_failures}
        self._journal(FOOTER, footer, sync=True)
        self.dataset.footer = footer
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
        for i in order:
            self.control.check(on_pause=self._on_pause)
            for name in sweep_names:
                v = self.setpoints[k][name][i]
                self._set(name, float(v) if np.ndim(v) == 0 else v)
            if dim["pre_delay"]:
                self.control.sleep(dim["pre_delay"])
            idx = (i,) + outer
            if not dim["record_after_inner"] or k == 0:
                self._record_event(k, idx, dim)
            if k > 0:
                self._loop(k - 1, idx)
            if dim["record_after_inner"] and k > 0:
                self._record_event(k, idx, dim)

    def _record_event(self, k: int, idx: tuple, dim: dict) -> None:
        values, errors = self._record(dim["record"])
        frame = {"k": k, "i": list(idx), "v": values, "e": errors, "t": time.time()}
        self._journal(POINT, frame)
        try:
            self.dataset.apply_point(k, idx, values, errors)
        except Exception as exc:
            log.error("run %d: cannot assemble point %s: %s", self.run_number, idx, exc)
        self._emit("sweep_point", {"run": self.run_number, "k": k, "i": list(idx),
                                   "v": values, "e": errors,
                                   "progress": self.progress()})
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
