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
* ``allowed``       - for string channels, the list of accepted values (narrows the
                      instrument channel's own ``allowed`` list, if it has one).
* ``read_only``     - refuse all writes through this alias.

Composite channels
------------------
Virtual channels defined by formulas (:mod:`.composites`): multivariate writes
(variables driving several targets), composite reads (a formula of channels) and
one-to-one mappings (forward / backward, checked to be inverse).  They nest; a
dependency loop is refused when it would be defined.  Setting a composite channel
computes everything it drives (through nested composites, in dependency order),
checks every value against its limits before anything is written, and moves all
hardware targets together so that no alias, variable or mapping exceeds its ramp
rate, also along non-linear relations.

Special read-only channels ``@time`` (unix time) are always available.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from ..common.errors import ChannelError, ExpressionError, LimitError, OutOfRange
from .composites import CompositeBase, as_value, find_cycle
from .instrument import ChannelSpec, InstrumentManager
from .logsys import get_logger

log = get_logger("channels")

RAMP_INTERVAL = 0.05  # seconds between ramp steps
TRUST_LAST_S = 2.0    # a value we set less than this long ago is not read back

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


def _max_abs_diff(a, b) -> float:
    try:
        d = np.abs(np.asarray(a, dtype=float) - np.asarray(b, dtype=float))
        return float(np.nanmax(d)) if d.size else 0.0
    except (TypeError, ValueError):
        return float("inf")


def _close(a, b) -> bool:
    try:
        aa = np.asarray(a, dtype=float)
        return _max_abs_diff(a, b) <= 1e-9 * (1.0 + float(np.max(np.abs(aa))) if aa.size else 1.0)
    except (TypeError, ValueError):
        return False


GROUP = "§"             # prefix of the pseudo node of a multivariate composite in plans
MAX_DEPTH = 64          # nesting deeper than this is refused (and means a loop at run time)


class ChannelManager:
    """Resolves names to aliases/composites and performs protected get/set."""

    def __init__(self, instruments: InstrumentManager):
        self.instruments = instruments
        self._aliases: Dict[str, Alias] = {}
        self._composites: Dict[str, CompositeBase] = {}
        self._owner: Dict[str, CompositeBase] = {}   # composite channel -> its composite
        self._last: Dict[str, Any] = {}        # last value written per alias / composite channel
        self._last_time: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._set_locks: Dict[str, threading.RLock] = {}
        self._reading = threading.local()       # per thread: composite reads in progress
        self.listeners: List[Callable[[str, Any], None]] = []

    # -- definition --------------------------------------------------------------
    def add_alias(self, alias: Alias, replace: bool = False) -> None:
        try:
            _, spec = self.instruments.resolve(alias.target)
        except ChannelError:
            spec = None          # instrument not available yet: checked again on use
        if spec is not None and spec.config:
            raise ChannelError("%s is a configuration channel; configuration channels are set "
                               "directly and cannot be aliased" % alias.target)
        with self._lock:
            if alias.name in self._owner or alias.name in self._composites or \
                    (alias.name in self._aliases and not replace):
                raise ChannelError("channel name %r already in use" % alias.name)
            old = self._aliases.get(alias.name)
            self._aliases[alias.name] = alias
            if old is not None:
                try:
                    self._validate(self._composites)   # composites using it still work?
                except ChannelError:
                    self._aliases[alias.name] = old
                    raise
            self._last.pop(alias.name, None)
        log.info("alias %s -> %s defined", alias.name, alias.target)

    def remove_alias(self, name: str) -> None:
        with self._lock:
            users = [c.name for c in self._composites.values() if name in c.references()]
            if users:
                raise ChannelError("alias %s is used by composite(s) %s" % (name, ", ".join(users)))
            if self._aliases.pop(name, None) is None:
                raise ChannelError("no alias %r" % name)

    def add_composite(self, comp: CompositeBase, replace: bool = False) -> List[str]:
        """Add (or with *replace* redefine) a composite; returns warnings.  Refused if a
        channel it uses does not exist or is of the wrong kind, if a name is taken, or
        if it would close a dependency loop."""
        with self._lock:
            if comp.name in self._composites and not replace:
                raise ChannelError("composite %r already exists" % comp.name)
            cand = dict(self._composites)
            old = cand.pop(comp.name, None)
            if old is not None:
                gone = set(old.channels()) - set(comp.channels())
                users = [c.name for c in cand.values() if gone & set(c.references())]
                if users:
                    raise ChannelError("composite %s: %s still used by %s" % (
                        comp.name, ", ".join(sorted(gone)), ", ".join(users)))
            cand[comp.name] = comp
            warnings = self._validate(cand, comp)
            self._composites = cand
            self._reindex()
            for ch in (old.channels() if old is not None else []):
                if ch not in comp.channels() or comp.type != "multi":
                    self._last.pop(ch, None)
            for ch in comp.channels():
                if comp.type != "multi":
                    self._last.pop(ch, None)
        log.info("composite %s (%s) defined: %s", comp.name, comp.type, ", ".join(comp.channels()))
        return warnings

    def remove_composite(self, name: str) -> None:
        with self._lock:
            c = self._composites.get(name)
            if c is None:
                raise ChannelError("no composite %r" % name)
            users = [o.name for o in self._composites.values()
                     if o is not c and set(c.channels()) & set(o.references())]
            if users:
                raise ChannelError("composite %s is used by %s" % (name, ", ".join(users)))
            del self._composites[name]
            self._reindex()
            for ch in c.channels():
                self._last.pop(ch, None)

    def _reindex(self) -> None:
        self._owner = {ch: c for c in self._composites.values() for ch in c.channels()}

    def _validate(self, comps: Dict[str, CompositeBase], new: Optional[CompositeBase] = None
                  ) -> List[str]:
        """Check a whole set of composites against the aliases; raise on problems."""
        owner: Dict[str, CompositeBase] = {}
        for c in comps.values():
            for ch in c.channels():
                if ch in self._aliases:
                    raise ChannelError("composite %s: channel name %r is an alias" % (c.name, ch))
                if ch in owner:
                    raise ChannelError("channel %s is defined by both %s and %s"
                                       % (ch, owner[ch].name, c.name))
                owner[ch] = c
        for c in comps.values():
            if c.name in self._aliases:
                raise ChannelError("composite name %r is an alias" % c.name)
            if c.name in owner and owner[c.name] is not c:
                raise ChannelError("composite name %r is a channel of %s"
                                   % (c.name, owner[c.name].name))

        def kind(n):
            if n in self._aliases:
                return "alias"
            o = owner.get(n)
            if o is None:
                return None
            return "variable" if o.type == "multi" else o.type

        def writable_target(c, t, scalar):
            k = kind(t)
            if k is None:
                raise ChannelError("composite %s: %s does not exist (define it first: an alias, "
                                   "mapping or variable)" % (c.name, t))
            if k == "read":
                raise ChannelError("composite %s: %s is a composite read channel, it cannot be "
                                   "set" % (c.name, t))
            if k == "alias":
                a = self._aliases[t]
                if a.read_only:
                    raise ChannelError("composite %s: alias %s is read-only" % (c.name, t))
                try:
                    _, spec = self.instruments.resolve(a.target)
                except ChannelError:
                    return                       # instrument offline: checked on use
                if not spec.writable or spec.dtype == "str":
                    raise ChannelError("composite %s: alias %s (%s) cannot be set to numbers"
                                       % (c.name, t, a.target))
                if scalar and spec.shape not in ((), None):
                    raise ChannelError("composite %s: alias %s is not a scalar channel"
                                       % (c.name, t))

        warnings: List[str] = []
        for c in comps.values():
            if c.type == "multi":
                for t in c.targets:
                    writable_target(c, t, scalar=False)
                for v in c.unused():
                    warnings.append("composite %s: variable %s drives no target" % (c.name, v))
            elif c.type == "map":
                writable_target(c, c.target, scalar=True)
                a = self._aliases.get(c.target)
                lo, hi = c.image
                if a is not None and ((a.min is not None and lo < a.min) or
                                      (a.max is not None and hi > a.max)):
                    warnings.append("mapping %s: forward gives %s in [%g, %g], beyond the limits "
                                    "of %s [%s, %s]: those x will be refused" % (
                                        c.name, c.target, lo, hi, c.target, a.min, a.max))
            elif c.type == "read":
                c.resolve_inputs(lambda n: kind(n) is not None)
        edges: Dict[str, List[str]] = {}
        for c in comps.values():
            for a, b in c.edges():
                edges.setdefault(a, []).append(b)
        cyc = find_cycle(edges, new.channels() if new is not None else ())
        if cyc:
            raise ChannelError("circular dependency: %s" % " → ".join(cyc))
        return warnings

    def clear(self) -> None:
        with self._lock:
            self._aliases.clear()
            self._composites.clear()
            self._owner.clear()
            self._last.clear()

    def aliases(self) -> List[Alias]:
        with self._lock:
            return list(self._aliases.values())

    def composites(self) -> List[CompositeBase]:
        with self._lock:
            return list(self._composites.values())

    def get_alias(self, name: str) -> Alias:
        with self._lock:
            a = self._aliases.get(name)
        if a is None:
            raise ChannelError("no alias %r" % name)
        return a

    def kind(self, name: str) -> Optional[str]:
        """``alias``, ``variable``, ``map``, ``read`` or None (raw / unknown)."""
        if name in self._aliases:
            return "alias"
        o = self._owner.get(name)
        if o is None:
            return None
        return "variable" if o.type == "multi" else o.type

    def is_composite(self, name: str) -> bool:
        return name in self._owner

    def group_of(self, name: str) -> Optional[str]:
        """The multivariate composite a variable belongs to (None otherwise)."""
        o = self._owner.get(name)
        return o.name if o is not None and o.type == "multi" else None

    def exists(self, name: str) -> bool:
        if name.startswith("@"):
            return name in ("@time",)
        if name in self._aliases or name in self._owner:
            return True
        try:
            self.instruments.resolve(name)
            return True
        except Exception:
            return False

    def aliases_behind(self, name: str) -> set:
        """The aliases a channel ultimately writes or reads (through composites)."""
        out, seen, todo = set(), set(), [name]
        while todo:
            n = todo.pop()
            if n in seen:
                continue
            seen.add(n)
            if n in self._aliases:
                out.add(n)
                continue
            o = self._owner.get(n)
            if o is not None:
                todo += [b for a, b in o.edges() if a == n]
        return out

    def instruments_behind(self, name: str) -> set:
        if "." in name:
            return {name.split(".", 1)[0]}
        return {self._aliases[a].target.split(".", 1)[0] for a in self.aliases_behind(name)}

    # -- introspection -------------------------------------------------------------
    def info(self, name: str) -> dict:
        """Type information: dtype, shape, readable, writable, unit."""
        if name == "@time":
            return {"name": name, "kind": "special", "dtype": "float", "shape": [],
                    "readable": True, "writable": False, "unit": "s"}
        with self._lock:
            a = self._aliases.get(name)
            o = self._owner.get(name)
        if o is not None:
            return self._virtual_info(o, name)
        if a is not None:
            spec = self._alias_spec(a)
            return {"name": name, "kind": "alias", "target": a.target, "dtype": spec.dtype,
                    "shape": None if spec.shape is None else list(spec.shape),
                    "readable": spec.readable, "writable": spec.writable and not a.read_only,
                    "unit": a.unit or spec.unit, "min": a.min, "max": a.max,
                    "ramp_rate": a.ramp_rate, "choices": self._choices(a, spec)}
        h, spec = self.instruments.resolve(name)
        return {"name": name, "kind": "raw", "dtype": spec.dtype,
                "shape": None if spec.shape is None else list(spec.shape),
                "readable": spec.readable, "writable": spec.writable and spec.config,
                "unit": spec.unit, "config": spec.config,
                "choices": None if spec.allowed is None else list(spec.allowed)}

    def _virtual_info(self, o: CompositeBase, name: str) -> dict:
        base = {"name": name, "composite": o.name, "dtype": "float", "readable": True,
                "choices": None, "description": o.description}
        if o.type == "multi":
            v = o.var[name]
            base.update(kind="variable", shape=[], writable=True, unit=v.unit, min=v.min,
                        max=v.max, ramp_rate=v.ramp_rate, description=v.description or
                        o.description)
        elif o.type == "map":
            base.update(kind="map", shape=[], writable=True, unit=o.unit, min=o.min, max=o.max,
                        ramp_rate=o.ramp_rate, target=o.target)
        else:
            base.update(kind="read", shape=None, writable=False, unit=o.unit, min=None,
                        max=None, ramp_rate=None)
        return base

    @staticmethod
    def _choices(a: Alias, spec: ChannelSpec):
        """Values a front end should offer for an alias (None = free input)."""
        if spec.dtype == "str" and a.allowed:
            if spec.allowed is None:
                return list(a.allowed)
            return [spec.match_allowed(v) for v in a.allowed if spec.match_allowed(v) is not None]
        if spec.allowed is not None and a.scale == 1.0 and a.offset == 0.0:
            return list(spec.allowed)
        return None

    def value_axes(self, name: str) -> Optional[list]:
        """Coordinates of the value axes of an array channel (alias or
        ``instrument.channel``) whose driver names them (``ChannelSpec.axes``):
        ``[{"name", "unit", "coords"} or None per axis]``, or None."""
        with self._lock:
            a = self._aliases.get(name)
        target = a.target if a is not None else name
        if self.is_composite(name) or "." not in target or target.startswith("@"):
            return None
        h, spec = self.instruments.resolve(target)
        if not spec.axes:
            return None
        inst = target.split(".", 1)[0]
        out = []
        for ax in spec.axes:
            if ax is None:
                out.append(None)
                continue
            aspec = h.spec(ax)
            coords = np.asarray(h.read(ax), dtype=float).ravel()
            out.append({"name": ax, "unit": aspec.unit, "label": aspec.description or ax,
                        "channel": "%s.%s" % (inst, ax), "coords": coords})
        return out

    def _alias_spec(self, a: Alias) -> ChannelSpec:
        _, spec = self.instruments.resolve(a.target)
        return spec

    def forget_instrument(self, instrument: str) -> None:
        """Drop cached values of every alias on *instrument* (after an action such
        as a reset, the instrument state is unknown and must be read back)."""
        prefix = instrument + "."
        for a in self.aliases():
            if a.target.startswith(prefix):
                self._last.pop(a.name, None)
        for c in self.composites():
            if c.type == "map":
                self._last.pop(c.name, None)

    def describe(self) -> dict:
        out_a = []
        for a in self.aliases():
            d = a.to_dict()
            try:
                inf = self.info(a.name)
                d.update(dtype=inf["dtype"], shape=inf["shape"], readable=inf["readable"],
                         writable=inf["writable"], choices=inf["choices"],
                         display_unit=inf["unit"])     # alias unit, else the instrument's
            except Exception as exc:
                d.update(error=str(exc))
            d["last"] = self._last.get(a.name)
            out_a.append(d)
        out_c, virtual = [], []
        for c in self.composites():
            d = c.to_dict()
            if c.type == "multi":
                d["last"] = {v: self._last.get(v) for v in c.channels()}
            elif c.type == "map":
                d["last"] = self._last.get(c.name)
            out_c.append(d)
            for ch in c.channels():
                inf = self._virtual_info(c, ch)
                inf["last"] = self._last.get(ch)
                virtual.append(inf)
        return {"aliases": out_a, "composites": out_c, "virtual": virtual}

    def last_value(self, name: str):
        return self._last.get(name)

    # -- validation --------------------------------------------------------------
    def check_value(self, name: str, value: Any, partial: bool = False) -> Any:
        """Validate a value against protection; returns the canonical value.  For a
        composite channel everything it would write is checked; with *partial*
        targets that need a variable that was never set are skipped (sweep
        validation: the sweep sets it first)."""
        with self._lock:
            a = self._aliases.get(name)
            k = self.kind(name)
        if k in ("variable", "map"):
            self.check_many({name: value}, partial=partial)
            return self._to_float(name, value)
        if k == "read":
            raise ChannelError("%s is a composite read channel (read-only)" % name)
        if a is not None:
            if a.read_only:
                raise ChannelError("alias %s is read-only" % name)
            spec = self._alias_spec(a)
            if not spec.writable:
                raise ChannelError("alias %s (%s) is not writable" % (name, a.target))
            if spec.dtype == "str":
                from .instrument import coerce_value
                v = coerce_value(str(value), spec, "write")   # instrument's own list
                if a.allowed and not _in_allowed(v, a.allowed):
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
        spec = self._raw_config_spec(name)
        if spec is not None:
            if not spec.writable:
                raise ChannelError("%s is read-only" % name)
            from .instrument import coerce_value
            return coerce_value(value, spec, "write")
        if "." in name:
            self.instruments.resolve(name)     # says which channels the instrument has now
            raise ChannelError("%s is a measurement channel: define an alias to set it (limits "
                               "and ramps apply); only configuration channels are set directly"
                               % name)
        raise ChannelError("unknown channel %r" % name)

    def check_many(self, values: Dict[str, Any], partial: bool = False) -> Dict[str, Any]:
        """Check setting several channels at once (variables of one composite together);
        returns every value that would be written."""
        origins: Dict[str, str] = {}
        plan = self._plan(self._start_values(values), partial=partial, origins=origins)
        self._check_plan(plan, origins)
        return plan

    def _check_plan(self, plan: Dict[str, Any], origins: Dict[str, str]) -> None:
        """Limits of every channel of a plan.  A value a multivariate composite
        computed that is out of range raises :class:`OutOfRange`, naming the
        composite and its out-of-range action (a sweep skips such points)."""
        for n, v in plan.items():
            try:
                self._check_node(n, v)
            except OutOfRange:
                raise
            except LimitError as exc:
                c = self._composites.get(origins.get(n, ""))
                if c is None or c.type != "multi":
                    raise
                mode = "skip" if c.out_of_range == "skip" else "error"
                raise OutOfRange("%s (computed by composite %s%s)" % (
                    exc, c.name, ", which skips such points in sweeps" if mode == "skip" else ""),
                    c.name, mode) from None

    def check_point(self, values: Dict[str, Any]) -> Optional[str]:
        """For a sweep point: ``None`` if *values* can be set, the reason if a
        composite with ``out_of_range="skip"`` puts a target out of range (the point
        is to be skipped).  Every other problem raises."""
        try:
            self.check_many(values)
        except OutOfRange as exc:
            if exc.mode == "skip":
                return str(exc)
            raise
        return None

    def limits_of(self, n: str):
        """``(min, max)`` of a settable channel (alias, variable or mapping); ``None``
        where there is no limit."""
        k = self.kind(n)
        if k == "alias":
            a = self._aliases[n]
            return a.min, a.max
        if k == "variable":
            var = self._owner[n].var[n]
            return var.min, var.max
        if k == "map":
            m = self._owner[n]
            return m.min, m.max
        return None, None

    def _raw_config_spec(self, name: str):
        """The spec of ``instrument.channel`` if it is a configuration channel."""
        if "." not in name:
            return None
        try:
            _, spec = self.instruments.resolve(name)
        except ChannelError:
            return None
        return spec if spec.config else None

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

    # -- composite plans ---------------------------------------------------------------
    def _start_values(self, values: Dict[str, Any]) -> Dict[str, Any]:
        out = {}
        for n, v in values.items():
            k = self.kind(n)
            if k in ("variable", "map"):
                out[n] = self._to_float(n, v)
            elif k == "alias":
                out[n] = self.check_value(n, v)
            elif k == "read":
                raise ChannelError("%s is a composite read channel (read-only)" % n)
            else:
                raise ChannelError("%s is not an alias or composite channel" % n)
        return out

    def _write_succ(self, n: str) -> List[str]:
        if n.startswith(GROUP):
            return list(self._composites[n[1:]].targets)
        o = self._owner.get(n)
        if o is None:
            return []
        if o.type == "multi":
            return [GROUP + o.name]
        if o.type == "map":
            return [o.target]
        return []

    def _plan(self, start: Dict[str, Any], partial: bool = False,
              origins: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Every channel a write of *start* sets, with its value: variables of
        multivariate composites drive their targets (the other variables at their
        last values), mappings their target; processed in dependency order so that a
        composite fed by several changed variables is evaluated once with all of them.
        Targets of a composite with ``out_of_range="clamp"`` are clamped to their
        range.  *origins*, if given, receives the source of every value (the
        composite that computed it, or ``"the request"``)."""
        order: List[str] = []
        state: Dict[str, int] = {}
        for root in start:                         # depth-first post-order, iterative
            if state.get(root):
                continue
            stack = [(root, iter(self._write_succ(root)))]
            state[root] = 1
            while stack:
                n, it = stack[-1]
                nxt = next(it, None)
                if nxt is None:
                    stack.pop()
                    state[n] = 2
                    order.append(n)
                    continue
                st = state.get(nxt, 0)
                if st == 1:
                    path = [x for x, _ in stack] + [nxt]
                    raise ChannelError("circular dependency: %s" % " → ".join(
                        p.lstrip(GROUP) for p in path[path.index(nxt):]))
                if st == 0:
                    if len(stack) > MAX_DEPTH:
                        raise ChannelError("composites nested deeper than %d" % MAX_DEPTH)
                    state[nxt] = 1
                    stack.append((nxt, iter(self._write_succ(nxt))))
        vals: Dict[str, Any] = {}
        origin: Dict[str, str] = {}

        def assign(n, v, src):
            if n in vals and not _close(vals[n], v):
                raise ChannelError("%s would be set to two values at once (%s from %s, %s from "
                                   "%s)" % (n, _fmt(vals[n]), origin[n], _fmt(v), src))
            vals[n] = v
            origin.setdefault(n, src)
        for n, v in start.items():
            assign(n, v, "the request")
        for n in reversed(order):
            if n.startswith(GROUP):
                c = self._composites[n[1:]]
                changed = {v for v in c.channels() if v in vals}
                if not changed:
                    continue
                env = {}
                for v in c.channels():
                    x = vals.get(v, self._last.get(v))
                    env[v] = float("nan") if x is None else float(x)
                for t, deps in c.deps.items():
                    if not changed & set(deps):
                        continue
                    missing = [d for d in deps if not math.isfinite(env[d])]
                    if missing:
                        if partial:
                            continue
                        one = len(missing) == 1
                        raise ChannelError(
                            "%s: %s %s never been set and %s needs %s: set %s first, or set "
                            "the variables together (set_many; in a sweep: in the same "
                            "dimension; manual panel: Set all)" % (
                                c.name, ", ".join(missing), "has" if one else "have", t,
                                "it" if one else "them", "it" if one else "them"))
                    try:
                        tv = as_value(c.exprs[t].evaluate({d: env[d] for d in deps}))
                    except (ExpressionError, ChannelError) as exc:
                        raise ChannelError("composite %s, target %s: %s" % (c.name, t, exc)) \
                            from None
                    if not np.isfinite(tv).all():
                        raise OutOfRange("composite %s: target %s would be %s at %s" % (
                            c.name, t, _fmt(tv), ", ".join("%s=%g" % (d, env[d]) for d in deps)),
                            c.name, "skip" if c.out_of_range == "skip" else "error")
                    if c.out_of_range == "clamp":
                        lo, hi = self.limits_of(t)
                        if lo is not None or hi is not None:
                            tv = as_value(np.clip(tv, -np.inf if lo is None else lo,
                                                  np.inf if hi is None else hi))
                    assign(t, tv, c.name)
            elif n in vals:
                o = self._owner.get(n)
                if o is not None and o.type == "map":
                    # forward only inside its range
                    self._check_plan({n: vals[n]}, origin)
                    try:
                        y = o.fwd(vals[n])
                    except (ExpressionError, ChannelError) as exc:
                        raise ChannelError(str(exc)) from None
                    assign(o.target, y, o.name)
        if origins is not None:
            origins.update(origin)
        return vals

    def _check_node(self, n: str, v) -> None:
        """Limits of one channel of a plan (an alias also: writable, scalar …)."""
        k = self.kind(n)
        if k == "alias":
            self.check_value(n, v)
        elif k == "variable":
            var = self._owner[n].var[n]
            self._check_limits(n, v, var.min, var.max)
        elif k == "map":
            m = self._owner[n]
            x = float(v)
            tol = 1e-9 * (m.max - m.min)
            if not m.min - tol <= x <= m.max + tol:
                raise LimitError("%s: %g is outside the range of the mapping [%g, %g]"
                                 % (n, x, m.min, m.max))
        else:
            raise ChannelError("%s cannot be set" % n)

    def _check_node_fast(self, n: str, v) -> None:
        """Limits only (ramp paths: the kinds were checked with the plan)."""
        if n in self._aliases:
            a = self._aliases[n]
            if not np.isfinite(np.asarray(v, dtype=float)).all():
                raise LimitError("%s: ramp path contains NaN/inf" % n)
            self._check_limits(n, v, a.min, a.max)
        else:
            self._check_node(n, v)

    def _rate_of(self, n: str):
        """(ramp_rate, ramp_step) of a plan channel."""
        if n in self._aliases:
            a = self._aliases[n]
            return a.ramp_rate, a.ramp_step
        o = self._owner.get(n)
        if o is None:
            return None, None
        if o.type == "multi":
            return o.var[n].ramp_rate, None
        return getattr(o, "ramp_rate", None), None

    # -- read ----------------------------------------------------------------------
    def get(self, name: str) -> Any:
        if name == "@time":
            return time.time()
        with self._lock:
            a = self._aliases.get(name)
            o = self._owner.get(name)
        if o is not None:
            return self._get_virtual(o, name)
        if a is not None:
            h, spec = self.instruments.resolve(a.target)
            raw = h.read(spec.name)
            return self._from_raw(a, spec, raw)
        h, spec = self.instruments.resolve(name)
        return h.read(spec.name)

    def _get_virtual(self, o: CompositeBase, name: str):
        stack = getattr(self._reading, "stack", None)
        if stack is None:
            stack = self._reading.stack = []
        if name in stack:
            raise ChannelError("circular dependency: %s" % " → ".join(stack[stack.index(name):]
                                                                       + [name]))
        if len(stack) > MAX_DEPTH:
            raise ChannelError("composites nested deeper than %d" % MAX_DEPTH)
        stack.append(name)
        try:
            if o.type == "multi":
                v = self._last.get(name)
                return float("nan") if v is None else float(v)
            if o.type == "map":
                y = self.get(o.target)
                return o.bwd(_scalar_reading(o.target, y))
            vals = {i: self.get(i) for i in o.inputs}
            try:
                return as_value(o.expr.evaluate(vals))
            except (ExpressionError, ChannelError) as exc:
                raise ChannelError("composite %s: %s" % (o.name, exc)) from None
        finally:
            stack.pop()

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
        # a value we wrote ourselves moments ago (e.g. the previous sweep step) is
        # trusted; anything older is verified by reading the instrument back
        fresh = ok(self._last.get(a.name))
        if fresh is not None and time.monotonic() - self._last_time.get(a.name, -1e9) < TRUST_LAST_S:
            return fresh
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
                if a.allowed and not _in_allowed(str(value), a.allowed):
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
        """Protected write to an alias or composite channel.  Returns the value set."""
        k = self.kind(name)
        if k in ("variable", "map", "read"):
            plan = self.set_many({name: value}, ramp=ramp, abort_check=abort_check)
            return plan[name]
        value = self.check_value(name, value)
        if k is None:
            # configuration channel: written directly, no protection / ramping
            h, spec = self.instruments.resolve(name)
            h.write(spec.name, value)
            self._notify(name, value)
            return value
        with self._set_lock(name):
            self._set_alias(self.get_alias(name), value, ramp, abort_check)
        self._notify(name, value)
        return value

    def set_many(self, values: Dict[str, Any], ramp: bool = True,
                 abort_check: AbortCheck = None) -> Dict[str, Any]:
        """Set several channels together (e.g. two variables of one composite at the
        same point of a sweep): everything they drive is computed and checked first,
        then all hardware targets move together.  Returns every value written."""
        start = self._start_values(values)
        origins: Dict[str, str] = {}
        plan = self._plan(start, origins=origins)
        self._check_plan(plan, origins)
        leaves = sorted(n for n in plan if n in self._aliases)
        locks = [self._set_lock(n) for n in leaves]
        for lk in locks:
            lk.acquire()
        try:
            self._write_plan(start, plan, leaves, ramp, abort_check)
        finally:
            for lk in reversed(locks):
                lk.release()
        for n, v in plan.items():
            self._notify(n, v)
        return plan

    def _write_plan(self, start, plan, leaves, ramp, abort_check) -> None:
        items = []
        for n in leaves:
            a = self._aliases[n]
            items.append((a, self._alias_spec(a)))
        rated = {n: r for n in plan for r in [self._rate_of(n)] if r[0] or r[1]}
        if not (ramp and rated):
            for a, spec in items:
                v = np.asarray(plan[a.name], dtype=float)
                self._write_alias_now(a, spec, float(v) if v.ndim == 0 else v)
            self._remember(plan, skip=leaves)
            return
        starts = {}
        for a, spec in items:
            tv = np.asarray(plan[a.name], dtype=float)
            st = self._current_alias_value(a, spec, tv.shape)
            if st is None:
                raise LimitError("%s: cannot determine the present value of %s to ramp from; "
                                 "set it once with ramp=False to force"
                                 % (", ".join(start), a.name))
            starts[a.name] = st
        # the relation is followed from where the composites were last set, if the
        # hardware is still there; otherwise every target moves on a straight line
        origin = {}
        for n in start:
            x0 = self._last.get(n) if n not in self._aliases else starts.get(n)
            if x0 is None and self.kind(n) == "map":
                try:
                    x0 = self.get(n)
                except Exception:
                    x0 = None
            origin[n] = x0
        on_path = False
        if all(x is not None and np.isfinite(np.asarray(x, dtype=float)).all()
               for x in origin.values()):
            try:
                expected = self._plan(origin)
                on_path = all(
                    _max_abs_diff(expected[n], starts[n]) <=
                    1e-6 * (1.0 + float(np.max(np.abs(starts[n])))) for n in leaves)
            except Exception:
                on_path = False
        if on_path:
            x0 = {n: np.asarray(origin[n], dtype=float) for n in start}
            x1 = {n: np.asarray(start[n], dtype=float) for n in start}

            def path(u):
                if u >= 1.0:
                    return plan
                return self._plan({n: as_value(x0[n] + (x1[n] - x0[n]) * u) for n in start})
            self._ramp(items, path, 257, abort_check, rated)
        else:
            tv = {n: np.asarray(plan[n], dtype=float) for n in leaves}
            self._ramp(items, lambda u: {n: starts[n] + (tv[n] - starts[n]) * u for n in leaves},
                       2, abort_check, {n: r for n, r in rated.items() if n in starts})
        self._remember(plan, skip=leaves)

    def _remember(self, values: Dict[str, Any], skip=()) -> None:
        now = time.monotonic()
        for n, v in values.items():
            if n in skip:
                continue
            v = np.asarray(v, dtype=float)
            self._last[n] = float(v) if v.ndim == 0 else v
            self._last_time[n] = now

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
        self._last_time[a.name] = time.monotonic()

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
        self._ramp([(a, spec)], lambda u: {a.name: start + (target - start) * u}, 2, abort_check,
                   {a.name: (a.ramp_rate, a.ramp_step)})

    def _ramp(self, items, path, nsamples: int, abort_check: AbortCheck,
              rated: Dict[str, tuple]) -> None:
        """Move aliases along ``path(u)`` (``{channel: value}`` for ``u`` 0 … 1; the
        channels include the composite channels the aliases are driven through).

        The path is sampled first: every sample of every channel is limit-checked
        *before* anything is written, and the cumulative "time cost" (max over the
        channels with a ramp rate of distance / rate) and "step cost" (distance /
        ramp_step) are tabulated.  Progress is then limited so that no ramp rate (of
        an alias, a variable or a mapping) and no step size is exceeded, also for
        non-linear (composite) paths.
        """
        us = np.linspace(0.0, 1.0, max(2, int(nsamples)))
        samples = []
        for u in us:
            vals = path(float(u))
            for n, v in vals.items():
                self._check_node_fast(n, v)
            samples.append(vals)
        ct = [0.0]
        cs = [0.0]
        for i in range(1, len(us)):
            dt = ds = 0.0
            for n, (rate, step) in rated.items():
                if n not in samples[i] or n not in samples[i - 1]:
                    continue
                d = _max_abs_diff(samples[i - 1][n], samples[i][n])
                if rate:
                    dt = max(dt, d / rate)
                if step:
                    ds = max(ds, d / step)
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

        leaf_names = {a.name for a, _ in items}
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
            vals = path(u) if u < 1.0 else samples[-1]
            for n, v in vals.items():
                self._check_node_fast(n, v)
            for a, spec in items:
                v = np.asarray(vals[a.name], dtype=float)
                self._write_alias_now(a, spec, float(v) if v.ndim == 0 else v)
            self._remember(vals, skip=leaf_names)        # composite channels follow along
            if u >= 1.0:
                return
            last_u = u
            time.sleep(RAMP_INTERVAL)


def _scalar_reading(name: str, v) -> float:
    a = np.asarray(v, dtype=float)
    if a.size != 1:
        raise ChannelError("%s gives an array; a mapping needs a scalar" % name)
    return float(a.reshape(()))


def _in_allowed(v: str, allowed) -> bool:
    from .instrument import _text_key
    key = _text_key(v)
    return any(_text_key(a) == key for a in allowed)


def _fmt(v) -> str:
    a = np.asarray(v)
    if a.ndim == 0:
        return "%g" % float(a)
    return "array%s[min %g, max %g]" % (a.shape, float(np.nanmin(a)), float(np.nanmax(a)))
