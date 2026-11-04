"""Channel aliasing, protection (limits / ramp rates) and composite channels.

Aliases
-------
An alias gives a friendly name to one instrument channel (``"Vg" -> "src.V1"``)
and carries the protection settings - these belong to the alias, not to the
instrument layer:

* ``min`` / ``max``  - hard limits (in alias units); violating values are refused.
* ``ramp_rate``     - maximum rate of change in units/second (``None`` = jump).
* ``ramp_step``     - optional maximum step per write while ramping.
* ``scale`` / ``offset`` - alias value = raw * scale + offset (e.g. amplifier gain).
* ``allowed``       - for string channels, the list of accepted values.
* ``read_only``     - refuse all writes through this alias.

Composite channels
------------------
A composite is a virtual channel driving several aliases together::

    {"name": "detuning", "targets": {"VL": "x", "VR": "-x + 0.1"},
     "read": "VL - VR", "min": -1, "max": 1, "ramp_rate": 0.5}

Setting the composite evaluates each target expression with ``x`` (the
composite value) and all targets are moved simultaneously.  The ramp duration is
chosen so that *no* target exceeds its own ramp rate.  Every intermediate value
is limit-checked before anything is written.

Special read-only channels ``@time`` (unix time) are always available.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from ..common.errors import AbortRequested, ChannelError, LimitError
from ..common.exprs import Expression
from .instrument import ChannelSpec, InstrumentManager
from .logsys import get_logger

log = get_logger("channels")

RAMP_INTERVAL = 0.05  # seconds between ramp steps

AbortCheck = Optional[Callable[[], None]]


def _ok_name(name: str) -> bool:
    return bool(name) and (name[0].isalpha() or name[0] == "_") and \
        all(c.isalnum() or c == "_" for c in name)


@dataclass
class Alias:
    name: str
    target: str                      # "instrument.channel"
    min: Optional[float] = None
    max: Optional[float] = None
    ramp_rate: Optional[float] = None
    ramp_step: Optional[float] = None
    scale: float = 1.0
    offset: float = 0.0
    unit: str = ""
    allowed: Optional[List[str]] = None
    read_only: bool = False
    description: str = ""

    def __post_init__(self):
        if not _ok_name(self.name):
            raise ChannelError("invalid alias name %r (use letters, digits, _)" % self.name)
        if "." not in self.target:
            raise ChannelError("alias target must be 'instrument.channel', got %r" % self.target)
        for k in ("min", "max", "ramp_rate", "ramp_step"):
            v = getattr(self, k)
            if v is not None:
                v = float(v)
                if math.isnan(v):
                    v = None
                setattr(self, k, v)
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ChannelError("alias %s: min > max" % self.name)
        if self.ramp_rate is not None and self.ramp_rate <= 0:
            raise ChannelError("alias %s: ramp_rate must be positive" % self.name)
        if self.ramp_step is not None and self.ramp_step <= 0:
            raise ChannelError("alias %s: ramp_step must be positive" % self.name)
        self.scale = float(self.scale)
        self.offset = float(self.offset)
        if self.scale == 0:
            raise ChannelError("alias %s: scale must be non-zero" % self.name)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "name", "target", "min", "max", "ramp_rate", "ramp_step", "scale", "offset",
            "unit", "allowed", "read_only", "description")}

    @classmethod
    def from_dict(cls, d: dict) -> "Alias":
        keys = {"name", "target", "min", "max", "ramp_rate", "ramp_step", "scale", "offset",
                "unit", "allowed", "read_only", "description"}
        return cls(**{k: v for k, v in d.items() if k in keys})


@dataclass
class Composite:
    name: str
    targets: Dict[str, str]          # alias -> expression of x
    read: Optional[str] = None       # expression of target alias names
    min: Optional[float] = None
    max: Optional[float] = None
    ramp_rate: Optional[float] = None
    unit: str = ""
    description: str = ""
    _exprs: Dict[str, Expression] = field(default_factory=dict, repr=False)
    _read_expr: Optional[Expression] = field(default=None, repr=False)

    def __post_init__(self):
        if not _ok_name(self.name):
            raise ChannelError("invalid composite name %r" % self.name)
        if not self.targets:
            raise ChannelError("composite %s has no targets" % self.name)
        self._exprs = {a: Expression(str(e), allowed_names=["x"]) for a, e in self.targets.items()}
        self._read_expr = Expression(self.read, allowed_names=list(self.targets)) \
            if self.read else None
        for k in ("min", "max", "ramp_rate"):
            v = getattr(self, k)
            if v is not None:
                setattr(self, k, float(v))
        if self.ramp_rate is not None and self.ramp_rate <= 0:
            raise ChannelError("composite %s: ramp_rate must be positive" % self.name)

    def target_values(self, x) -> Dict[str, Any]:
        return {a: e.evaluate({"x": x}) for a, e in self._exprs.items()}

    def to_dict(self) -> dict:
        return {"name": self.name, "targets": dict(self.targets), "read": self.read,
                "min": self.min, "max": self.max, "ramp_rate": self.ramp_rate,
                "unit": self.unit, "description": self.description}

    @classmethod
    def from_dict(cls, d: dict) -> "Composite":
        keys = {"name", "targets", "read", "min", "max", "ramp_rate", "unit", "description"}
        return cls(**{k: v for k, v in d.items() if k in keys})


def _max_abs_diff(a, b) -> float:
    try:
        d = np.abs(np.asarray(a, dtype=float) - np.asarray(b, dtype=float))
        return float(np.nanmax(d)) if d.size else 0.0
    except (TypeError, ValueError):
        return float("inf")


class ChannelManager:
    """Resolves names to aliases/composites and performs protected get/set."""

    def __init__(self, instruments: InstrumentManager):
        self.instruments = instruments
        self._aliases: Dict[str, Alias] = {}
        self._composites: Dict[str, Composite] = {}
        self._last: Dict[str, Any] = {}        # last value written per alias/composite
        self._lock = threading.RLock()
        self._set_locks: Dict[str, threading.RLock] = {}
        self.listeners: List[Callable[[str, Any], None]] = []

    # -- definition --------------------------------------------------------------
    def add_alias(self, alias: Alias, replace: bool = False) -> None:
        with self._lock:
            if alias.name in self._composites or (alias.name in self._aliases and not replace):
                raise ChannelError("channel name %r already in use" % alias.name)
            self._aliases[alias.name] = alias
            self._last.pop(alias.name, None)
        log.info("alias %s -> %s defined", alias.name, alias.target)

    def remove_alias(self, name: str) -> None:
        with self._lock:
            users = [c.name for c in self._composites.values() if name in c.targets]
            if users:
                raise ChannelError("alias %s is used by composite(s) %s" % (name, ", ".join(users)))
            if self._aliases.pop(name, None) is None:
                raise ChannelError("no alias %r" % name)

    def add_composite(self, comp: Composite, replace: bool = False) -> None:
        with self._lock:
            if comp.name in self._aliases or (comp.name in self._composites and not replace):
                raise ChannelError("channel name %r already in use" % comp.name)
            for a in comp.targets:
                if a not in self._aliases:
                    raise ChannelError("composite %s: target %r is not an alias" % (comp.name, a))
            self._composites[comp.name] = comp
            self._last.pop(comp.name, None)
        log.info("composite %s defined (targets: %s)", comp.name, ", ".join(comp.targets))

    def remove_composite(self, name: str) -> None:
        with self._lock:
            if self._composites.pop(name, None) is None:
                raise ChannelError("no composite %r" % name)

    def clear(self) -> None:
        with self._lock:
            self._aliases.clear()
            self._composites.clear()
            self._last.clear()

    def aliases(self) -> List[Alias]:
        with self._lock:
            return list(self._aliases.values())

    def composites(self) -> List[Composite]:
        with self._lock:
            return list(self._composites.values())

    def get_alias(self, name: str) -> Alias:
        with self._lock:
            a = self._aliases.get(name)
        if a is None:
            raise ChannelError("no alias %r" % name)
        return a

    def is_composite(self, name: str) -> bool:
        return name in self._composites

    def exists(self, name: str) -> bool:
        if name.startswith("@"):
            return name in ("@time",)
        if name in self._aliases or name in self._composites:
            return True
        try:
            self.instruments.resolve(name)
            return True
        except Exception:
            return False

    # -- introspection -------------------------------------------------------------
    def info(self, name: str) -> dict:
        """Type information: dtype, shape, readable, writable, unit."""
        if name == "@time":
            return {"name": name, "kind": "special", "dtype": "float", "shape": [],
                    "readable": True, "writable": False, "unit": "s"}
        with self._lock:
            a = self._aliases.get(name)
            c = self._composites.get(name)
        if c is not None:
            return {"name": name, "kind": "composite", "dtype": "float", "shape": [],
                    "readable": True, "writable": True, "unit": c.unit,
                    "min": c.min, "max": c.max, "ramp_rate": c.ramp_rate}
        if a is not None:
            spec = self._alias_spec(a)
            return {"name": name, "kind": "alias", "target": a.target, "dtype": spec.dtype,
                    "shape": None if spec.shape is None else list(spec.shape),
                    "readable": spec.readable, "writable": spec.writable and not a.read_only,
                    "unit": a.unit or spec.unit, "min": a.min, "max": a.max,
                    "ramp_rate": a.ramp_rate}
        h, spec = self.instruments.resolve(name)
        return {"name": name, "kind": "raw", "dtype": spec.dtype,
                "shape": None if spec.shape is None else list(spec.shape),
                "readable": spec.readable, "writable": spec.writable, "unit": spec.unit}

    def _alias_spec(self, a: Alias) -> ChannelSpec:
        _, spec = self.instruments.resolve(a.target)
        return spec

    def describe(self) -> dict:
        out_a = []
        for a in self.aliases():
            d = a.to_dict()
            try:
                inf = self.info(a.name)
                d.update(dtype=inf["dtype"], shape=inf["shape"], readable=inf["readable"],
                         writable=inf["writable"])
            except Exception as exc:
                d.update(error=str(exc))
            d["last"] = self._last.get(a.name)
            out_a.append(d)
        out_c = []
        for c in self.composites():
            d = c.to_dict()
            d["last"] = self._last.get(c.name)
            out_c.append(d)
        return {"aliases": out_a, "composites": out_c}

    def last_value(self, name: str):
        return self._last.get(name)

    # -- validation --------------------------------------------------------------
    def check_value(self, name: str, value: Any) -> Any:
        """Validate a value against protection; returns the canonical value."""
        with self._lock:
            a = self._aliases.get(name)
            c = self._composites.get(name)
        if c is not None:
            x = self._to_float(name, value)
            self._check_limits(name, x, c.min, c.max)
            for alias_name, tv in c.target_values(x).items():
                self.check_value(alias_name, tv)
            return x
        if a is not None:
            if a.read_only:
                raise ChannelError("alias %s is read-only" % name)
            spec = self._alias_spec(a)
            if not spec.writable:
                raise ChannelError("alias %s (%s) is not writable" % (name, a.target))
            if spec.dtype == "str":
                v = str(value)
                if a.allowed and v not in a.allowed:
                    raise LimitError("%s: %r not in allowed values %s" % (name, v, a.allowed))
                return v
            arr = self._to_array(name, value)
            if spec.shape == ():
                if arr.size != 1:
                    raise ChannelError("%s is a scalar channel, got shape %s" % (name, arr.shape))
                arr = arr.reshape(())
            self._check_limits(name, arr, a.min, a.max)
            return float(arr) if arr.ndim == 0 else arr
        if name.startswith("@"):
            raise ChannelError("%s is read-only" % name)
        raise ChannelError("unknown channel %r (raw instrument channels must be "
                           "aliased before they can be set)" % name)

    @staticmethod
    def _to_float(name, value) -> float:
        try:
            x = float(value)
        except (TypeError, ValueError):
            raise ChannelError("%s needs a numeric scalar, got %r" % (name, value)) from None
        if not math.isfinite(x):
            raise LimitError("%s: refusing to set non-finite value %r" % (name, x))
        return x

    @staticmethod
    def _to_array(name, value) -> np.ndarray:
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                raise ChannelError("%s needs a numeric value, got %r" % (name, value)) from None
        try:
            arr = np.asarray(value, dtype=float)
        except (TypeError, ValueError):
            raise ChannelError("%s needs numeric data" % name) from None
        if not np.isfinite(arr).all():
            raise LimitError("%s: refusing to set NaN/inf" % name)
        return arr

    @staticmethod
    def _check_limits(name, arr, lo, hi) -> None:
        a = np.asarray(arr, dtype=float)
        if lo is not None and np.any(a < lo - 1e-12 * max(1.0, abs(lo))):
            raise LimitError("%s: value %s below minimum %g" % (name, _fmt(a), lo))
        if hi is not None and np.any(a > hi + 1e-12 * max(1.0, abs(hi))):
            raise LimitError("%s: value %s above maximum %g" % (name, _fmt(a), hi))

    # -- read ----------------------------------------------------------------------
    def get(self, name: str) -> Any:
        if name == "@time":
            return time.time()
        with self._lock:
            a = self._aliases.get(name)
            c = self._composites.get(name)
        if c is not None:
            if c._read_expr is not None:
                vals = {t: self.get(t) for t in c.targets}
                return float(np.asarray(c._read_expr.evaluate(vals), dtype=float))
            last = self._last.get(name)
            return float("nan") if last is None else last
        if a is not None:
            h, spec = self.instruments.resolve(a.target)
            raw = h.read(spec.name)
            return self._from_raw(a, spec, raw)
        h, spec = self.instruments.resolve(name)
        return h.read(spec.name)

    @staticmethod
    def _from_raw(a: Alias, spec: ChannelSpec, raw):
        if spec.dtype == "str" or (a.scale == 1.0 and a.offset == 0.0):
            return raw
        return raw * a.scale + a.offset

    @staticmethod
    def _to_raw(a: Alias, spec: ChannelSpec, value):
        if spec.dtype == "str" or (a.scale == 1.0 and a.offset == 0.0):
            return value
        return (value - a.offset) / a.scale

    def _current_alias_value(self, a: Alias, spec: ChannelSpec, shape=None):
        """Where a ramp starts.  The instrument is read back whenever possible:
        the cached last value can be stale (front-panel change, raw write, failed
        write, instrument restart).  Returns None if the value cannot be known."""
        def ok(v):
            if v is None:
                return None
            try:
                arr = np.asarray(v, dtype=float)
            except (TypeError, ValueError):
                return None
            if not np.isfinite(arr).all():
                return None
            if shape is not None and arr.shape != tuple(shape):
                if arr.size == int(np.prod(shape)):
                    arr = arr.reshape(shape)
                else:
                    return None
            return arr
        if spec.readable:
            try:
                h, _ = self.instruments.resolve(a.target)
                v = ok(self._from_raw(a, spec, h.read(spec.name)))
                if v is not None:
                    return v
            except Exception as exc:
                log.warning("cannot read %s to start ramp: %s", a.name, exc)
        return ok(self._last.get(a.name))

    # -- write ---------------------------------------------------------------------
    def _set_lock(self, name: str) -> threading.RLock:
        with self._lock:
            lk = self._set_locks.get(name)
            if lk is None:
                lk = self._set_locks[name] = threading.RLock()
            return lk

    def _guard_raw(self, ref: str, value: Any) -> List[Alias]:
        """A raw write must still respect the limits of every alias on that target."""
        h, spec = self.instruments.resolve(ref)
        guards = [a for a in self.aliases() if a.target == ref]
        for a in guards:
            if a.read_only:
                raise ChannelError("%s is protected by read-only alias %s" % (ref, a.name))
            if spec.dtype == "str":
                if a.allowed and str(value) not in a.allowed:
                    raise LimitError("%s (alias %s): %r not in allowed values %s"
                                     % (ref, a.name, value, a.allowed))
                continue
            arr = self._to_array(ref, value)
            self._check_limits("%s (alias %s)" % (ref, a.name), arr * a.scale + a.offset,
                               a.min, a.max)
        return guards

    def set_raw(self, ref: str, value: Any) -> None:
        """Write to ``instrument.channel`` without ramping (scripts).  Limits of
        aliases on the same target are still enforced."""
        h, spec = self.instruments.resolve(ref)
        guards = self._guard_raw(ref, value)
        try:
            h.write(spec.name, value)
        finally:
            for a in guards:
                self._last.pop(a.name, None)   # cached value is stale now

    def query_raw(self, ref: str, value: Any) -> Any:
        h, spec = self.instruments.resolve(ref)
        guards = self._guard_raw(ref, value)
        try:
            return h.query(spec.name, value)
        finally:
            for a in guards:
                self._last.pop(a.name, None)

    def set(self, name: str, value: Any, ramp: bool = True, abort_check: AbortCheck = None) -> Any:
        """Protected write to an alias or composite.  Returns the value set."""
        value = self.check_value(name, value)
        with self._set_lock(name):
            if name in self._composites:
                self._set_composite(self._composites[name], value, ramp, abort_check)
            else:
                self._set_alias(self.get_alias(name), value, ramp, abort_check)
        self._notify(name, value)
        return value

    def _notify(self, name, value):
        for cb in list(self.listeners):
            try:
                cb(name, value)
            except Exception:
                pass

    def _write_alias_now(self, a: Alias, spec: ChannelSpec, value) -> None:
        h, _ = self.instruments.resolve(a.target)
        try:
            h.write(spec.name, self._to_raw(a, spec, value))
        except Exception:
            # the instrument may or may not have applied it: forget the cache so the
            # next ramp starts from a fresh reading instead of a stale assumption
            self._last.pop(a.name, None)
            raise
        self._last[a.name] = value

    @staticmethod
    def _has_ramp(a: Alias) -> bool:
        return a.ramp_rate is not None or a.ramp_step is not None

    def _set_alias(self, a: Alias, value, ramp: bool, abort_check: AbortCheck) -> None:
        spec = self._alias_spec(a)
        if spec.dtype == "str" or not ramp or not self._has_ramp(a):
            self._write_alias_now(a, spec, value)
            return
        target = np.asarray(value, dtype=float)
        start = self._current_alias_value(a, spec, target.shape)
        if start is None:
            raise LimitError("%s: cannot determine the present value to ramp from "
                             "(read-back failed or unavailable); set it once with ramp=False "
                             "to force" % a.name)
        self._ramp([(a, spec)], lambda u: [start + (target - start) * u], 2, abort_check)

    def _ramp(self, items, value_at, nsamples: int, abort_check: AbortCheck,
              x_span: float = 0.0, x_rate: Optional[float] = None) -> None:
        """Move aliases along a path ``value_at(u)``, ``u`` from 0 to 1.

        The path is sampled first: every sample is limit-checked *before* anything is
        written, and the cumulative "time cost" (max over aliases of distance / ramp
        rate) and "step cost" (distance / ramp_step) are tabulated.  Progress is then
        limited so that neither the ramp rate nor the step size of any alias is
        exceeded, even for non-linear (composite) paths.
        """
        us = np.linspace(0.0, 1.0, max(2, int(nsamples)))
        path = []
        for u in us:
            vals = [np.asarray(v, dtype=float) for v in value_at(float(u))]
            for (a, _), v in zip(items, vals):
                if not np.isfinite(v).all():
                    raise LimitError("%s: ramp path contains NaN/inf" % a.name)
                self._check_limits(a.name, v, a.min, a.max)
            path.append(vals)
        ct = [0.0]
        cs = [0.0]
        for i in range(1, len(us)):
            dt = ds = 0.0
            for (a, _), v0, v1 in zip(items, path[i - 1], path[i]):
                d = _max_abs_diff(v0, v1)
                if a.ramp_rate:
                    dt = max(dt, d / a.ramp_rate)
                if a.ramp_step:
                    ds = max(ds, d / a.ramp_step)
            if x_rate:
                dt = max(dt, x_span * (us[i] - us[i - 1]) / x_rate)
            ct.append(ct[-1] + dt)
            cs.append(cs[-1] + ds)
        ct_a = np.asarray(ct)
        cs_a = np.asarray(cs)

        def invert(cum, c):
            if cum[-1] <= c:
                return 1.0
            j = int(np.searchsorted(cum, c, side="right"))
            j = max(1, min(j, len(cum) - 1))
            c0, c1 = cum[j - 1], cum[j]
            f = 0.0 if c1 <= c0 else (c - c0) / (c1 - c0)
            return float(us[j - 1] + (us[j] - us[j - 1]) * min(max(f, 0.0), 1.0))

        t0 = time.monotonic()
        k = 0
        last_u = 0.0
        while True:
            if abort_check is not None:
                abort_check()
            k += 1
            elapsed = time.monotonic() - t0
            allowed_t = min(k * RAMP_INTERVAL, elapsed + RAMP_INTERVAL)
            u = min(invert(ct_a, allowed_t), invert(cs_a, float(k)))
            u = max(u, last_u)
            vals = value_at(u) if u < 1.0 else path[-1]
            vals = [np.asarray(v, dtype=float) for v in vals]
            for (a, _), v in zip(items, vals):
                self._check_limits(a.name, v, a.min, a.max)
            for (a, spec), v in zip(items, vals):
                self._write_alias_now(a, spec, float(v) if v.ndim == 0 else v)
            if u >= 1.0:
                return
            last_u = u
            time.sleep(RAMP_INTERVAL)

    def _set_composite(self, c: Composite, x: float, ramp: bool, abort_check: AbortCheck) -> None:
        targets = c.target_values(x)
        items = []
        for alias_name in targets:
            a = self.get_alias(alias_name)
            items.append((a, self._alias_spec(a)))
        tvals = [np.asarray(targets[a.name], dtype=float) for a, _ in items]
        needs_ramp = ramp and (c.ramp_rate is not None or any(self._has_ramp(a) for a, _ in items))
        if not needs_ramp:
            for (a, spec), v in zip(items, tvals):
                self._check_limits(a.name, v, a.min, a.max)
            for (a, spec), v in zip(items, tvals):
                self._write_alias_now(a, spec, float(v) if v.ndim == 0 else v)
            self._last[c.name] = x
            return
        starts = []
        for (a, spec), tv in zip(items, tvals):
            st = self._current_alias_value(a, spec, tv.shape)
            if st is None:
                raise LimitError("composite %s: cannot determine the present value of %s to ramp "
                                 "from; set it once with ramp=False to force" % (c.name, a.name))
            starts.append(st)
        x0 = self._last.get(c.name)
        on_path = False
        if x0 is not None:
            try:
                expected = c.target_values(x0)
                on_path = all(
                    _max_abs_diff(expected[a.name], s0) <= 1e-6 * (1.0 + float(np.max(np.abs(s0))))
                    for (a, _), s0 in zip(items, starts))
            except Exception:
                on_path = False
        if on_path:
            # follow the composite relation (may be non-linear in every target)
            x0f, x1f = float(x0), float(x)
            self._ramp(items, lambda u: [c.target_values(x0f + (x1f - x0f) * u)[a.name]
                                         for a, _ in items],
                       257, abort_check, abs(x1f - x0f), c.ramp_rate)
        else:
            # unknown/inconsistent origin: straight line from the actual values
            self._ramp(items, lambda u: [s0 + (t - s0) * u for s0, t in zip(starts, tvals)],
                       2, abort_check)
        self._last[c.name] = x


def _fmt(v) -> str:
    a = np.asarray(v)
    if a.ndim == 0:
        return "%g" % float(a)
    return "array%s[min %g, max %g]" % (a.shape, float(np.nanmin(a)), float(np.nanmax(a)))
