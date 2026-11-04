"""Display size settings: font size (live) and overall UI scale (on restart).

* ``ui_font_pt`` (QSettings, 0 = system default) is applied immediately to all
  widgets and to the pyqtgraph axes.
* ``ui_scale`` (percent) sets ``QT_SCALE_FACTOR`` when the GUI starts, which scales
  everything (widgets, icons, plots) - useful on very high or very low resolution
  screens.  It must be known before the QApplication exists, hence a restart.
"""

from __future__ import annotations

import os
import weakref

from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

ORG, APP = "easyMeasure", "gui"
_default_pt = None
_scaled = weakref.WeakKeyDictionary()


def settings() -> QSettings:
    return QSettings(ORG, APP)


def ui_scale_percent() -> int:
    try:
        return int(settings().value("ui_scale", 100))
    except (TypeError, ValueError):
        return 100


def font_pt() -> float:
    try:
        return float(settings().value("ui_font_pt", 0))
    except (TypeError, ValueError):
        return 0.0


def prepare_environment() -> None:
    """Call BEFORE creating the QApplication."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    # honour fractional Windows scaling (125 %, 150 % ...) exactly
    try:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass
    pct = ui_scale_percent()
    if pct != 100 and "QT_SCALE_FACTOR" not in os.environ:
        os.environ["QT_SCALE_FACTOR"] = "%g" % (max(25, min(400, pct)) / 100.0)


def current_font_pt() -> float:
    app = QApplication.instance()
    if app is None:
        return 9.0
    f = app.font()
    return f.pointSizeF() if f.pointSizeF() > 0 else 9.0


def apply_font(pt: float) -> None:
    """Set the application font size (0 = system default) and restyle plots."""
    global _default_pt
    app = QApplication.instance()
    if app is None:
        return
    if _default_pt is None:
        _default_pt = app.font().pointSizeF() if app.font().pointSizeF() > 0 else 9.0
    f = QFont(app.font())
    f.setPointSizeF(pt if pt and pt > 0 else _default_pt)
    app.setFont(f)
    # widgets with an explicitly set font (monospace editors, big monitor values)
    # keep their family but follow the size, scaled by their registered factor
    for w in list(_scaled):
        try:
            g = QFont(w.font())
            g.setPointSizeF(f.pointSizeF() * _scaled[w])
            w.setFont(g)
        except Exception:
            pass
    from .plotting.style import restyle_all
    restyle_all()


def register_font(widget, factor: float = 1.0, monospace: bool = False) -> None:
    """Give *widget* a font that follows the UI font size (times *factor*)."""
    f = QFont(widget.font())
    if monospace:
        f = QFont("Consolas" if os.name == "nt" else "monospace")
        f.setStyleHint(QFont.Monospace)
    f.setPointSizeF(current_font_pt() * factor)
    widget.setFont(f)
    _scaled[widget] = factor


def apply_saved_font() -> None:
    pt = font_pt()
    if pt > 0:
        apply_font(pt)
