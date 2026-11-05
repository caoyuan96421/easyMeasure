"""Small, sandboxed expression evaluator for user formulas.

Used for sweep mapping functions (``start + (stop-start)*t**2``), composite
channel definitions (``0.5*x + 0.1``), composite read-back formulas and the
manual-control "set from expression".

Because these formulas can arrive from remote (web) users, this module is a
security boundary, built as an allow-list rather than a deny-list:

* only arithmetic, comparisons, conditionals, indexing, tuples/lists and calls of
  allow-listed functions are accepted (no attribute access except ``np.<fn>`` /
  ``math.<fn>`` from a fixed list and a few data properties such as ``.real``);
* no builtins are reachable, no names except the variables, math functions and
  constants; no lambdas, comprehensions, f-strings or starred arguments;
* resource bombs are defused: ``**``, ``*``, ``%`` and ``<<`` go through guarded
  helpers (no ``9**9**9`` or ``'a'*10**9``) and array constructors refuse to
  build more than :data:`MAX_ELEMENTS` elements.
"""

from __future__ import annotations

import ast
import math
from typing import Any, Dict, Iterable, Optional

import numpy as np

from .errors import ExpressionError

MAX_SOURCE = 4000           # characters
MAX_ELEMENTS = 10_000_000   # largest array an expression may create
MAX_INT_BITS = 4096         # largest integer produced by ** or <<


def _check_size(n) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError, OverflowError):
        raise ExpressionError("invalid array size") from None
    if n < 0 or n > MAX_ELEMENTS:
        raise ExpressionError("array of %d elements exceeds the limit of %d" % (n, MAX_ELEMENTS))
    return n


def _shape_size(shape) -> int:
    if isinstance(shape, (int, np.integer)):
        return _check_size(shape)
    total = 1
    for s in shape:
        total *= _check_size(s)
        _check_size(total)
    return total


# ---- guarded operators -------------------------------------------------------
def _pow(a, b):
    if isinstance(a, (int, np.integer)) and isinstance(b, (int, np.integer)) \
            and not isinstance(a, bool):
        if b > 0 and abs(int(a)) > 1 and int(b) * max(1, abs(int(a)).bit_length()) > MAX_INT_BITS:
            return float(a) ** float(b)          # float overflow -> inf / OverflowError
    try:
        return a ** b
    except OverflowError as exc:
        raise ExpressionError("overflow in power: %s" % exc) from None


def _mul(a, b):
    for seq, n in ((a, b), (b, a)):
        if isinstance(seq, (str, bytes, list, tuple)) and isinstance(n, (int, np.integer)):
            _check_size(len(seq) * max(0, int(n)))
    return a * b


def _mod(a, b):
    if isinstance(a, (str, bytes)):
        raise ExpressionError("string formatting is not allowed in expressions")
    return a % b


def _lshift(a, b):
    if isinstance(b, (int, np.integer)) and int(b) > MAX_INT_BITS:
        raise ExpressionError("shift too large")
    return a << b


# ---- guarded array constructors ---------------------------------------------
def _linspace(start, stop, num=50, endpoint=True):
    return np.linspace(start, stop, _check_size(num), endpoint=bool(endpoint))


def _logspace(start, stop, num=50, endpoint=True, base=10.0):
    return np.logspace(start, stop, _check_size(num), endpoint=bool(endpoint), base=base)


def _geomspace(start, stop, num=50, endpoint=True):
    return np.geomspace(start, stop, _check_size(num), endpoint=bool(endpoint))


def _arange(*args):
    if not 1 <= len(args) <= 3:
        raise ExpressionError("arange takes 1 to 3 arguments")
    start, stop, step = (0, args[0], 1) if len(args) == 1 else \
        (args[0], args[1], args[2] if len(args) == 3 else 1)
    if step == 0:
        raise ExpressionError("arange step must not be 0")
    n = math.ceil((float(stop) - float(start)) / float(step))
    _check_size(max(0, n))
    return np.arange(start, stop, step)


def _zeros(shape):
    _shape_size(shape)
    return np.zeros(shape)


def _ones(shape):
    _shape_size(shape)
    return np.ones(shape)


def _full(shape, value):
    _shape_size(shape)
    return np.full(shape, value, dtype=float)


def _array(obj):
    a = np.asarray(obj)
    if a.dtype.kind not in "biufc?":
        raise ExpressionError("arrays must be numeric")
    return np.array(a, dtype=complex if a.dtype.kind == "c" else float)


def _outer(a, b):
    _check_size(np.size(a) * np.size(b))
    return np.outer(a, b)


def _meshgrid(*arrays):
    total = 1
    for a in arrays:
        total *= _check_size(np.size(a))
        _check_size(total * len(arrays))
    return np.meshgrid(*arrays)


def _tile(a, reps):
    _check_size(np.size(a) * _shape_size(reps))
    return np.tile(a, reps)


def _repeat(a, n):
    _check_size(np.size(a) * int(n))
    return np.repeat(a, int(n))


_NP_FUNCS = {
    # elementwise math
    "sin": np.sin, "cos": np.cos, "tan": np.tan, "arcsin": np.arcsin, "arccos": np.arccos,
    "arctan": np.arctan, "arctan2": np.arctan2, "sinh": np.sinh, "cosh": np.cosh,
    "tanh": np.tanh, "arcsinh": np.arcsinh, "arccosh": np.arccosh, "arctanh": np.arctanh,
    "exp": np.exp, "expm1": np.expm1, "log": np.log, "log10": np.log10, "log2": np.log2,
    "log1p": np.log1p, "sqrt": np.sqrt, "cbrt": np.cbrt, "square": np.square,
    "floor": np.floor, "ceil": np.ceil, "trunc": np.trunc, "rint": np.rint, "round": np.round,
    "sign": np.sign, "abs": np.abs, "absolute": np.abs, "mod": np.mod, "fmod": np.fmod,
    "hypot": np.hypot, "degrees": np.degrees, "radians": np.radians, "deg2rad": np.deg2rad,
    "rad2deg": np.rad2deg, "clip": np.clip, "where": np.where, "maximum": np.maximum,
    "minimum": np.minimum, "isnan": np.isnan, "isfinite": np.isfinite, "real": np.real,
    "imag": np.imag, "angle": np.angle, "conj": np.conj, "heaviside": np.heaviside,
    "sinc": np.sinc,
    # reductions / shape
    "sum": np.sum, "mean": np.mean, "min": np.min, "max": np.max, "amin": np.amin,
    "amax": np.amax, "prod": np.prod, "cumsum": np.cumsum, "cumprod": np.cumprod,
    "diff": np.diff, "median": np.median, "std": np.std, "var": np.var, "argmin": np.argmin,
    "argmax": np.argmax, "flip": np.flip, "roll": np.roll, "reshape": np.reshape,
    "transpose": np.transpose, "concatenate": np.concatenate, "stack": np.stack,
    "hstack": np.hstack, "vstack": np.vstack, "interp": np.interp, "dot": np.dot,
    "size": np.size, "shape": np.shape, "ndim": np.ndim, "nan_to_num": np.nan_to_num,
    # guarded constructors
    "linspace": _linspace, "logspace": _logspace, "geomspace": _geomspace,
    "arange": _arange, "zeros": _zeros, "ones": _ones, "full": _full, "array": _array,
    "asarray": _array, "outer": _outer, "meshgrid": _meshgrid, "tile": _tile,
    "repeat": _repeat,
}
_NP_CONSTS = {"pi": np.pi, "e": np.e, "inf": np.inf, "nan": np.nan}
_MATH_FUNCS = {n: getattr(math, n) for n in (
    "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sinh", "cosh", "tanh", "exp",
    "log", "log10", "log2", "sqrt", "floor", "ceil", "fabs", "hypot", "degrees", "radians",
    "copysign", "isnan", "isfinite", "trunc", "fmod")}
_MATH_CONSTS = {"pi": math.pi, "e": math.e, "inf": math.inf, "nan": math.nan, "tau": math.tau}

_BUILTINS = {
    "abs": abs, "min": min, "max": max, "round": round, "float": float, "int": int,
    "bool": bool, "len": len, "pow": _pow, "complex": complex,
}
_CONSTS = {"True": True, "False": False, "None": None}

#: names usable bare (``sin(x)``, ``pi``) in addition to the variables
_TOP_LEVEL: Dict[str, Any] = {}
_TOP_LEVEL.update({k: v for k, v in _NP_FUNCS.items() if k not in ("min", "max", "abs", "round",
                                                                    "sum", "size", "shape")})
_TOP_LEVEL.update(_NP_CONSTS)
_TOP_LEVEL.update(_BUILTINS)
_TOP_LEVEL.update(_CONSTS)
_TOP_LEVEL["sum"] = np.sum

#: data properties allowed on values (never callable members)
_SAFE_ATTRS = {"real", "imag", "shape", "size", "ndim", "T"}
_MODULES = {"np": _NP_FUNCS, "numpy": _NP_FUNCS, "math": _MATH_FUNCS}
_MODULE_CONSTS = {"np": _NP_CONSTS, "numpy": _NP_CONSTS, "math": _MATH_CONSTS}

_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call,
    ast.Name, ast.Load, ast.Constant, ast.Tuple, ast.List, ast.Subscript, ast.Slice,
    ast.Attribute, ast.keyword,
    # operators
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.LShift,
    ast.RShift, ast.BitAnd, ast.BitOr, ast.BitXor, ast.MatMult, ast.UAdd, ast.USub, ast.Not,
    ast.Invert, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)
if hasattr(ast, "Index"):          # Python < 3.9 wraps subscripts
    _ALLOWED_NODES = _ALLOWED_NODES + (ast.Index,)
_KEYWORDS = {"num", "endpoint", "base", "axis"}


class _Validator(ast.NodeVisitor):
    """Reject everything that is not on the allow-list."""

    def __init__(self, variables: Optional[set]):
        self.variables = variables
        self.names = set()

    def generic_visit(self, node):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError("%s is not allowed in expressions" % type(node).__name__)
        super().generic_visit(node)

    def visit_Constant(self, node):
        if not isinstance(node.value, (int, float, complex, str, bool, type(None))):
            raise ExpressionError("constant of type %s not allowed" % type(node.value).__name__)
        if isinstance(node.value, str) and len(node.value) > 1000:
            raise ExpressionError("string constant too long")

    def visit_Name(self, node):
        if node.id.startswith("_"):
            raise ExpressionError("name %r not allowed" % node.id)
        self.names.add(node.id)

    def visit_Attribute(self, node):
        if node.attr.startswith("_"):
            raise ExpressionError("access to private attribute %r not allowed" % node.attr)
        if isinstance(node.value, ast.Name) and node.value.id in _MODULES:
            mod = node.value.id
            if node.attr not in _MODULES[mod] and node.attr not in _MODULE_CONSTS[mod]:
                raise ExpressionError("%s.%s is not available in expressions" % (mod, node.attr))
            self.names.add(mod)
            return
        if node.attr not in _SAFE_ATTRS:
            raise ExpressionError("attribute %r not allowed" % node.attr)
        self.visit(node.value)

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Name):
            if f.id not in _TOP_LEVEL or not callable(_TOP_LEVEL[f.id]):
                raise ExpressionError("%r is not a function available in expressions" % f.id)
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id in _MODULES:
            if f.attr not in _MODULES[f.value.id]:
                raise ExpressionError("%s.%s is not available in expressions" % (f.value.id, f.attr))
        else:
            raise ExpressionError("only plain function calls are allowed")
        for kw in node.keywords:
            if kw.arg is None or kw.arg not in _KEYWORDS:
                raise ExpressionError("keyword argument %r not allowed" % kw.arg)
        for a in node.args:
            if isinstance(a, ast.Starred):
                raise ExpressionError("starred arguments not allowed")
        self.generic_visit(node)


class _Rewriter(ast.NodeTransformer):
    """Route the operators that can explode (``**``, ``*``, ``<<``) through guards."""

    _MAP = {ast.Pow: "_pow", ast.Mult: "_mul", ast.LShift: "_lshift", ast.Mod: "_mod"}

    def visit_BinOp(self, node):
        self.generic_visit(node)
        fn = self._MAP.get(type(node.op))
        if fn is None:
            return node
        return ast.copy_location(ast.Call(func=ast.Name(id=fn, ctx=ast.Load()),
                                          args=[node.left, node.right], keywords=[]), node)


class _Module:
    """Stand-in for ``np`` / ``math`` exposing only allow-listed members."""

    def __init__(self, funcs, consts):
        self.__dict__.update(funcs)
        self.__dict__.update(consts)


_NS_BASE: Dict[str, Any] = {"__builtins__": {}, "_pow": _pow, "_mul": _mul, "_lshift": _lshift, "_mod": _mod,
                            "np": _Module(_NP_FUNCS, _NP_CONSTS),
                            "numpy": _Module(_NP_FUNCS, _NP_CONSTS),
                            "math": _Module(_MATH_FUNCS, _MATH_CONSTS)}
_NS_BASE.update(_TOP_LEVEL)


class Expression:
    """A compiled expression.  Call :meth:`evaluate` with a variable mapping."""

    def __init__(self, source: str, allowed_names: Optional[Iterable[str]] = None):
        if not isinstance(source, str) or not source.strip():
            raise ExpressionError("empty expression")
        self.source = source.strip()
        if len(self.source) > MAX_SOURCE:
            raise ExpressionError("expression longer than %d characters" % MAX_SOURCE)
        try:
            tree = ast.parse(self.source, mode="eval")
        except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
            raise ExpressionError("syntax error in %r: %s"
                                  % (self.source[:80], getattr(exc, "msg", exc))) from None
        v = _Validator(set(allowed_names) if allowed_names is not None else None)
        try:
            v.visit(tree)
        except RecursionError:
            raise ExpressionError("expression nested too deeply") from None
        known = set(_TOP_LEVEL) | set(_MODULES)
        self.names = sorted(n for n in v.names if n not in _MODULES)
        if allowed_names is not None:
            unknown = [n for n in self.names if n not in known and n not in set(allowed_names)]
            if unknown:
                raise ExpressionError("unknown name(s) %s in %r" % (", ".join(unknown), self.source))
        tree = ast.fix_missing_locations(_Rewriter().visit(tree))
        self._code = compile(tree, "<expr>", "eval")

    def evaluate(self, variables: Optional[Dict[str, Any]] = None) -> Any:
        ns = dict(_NS_BASE)
        if variables:
            for k, val in variables.items():
                if not str(k).startswith("_"):
                    ns[k] = val
        try:
            return eval(self._code, ns)  # noqa: S307 - allow-listed AST, no builtins
        except ExpressionError:
            raise
        except Exception as exc:
            raise ExpressionError("error evaluating %r: %s: %s"
                                  % (self.source[:200], type(exc).__name__, exc)) from None

    def __repr__(self):
        return "Expression(%r)" % self.source


def evaluate(source: str, **variables) -> Any:
    return Expression(source).evaluate(variables)
