import math

import numpy as np
import pytest

from easymeasure.common import codec
from easymeasure.common.errors import ExpressionError, ProtocolError
from easymeasure.common.exprs import Expression


def test_roundtrip_mixed():
    obj = {"a": 1, "b": 2.5, "c": "x", "d": None, "e": [1, (2, 3)], "nan": float("nan"),
           "inf": float("-inf"), "arr": np.arange(12.0).reshape(3, 4),
           "iarr": np.arange(5, dtype=np.int16), "bytes": b"\x00\x01", "cplx": 1 + 2j,
           "strarr": np.array(["a", "bb"]), "np_scalar": np.float32(1.5), "bool": True,
           "big_endian": np.arange(3, dtype=">f8")}
    out = codec.decode(codec.encode(obj))
    assert out["a"] == 1 and out["b"] == 2.5 and out["c"] == "x" and out["d"] is None
    assert out["e"] == [1, [2, 3]]
    assert math.isnan(out["nan"]) and out["inf"] == float("-inf")
    np.testing.assert_array_equal(out["arr"], obj["arr"])
    assert out["iarr"].dtype == np.int16
    assert out["bytes"] == b"\x00\x01"
    assert out["cplx"] == 1 + 2j
    assert list(out["strarr"]) == ["a", "bb"]
    assert out["np_scalar"] == 1.5 and out["bool"] is True
    np.testing.assert_array_equal(out["big_endian"], [0, 1, 2])


def test_frame_reader_split_and_garbage():
    frames = b"".join(codec.encode({"i": i, "a": np.ones(100) * i}) for i in range(5))
    r = codec.FrameReader()
    got = []
    for j in range(0, len(frames), 7):  # byte dribble
        got.extend(r.feed(frames[j:j + 7]))
    assert [g["i"] for g in got] == list(range(5))
    with pytest.raises(ProtocolError):
        list(codec.FrameReader().feed(b"\xff\xff\xff\xff" + b"x" * 10))
    with pytest.raises(ProtocolError):
        codec.decode(b"\x05\x00\x00\x00\x01\x00\x00\x00{")
    # buffer index out of range must not crash
    bad = codec.encode({"a": np.ones(3)}).replace(b'"__nd__":0', b'"__nd__":9')
    with pytest.raises(ProtocolError):
        codec.decode(bad)


def test_object_dtype_is_not_executable():
    # An "object" dtype placeholder is rejected rather than evaluated
    body = codec.encode({"a": np.ones(2)}).replace(b"<f8", b"|O8")
    with pytest.raises(ProtocolError):
        codec.decode(body)


def test_expressions():
    assert Expression("start + (stop-start)*t**2").evaluate({"start": 1, "stop": 3, "t": 0.5}) == 1.5
    v = Expression("np.sin(np.linspace(0, pi, 5)) * t").evaluate({"t": 2})
    assert v.shape == (5,)
    for bad in ["__import__('os')", "().__class__", "x.__dict__", "lambda: 1", "", "1 +"]:
        with pytest.raises(ExpressionError):
            Expression(bad).evaluate({"x": 1})
    with pytest.raises(ExpressionError):
        Expression("y + 1", allowed_names=["x"])
    with pytest.raises(ExpressionError):
        Expression("1/x").evaluate({"x": 0})
