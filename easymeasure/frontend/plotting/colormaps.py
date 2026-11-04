"""Colormap helpers."""

from __future__ import annotations

from functools import lru_cache

import numpy as np
import pyqtgraph as pg

NAMES = ["viridis", "plasma", "inferno", "magma", "cividis", "turbo", "gray", "RdBu_r",
         "coolwarm", "seismic", "hot", "jet", "CET-D1", "CET-L17"]


@lru_cache(maxsize=None)
def get(name: str) -> pg.ColorMap:
    for source in (None, "matplotlib"):
        try:
            cm = pg.colormap.get(name, source=source) if source else pg.colormap.get(name)
            if cm is not None:
                return cm
        except Exception:
            continue
    if name == "gray":
        return pg.ColorMap([0, 1], [(0, 0, 0), (255, 255, 255)])
    return pg.colormap.get("viridis")


def available():
    out = []
    for n in NAMES:
        try:
            get(n)
            out.append(n)
        except Exception:
            pass
    return out


def colors(name: str, n: int):
    cm = get(name)
    return [cm.map(x, mode="qcolor") for x in np.linspace(0.05, 0.95, max(n, 1))]
