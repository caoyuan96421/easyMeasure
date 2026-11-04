"""Wire codec for the backend <-> frontend link.

A message is any JSON-like Python object (dict/list/str/float/int/bool/None)
which may additionally contain ``numpy.ndarray`` (numeric/bool dtypes), numpy
scalars, tuples and ``bytes``.  Arrays are not JSON-encoded: they are replaced by
a small placeholder and their raw memory is appended after the JSON header, so
large 2-D maps travel efficiently.

Frame layout (all integers little-endian)::

    u32  frame_length          (number of bytes that follow)
    u32  json_length
    json_length bytes of UTF-8 JSON
    concatenated raw buffers referenced from the JSON

No pickle is used anywhere, so a malformed or hostile frame cannot execute code.
"""

from __future__ import annotations

import json
import math
import struct
from typing import Any, Iterator, List

import numpy as np

from .errors import ProtocolError

MAX_FRAME = 1 << 30  # 1 GiB hard limit to protect against garbage lengths
_HDR = struct.Struct("<I")

_ALLOWED_KINDS = set("biufc?")  # bool, int, uint, float, complex


def _flatten(obj: Any, buffers: List[bytes]) -> Any:
    """Replace arrays/bytes by placeholders; return a JSON-safe structure."""
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        if math.isfinite(obj):
            return obj
        return {"__f__": repr(obj)}  # nan / inf / -inf
    if isinstance(obj, np.ndarray):
        if obj.dtype.kind in _ALLOWED_KINDS:
            arr = np.ascontiguousarray(obj)
            if arr.dtype.byteorder == ">":
                arr = arr.astype(arr.dtype.newbyteorder("<"))
            buffers.append(arr.tobytes())
            return {"__nd__": len(buffers) - 1, "dtype": arr.dtype.str, "shape": list(arr.shape)}
        if obj.dtype.kind in "US":
            return {"__ndstr__": [_flatten(x, buffers) for x in obj.ravel().tolist()],
                    "shape": list(obj.shape)}
        # object arrays: encode element-wise as a nested list
        return {"__ndobj__": [_flatten(x, buffers) for x in obj.ravel().tolist()],
                "shape": list(obj.shape)}
    if isinstance(obj, np.generic):
        return _flatten(obj.item(), buffers)
    if isinstance(obj, complex):
        return {"__c__": [obj.real, obj.imag]}
    if isinstance(obj, (bytes, bytearray, memoryview)):
        buffers.append(bytes(obj))
        return {"__b__": len(buffers) - 1}
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out[str(k)] = _flatten(v, buffers)
        return out
    if isinstance(obj, (list, tuple)):
        return [_flatten(v, buffers) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return [_flatten(v, buffers) for v in sorted(obj, key=repr)]
    # Fallback: stringify unknown objects rather than failing the whole message
    return {"__repr__": repr(obj)}


def _restore(obj: Any, buffers: List[memoryview]) -> Any:
    if isinstance(obj, list):
        return [_restore(v, buffers) for v in obj]
    if isinstance(obj, dict):
        if "__nd__" in obj and len(obj) == 3:
            idx = obj["__nd__"]
            if not isinstance(idx, int) or not 0 <= idx < len(buffers):
                raise ProtocolError("bad buffer index")
            dt = np.dtype(obj["dtype"])
            if dt.kind not in _ALLOWED_KINDS:
                raise ProtocolError("illegal dtype %r" % obj["dtype"])
            shape = tuple(int(s) for s in obj["shape"])
            arr = np.frombuffer(buffers[idx], dtype=dt)
            expected = int(np.prod(shape)) if shape else 1
            if arr.size != expected:
                raise ProtocolError("buffer size mismatch")
            return arr.reshape(shape).copy()
        if "__ndstr__" in obj and len(obj) == 2:
            items = [_restore(v, buffers) for v in obj["__ndstr__"]]
            return np.array(items, dtype=object).reshape(tuple(obj["shape"]))
        if "__ndobj__" in obj and len(obj) == 2:
            items = [_restore(v, buffers) for v in obj["__ndobj__"]]
            arr = np.empty(len(items), dtype=object)
            for i, it in enumerate(items):
                arr[i] = it
            return arr.reshape(tuple(obj["shape"]))
        if "__f__" in obj and len(obj) == 1:
            return float(obj["__f__"])
        if "__c__" in obj and len(obj) == 1:
            re, im = obj["__c__"]
            return complex(re, im)
        if "__b__" in obj and len(obj) == 1:
            idx = obj["__b__"]
            if not isinstance(idx, int) or not 0 <= idx < len(buffers):
                raise ProtocolError("bad buffer index")
            return bytes(buffers[idx])
        if "__repr__" in obj and len(obj) == 1:
            return obj["__repr__"]
        return {k: _restore(v, buffers) for k, v in obj.items()}
    return obj


def encode(obj: Any) -> bytes:
    """Encode *obj* into a complete frame (including the length prefix)."""
    buffers: List[bytes] = []
    flat = _flatten(obj, buffers)
    meta = {"m": flat, "b": [len(b) for b in buffers]}
    js = json.dumps(meta, separators=(",", ":"), allow_nan=False).encode("utf-8")
    body_len = 4 + len(js) + sum(len(b) for b in buffers)
    if body_len > MAX_FRAME:
        raise ProtocolError("message too large (%d bytes)" % body_len)
    parts = [_HDR.pack(body_len), _HDR.pack(len(js)), js]
    parts.extend(buffers)
    return b"".join(parts)


def decode_body(body: bytes) -> Any:
    """Decode the part of a frame after the 4-byte frame length."""
    try:
        if len(body) < 4:
            raise ProtocolError("frame too short")
        (jlen,) = _HDR.unpack_from(body, 0)
        if 4 + jlen > len(body):
            raise ProtocolError("bad json length")
        meta = json.loads(bytes(body[4:4 + jlen]).decode("utf-8"))
        sizes = meta.get("b", [])
        mv = memoryview(body)
        offset = 4 + jlen
        bufs = []
        for s in sizes:
            s = int(s)
            if s < 0 or offset + s > len(body):
                raise ProtocolError("bad buffer length")
            bufs.append(mv[offset:offset + s])
            offset += s
        return _restore(meta.get("m"), bufs)
    except ProtocolError:
        raise
    except Exception as exc:  # json errors, unicode errors, ...
        raise ProtocolError("cannot decode frame: %s" % exc) from exc


def decode(frame: bytes) -> Any:
    """Decode a complete frame produced by :func:`encode`."""
    if len(frame) < 4:
        raise ProtocolError("frame too short")
    (n,) = _HDR.unpack_from(frame, 0)
    if n != len(frame) - 4:
        raise ProtocolError("frame length mismatch")
    return decode_body(frame[4:])


class FrameReader:
    """Incrementally split a byte stream into frames."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes) -> Iterator[Any]:
        self._buf.extend(data)
        while True:
            if len(self._buf) < 4:
                return
            (n,) = _HDR.unpack_from(self._buf, 0)
            if n > MAX_FRAME or n < 4:
                self._buf.clear()
                raise ProtocolError("invalid frame length %d" % n)
            if len(self._buf) < 4 + n:
                return
            body = bytes(self._buf[4:4 + n])
            del self._buf[:4 + n]
            yield decode_body(body)

    def pending(self) -> int:
        return len(self._buf)


def to_jsonable(obj: Any) -> Any:
    """Convert numpy containers into plain JSON-serialisable Python objects.

    Used when writing human-readable JSON (setup files, sweep definitions).
    """
    if isinstance(obj, np.ndarray):
        return [to_jsonable(x) for x in obj.tolist()]
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj
