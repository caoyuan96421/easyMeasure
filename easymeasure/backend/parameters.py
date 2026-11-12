"""Parameters: named numerical constants of a setup.

A parameter is either a **value** (a number set by hand: a gate capacitance, a
geometry factor, a bias resistor) or a **formula** of other parameters
(``Cb/e``, ``2*pi*f0``).  Formulas may use only parameters (never live channels),
and may not refer to each other in a circle.

Parameters read like channels (``get('Cb')``, a sweep may record them, a composite
read may use them) but cannot be written as channels: a value parameter is set with
``set_parameter`` (the Parameters panel or the Manual panel), a formula parameter
only by changing its formula.  Composite channels (targets, read formulas, mapping
formulas) and actions may use parameters in their formulas; they use the value the
parameter has at that moment, so changing a parameter does not move any hardware by
itself - the next write uses the new value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from ..common.errors import ChannelError, ExpressionError
from ..common.exprs import Expression
from .composites import _check_channel_name, find_cycle


@dataclass
class Parameter:
    """A value parameter (``value``) or a formula parameter (``formula``)."""
    name: str
    value: Optional[float] = None
    formula: str = ""
    unit: str = ""
    description: str = ""
    expr: Optional[Expression] = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        _check_channel_name(self.name, "parameter")
        self.unit = str(self.unit or "")
        self.description = str(self.description or "")
        self.formula = str(self.formula or "").strip()
        if self.formula:
            if self.value not in (None, ""):
                raise ChannelError("parameter %s: give a value or a formula, not both" % self.name)
            self.value = None
            try:
                self.expr = Expression(self.formula)
            except ExpressionError as exc:
                raise ExpressionError("parameter %s: %s" % (self.name, exc)) from None
            self.formula = self.expr.source
        else:
            if self.value in (None, ""):
                raise ChannelError("parameter %s needs a value or a formula" % self.name)
            try:
                v = float(self.value)
            except (TypeError, ValueError):
                raise ChannelError("parameter %s: value must be a number, got %r"
                                   % (self.name, self.value)) from None
            if not math.isfinite(v):
                raise ChannelError("parameter %s: value must be finite" % self.name)
            self.value = v

    @property
    def is_formula(self) -> bool:
        return bool(self.formula)

    def refs(self) -> List[str]:
        """The names its formula uses (must all be parameters)."""
        return list(self.expr.free_names) if self.expr is not None else []

    def to_dict(self) -> dict:
        d = {"name": self.name, "unit": self.unit, "description": self.description}
        if self.formula:
            d["formula"] = self.formula
        else:
            d["value"] = self.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Parameter":
        if not isinstance(d, dict):
            raise ChannelError("a parameter is {name, value | formula, unit, description}")
        return cls(name=d.get("name", ""), value=d.get("value"), formula=d.get("formula") or "",
                   unit=d.get("unit", ""), description=d.get("description", ""))


def _err(name: str, msg: str) -> ChannelError:
    e = ChannelError(msg)
    e.parameter = name                 # which parameter is at fault (setups leave it out)
    return e


def compute(params: Dict[str, Parameter], is_channel=lambda n: False) -> Dict[str, float]:
    """The values of all *params*, formulas evaluated in dependency order.  Raises
    :class:`ChannelError` for a formula using an unknown name or a live channel
    (*is_channel*), a circular reference, or a value that is not a finite number."""
    for p in params.values():
        for r in p.refs():
            if r not in params:
                if is_channel(r):
                    raise _err(p.name, "parameter %s: %s is a channel; a parameter formula "
                                       "may use only other parameters" % (p.name, r))
                raise _err(p.name, "parameter %s: unknown name %s (not a parameter)"
                           % (p.name, r))
    cyc = find_cycle({p.name: p.refs() for p in params.values()})
    if cyc:
        raise _err(cyc[0], "parameters refer to each other in a circle: %s" % " → ".join(cyc))
    values: Dict[str, float] = {}

    def value(n: str) -> float:
        if n in values:
            return values[n]
        p = params[n]
        if p.is_formula:
            env = {r: value(r) for r in p.refs()}
            try:
                v = p.expr.evaluate(env)
                v = float(v)
            except (ExpressionError, TypeError, ValueError) as exc:
                raise _err(n, "parameter %s = %s: %s" % (n, p.formula, exc)) from None
            if not math.isfinite(v):
                raise _err(n, "parameter %s = %s is not a finite number (%r)"
                           % (n, p.formula, v))
        else:
            v = float(p.value)
        values[n] = v
        return v
    for n in params:
        value(n)
    return values


def users_of(params: Dict[str, Parameter], name: str) -> List[str]:
    """Parameters whose formula uses *name*."""
    return sorted(p.name for p in params.values() if name in p.refs())


def ordered(params: Iterable[Parameter]) -> List[Parameter]:
    """Parameters ordered so that every formula comes after what it uses (for
    files); unknown references are left to :func:`compute` to report."""
    ps = {p.name: p for p in params}
    out, seen = [], set()

    def visit(n, stack=()):
        if n in seen or n not in ps or n in stack:
            return
        for r in ps[n].refs():
            visit(r, stack + (n,))
        seen.add(n)
        out.append(ps[n])
    for n in ps:
        visit(n)
    return out
