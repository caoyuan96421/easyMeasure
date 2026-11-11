"""Slicing N-dimensional sweep data for display (shared by GUI and reports).

For a channel recorded in dimension *k* of a sweep the data array has shape
``(N_k, ..., N_{D-1}) + value_shape``.  Every one of these array axes is a
*display axis*: sweep axes use the setpoints of their first scalar sweep
channel as coordinates; value axes use the coordinates the driver gave
(``DataSet.value_axes``, e.g. the frequencies of a VNA trace) or their index.  A view selects one axis
(1-D curve) or two axes (heat map / stacked curves) and fixes every other axis
at an index.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np


def axes_for(ds, name: str) -> List[dict]:
    arr = ds.data.get(name)
    k = ds.dim_of.get(name, 0)
    out = []
    for dim in range(k, ds.ndim):
        label, coords = ds.sweep_axis(dim)
        unit = (ds.header.get("units") or {}).get(label, "")
        out.append({"id": "d%d" % dim, "kind": "sweep", "dim": dim, "name": label, "unit": unit,
                    "label": label + (" (%s)" % unit if unit else ""),
                    "size": ds.shape[dim], "coords": coords})
    if arr is not None:
        vshape = arr.shape[ds.ndim - k:]
        given = (getattr(ds, "value_axes", None) or {}).get(name) or []
        for j, n in enumerate(vshape):
            ax = given[j] if j < len(given) else None
            coords = None if not ax else np.asarray(ax.get("coords"), dtype=float).ravel()
            if coords is not None and coords.size == n:
                unit = ax.get("unit") or ""
                label = ax.get("name") or "%s[axis %d]" % (name, j)
                out.append({"id": "v%d" % j, "kind": "value", "dim": j, "unit": unit,
                            "name": label, "label": label + (" (%s)" % unit if unit else ""),
                            "size": n, "coords": coords})
            else:
                out.append({"id": "v%d" % j, "kind": "value", "dim": j, "unit": "",
                            "name": "%s[axis %d]" % (name, j),
                            "label": "%s[axis %d]" % (name, j), "size": n,
                            "coords": np.arange(n, dtype=float)})
    return out


def latest_indices(ds, name: str) -> Dict[str, int]:
    """Indices of the most recently written point, for 'follow' mode."""
    out: Dict[str, int] = {}
    k = ds.dim_of.get(name, 0)
    li = None
    if getattr(ds, "last_by_dim", None) and k in ds.last_by_dim:
        li = (k, ds.last_by_dim[k])      # newest point that actually has this channel
    elif ds.last_index is not None:
        li = ds.last_index
    if li is not None:
        lk, idx = li
        # idx covers dims lk..D-1; we need dims k..D-1
        for dim in range(max(k, lk), ds.ndim):
            out["d%d" % dim] = int(idx[dim - lk])
    return out


def extract(ds, name: str, x: str, y: Optional[str] = None,
            fixed: Optional[Dict[str, int]] = None
            ) -> Tuple[np.ndarray, Optional[np.ndarray], np.ndarray]:
    """Return ``(xcoords, ycoords or None, values)``.

    ``values`` is 1-D (len(x)) or 2-D with shape ``(len(y), len(x))``.
    """
    arr = ds.data.get(name)
    if arr is None or arr.dtype == object:
        raise KeyError("no numeric data for %s" % name)
    axes = axes_for(ds, name)
    ids = [a["id"] for a in axes]
    if x not in ids or (y is not None and y not in ids) or x == y:
        raise ValueError("invalid axis selection")
    fixed = fixed or {}
    index = []
    for a in axes:
        if a["id"] in (x, y):
            index.append(slice(None))
        else:
            i = int(fixed.get(a["id"], 0))
            index.append(min(max(i, 0), a["size"] - 1))
    with ds.lock:
        sub = np.array(arr[tuple(index)], dtype=float, copy=True)
    # remaining axes are in original order; arrange to (y, x)
    remaining = [a["id"] for a in axes if a["id"] in (x, y)]
    if y is None:
        return axes[ids.index(x)]["coords"], None, sub
    if remaining == [x, y]:
        sub = sub.T
    return axes[ids.index(x)]["coords"], axes[ids.index(y)]["coords"], sub


def default_view(ds, name: str) -> Tuple[str, Optional[str]]:
    axes = axes_for(ds, name)
    if not axes:
        return "d0", None
    # prefer: value axis as x for vectors (spectra), else innermost sweep axis
    vals = [a for a in axes if a["kind"] == "value" and a["size"] > 1]
    sweeps = [a for a in axes if a["kind"] == "sweep" and a["size"] > 1]
    if len(vals) >= 2:
        return vals[0]["id"], vals[1]["id"]
    if len(vals) == 1:
        return vals[0]["id"], (sweeps[0]["id"] if sweeps else None)
    if len(sweeps) >= 2:
        return sweeps[0]["id"], sweeps[1]["id"]
    if sweeps:
        return sweeps[0]["id"], None
    return axes[0]["id"], None
