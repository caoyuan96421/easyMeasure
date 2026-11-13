"""Channels: one namespace, one way to read and write.

Every name in a setup is a **channel** (or the name of the thing that owns some):

==============  ==========================  ==========================================
kind            name                        access
==============  ==========================  ==========================================
``hardware``    ``instrument.channel``      what the driver says (r, w, rw)
``config``      ``instrument.setting``      what the driver says (a configuration channel)
``action``      ``instrument.action``       w: writing it runs the action
``alias``       ``Vg``                      the target's, without w if read-only
``variable``    ``composite.variable``      rw (multivariate composite)
``map``         ``name``                    rw (one-to-one mapping)
``read``        ``name``                    r (composite read: a formula of channels)
``parameter``   ``name``                    rw for a value, r for a formula
``user_action`` ``name``                    w: writing it runs the user's code
``special``     ``@time``                   r
==============  ==========================  ==========================================

``get``, ``set``, ``set_many``, ``check_value`` and ``info`` take any of them; the
access is respected and nothing else is special-cased.  Names are unique across
all kinds (aliases, parameters, composites and their channels, user actions,
instruments): a name that is taken is refused when it is defined.

Writing a hardware channel is a plain write: no limits, no ramp.  Protection
(``min`` / ``max``, ``ramp_rate``, ``ramp_step``, ``scale`` / ``offset``,
``allowed``, ``read_only``) belongs to aliases.  Writing an action (instrument or
user) runs it; the value, if any, is ignored.

Composite channels (:mod:`.composites`) are formulas of channels: multivariate
writes (variables driving several targets), composite reads and one-to-one
mappings.  Their targets and inputs may be any channel of the right access
(aliases, other composite channels, hardware channels).  They nest; a dependency
loop is refused when it would be defined.  Setting a composite channel computes
everything it drives, checks every value against its limits before anything is
written, and moves all targets together so that no ramp rate is exceeded, also
along non-linear relations.
"""

from __future__ import annotations

import contextlib
import contextvars
import itertools
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from ..common.errors import ChannelError, ExpressionError, LimitError, OutOfRange, RampStopped
from ..common.exprs import reserved
from .composites import CompositeBase, as_value, find_cycle
from .parameters import Parameter, compute as compute_parameters, users_of as parameter_users
from .instrument import ChannelSpec, InstrumentManager
from .logsys import get_logger

log = get_logger("channels")

RAMP_INTERVAL = 0.05  # seconds between ramp steps
PATH_SAMPLES = 257    # samples of a composite path, all limit-checked before a ramp

#: the stop token of the direct write running in this thread (see ``stoppable_ramps``)
_RAMP_TOKEN: contextvars.ContextVar = contextvars.ContextVar("ramp_token", default=None)
_RAMP_IDS = itertools.count(1)


class RampToken:
    """Lets ``stop_ramps()`` stop the ramps of one direct write from another thread."""

    def __init__(self, source: str = ""):
        self.source = source
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()
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
    def identity(cls, ref: str) -> "Alias":
        """The plain write path of a hardware channel (no limits, ramp or scaling):
        how a composite or a sweep writes ``instrument.channel``."""
        a = cls.__new__(cls)
        a.name = a.target = ref
        a.min = a.max = a.ramp_rate = a.ramp_step = None
        a.scale, a.offset, a.unit, a.allowed, a.read_only, a.description = \
            1.0, 0.0, "", None, False, ""
        return a

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
        self._params: Dict[str, Parameter] = {}      # parameters, in definition order
        self._pvals: Dict[str, float] = {}           # their values
        self._owner: Dict[str, CompositeBase] = {}   # composite channel -> its composite
        self._last: Dict[str, Any] = {}        # last value written per alias / composite channel
        self._last_time: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._set_locks: Dict[str, threading.RLock] = {}
        self._reading = threading.local()       # per thread: composite reads in progress
        self.listeners: List[Callable[[str, Any], None]] = []
        #: ramps of direct writes in progress: id -> {channels, token, since}
        self.ramps: Dict[int, dict] = {}
        self._ramps_lock = threading.Lock()
        #: called with the list of active ramps whenever it changes
        self.ramp_listeners: List[Callable[[List[dict]], None]] = []
        #: the user actions (:class:`.user_actions.UserActions`), set by the service
        self.user_actions = None
        self._identity: Dict[str, Alias] = {}   # write paths of hardware channels

    # -- the namespace -----------------------------------------------------------
    def _instrument_names(self) -> List[str]:
        try:
            return list(self.instruments.names())
        except Exception:
            return []

    def _hw(self, name: str):
        """``(kind, handle, spec)`` for ``instrument.name``: kind ``hardware``,
        ``config`` (a ChannelSpec) or ``action`` (an ActionSpec); None otherwise.
        Never does I/O."""
        if not isinstance(name, str) or "." not in name or name.startswith("@"):
            return None
        inst, ch = name.split(".", 1)
        try:
            h = self.instruments.get(inst)
        except ChannelError:
            return None
        spec = h.channels.get(ch)
        if spec is not None:
            return ("config" if spec.config else "hardware"), h, spec
        act = h.actions.get(ch)
        if act is not None:
            return "action", h, act
        return None

    def owner_of(self, name: str) -> Optional[Tuple[str, str]]:
        """``(what, description)`` of whatever has *name* - what is ``alias``,
        ``parameter``, ``composite``, ``user_action``, ``instrument`` or ``channel``
        (of a composite or an instrument, or ``@time``) - or None if it is free."""
        if name in self._aliases:
            return "alias", "an alias"
        if name in self._params:
            return "parameter", "a parameter"
        if name in self._composites:
            return "composite", "a composite"
        o = self._owner.get(name)
        if o is not None:
            return "channel", "a channel of composite %s" % o.name
        ua = self.user_actions
        if ua is not None and ua.has(name):
            return "user_action", "a user action"
        if name in self._instrument_names():
            return "instrument", "an instrument"
        if name == "@time":
            return "channel", "a special channel"
        hw = self._hw(name)
        if hw is not None:
            return "channel", "%s of instrument %s" % (
                "an action" if hw[0] == "action" else "a channel", hw[1].name)
        return None

    def claim(self, name: str, what: str, replace: bool = False) -> None:
        """Refuse *name* for a new *what* (``alias``, ``parameter``, ``composite``,
        ``user_action``, ``instrument``) if anything else has it, or if a *what* of
        that name exists and *replace* is not given."""
        o = self.owner_of(name)
        if o is None:
            return
        if o[0] == what:
            if replace:
                return
            raise ChannelError("%s %r already exists" % (what.replace("_", " "), name))
        raise ChannelError("the name %r is taken: it is already %s" % (name, o[1]))

    # -- definition --------------------------------------------------------------
    def add_alias(self, alias: Alias, replace: bool = False) -> None:
        hw = self._hw(alias.target)
        inst = alias.target.split(".", 1)[0]
        if hw is None and inst in self._instrument_names() and \
                self.instruments.get(inst).channels:
            # the instrument's channels are known: say which it has (unknown, e.g. a
            # dynamic driver never connected: checked when it is used)
            self.instruments.resolve(alias.target)
        if hw is not None and hw[0] == "action":
            raise ChannelError("%s is an action: an alias names a channel with a value"
                               % alias.target)
        with self._lock:
            self.claim(alias.name, "alias", replace)
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

    # -- parameters ---------------------------------------------------------------
    def parameters(self) -> List[Parameter]:
        with self._lock:
            return list(self._params.values())

    def parameter_values(self) -> Dict[str, float]:
        """name -> value of every parameter (formulas evaluated)."""
        return dict(self._pvals)

    def describe_parameters(self) -> List[dict]:
        with self._lock:
            out = []
            for p in self._params.values():
                d = p.to_dict()
                d["current"] = self._pvals.get(p.name)
                d["used_by"] = self._parameter_users(p.name)
                out.append(d)
            return out

    def _parameter_users(self, name: str) -> List[str]:
        return parameter_users(self._params, name) + sorted(
            c.name for c in self._composites.values() if name in c.references())

    def _is_live_channel(self, n: str) -> bool:
        return self.kind(n) not in (None, "parameter")

    def _apply_parameters(self, cand: Dict[str, Parameter]) -> List[str]:
        """Make *cand* the parameters, if their values compute and every composite
        still checks out with them (a mapping using one is checked to be inverse
        again); otherwise raise and keep the old ones."""
        for n in cand:
            self.claim(n, "parameter", replace=True)
        vals = compute_parameters(cand, self._is_live_channel)
        try:
            warnings = self._validate(self._composites, pvals=vals)
        except Exception:
            self._validate(self._composites)              # bind the old values again
            raise
        self._params = cand
        self._pvals = vals
        return warnings

    def add_parameter(self, p: Parameter, replace: bool = False) -> List[str]:
        """Define (or with *replace* redefine) a parameter.  Refused for a name in use,
        a formula with unknown names, live channels or a circle, or if a composite
        would no longer work with the new value."""
        with self._lock:
            if p.name in self._params and not replace:
                raise ChannelError("parameter %r already exists" % p.name)
            cand = dict(self._params)
            cand[p.name] = p
            w = self._apply_parameters(cand)
        log.info("parameter %s = %s", p.name, p.formula or p.value)
        return w

    def check_parameter(self, p: Parameter, replace: bool = False) -> Tuple[float, List[str]]:
        """What :meth:`add_parameter` would say, without defining it: ``(value,
        warnings)``; raises like it."""
        with self._lock:
            if p.name in self._params and not replace:
                raise ChannelError("parameter %r already exists" % p.name)
            cand = dict(self._params)
            cand[p.name] = p
            for n in cand:
                self.claim(n, "parameter", replace=True)
            vals = compute_parameters(cand, self._is_live_channel)
            try:
                warnings = self._validate(self._composites, pvals=vals)
            finally:
                self._validate(self._composites)          # bind the present values again
            return vals[p.name], warnings

    def set_parameter(self, name: str, value: float) -> float:
        """Set the value of a value parameter (formulas follow)."""
        with self._lock:
            p = self._params.get(name)
            if p is None:
                raise ChannelError("no parameter %r" % name)
            if p.is_formula:
                raise ChannelError("%s is read-only: a parameter computed from its formula %s"
                                   % (name, p.formula))
            q = Parameter(name, value=value, unit=p.unit, description=p.description)
            cand = dict(self._params)
            cand[name] = q
            self._apply_parameters(cand)
        log.info("parameter %s set to %g", name, q.value)
        return q.value

    def remove_parameter(self, name: str) -> None:
        with self._lock:
            if name not in self._params:
                raise ChannelError("no parameter %r" % name)
            users = self._parameter_users(name)
            if users:
                raise ChannelError("parameter %s is used by %s" % (name, ", ".join(users)))
            cand = dict(self._params)
            del cand[name]
            self._apply_parameters(cand)

    def load_parameters(self, items: List[Parameter]) -> None:
        """Replace all parameters at once (setups: formulas may come in any order)."""
        with self._lock:
            self._apply_parameters({p.name: p for p in items})

    def _reindex(self) -> None:
        self._owner = {ch: c for c in self._composites.values() for ch in c.channels()}

    def _validate(self, comps: Dict[str, CompositeBase], new: Optional[CompositeBase] = None,
                  pvals: Optional[Dict[str, float]] = None) -> List[str]:
        """Check a whole set of composites against the aliases and the parameters
        (values *pvals*, default the current ones); raise on problems.  Binds the
        parameter values the composites' formulas use."""
        pvals = self._pvals if pvals is None else pvals
        owner: Dict[str, CompositeBase] = {}
        for c in comps.values():
            for ch in c.channels():
                if ch in owner:
                    raise ChannelError("channel %s is defined by both %s and %s"
                                       % (ch, owner[ch].name, c.name))
                owner[ch] = c
        for c in comps.values():
            for nm in dict.fromkeys([c.name] + c.channels()):
                taken = self._taken_besides_composites(nm, pvals)
                if taken:
                    raise ChannelError("composite %s: the name %r is taken: it is already %s"
                                       % (c.name, nm, taken))
            if c.name in owner and owner[c.name] is not c:
                raise ChannelError("composite name %r is a channel of %s"
                                   % (c.name, owner[c.name].name))

        def kind(n):
            if n in self._aliases:
                return "alias"
            if n in pvals:
                return "parameter"
            o = owner.get(n)
            if o is not None:
                return "variable" if o.type == "multi" else o.type
            if n == "@time":
                return "special"
            hw = self._hw(n)
            return hw[0] if hw is not None else None

        def writable_target(c, t, scalar):
            k = kind(t)
            if k is None:
                raise ChannelError("composite %s: %s does not exist (define it first: an alias, "
                                   "mapping or variable, or a channel of an instrument)"
                                   % (c.name, t))
            if k in ("read", "special"):
                raise ChannelError("composite %s: %s is read-only" % (c.name, t))
            if k == "parameter":
                raise ChannelError("composite %s: %s is a parameter, it cannot be set by a "
                                   "composite" % (c.name, t))
            if k == "action":
                raise ChannelError("composite %s: %s is an action, it has no value to set"
                                   % (c.name, t))
            if k in ("alias", "hardware", "config"):
                if k == "alias":
                    a = self._aliases[t]
                    if a.read_only:
                        raise ChannelError("composite %s: alias %s is read-only" % (c.name, t))
                    try:
                        _, spec = self.instruments.resolve(a.target)
                    except ChannelError:
                        return                       # instrument offline: checked on use
                    what = "alias %s (%s)" % (t, a.target)
                else:
                    spec = self._hw(t)[2]
                    what = t
                if not spec.writable or spec.dtype == "str":
                    raise ChannelError("composite %s: %s cannot be set to numbers"
                                       % (c.name, what))
                if scalar and spec.shape not in ((), None):
                    raise ChannelError("composite %s: %s is not a scalar channel"
                                       % (c.name, what))

        warnings: List[str] = []
        for c in comps.values():
            unknown = [n for n in c.externals() if n not in pvals]
            if c.type != "read" and unknown:
                where = ("the variables %s" % ", ".join(c.channels())) if c.type == "multi" \
                    else "x (forward), y (backward)"
                raise ExpressionError("composite %s: unknown name(s) %s (a formula may use %s "
                                      "and parameters)" % (c.name, ", ".join(unknown), where))
            c.penv = {n: pvals[n] for n in c.externals() if n in pvals}
            if c.type == "map" and c.externals():
                c.image = c.check_inverse()       # with the parameters' present values
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
                c.resolve_inputs(lambda n: kind(n) not in (None, "action"))
                wonly = [n for n in c.inputs if kind(n) in ("hardware", "config") and
                         not self._hw(n)[2].readable]
                if wonly:
                    raise ChannelError("composite %s: %s cannot be read (write-only)"
                                       % (c.name, ", ".join(wonly)))
        edges: Dict[str, List[str]] = {}
        for c in comps.values():
            for a, b in c.edges():
                edges.setdefault(a, []).append(b)
        cyc = find_cycle(edges, new.channels() if new is not None else ())
        if cyc:
            raise ChannelError("circular dependency: %s" % " → ".join(cyc))
        return warnings

    def _taken_besides_composites(self, name: str, pvals) -> Optional[str]:
        """What, other than a composite, has *name* (composite validation)."""
        if name in self._aliases:
            return "an alias"
        if name in pvals:
            return "a parameter"
        o = self.owner_of(name)
        if o is not None and o[0] not in ("alias", "parameter", "composite") and \
                not (o[0] == "channel" and name in self._owner):
            return o[1]
        return None

    def clear(self) -> None:
        with self._lock:
            self._aliases.clear()
            self._composites.clear()
            self._owner.clear()
            self._last.clear()
            self._params = {}
            self._pvals = {}

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
        """``hardware``, ``config``, ``action`` (``instrument.name``), ``alias``,
        ``parameter``, ``variable``, ``map``, ``read``, ``user_action``, ``special``,
        or None for an unknown name."""
        if name in self._aliases:
            return "alias"
        if name in self._params:
            return "parameter"
        o = self._owner.get(name)
        if o is not None:
            return "variable" if o.type == "multi" else o.type
        if name == "@time":
            return "special"
        ua = self.user_actions
        if ua is not None and ua.has(name):
            return "user_action"
        hw = self._hw(name)
        return hw[0] if hw is not None else None

    def _unknown(self, name: str):
        """Raise the most helpful error for a name that is not a channel."""
        if isinstance(name, str) and "." in name and \
                name.split(".", 1)[0] in self._instrument_names() and not name.startswith("@"):
            self.instruments.resolve(name)       # "instrument x has no channel y (now: …)"
        raise ChannelError("unknown channel %r" % (name,))

    def access(self, name: str) -> str:
        """``r``, ``w`` or ``rw``."""
        return self.info(name)["access"]

    def is_composite(self, name: str) -> bool:
        return name in self._owner

    def group_of(self, name: str) -> Optional[str]:
        """The multivariate composite a variable belongs to (None otherwise)."""
        o = self._owner.get(name)
        return o.name if o is not None and o.type == "multi" else None

    def exists(self, name: str) -> bool:
        return self.kind(name) is not None

    def hardware_behind(self, name: str) -> set:
        """The instrument channels and actions (``instrument.name``) a channel
        ultimately writes or reads, through aliases and composites."""
        out, seen, todo = set(), set(), [name]
        while todo:
            n = todo.pop()
            if n in seen:
                continue
            seen.add(n)
            a = self._aliases.get(n)
            if a is not None:
                out.add(a.target)
                continue
            o = self._owner.get(n)
            if o is not None:
                todo += [b for x, b in o.edges() if x == n]
            elif self._hw(n) is not None or (
                    "." in n and n.split(".", 1)[0] in self._instrument_names()):
                out.add(n)          # also a channel the instrument does not offer (yet)
        return out

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
        return {r.split(".", 1)[0] for r in self.hardware_behind(name)}

    # -- introspection -------------------------------------------------------------
    def info(self, name: str) -> dict:
        """What a channel is: ``kind``, ``access`` (``readable`` / ``writable``),
        ``dtype``, ``shape``, ``unit``, ``min`` / ``max`` / ``ramp_rate``, ``choices``,
        ``description`` and what is particular to its kind."""
        k = self.kind(name)
        base = {"name": name, "kind": k, "dtype": "float", "shape": [], "unit": "",
                "min": None, "max": None, "ramp_rate": None, "choices": None,
                "description": ""}
        if k is None:
            self._unknown(name)
        if k == "special":
            out = dict(base, access="r", unit="s", description="unix time")
        elif k == "parameter":
            with self._lock:
                p = self._params[name]
            out = dict(base, access="r" if p.is_formula else "rw", unit=p.unit,
                       description=p.description, formula=p.formula or None,
                       value=self._pvals.get(name))
        elif k in ("variable", "map", "read"):
            out = self._virtual_info(self._owner[name], name)
        elif k == "alias":
            a = self._aliases[name]
            spec = self._alias_spec(a)
            acc = spec.access if not a.read_only else spec.access.replace("w", "")
            out = dict(base, access=acc or "r", target=a.target, dtype=spec.dtype,
                       shape=None if spec.shape is None else list(spec.shape),
                       unit=a.unit or spec.unit, min=a.min, max=a.max, ramp_rate=a.ramp_rate,
                       choices=self._choices(a, spec), description=a.description)
        elif k == "user_action":
            ua = self.user_actions.get(name)
            out = dict(base, access="w", dtype=None, shape=None, description=ua.description,
                       confirm=ua.confirm)
        else:                                       # hardware, config, action
            _, h, spec = self._hw(name)
            if k == "action":
                out = dict(base, access="w", dtype=None, shape=None, instrument=h.name,
                           description=spec.description)
            else:
                out = dict(base, access=spec.access, dtype=spec.dtype, instrument=h.name,
                           shape=None if spec.shape is None else list(spec.shape),
                           unit=spec.unit, description=spec.description,
                           config=bool(spec.config),
                           choices=None if spec.allowed is None else list(spec.allowed))
        out["readable"] = "r" in out["access"]
        out["writable"] = "w" in out["access"]
        return out

    def _virtual_info(self, o: CompositeBase, name: str) -> dict:
        base = {"name": name, "composite": o.name, "dtype": "float", "choices": None,
                "description": o.description}
        if o.type == "multi":
            v = o.var[name]
            base.update(kind="variable", access="rw", shape=[], unit=v.unit, min=v.min,
                        max=v.max, ramp_rate=v.ramp_rate, description=v.description or
                        o.description)
        elif o.type == "map":
            base.update(kind="map", access="rw", shape=[], unit=o.unit, min=o.min, max=o.max,
                        ramp_rate=o.ramp_rate, target=o.target)
        else:
            base.update(kind="read", access="r", shape=None, unit=o.unit, min=None,
                        max=None, ramp_rate=None)
        base["readable"] = "r" in base["access"]
        base["writable"] = "w" in base["access"]
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
        if a is None and self.kind(name) not in ("hardware", "config"):
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
        for n in [n for n in self._last if n.startswith(prefix) and n not in self._owner]:
            self._last.pop(n, None)
        for c in self.composites():
            if c.type == "map":
                self._last.pop(c.name, None)

    def channel_names(self) -> List[str]:
        """Every channel: aliases, composite channels, parameters, user actions,
        ``@time``, then the channels and actions of each instrument."""
        with self._lock:
            out = list(self._aliases) + [ch for c in self._composites.values()
                                         for ch in c.channels()] + list(self._params)
        ua = self.user_actions
        if ua is not None:
            out += [a.name for a in ua.actions()]
        out.append("@time")
        for h in self.instruments.handles():
            out += ["%s.%s" % (h.name, c) for c in h.channels]
            out += ["%s.%s" % (h.name, a) for a in h.actions]
        return out

    def describe(self) -> dict:
        """``channels``: every channel's :meth:`info` with its ``last`` value (one flat
        list: front ends filter it by kind and access); ``aliases``, ``composites``,
        ``parameters``: the definitions."""
        listing = []
        for n in self.channel_names():
            try:
                inf = self.info(n)
            except Exception as exc:
                inf = {"name": n, "kind": self.kind(n), "access": "", "readable": False,
                       "writable": False, "error": str(exc)}
            hw = self._hw(n) if inf.get("kind") in ("hardware", "config") else None
            if hw is not None:
                inf["last"] = hw[1]._last_of(hw[2].name)["last"]
            elif inf.get("kind") == "parameter":
                inf["last"] = inf.get("value")
            else:
                inf["last"] = _jsonable_last(self._last.get(n))
            listing.append(inf)
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
        out_c = []
        for c in self.composites():
            d = c.to_dict()
            if c.type == "multi":
                d["last"] = {v: self._last.get(v) for v in c.channels()}
            elif c.type == "map":
                d["last"] = self._last.get(c.name)
            out_c.append(d)
        return {"channels": listing, "aliases": out_a, "composites": out_c,
                "parameters": self.describe_parameters()}

    def last_value(self, name: str):
        return self._last.get(name)

    # -- validation --------------------------------------------------------------
    def _read_only(self, name: str, k: str) -> ChannelError:
        why = {"read": "a composite read", "special": "a special channel",
               "parameter": "a parameter computed from its formula",
               "alias": "a read-only alias", "hardware": "a read-only channel",
               "config": "a read-only setting"}.get(k, k)
        return ChannelError("%s is read-only (%s)" % (name, why))

    def check_value(self, name: str, value: Any, partial: bool = False) -> Any:
        """Check a write of *value* to *name* (access, type, limits); returns the
        canonical value (None for an action).  For a composite channel everything it
        would write is checked; with *partial* targets that need a variable that was
        never set are skipped (sweep validation: the sweep sets it first)."""
        k = self.kind(name)
        if k is None:
            self._unknown(name)
        if k in ("variable", "map"):
            self.check_many({name: value}, partial=partial)
            return self._to_float(name, value)
        if k in ("read", "special"):
            raise self._read_only(name, k)
        if k in ("action", "user_action"):
            return None
        if k == "parameter":
            if self._params[name].is_formula:
                raise self._read_only(name, k)
            v = self._to_float(name, value)
            if not math.isfinite(v):
                raise ChannelError("parameter %s: value must be finite" % name)
            return v
        if k in ("hardware", "config"):
            _, h, spec = self._hw(name)
            if not spec.writable:
                raise self._read_only(name, k)
            from .instrument import coerce_value
            return coerce_value(value, spec, "write")
        a = self._aliases[name]
        if a.read_only:
            raise self._read_only(name, "alias")
        spec = self._alias_spec(a)
        if not spec.writable:
            raise ChannelError("%s is read-only (alias of the read-only %s)" % (name, a.target))
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

    def check_many(self, values: Dict[str, Any], partial: bool = False) -> Dict[str, Any]:
        """Check setting several channels at once (variables of one composite together);
        returns every value that would be written."""
        planned = {n: v for n, v in values.items()
                   if self.kind(n) in ("alias", "variable", "map")}
        out = {n: self.check_value(n, v) for n, v in values.items() if n not in planned}
        origins: Dict[str, str] = {}
        plan = self._plan(self._start_values(planned), partial=partial, origins=origins)
        self._check_plan(plan, origins)
        twice = sorted(set(out) & set(plan))
        if twice:
            raise ChannelError("%s would be set twice (directly and by %s)" % (
                ", ".join(twice), ", ".join(origins.get(n, "?") for n in twice)))
        out.update(plan)
        return out

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

    def grid_out_of_range(self, values: Dict[str, np.ndarray]) -> Optional[np.ndarray]:
        """Fast check of many combinations at once, for sweep validation: *values*
        maps variables of **one** multivariate composite to equally shaped arrays.
        Returns a boolean array, True where a target of the composite would be out
        of its range (or not finite), i.e. where ``check_many`` raises
        :class:`OutOfRange`; never True for ``out_of_range="clamp"``.

        Returns ``None`` when the fast check does not apply (targets that are not
        plain scalar aliases, a variable outside its own range, a formula that does
        not evaluate element-wise …): the caller then checks the combinations one
        by one, which also gives the exact error message."""
        with self._lock:
            groups = {self.group_of(n) for n in values}
            if len(groups) != 1 or None in groups:
                return None
            c = self._composites[groups.pop()]
            arrs = {n: np.asarray(v, dtype=float) for n, v in values.items()}
            shape = next(iter(arrs.values())).shape
            if any(a.shape != shape for a in arrs.values()):
                return None
            env = {}
            for n in c.channels():
                if n in arrs:
                    a = arrs[n]
                    var = c.var[n]
                    if not np.isfinite(a).all():
                        return None
                    try:
                        self._check_limits(n, a, var.min, var.max)
                    except LimitError:
                        return None
                    env[n] = a
                else:
                    x = self._last.get(n)
                    env[n] = None if x is None else float(x)
            bad = np.zeros(shape, dtype=bool)
            for t, deps in c.deps.items():
                if not set(deps) & set(arrs):
                    continue
                if any(env[d] is None for d in deps):
                    continue                  # partial: needs a variable never set
                a = self._aliases.get(t)
                if a is None or a.read_only:
                    return None
                try:
                    spec = self._alias_spec(a)
                except Exception:
                    return None
                if spec.shape != () or spec.dtype == "str" or not spec.writable:
                    return None
                try:
                    tv = c.exprs[t].evaluate(c.env(t, env))
                    tv = np.broadcast_to(np.asarray(tv, dtype=float), shape)
                except Exception:
                    return None
                # element-wise? compare a few combinations with the scalar evaluation
                flat = [0, tv.size - 1, tv.size // 2, tv.size // 3]
                for j in set(f for f in flat if 0 <= f < tv.size):
                    ij = np.unravel_index(j, shape)
                    try:
                        one = float(c.exprs[t].evaluate(c.env(t, {
                            d: (env[d][ij] if isinstance(env[d], np.ndarray) else env[d])
                            for d in deps})))
                    except Exception:
                        return None
                    if not (np.isclose(one, tv[ij], rtol=1e-9, atol=1e-12) or
                            (np.isnan(one) and np.isnan(tv[ij]))):
                        return None
                if c.out_of_range == "clamp":
                    bad |= ~np.isfinite(tv)
                    continue
                with np.errstate(invalid="ignore"):
                    out = ~np.isfinite(tv)
                    if a.min is not None:
                        out |= tv < a.min - 1e-12 * max(1.0, abs(a.min))
                    if a.max is not None:
                        out |= tv > a.max + 1e-12 * max(1.0, abs(a.max))
                bad |= out
            return bad

    # -- stoppable ramps of direct writes ----------------------------------------------
    @contextlib.contextmanager
    def stoppable_ramps(self, source: str = ""):
        """Writes inside this block (in this thread) can have their ramps stopped by
        :meth:`stop_ramps`; while a ramp runs it is listed in :meth:`active_ramps`.
        Used for direct writes (Manual panel, clients); sweeps, scripts and user
        actions have their own abort."""
        tok = RampToken(source)
        reset = _RAMP_TOKEN.set(tok)
        try:
            yield tok
        finally:
            _RAMP_TOKEN.reset(reset)

    def active_ramps(self) -> List[dict]:
        """The ramps of direct writes now running: ``[{id, channels, targets, since,
        source}]`` (*channels* the aliases that move, *targets* what was asked)."""
        with self._ramps_lock:
            return [{"id": i, "channels": list(r["channels"]), "targets": dict(r["targets"]),
                     "since": r["since"], "source": r["token"].source}
                    for i, r in sorted(self.ramps.items())]

    def stop_ramps(self) -> List[str]:
        """Stop every running ramp of a direct write; each channel stays where its
        ramp had got to.  Returns the channels that were ramping."""
        with self._ramps_lock:
            regs = list(self.ramps.values())
        for r in regs:
            r["token"].stop()
        return sorted({c for r in regs for c in r["channels"]})

    def _ramps_changed(self) -> None:
        act = self.active_ramps()
        for cb in list(self.ramp_listeners):
            try:
                cb(act)
            except Exception:
                pass

    def out_of_range_mode(self, composite: str) -> Optional[str]:
        """The ``out_of_range`` action of a multivariate composite (None otherwise)."""
        c = self._composites.get(composite)
        return getattr(c, "out_of_range", None) if c is not None and c.type == "multi" else None

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
        if isinstance(arr, float):            # the common case, without numpy
            if lo is not None and arr < lo - 1e-12 * max(1.0, abs(lo)):
                raise LimitError("%s: value %s below minimum %g" % (name, _fmt(arr), lo))
            if hi is not None and arr > hi + 1e-12 * max(1.0, abs(hi)):
                raise LimitError("%s: value %s above maximum %g" % (name, _fmt(arr), hi))
            return
        a = np.asarray(arr, dtype=float)
        if lo is not None and np.any(a < lo - 1e-12 * max(1.0, abs(lo))):
            raise LimitError("%s: value %s below minimum %g" % (name, _fmt(a), lo))
        if hi is not None and np.any(a > hi + 1e-12 * max(1.0, abs(hi))):
            raise LimitError("%s: value %s above maximum %g" % (name, _fmt(a), hi))

    # -- composite plans ---------------------------------------------------------------
    def _start_values(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """Start values of a plan: aliases, variables and mappings."""
        out = {}
        for n, v in values.items():
            k = self.kind(n)
            if k in ("variable", "map"):
                out[n] = self._to_float(n, v)
            elif k == "alias":
                out[n] = self.check_value(n, v)
            else:
                raise ChannelError("%s is not set through a plan" % n)
        return out

    def _is_leaf(self, n: str) -> bool:
        """A channel of a plan that is written to an instrument."""
        return n in self._aliases or self.kind(n) in ("hardware", "config")

    def _leaf(self, n: str) -> Alias:
        a = self._aliases.get(n)
        if a is not None:
            return a
        a = self._identity.get(n)
        if a is None:
            a = self._identity[n] = Alias.identity(n)
        return a

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
                        tv = as_value(c.exprs[t].evaluate(c.env(t, env)))
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
        if k in ("alias", "hardware", "config"):
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
        if self._is_leaf(n):
            a = self._leaf(n)
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
        """Read any readable channel."""
        k = self.kind(name)
        if k == "special":
            return time.time()
        if k == "parameter":
            return self._pvals[name]
        if k in ("variable", "map", "read"):
            return self._get_virtual(self._owner[name], name)
        if k == "alias":
            a = self._aliases[name]
            h, spec = self.instruments.resolve(a.target)
            if not spec.readable:
                raise ChannelError("%s is write-only (alias of %s)" % (name, a.target))
            return self._from_raw(a, spec, h.read(spec.name))
        if k in ("hardware", "config"):
            _, h, spec = self._hw(name)
            if not spec.readable:
                raise ChannelError("%s is write-only" % name)
            return h.read(spec.name)
        if k in ("action", "user_action"):
            raise ChannelError("%s is an action: it has no value to read (write it to run it)"
                               % name)
        self._unknown(name)

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

    def set(self, name: str, value: Any = None, ramp: bool = True,
            abort_check: AbortCheck = None) -> Any:
        """Write any writable channel; returns the value set (for a user action, how
        it ended: ``{status, error, elapsed}``)."""
        if self.kind(name) == "alias":
            value = self.check_value(name, value)
            with self._set_lock(name):
                self._set_alias(self._aliases[name], value, ramp, abort_check)
            self._notify(name, value)
            return value
        return self.set_many({name: value}, ramp=ramp, abort_check=abort_check).get(name)

    def set_many(self, values: Dict[str, Any], ramp: bool = True,
                 abort_check: AbortCheck = None) -> Dict[str, Any]:
        """Write several channels together.  Everything is checked before anything is
        written; then the parameters are set, then the aliases and composite channels
        (everything they drive computed and moved together), then the other channels
        of instruments, then the actions are run in the order given.  Returns every
        value written (an action: None, a user action: how it ended)."""
        kinds = {}
        for n in values:
            k = self.kind(n)
            if k is None:
                self._unknown(n)
            kinds[n] = k
        params = {n: self.check_value(n, v) for n, v in values.items() if kinds[n] == "parameter"}
        direct = {n: self.check_value(n, v) for n, v in values.items()
                  if kinds[n] in ("hardware", "config", "read", "special")}
        actions = [n for n in values if kinds[n] in ("action", "user_action")]
        planned = {n: v for n, v in values.items() if kinds[n] in ("alias", "variable", "map")}
        old_params = {n: self._params[n] for n in params}
        try:
            for n, v in params.items():            # constants of the formulas: first
                self.set_parameter(n, v)
            start = self._start_values(planned)
            origins: Dict[str, str] = {}
            plan = self._plan(start, origins=origins) if start else {}
            self._check_plan(plan, origins)
            twice = sorted(set(direct) & set(plan))
            if twice:
                raise ChannelError("%s would be set twice (directly and by %s)" % (
                    ", ".join(twice), ", ".join(origins.get(n, "?") for n in twice)))
        except Exception:
            if old_params:
                with self._lock:
                    cand = dict(self._params)
                    cand.update(old_params)
                    self._apply_parameters(cand)
            raise
        done: Dict[str, Any] = dict(params)
        if plan:
            leaves = sorted(n for n in plan if self._is_leaf(n))
            locks = [self._set_lock(n) for n in leaves]
            for lk in locks:
                lk.acquire()
            try:
                self._write_plan(start, plan, leaves, ramp, abort_check)
            finally:
                for lk in reversed(locks):
                    lk.release()
            done.update(plan)
        for n, v in direct.items():
            with self._set_lock(n):
                self._write_hardware(n, v)
            done[n] = v
        for n, v in done.items():
            self._notify(n, v)
        for n in actions:
            if abort_check is not None:
                abort_check()
            done[n] = self._run_action(n, kinds[n], abort_check)
        return done

    def _write_hardware(self, ref: str, value) -> None:
        """A plain write to ``instrument.channel``."""
        _, h, spec = self._hw(ref)
        try:
            h.write(spec.name, value)
        finally:
            for a in self.aliases():               # their cached values are stale now
                if a.target == ref:
                    self._last.pop(a.name, None)
        self._last[ref] = value
        self._last_time[ref] = time.monotonic()

    def _run_action(self, name: str, kind: str, abort_check: AbortCheck = None):
        if kind == "action":
            _, h, act = self._hw(name)
            try:
                h.run_action(act.name)
            finally:
                self.forget_instrument(h.name)
            self._notify(name, None)
            return None
        res = self.user_actions.run(name, abort_check=abort_check)
        if res.get("status") == "error":
            raise ChannelError("user action %s failed: %s" % (name, res.get("error")))
        return res

    def _write_plan(self, start, plan, leaves, ramp, abort_check) -> None:
        items = []
        for n in leaves:
            a = self._leaf(n)
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
            x0 = self._last.get(n) if not self._is_leaf(n) else starts.get(n)
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
            # a non-linear relation can leave the allowed range between two allowed
            # points (e.g. across points a sweep skips): then the hardware moves on a
            # straight line instead, which stays inside the limits of every channel
            try:
                for u in np.linspace(0.0, 1.0, PATH_SAMPLES)[1:-1]:
                    for n, v in path(float(u)).items():
                        self._check_node_fast(n, v)
            except (LimitError, ExpressionError) as exc:
                log.debug("%s: the relation leaves the range on the way (%s); straight line",
                          ", ".join(start), exc)
                on_path = False
        if on_path:
            self._ramp(items, path, PATH_SAMPLES, abort_check, rated)
        else:
            tv = {n: np.asarray(plan[n], dtype=float) for n in leaves}
            try:
                self._ramp(items, lambda u: {n: starts[n] + (tv[n] - starts[n]) * u for n in leaves},
                           2, abort_check, {n: r for n, r in rated.items() if n in starts})
            except RampStopped:
                # stopped off the relation: the composite channels have no value now
                for n in plan:
                    if not self._is_leaf(n):
                        self._last.pop(n, None)
                raise
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
        if a.target != a.name:
            self._last.pop(a.target, None)

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
        tok = _RAMP_TOKEN.get()
        rid = None
        if tok is not None and (ct_a[-1] > RAMP_INTERVAL or cs_a[-1] > 1.0):  # worth listing
            rid = next(_RAMP_IDS)
            with self._ramps_lock:
                self.ramps[rid] = {"channels": sorted(leaf_names), "token": tok,
                                   "since": time.time(),
                                   "targets": {n: _jsonable(v) for n, v in samples[-1].items()}}
            self._ramps_changed()
        try:
            self._ramp_loop(items, path, samples, us, ct_a, cs_a, invert, leaf_names,
                            abort_check, tok)
        finally:
            if rid is not None:
                with self._ramps_lock:
                    self.ramps.pop(rid, None)
                self._ramps_changed()

    def _ramp_loop(self, items, path, samples, us, ct_a, cs_a, invert, leaf_names,
                   abort_check, tok) -> None:
        t0 = time.monotonic()
        k = 0
        last_u = 0.0
        while True:
            if abort_check is not None:
                abort_check()
            if tok is not None and tok.stopped:
                raise RampStopped("ramp of %s stopped" % ", ".join(sorted(leaf_names)))
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


def _jsonable_last(v):
    """A last value for a state snapshot: arrays only by their shape."""
    if isinstance(v, np.ndarray):
        return {"shape": list(v.shape)} if v.ndim else float(v)
    return v


def _jsonable(v):
    a = np.asarray(v, dtype=float)
    return float(a) if a.ndim == 0 else a.tolist()


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
