"""Small, guarded expression evaluator for user formulas.

Used for sweep mapping functions (``start + (stop-start)*t**2``), composite
channel definitions (``0.5*x + 0.1``) and composite read-back formulas.

Expressions are parsed with :mod:`ast` and rejected if they try to access dunder
attributes or names, which removes the usual sandbox-escape routes.  This is a
guard against mistakes, not a security boundary - users of a measurement setup
already have full control of the computer.
"""

from __future__ import annotations

import ast
import math
from typing import Any, Dict, Iterable, Optional

import numpy as np

from .errors import ExpressionError

_SAFE_BUILTINS = {
    "abs": abs, "min": min, "max": max, "round": round, "len": len, "range": range,
    "float": float, "int": int, "bool": bool, "str": str, "sum": sum, "list": list,
    "tuple": tuple, "zip": zip, "enumerate": enumerate, "pow": pow, "divmod": divmod,
    "True": True, "False": False, "None": None,
}

_MATH_NAMES = {
    name: getattr(np, name)
    for name in (
        "sin", "cos", "tan", "arcsin", "arccos", "arctan", "arctan2", "sinh", "cosh",
        "tanh", "exp", "log", "log10", "log2", "sqrt", "floor", "ceil", "sign",
        "linspace", "arange", "logspace", "geomspace", "zeros", "ones", "full",
        "array", "meshgrid", "clip", "where", "pi", "e", "inf", "nan", "mod",
        "hypot", "degrees", "radians", "cumsum", "concatenate", "stack", "outer",
    )
}


class _Validator(ast.NodeVisitor):
    _FORBIDDEN = (ast.Lambda, ast.NamedExpr) if hasattr(ast, "NamedExpr") else (ast.Lambda,)

    def generic_visit(self, node):
        if isinstance(node, self._FORBIDDEN):
            raise ExpressionError("construct %s not allowed" % type(node).__name__)
        super().generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        if node.attr.startswith("_"):
            raise ExpressionError("access to private attribute %r not allowed" % node.attr)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if node.id.startswith("__"):
            raise ExpressionError("name %r not allowed" % node.id)
        self.generic_visit(node)


class Expression:
    """A compiled expression.  Call :meth:`evaluate` with a variable mapping."""

    def __init__(self, source: str, allowed_names: Optional[Iterable[str]] = None):
        if not isinstance(source, str) or not source.strip():
            raise ExpressionError("empty expression")
        self.source = source.strip()
        try:
            tree = ast.parse(self.source, mode="eval")
        except SyntaxError as exc:
            raise ExpressionError("syntax error in %r: %s" % (self.source, exc.msg)) from exc
        _Validator().visit(tree)
        self.names = sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)})
        if allowed_names is not None:
            known = set(allowed_names) | set(_MATH_NAMES) | set(_SAFE_BUILTINS) | {"np", "math"}
            unknown = [n for n in self.names if n not in known]
            if unknown:
                raise ExpressionError("unknown name(s) %s in %r" % (", ".join(unknown), self.source))
        self._code = compile(tree, "<expr>", "eval")

    def evaluate(self, variables: Optional[Dict[str, Any]] = None) -> Any:
        ns: Dict[str, Any] = {"__builtins__": _SAFE_BUILTINS, "np": np, "math": math}
        ns.update(_MATH_NAMES)
        if variables:
            ns.update(variables)
        try:
            return eval(self._code, ns)  # noqa: S307 - guarded above
        except ExpressionError:
            raise
        except Exception as exc:
            raise ExpressionError("error evaluating %r: %s: %s"
                                  % (self.source, type(exc).__name__, exc)) from exc

    def __repr__(self):
        return "Expression(%r)" % self.source


def evaluate(source: str, **variables) -> Any:
    return Expression(source).evaluate(variables)
