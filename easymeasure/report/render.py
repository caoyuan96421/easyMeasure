"""Render a dataset summary figure with matplotlib (headless, thread-safe use)."""

from __future__ import annotations

import io
import math
import threading
from typing import List, Optional

import numpy as np

from ..common import ndview

_MPL_LOCK = threading.Lock()  # matplotlib's pyplot state is not thread-safe


def numeric_channels(ds) -> List[str]:
    return [n for n, a in ds.data.items() if a.dtype != object and np.isfinite(a).any()]


def render_png(ds, channels: Optional[List[str]] = None, max_plots: int = 6,
               cmap: str = "viridis", width_in: float = 12.0, dpi: int = 110) -> bytes:
    """Return a PNG with one panel per record channel (≤ *max_plots*)."""
    import matplotlib
    matplotlib.use("Agg", force=False)
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    names = channels or numeric_channels(ds)
    names = names[:max_plots]
    n = max(1, len(names))
    cols = 1 if n == 1 else 2 if n <= 4 else 3
    rows = int(math.ceil(n / cols))
    height = min(7.0, 3.2 * rows) if n > 1 else 5.5
    with _MPL_LOCK:
        fig = Figure(figsize=(width_in, height), dpi=dpi)
        FigureCanvasAgg(fig)
        if not names:
            ax = fig.add_subplot(1, 1, 1)
            ax.text(0.5, 0.5, "no numeric data", ha="center", va="center")
            ax.set_axis_off()
        for j, name in enumerate(names):
            ax = fig.add_subplot(rows, cols, j + 1)
            try:
                _plot_channel(fig, ax, ds, name, cmap)
            except Exception as exc:  # never let one panel break the report
                ax.text(0.5, 0.5, "%s: cannot plot (%s)" % (name, exc), ha="center",
                        va="center", fontsize=8)
                ax.set_axis_off()
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        return buf.getvalue()


def _plot_channel(fig, ax, ds, name, cmap):
    x, y = ndview.default_view(ds, name)
    fixed = ndview.latest_indices(ds, name)
    xs, ys, z = ndview.extract(ds, name, x, y, fixed)
    axes = {a["id"]: a for a in ndview.axes_for(ds, name)}
    unit = (ds.header.get("units") or {}).get(name, "")
    if ys is None:
        ax.plot(xs, z, ".-", ms=3, lw=1)
        ax.set_xlabel(axes[x]["label"])
        ax.set_ylabel(name + (" (%s)" % unit if unit else ""))
    else:
        finite = z[np.isfinite(z)]
        if finite.size == 0:
            raise ValueError("no data yet")
        lo, hi = np.percentile(finite, [1, 99]) if finite.size > 10 else (finite.min(), finite.max())
        if hi <= lo:
            hi = lo + 1e-12
        extent = [_edge(xs)[0], _edge(xs)[1], _edge(ys)[0], _edge(ys)[1]]
        im = ax.imshow(np.ma.masked_invalid(z), origin="lower", aspect="auto", extent=extent,
                       cmap=cmap, vmin=lo, vmax=hi, interpolation="nearest")
        fig.colorbar(im, ax=ax, label=name + (" (%s)" % unit if unit else ""))
        ax.set_xlabel(axes[x]["label"])
        ax.set_ylabel(axes[y]["label"])
    others = [a for a in axes.values() if a["id"] not in (x, y)]
    if others:
        bits = []
        for a in others:
            i = int(fixed.get(a["id"], 0))
            c = a.get("coords")
            val = ("%.4g" % c[i]) if c is not None and i < len(c) else "?"
            bits.append("%s=%s [#%d]" % (a.get("name", a["label"]), val, i))
        ax.set_title(name + "  [" + ", ".join(bits) + "]", fontsize=8)
    else:
        ax.set_title(name, fontsize=9)


def _edge(c):
    c = np.asarray(c, dtype=float)
    if c.size == 0:
        return (0.0, 1.0)
    if c.size == 1:
        return (c[0] - 0.5, c[0] + 0.5)
    d = (c[-1] - c[0]) / (c.size - 1)
    if d == 0:
        d = 1.0
    return (c[0] - d / 2, c[-1] + d / 2)
