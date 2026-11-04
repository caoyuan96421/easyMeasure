"""Keep pyqtgraph axis fonts in step with the application font size."""

from __future__ import annotations

import weakref

import pyqtgraph as pg
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

_items = weakref.WeakSet()


def _pt() -> float:
    app = QApplication.instance()
    if app is None:
        return 9.0
    p = app.font().pointSizeF()
    return p if p > 0 else 9.0


def style_axes(item) -> None:
    """Apply the current font size to a PlotItem / HistogramLUTItem and remember it."""
    _items.add(item)
    _apply(item, _pt())


def _apply(item, pt: float) -> None:
    f = QFont()
    f.setPointSizeF(pt)
    axes = []
    if isinstance(item, pg.PlotItem):
        axes = [item.getAxis(n) for n in ("left", "bottom", "right", "top")]
    elif hasattr(item, "axis"):
        axes = [item.axis]
    for ax in axes:
        try:
            ax.setStyle(tickFont=f)
            ax.labelStyle = dict(ax.labelStyle or {}, **{"font-size": "%gpt" % pt})
            ax._updateLabel() if hasattr(ax, "_updateLabel") else None
            ax.label.setFont(f)
        except Exception:
            pass


def restyle_all() -> None:
    pt = _pt()
    for it in list(_items):
        try:
            _apply(it, pt)
        except Exception:
            pass
