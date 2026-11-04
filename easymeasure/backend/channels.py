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
        if math.isnan(x):
            raise LimitError("%s: refusing to set NaN" % name)
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
        if np.isnan(arr).any():
            raise LimitError("%s: refusing to set NaN" % name)
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

    def _current_alias_value(self, a: Alias, spec: ChannelSpec):
        last = self._last.get(a.name)
        if last is not None:
            return last
        if spec.readable:
            try:
                h, _ = self.instruments.resolve(a.target)
                return self._from_raw(a, spec, h.read(spec.name))
            except Exception as exc:
                log.warning("cannot read %s to start ramp: %s", a.name, exc)
        return None

    # -- write ---------------------------------------------------------------------
    def _set_lock(self, name: str) -> threading.RLock:
        with self._lock:
            lk = self._set_locks.get(name)
            if lk is None:
                lk = self._set_locks[name] = threading.RLock()
            return lk

    def set_raw(self, ref: str, value: Any) -> None:
        """Unprotected write to ``instrument.channel`` (scripts only)."""
        h, spec = self.instruments.resolve(ref)
        h.write(spec.name, value)

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

    def _set_alias(self, a: Alias, value, ramp: bool, abort_check: AbortCheck) -> None:
        spec = self._alias_spec(a)
        if spec.dtype == "str" or not ramp or (a.ramp_rate is None and a.ramp_step is None):
            self._write_alias_now(a, spec, value)
            return
        start = self._current_alias_value(a, spec)
        if start is None or np.shape(start) != np.shape(value):
            if start is None:
                log.warning("%s: unknown current value, setting %s without ramp",
                            a.name, _fmt(value))
            self._write_alias_now(a, spec, value)
            return
        start = np.asarray(start, dtype=float)
        target = np.asarray(value, dtype=float)
        dist = _max_abs_diff(start, target)
        if not math.isfinite(dist):
            self._write_alias_now(a, spec, value)
            return
        duration = dist / a.ramp_rate if a.ramp_rate else 0.0
        nsteps_by_step = int(math.ceil(dist / a.ramp_step)) if a.ramp_step else 1
        self._ramp([(a, spec, start, target)], duration, nsteps_by_step, abort_check)

    def _ramp(self, items, duration: float, min_steps: int, abort_check: AbortCheck,
              x_path: Optional[tuple] = None) -> None:
        """Move several aliases together from start to target values.

        *items* is a list of ``(alias, spec, start, target)``.  If *x_path* is given
        (composite, x0, x1) intermediate targets come from the composite mapping
        instead of linear interpolation.
        """
        t0 = time.monotonic()
        step_i = 0
        min_steps = max(1, int(min_steps))
        while True:
            if abort_check is not None:
                abort_check()
            elapsed = time.monotonic() - t0
            step_i += 1
            frac_t = 1.0 if duration <= 0 else min(1.0, elapsed / duration)
            frac_s = min(1.0, step_i / min_steps)
            frac = min(frac_t, frac_s)
            if x_path is not None:
                comp, x0, x1 = x_path
                tv = comp.target_values(x0 + (x1 - x0) * frac)
            # compute and check every value first, then write them all
            values = []
            for a, spec, s, t in items:
                v = np.asarray(tv[a.name], dtype=float) if x_path is not None else s + (t - s) * frac
                v = float(v) if np.ndim(v) == 0 else v
                self._check_limits(a.name, v, a.min, a.max)
                values.append(v)
            for (a, spec, _, _), v in zip(items, values):
                self._write_alias_now(a, spec, v)
            if frac >= 1.0:
                return
            # time-based progress: a slow instrument never makes us exceed the rate
            time.sleep(RAMP_INTERVAL)

    def _set_composite(self, c: Composite, x: float, ramp: bool, abort_check: AbortCheck) -> None:
        targets = c.target_values(x)
        items = []
        duration = 0.0
        for alias_name, tv in targets.items():
            a = self.get_alias(alias_name)
            spec = self._alias_spec(a)
            tv = np.asarray(tv, dtype=float)
            start = self._current_alias_value(a, spec) if ramp else None
            if start is not None and np.shape(start) == np.shape(tv):
                start = np.asarray(start, dtype=float)
                d = _max_abs_diff(start, tv)
                if a.ramp_rate and math.isfinite(d):
                    duration = max(duration, d / a.ramp_rate)
            else:
                start = tv
            items.append((a, spec, start, tv))
        x0 = self._last.get(c.name)
        if ramp and c.ramp_rate and x0 is not None:
            duration = max(duration, abs(x - x0) / c.ramp_rate)
        if not ramp:
            duration = 0.0
        # use the composite mapping for the path when we know where we come from
        # and the targets are consistent with it
        x_path = None
        if x0 is not None and duration > 0:
            expected = c.target_values(x0)
            consistent = all(
                _max_abs_diff(expected[a.name], s) <= 1e-9 * (1.0 + float(np.nanmax(np.abs(s))))
                for a, _, s, _ in items)
            if consistent:
                x_path = (c, float(x0), float(x))
        self._ramp(items, duration, 1, abort_check, x_path)
        self._last[c.name] = x


def _fmt(v) -> str:
    a = np.asarray(v)
    if a.ndim == 0:
        return "%g" % float(a)
    return "array%s[min %g, max %g]" % (a.shape, float(np.nanmin(a)), float(np.nanmax(a)))
