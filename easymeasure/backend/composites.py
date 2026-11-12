"""Composite (virtual) channels: definitions and their checks.

Three kinds, all built from user formulas (:mod:`easymeasure.common.exprs`, so
piecewise functions work: ``piecewise(x < 0, -x, x)``, ``a if c else b``,
``where``, ``interp`` tables):

``multi`` – multivariate write
    Variables ``x, y, …`` (each with its own name, min, max, unit and ramp rate)
    are channels of their own.  Every *target* (an alias, a mapping, or a
    variable of another multivariate composite) is a formula of the variables::

        {"type": "multi", "name": "gates",
         "variables": [{"name": "eps", "min": -1, "max": 1, "unit": "V", "ramp_rate": 0.1},
                       {"name": "U", "min": 0, "max": 2}],
         "targets": {"VL": "U/2 + eps", "VR": "U/2 - eps"}}

    Setting a variable writes the targets that depend on it (with the last set
    value of the others).  Reading a variable gives the value it was last set to,
    NaN before that.

    ``out_of_range`` says what happens when a combination of the variables would
    drive a target outside its own range (an alias' limits …):

    * ``"skip"`` (default): a sweep skips that point (as ``skip_point()`` before
      the swept channels are set); a direct write is refused;
    * ``"error"``: refused everywhere; a sweep containing such a point fails
      validation;
    * ``"clamp"``: the target is set to the nearest end of its range.

``read`` – composite read
    A formula of other channels (aliases, mappings, variables, other reads)::

        {"type": "read", "name": "R", "expression": "V / I if I != 0 else nan", "unit": "Ω"}

``map`` – simple mapping
    A one-to-one software channel for one hardware channel (alias, mapping or
    variable): ``forward`` gives the target value ``y`` from ``x``, ``backward``
    gives ``x`` from ``y``; ``min``/``max`` (required) is the range of ``x``.
    That ``backward(forward(x)) == x`` everywhere in the range is checked when the
    mapping is defined::

        {"type": "map", "name": "T", "target": "Vheater", "forward": "sqrt(x/10)",
         "backward": "10*y**2", "min": 0, "max": 50, "unit": "mW"}

The dependency graph (writes and reads) is checked for cycles by the
:class:`~easymeasure.backend.channels.ChannelManager` whenever a composite is
added or replaced.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from ..common.errors import ChannelError, ExpressionError
from ..common.exprs import Expression, rename, reserved

KINDS = ("multi", "read", "map")
#: what a multivariate composite does with combinations that put a target out of range
OUT_OF_RANGE = ("skip", "error", "clamp")
#: kinds of the channels composites provide (``ChannelManager.info()["kind"]``)
VIRTUAL_KINDS = ("variable", "map", "read")
INVERSE_SAMPLES = 2001


def ok_name(name: str) -> bool:
    return bool(name) and isinstance(name, str) and (name[0].isalpha() or name[0] == "_") \
        and all(c.isalnum() or c == "_" for c in name)


def _opt_float(v, what: str) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ChannelError("%s must be a number, got %r" % (what, v)) from None
    return None if math.isnan(f) else f


def _scalar(v, what: str) -> float:
    try:
        a = np.asarray(v)
        if a.dtype.kind == "c":
            raise ChannelError("%s is complex (%r)" % (what, v))
        if a.size != 1:
            raise ChannelError("%s must be a single number, got shape %s" % (what, a.shape))
        return float(a.reshape(()))
    except (TypeError, ValueError):
        raise ChannelError("%s must be a number, got %r" % (what, v)) from None


def _check_channel_name(name: str, what: str) -> None:
    if not ok_name(name):
        raise ChannelError("invalid %s name %r (use letters, digits, _)" % (what, name))
    if reserved(name):
        raise ChannelError("%s name %r is a function / constant of the formulas" % (what, name))


@dataclass
class Variable:
    """A variable of a multivariate composite (itself a channel)."""
    name: str
    min: Optional[float] = None
    max: Optional[float] = None
    unit: str = ""
    ramp_rate: Optional[float] = None
    description: str = ""

    def __post_init__(self):
        _check_channel_name(self.name, "variable")
        for k in ("min", "max", "ramp_rate"):
            setattr(self, k, _opt_float(getattr(self, k), "%s %s" % (self.name, k)))
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ChannelError("variable %s: min > max" % self.name)
        if self.ramp_rate is not None and self.ramp_rate <= 0:
            raise ChannelError("variable %s: ramp_rate must be positive" % self.name)
        self.unit = str(self.unit or "")

    def to_dict(self) -> dict:
        return {"name": self.name, "min": self.min, "max": self.max, "unit": self.unit,
                "ramp_rate": self.ramp_rate, "description": self.description}

    @classmethod
    def from_any(cls, v) -> "Variable":
        if isinstance(v, Variable):
            return v
        if isinstance(v, str):
            return cls(v)
        if isinstance(v, dict):
            keys = {"name", "min", "max", "unit", "ramp_rate", "description"}
            return cls(**{k: x for k, x in v.items() if k in keys})
        raise ChannelError("a variable is a name or {name, min, max, unit, ramp_rate}")


class CompositeBase:
    type = ""
    name: str
    description: str
    #: values of the parameters its formulas use (set by the ChannelManager)
    penv: Dict[str, float] = {}

    def externals(self) -> List[str]:
        """Names in its formulas that are neither its own channels nor its inputs:
        they must be parameters."""
        return []

    def channels(self) -> List[str]:
        """Channel names this composite provides."""
        raise NotImplementedError

    def edges(self) -> List[Tuple[str, str]]:
        """(from, to) dependencies: writing / reading *from* uses *to*."""
        raise NotImplementedError

    def references(self) -> List[str]:
        """Other channels (and parameters) this composite uses."""
        return sorted(({b for _, b in self.edges()} | set(self.externals()))
                      - set(self.channels()))

    def to_dict(self) -> dict:
        raise NotImplementedError


class MultiComposite(CompositeBase):
    type = "multi"

    def __init__(self, name: str, variables: Iterable, targets: Dict[str, str],
                 description: str = "", out_of_range: str = "skip"):
        if not ok_name(name):
            raise ChannelError("invalid composite name %r" % name)
        self.name = name
        self.description = description or ""
        oor = str(out_of_range or "skip").strip().lower()
        if oor not in OUT_OF_RANGE:
            raise ChannelError("composite %s: out_of_range must be one of %s, got %r"
                               % (name, ", ".join(OUT_OF_RANGE), out_of_range))
        self.out_of_range = oor
        self.variables: List[Variable] = [Variable.from_any(v) for v in (variables or [])]
        if not self.variables:
            raise ChannelError("composite %s has no variables" % name)
        names = [v.name for v in self.variables]
        if len(set(names)) != len(names):
            raise ChannelError("composite %s: variable names repeat" % name)
        self.var = {v.name: v for v in self.variables}
        if not targets:
            raise ChannelError("composite %s has no targets" % name)
        self.targets: Dict[str, str] = {}
        self.exprs: Dict[str, Expression] = {}
        self.deps: Dict[str, List[str]] = {}
        self.ext: Dict[str, List[str]] = {}
        for t, e in targets.items():
            t = str(t).strip()
            if t in self.var:
                raise ChannelError("composite %s: %s is one of its own variables, not a target"
                                   % (name, t))
            if not ok_name(t):
                raise ChannelError("composite %s: target %r must be an alias, mapping or "
                                   "variable name" % (name, t))
            try:
                ex = Expression(str(e))
            except ExpressionError as exc:
                raise ExpressionError("composite %s, target %s: %s" % (name, t, exc)) from None
            self.targets[t] = ex.source
            self.exprs[t] = ex
            self.deps[t] = [n for n in ex.free_names if n in self.var]
            # other names must be parameters (checked against them when it is added)
            self.ext[t] = [n for n in ex.free_names if n not in self.var]
        self.penv = {}

    def channels(self) -> List[str]:
        return [v.name for v in self.variables]

    def externals(self) -> List[str]:
        return sorted({n for ns in self.ext.values() for n in ns})

    def env(self, t: str, values: Dict[str, Any]) -> Dict[str, Any]:
        """The names target *t*'s formula needs: its variables from *values*, the
        parameters from ``penv``."""
        out = {n: self.penv[n] for n in self.ext.get(t, ()) if n in self.penv}
        out.update({d: values[d] for d in self.deps[t]})
        return out

    def edges(self) -> List[Tuple[str, str]]:
        return [(v, t) for t, ds in self.deps.items() for v in ds]

    def unused(self) -> List[str]:
        used = {v for ds in self.deps.values() for v in ds}
        return [v.name for v in self.variables if v.name not in used]

    def to_dict(self) -> dict:
        return {"type": "multi", "name": self.name,
                "variables": [v.to_dict() for v in self.variables],
                "targets": dict(self.targets), "out_of_range": self.out_of_range,
                "description": self.description}


class ReadComposite(CompositeBase):
    type = "read"

    def __init__(self, name: str, expression: str, unit: str = "", description: str = ""):
        _check_channel_name(name, "channel")
        self.name = name
        self.unit = str(unit or "")
        self.description = description or ""
        try:
            self.expr = Expression(str(expression or ""))
        except ExpressionError as exc:
            raise ExpressionError("composite %s: %s" % (name, exc)) from None
        self.expression = self.expr.source
        #: channels the formula reads (set when it is checked against the channels)
        self.inputs: List[str] = list(self.expr.free_names)

    def resolve_inputs(self, is_channel) -> None:
        """Which names of the formula are channels (a channel may be named like a
        constant, e.g. ``e``); every other free name must be one."""
        ins = [n for n in self.expr.names if is_channel(n)]
        unknown = [n for n in self.expr.free_names if n not in ins]
        if unknown:
            raise ChannelError("composite %s: unknown channel(s) %s" % (self.name,
                                                                       ", ".join(unknown)))
        self.inputs = ins

    def channels(self) -> List[str]:
        return [self.name]

    def edges(self) -> List[Tuple[str, str]]:
        return [(self.name, i) for i in self.inputs]

    def to_dict(self) -> dict:
        return {"type": "read", "name": self.name, "expression": self.expression,
                "unit": self.unit, "description": self.description}


class MapComposite(CompositeBase):
    type = "map"

    def __init__(self, name: str, target: str, forward: str, backward: str, min=None, max=None,
                 unit: str = "", ramp_rate=None, description: str = "", tolerance=None):
        _check_channel_name(name, "channel")
        self.name = name
        self.target = str(target or "").strip()
        if not ok_name(self.target):
            raise ChannelError("mapping %s: target must be an alias, mapping or variable" % name)
        if self.target == name:
            raise ChannelError("mapping %s maps onto itself" % name)
        self.unit = str(unit or "")
        self.description = description or ""
        self.min = _opt_float(min, "%s min" % name)
        self.max = _opt_float(max, "%s max" % name)
        self.ramp_rate = _opt_float(ramp_rate, "%s ramp_rate" % name)
        self.tolerance = _opt_float(tolerance, "%s tolerance" % name)
        if self.min is None or self.max is None or not (math.isfinite(self.min)
                                                         and math.isfinite(self.max)):
            raise ChannelError("mapping %s needs a finite min and max (the range in which "
                               "forward and backward are checked to be inverse)" % name)
        if self.min >= self.max:
            raise ChannelError("mapping %s: min must be below max" % name)
        if self.ramp_rate is not None and self.ramp_rate <= 0:
            raise ChannelError("mapping %s: ramp_rate must be positive" % name)
        try:
            self.f = Expression(str(forward or ""))
        except ExpressionError as exc:
            raise ExpressionError("mapping %s, forward (y from x): %s" % (name, exc)) from None
        try:
            self.g = Expression(str(backward or ""))
        except ExpressionError as exc:
            raise ExpressionError("mapping %s, backward (x from y): %s" % (name, exc)) from None
        if "y" in self.f.free_names:
            raise ExpressionError("mapping %s, forward (y from x) uses y" % name)
        if "x" in self.g.free_names:
            raise ExpressionError("mapping %s, backward (x from y) uses x" % name)
        self.forward, self.backward = self.f.source, self.g.source
        self.penv = {}
        # with parameters in the formulas the inverse is checked once they are known
        self.image = self.check_inverse() if not self.externals() else None

    def externals(self) -> List[str]:
        return sorted((set(self.f.free_names) - {"x"}) | (set(self.g.free_names) - {"y"}))

    def fwd(self, x: float) -> float:
        return _scalar(self.f.evaluate(dict(self.penv, x=float(x))),
                       "mapping %s: forward(%g)" % (self.name, x))

    def bwd(self, y: float) -> float:
        return _scalar(self.g.evaluate(dict(self.penv, y=float(y))),
                       "mapping %s: backward(%g)" % (self.name, y))

    def check_inverse(self) -> Tuple[float, float]:
        """``backward(forward(x)) == x`` on a dense grid of the range; returns the
        range of the target values.  Raises :class:`ChannelError` with the worst point."""
        lo, hi = self.min, self.max
        xs = list(np.linspace(lo, hi, INVERSE_SAMPLES))
        if lo > 0 and hi / lo > 100:                 # wide positive range: also log-spaced
            xs += list(np.geomspace(lo, hi, INVERSE_SAMPLES // 2))
        elif hi < 0 and lo / hi > 100:
            xs += list(-np.geomspace(-hi, -lo, INVERSE_SAMPLES // 2))
        tol = self.tolerance if self.tolerance is not None else 1e-6 * (hi - lo)
        worst, ys = None, []
        for x in xs:
            x = float(x)
            try:
                y = self.fwd(x)
            except (ChannelError, ExpressionError) as exc:
                raise ChannelError("mapping %s: forward fails at x = %.6g: %s"
                                   % (self.name, x, exc)) from None
            if not math.isfinite(y):
                raise ChannelError("mapping %s: forward(x) is not finite at x = %.6g (%r)"
                                   % (self.name, x, y))
            try:
                xb = self.bwd(y)
            except (ChannelError, ExpressionError) as exc:
                raise ChannelError("mapping %s: backward fails at y = %.6g (= forward(%.6g)): %s"
                                   % (self.name, y, x, exc)) from None
            d = abs(xb - x) if math.isfinite(xb) else math.inf
            if d > tol * (1 + 1e-9) + 1e-12 * abs(x) and (worst is None or d > worst[0]):
                worst = (d, x, y, xb)
            ys.append(y)
        if worst is not None:
            d, x, y, xb = worst
            raise ChannelError(
                "mapping %s: backward is not the inverse of forward in [%g, %g]: at x = %.6g "
                "forward gives y = %.6g and backward gives %.6g back (off by %.3g, tolerance "
                "%.3g)" % (self.name, lo, hi, x, y, xb, d, tol))
        return float(min(ys)), float(max(ys))

    def channels(self) -> List[str]:
        return [self.name]

    def edges(self) -> List[Tuple[str, str]]:
        return [(self.name, self.target)]

    def to_dict(self) -> dict:
        return {"type": "map", "name": self.name, "target": self.target,
                "forward": self.forward, "backward": self.backward, "min": self.min,
                "max": self.max, "unit": self.unit, "ramp_rate": self.ramp_rate,
                "tolerance": self.tolerance, "description": self.description,
                "image": list(self.image)}


def composite_from_dict(d: dict) -> Tuple[CompositeBase, List[str]]:
    """Build a composite from its dictionary; returns ``(composite, notes)``.

    A dictionary without ``type`` but with ``targets`` (the earlier one-variable
    composite, formulas of ``x``) becomes a multivariate composite whose single
    variable has the composite's name; its read-back formula is dropped (a note
    says so: define a ``read`` composite for it)."""
    d = dict(d or {})
    kind = d.get("type")
    notes: List[str] = []
    if kind is None and "targets" in d:
        name = d.get("name", "")
        var = {"name": name, "min": d.get("min"), "max": d.get("max"),
               "unit": d.get("unit", ""), "ramp_rate": d.get("ramp_rate")}
        try:
            targets = {t: rename(str(e), {"x": name}) for t, e in (d.get("targets") or {}).items()}
        except SyntaxError as exc:
            raise ExpressionError("composite %s: %s" % (name, exc)) from None
        if d.get("read"):
            notes.append("composite %s: its read formula %r is no longer part of it; define a "
                         "'read' composite for it" % (name, d["read"]))
        return MultiComposite(name, [var], targets, d.get("description", "")), notes
    if kind == "multi":
        return MultiComposite(d.get("name", ""), d.get("variables") or [], d.get("targets") or {},
                              d.get("description", ""),
                              d.get("out_of_range") or "skip"), notes
    if kind == "read":
        return ReadComposite(d.get("name", ""), d.get("expression", ""), d.get("unit", ""),
                             d.get("description", "")), notes
    if kind == "map":
        keys = {"name", "target", "forward", "backward", "min", "max", "unit", "ramp_rate",
                "description", "tolerance"}
        return MapComposite(**{k: v for k, v in d.items() if k in keys}), notes
    raise ChannelError("composite type must be one of %s, got %r" % (", ".join(KINDS), kind))


def find_cycle(edges: Dict[str, List[str]], prefer=()) -> Optional[List[str]]:
    """A dependency cycle as a list of names (first == last), or None. The cycle is
    told from the first of ``prefer`` on it (the channel just being defined)."""
    cyc = _find_cycle(edges)
    if cyc:
        loop = cyc[:-1]
        for p in prefer:
            if p in loop:
                i = loop.index(p)
                loop = loop[i:] + loop[:i]
                break
        cyc = loop + loop[:1]
    return cyc


def _find_cycle(edges: Dict[str, List[str]]) -> Optional[List[str]]:
    WHITE, GREY, BLACK = 0, 1, 2
    color: Dict[str, int] = {}
    stack: List[str] = []

    def visit(n) -> Optional[List[str]]:
        color[n] = GREY
        stack.append(n)
        for m in edges.get(n, ()):
            c = color.get(m, WHITE)
            if c == GREY:
                return stack[stack.index(m):] + [m]
            if c == WHITE:
                r = visit(m)
                if r:
                    return r
        stack.pop()
        color[n] = BLACK
        return None
    for n in list(edges):
        if color.get(n, WHITE) == WHITE:
            r = visit(n)
            if r:
                return r
    return None


def as_value(v: Any):
    """Expression results as float (scalars) or float arrays."""
    a = np.asarray(v)
    if a.dtype.kind == "c":
        raise ChannelError("complex value %r" % (v,))
    if a.ndim == 0:
        return float(a)
    return np.asarray(a, dtype=float)
