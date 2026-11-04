"""Crash-safe data storage for sweeps.

File format (``.emj`` - easyMeasure journal)
-------------------------------------------
An append-only sequence of self-checking frames, preceded by the 8-byte magic
``b"EMJ1\\r\\n\\x1a\\n"``::

    b"F"  u8 type  u32 payload_length  u32 crc32(payload)  payload

``payload`` is an :mod:`easymeasure.common.codec` frame (JSON + raw arrays, no
pickle).  Frame types:

* ``HEADER`` - sweep definition, setpoints, metadata (first frame)
* ``POINT``  - values recorded at one recording event: ``{"k": dim, "i": index,
  "v": {channel: value}, "e": {channel: error}, "t": time}``
* ``PHASE``  - pre/post sweep recordings
* ``EVENT``  - status changes, notes
* ``FOOTER`` - final status

Every frame is flushed to the OS immediately and ``fsync``-ed at least every
``fsync_interval`` seconds (and at every phase change), so a crash or power loss
loses at most that window.  A torn last frame is detected by its length/CRC and
ignored on recovery; everything before it is usable.  Loading a partial file
gives arrays filled with NaN where no data was taken.

``export_hdf5`` converts a journal into a conventional HDF5 file for analysis.
"""

from __future__ import annotations

import json
import os
import re
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import numpy as np

from ..common import codec
from ..common.errors import EasyMeasureError
from .logsys import get_logger

log = get_logger("storage")

MAGIC = b"EMJ1\r\n\x1a\n"
_FRAME = struct.Struct("<cBII")  # b'F', type, length, crc

HEADER, POINT, PHASE, EVENT, FOOTER = 1, 2, 3, 4, 5
_TYPE_NAMES = {HEADER: "header", POINT: "point", PHASE: "phase", EVENT: "event", FOOTER: "footer"}


class StorageError(EasyMeasureError):
    pass


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------
class JournalWriter:
    def __init__(self, path: Path, fsync_interval: float = 0.5):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fsync_interval = float(fsync_interval)
        self._lock = threading.Lock()
        self._f = open(self.path, "xb")  # never overwrite an existing data file
        self._f.write(MAGIC)
        self._f.flush()
        self._last_sync = 0.0
        self._sync(force=True)
        self.frames = 0
        self.bytes = len(MAGIC)
        self.failed = False

    def _sync(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_sync >= self.fsync_interval:
            try:
                os.fsync(self._f.fileno())
            except OSError as exc:  # pragma: no cover - e.g. network drives
                log.warning("fsync failed on %s: %s", self.path, exc)
            self._last_sync = now

    def write(self, ftype: int, obj: Any, sync: bool = False) -> None:
        payload = codec.encode(obj)
        hdr = _FRAME.pack(b"F", ftype, len(payload), zlib.crc32(payload) & 0xFFFFFFFF)
        with self._lock:
            if self._f is None:
                raise StorageError("journal %s is closed" % self.path)
            try:
                self._f.write(hdr + payload)
                self._f.flush()
                self._sync(force=sync)
            except OSError as exc:
                self.failed = True
                raise StorageError("cannot write %s: %s" % (self.path, exc)) from exc
            self.frames += 1
            self.bytes += len(hdr) + len(payload)

    def close(self) -> None:
        with self._lock:
            f, self._f = self._f, None
            if f is not None:
                try:
                    f.flush()
                    os.fsync(f.fileno())
                except OSError:
                    pass
                f.close()


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------
def iter_frames(path: Path) -> Iterator[Tuple[int, Any]]:
    """Yield ``(type, object)`` for every intact frame; stop at the first bad one."""
    with open(path, "rb") as f:
        magic = f.read(len(MAGIC))
        if magic != MAGIC:
            raise StorageError("%s is not an easyMeasure journal" % path)
        while True:
            hdr = f.read(_FRAME.size)
            if len(hdr) < _FRAME.size:
                return  # clean EOF or torn header
            tag, ftype, n, crc = _FRAME.unpack(hdr)
            if tag != b"F" or n > codec.MAX_FRAME:
                log.warning("%s: corrupt frame header, stopping", path)
                return
            payload = f.read(n)
            if len(payload) < n:
                log.info("%s: truncated final frame ignored (crash recovery)", path)
                return
            if zlib.crc32(payload) & 0xFFFFFFFF != crc:
                log.warning("%s: CRC mismatch, stopping at frame", path)
                return
            try:
                yield ftype, codec.decode(payload)
            except Exception as exc:
                log.warning("%s: undecodable frame skipped: %s", path, exc)


# ---------------------------------------------------------------------------
# DataSet - the in-memory N-D view of a run (used by backend and frontend)
# ---------------------------------------------------------------------------
class DataSet:
    """N-dimensional data of one sweep run.

    ``header`` contains (among others):

    * ``shape``: list of dimension sizes, dim 0 = innermost (fastest)
    * ``dims``: per dimension ``{"sweep": [names], "record": [names], "label": ...}``
    * ``setpoints``: ``{dim: {channel: array of shape (N_k, *channel_shape)}}``
    * ``definition``: the full sweep definition (dict)

    Data arrays: ``data[name]`` for a channel recorded in dimension ``k`` has
    shape ``(N_k, N_{k+1}, ..., N_{D-1}) + value_shape`` with float64 (NaN =
    missing) or object dtype for strings.
    """

    def __init__(self, header: dict):
        self.header = header
        self.shape: List[int] = [int(n) for n in header["shape"]]
        self.dims: List[dict] = header["dims"]
        self.data: Dict[str, np.ndarray] = {}
        self.dim_of: Dict[str, int] = {}
        self.filled: Dict[int, np.ndarray] = {}
        self.errors: Dict[str, int] = {}
        self.pre: Dict[str, Any] = {}
        self.post: Dict[str, Any] = {}
        self.events: List[dict] = []
        self.footer: Optional[dict] = None
        self.points = 0
        self.last_index: Optional[Tuple[int, Tuple[int, ...]]] = None
        self.last_by_dim: Dict[int, Tuple[int, ...]] = {}
        self.lock = threading.RLock()
        for k, d in enumerate(self.dims):
            self.filled[k] = np.zeros(self.shape[k:], dtype=bool)
            for name in d.get("record", []):
                self.dim_of[name] = k
        self.setpoints: Dict[int, Dict[str, np.ndarray]] = {}
        for k, sp in (header.get("setpoints") or {}).items():
            self.setpoints[int(k)] = {n: np.asarray(v) for n, v in sp.items()}

    # -- properties ------------------------------------------------------------
    @property
    def ndim(self) -> int:
        return len(self.shape)

    @property
    def total_points(self) -> int:
        return int(np.prod(self.shape)) if self.shape else 0

    @property
    def completed_points(self) -> int:
        """Number of completed innermost recording events."""
        return int(self.filled[0].sum()) if 0 in self.filled else 0

    @property
    def status(self) -> str:
        if self.footer is not None:
            return self.footer.get("status", "finished")
        return self.header.get("status", "incomplete")

    def record_names(self) -> List[str]:
        return list(self.dim_of)

    def sweep_axis(self, k: int) -> Tuple[str, np.ndarray]:
        """A 1-D coordinate (name, values) for dimension *k* (first scalar sweep channel)."""
        sp = self.setpoints.get(k, {})
        for name, arr in sp.items():
            a = np.asarray(arr)
            if a.ndim == 1 and a.dtype.kind in "fiu":
                return name, a.astype(float)
        return "index %d" % k, np.arange(self.shape[k], dtype=float)

    # -- updates ---------------------------------------------------------------
    def _alloc(self, name: str, k: int, value: Any) -> np.ndarray:
        if isinstance(value, str):
            arr = np.full(tuple(self.shape[k:]), None, dtype=object)
        else:
            v = np.asarray(value, dtype=float)
            arr = np.full(tuple(self.shape[k:]) + v.shape, np.nan)
        self.data[name] = arr
        return arr

    def apply_point(self, k: int, index, values: Dict[str, Any],
                    errors: Optional[Dict[str, str]] = None) -> None:
        idx = tuple(int(i) for i in index)
        if len(idx) != self.ndim - k:
            raise StorageError("bad index length for dimension %d" % k)
        if any(not 0 <= i < n for i, n in zip(idx, self.shape[k:])):
            raise StorageError("index %s out of range" % (idx,))
        with self.lock:
            for name, val in values.items():
                if val is None:
                    continue
                arr = self.data.get(name)
                if arr is None:
                    arr = self._alloc(name, k, val)
                if arr.dtype == object:
                    arr[idx] = str(val)
                    continue
                if isinstance(val, str):
                    try:
                        val = float(val)
                    except ValueError:
                        val = np.nan
                v = np.asarray(val, dtype=float)
                target_shape = arr.shape[len(idx):]
                if v.shape == target_shape:
                    arr[idx] = v
                else:
                    # shape changed during the sweep: fit what overlaps, pad with NaN
                    out = np.full(target_shape, np.nan)
                    if v.ndim == len(target_shape):
                        sl = tuple(slice(0, min(a, b)) for a, b in zip(v.shape, target_shape))
                        out[sl] = v[sl]
                    elif v.size == 1:
                        out[...] = float(v.reshape(-1)[0])
                    arr[idx] = out
            for name in (errors or {}):
                self.errors[name] = self.errors.get(name, 0) + 1
            self.filled[k][idx] = True
            if k == 0:
                self.points += 1
            self.last_index = (k, idx)
            self.last_by_dim[k] = idx

    def apply_frame(self, ftype: int, obj: dict) -> None:
        if ftype == POINT:
            self.apply_point(obj["k"], obj["i"], obj.get("v", {}), obj.get("e"))
        elif ftype == PHASE:
            target = self.pre if obj.get("phase") == "pre" else self.post
            target.update(obj.get("v", {}))
        elif ftype == EVENT:
            self.events.append(obj)
        elif ftype == FOOTER:
            self.footer = obj

    # -- slicing for plots -----------------------------------------------------
    def snapshot(self) -> dict:
        """Everything needed to reconstruct this dataset elsewhere."""
        with self.lock:
            return {
                "header": self.header,
                "data": {k: v.copy() for k, v in self.data.items()},
                "filled": {str(k): v.copy() for k, v in self.filled.items()},
                "errors": dict(self.errors),
                "pre": dict(self.pre), "post": dict(self.post),
                "footer": self.footer, "points": self.points,
                "last_index": None if self.last_index is None else
                [self.last_index[0], list(self.last_index[1])],
                "last_by_dim": {str(k): list(v) for k, v in self.last_by_dim.items()},
            }

    @classmethod
    def from_snapshot(cls, snap: dict) -> "DataSet":
        ds = cls(snap["header"])
        for k, v in snap.get("data", {}).items():
            ds.data[k] = np.asarray(v) if not isinstance(v, np.ndarray) else v
        for k, v in snap.get("filled", {}).items():
            ds.filled[int(k)] = np.asarray(v, dtype=bool)
        ds.errors = dict(snap.get("errors", {}))
        ds.pre = dict(snap.get("pre", {}))
        ds.post = dict(snap.get("post", {}))
        ds.footer = snap.get("footer")
        ds.points = int(snap.get("points", 0))
        li = snap.get("last_index")
        ds.last_index = None if li is None else (int(li[0]), tuple(li[1]))
        for k, idx in (snap.get("last_by_dim") or {}).items():
            ds.last_by_dim[int(k)] = tuple(idx)
        return ds

    # -- loading ---------------------------------------------------------------
    @classmethod
    def load(cls, path) -> "DataSet":
        path = Path(path)
        if path.suffix.lower() in (".h5", ".hdf5"):
            return load_hdf5(path)
        ds: Optional[DataSet] = None
        for ftype, obj in iter_frames(path):
            if ftype == HEADER:
                ds = cls(obj)
                ds.header["file"] = str(path)
            elif ds is not None:
                try:
                    ds.apply_frame(ftype, obj)
                except Exception as exc:
                    log.warning("%s: bad %s frame skipped: %s", path,
                                _TYPE_NAMES.get(ftype, ftype), exc)
        if ds is None:
            raise StorageError("%s contains no header" % path)
        if ds.footer is None:
            ds.header["status"] = "incomplete"
        return ds


def read_header(path) -> dict:
    """Return just the header (sweep definition etc.) of a data file."""
    path = Path(path)
    if path.suffix.lower() in (".h5", ".hdf5"):
        return load_hdf5(path).header
    for ftype, obj in iter_frames(path):
        if ftype == HEADER:
            return obj
        break
    raise StorageError("%s contains no header" % path)


# ---------------------------------------------------------------------------
# HDF5 export
# ---------------------------------------------------------------------------
def export_hdf5(ds: DataSet, path) -> Path:
    try:
        import h5py  # type: ignore
    except ImportError as exc:
        raise StorageError("h5py is required for HDF5 export") from exc
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with ds.lock, h5py.File(tmp, "w") as f:
        hdr = {k: v for k, v in ds.header.items() if k != "setpoints"}
        f.attrs["header_json"] = json.dumps(codec.to_jsonable(hdr))
        f.attrs["status"] = ds.status
        if ds.footer:
            f.attrs["footer_json"] = json.dumps(codec.to_jsonable(ds.footer))
        g = f.create_group("data")
        for name, arr in ds.data.items():
            if arr.dtype == object:
                dt = h5py.string_dtype()
                g.create_dataset(name, data=np.array([("" if x is None else str(x))
                                                     for x in arr.ravel()], dtype=dt)
                                 .reshape(arr.shape))
            else:
                g.create_dataset(name, data=arr, compression="gzip", compression_opts=4)
            g[name].attrs["dim"] = ds.dim_of.get(name, 0)
        sp = f.create_group("setpoints")
        for k, chans in ds.setpoints.items():
            gk = sp.create_group(str(k))
            for n, a in chans.items():
                a = np.asarray(a)
                if a.dtype == object:
                    a = np.array([str(x) for x in a.ravel()], dtype=h5py.string_dtype())
                gk.create_dataset(n, data=a)
        fl = f.create_group("filled")
        for k, a in ds.filled.items():
            fl.create_dataset(str(k), data=a)
        for phase, vals in (("pre", ds.pre), ("post", ds.post)):
            gp = f.create_group(phase)
            for n, v in vals.items():
                if isinstance(v, str):
                    gp.attrs[n] = v
                elif v is not None:
                    gp.create_dataset(n, data=np.asarray(v, dtype=float))
    os.replace(tmp, path)
    return path


def load_hdf5(path) -> DataSet:
    import h5py  # type: ignore
    with h5py.File(path, "r") as f:
        hdr = json.loads(f.attrs["header_json"])
        sp = {}
        for k in f["setpoints"]:
            sp[k] = {}
            for n in f["setpoints"][k]:
                a = f["setpoints"][k][n][()]
                if a.dtype.kind in "SO":
                    a = np.array([x.decode() if isinstance(x, bytes) else x for x in a.ravel()],
                                 dtype=object)
                sp[k][n] = a
        hdr["setpoints"] = sp
        ds = DataSet(hdr)
        for n in f["data"]:
            a = f["data"][n][()]
            if a.dtype.kind in "SO":
                a = np.array([x.decode() if isinstance(x, bytes) else x for x in a.ravel()],
                             dtype=object).reshape(a.shape)
            ds.data[n] = a
        for k in f["filled"]:
            ds.filled[int(k)] = f["filled"][k][()]
        for phase, target in (("pre", ds.pre), ("post", ds.post)):
            if phase in f:
                for n in f[phase]:
                    target[n] = f[phase][n][()]
                for n, v in f[phase].attrs.items():
                    target[n] = v
        if "footer_json" in f.attrs:
            ds.footer = json.loads(f.attrs["footer_json"])
        ds.points = int(ds.filled[0].sum()) if 0 in ds.filled else 0
        ds.header["file"] = str(path)
        return ds


# ---------------------------------------------------------------------------
# Data directory / run numbering
# ---------------------------------------------------------------------------
_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


class DataStore:
    """Allocates file names: ``<root>/<YYYY-MM-DD>/<run:05d>_<name>.emj``."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._counter_file = self.root / "run_counter.txt"

    def next_run_number(self) -> int:
        with self._lock:
            n = 0
            try:
                n = int(self._counter_file.read_text().strip() or 0)
            except (OSError, ValueError):
                # counter lost: rebuild from existing files
                for p in self.root.glob("*/*.emj"):
                    m = re.match(r"(\d+)_", p.name)
                    if m:
                        n = max(n, int(m.group(1)))
            n += 1
            tmp = self._counter_file.with_suffix(".tmp")
            tmp.write_text(str(n))
            os.replace(tmp, self._counter_file)
            return n

    def new_path(self, name: str) -> Tuple[int, Path]:
        run = self.next_run_number()
        day = time.strftime("%Y-%m-%d")
        safe = _SAFE.sub("_", name or "sweep").strip("_")[:60] or "sweep"
        p = self.root / day / ("%05d_%s.emj" % (run, safe))
        return run, p

    def list_runs(self, limit: int = 200) -> List[dict]:
        files = sorted(self.root.glob("*/*.emj"), key=lambda p: p.name, reverse=True)[:limit]
        out = []
        for p in files:
            try:
                st = p.stat()
                out.append({"path": str(p), "name": p.name, "size": st.st_size,
                            "mtime": st.st_mtime})
            except OSError:
                pass
        return out
