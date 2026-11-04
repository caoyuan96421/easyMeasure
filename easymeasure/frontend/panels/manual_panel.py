"""Manual panel: set any aliased / composite channel directly.

Each channel row has a spin box, a slider (when the alias has both a minimum and
a maximum) and a context menu on the spin box (right click) to enter a math
expression of the present value ``x`` (e.g. ``x + 0.01``, ``x/2``, ``0.1*pi``).

Writes go through the backend's protected path (limits, ramp rates).  While a
channel is still ramping, newer requests for it are coalesced: only the latest
value is sent once the previous write has finished.  Mouse-wheel changes are
ignored unless the widget has keyboard focus, so scrolling the panel can never
move an instrument by accident.
"""

from __future__ import annotations

import math
from typing import Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QMenu, QScrollArea, QSlider,
                               QToolButton, QVBoxLayout, QWidget)

from ...common.exprs import Expression
from ..widgets import shrinkable_label

SLIDER_STEPS = 1000
BIG = 1e12


class _NoWheelMixin:
    def wheelEvent(self, ev):                       # noqa: N802 - Qt API
        if self.hasFocus():
            super().wheelEvent(ev)
        else:
            ev.ignore()                              # let the panel scroll instead


class SafeSpinBox(_NoWheelMixin, QDoubleSpinBox):
    expressionRequested = Signal()
    readRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setKeyboardTracking(False)              # send on Enter / arrows, not per digit
        # right click is handled here (not by the embedded line edit)
        self.lineEdit().setContextMenuPolicy(Qt.NoContextMenu)

    def contextMenuEvent(self, ev):                  # noqa: N802
        menu = QMenu(self)
        menu.addAction("Set from expression…", self.expressionRequested.emit)
        menu.addAction("Read back from instrument", self.readRequested.emit)
        menu.addSeparator()
        menu.addAction("Copy value", lambda: QGuiApplication.clipboard().setText(self.cleanText()))
        menu.exec(ev.globalPos())


class SafeSlider(_NoWheelMixin, QSlider):
    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setFocusPolicy(Qt.StrongFocus)


class SafeCombo(_NoWheelMixin, QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)


def _decimals_for(lo: Optional[float], hi: Optional[float]) -> int:
    if lo is None or hi is None or not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        return 6
    span = hi - lo
    return int(min(9, max(2, 4 - math.floor(math.log10(span)))))


class ChannelRow:
    """Widgets of one channel inside the panel's grid."""

    def __init__(self, panel: "ManualPanel", meta: dict, kind: str):
        self.panel = panel
        self.name = meta["name"]
        self.kind = kind
        self.meta = meta
        self.lo = meta.get("min")
        self.hi = meta.get("max")
        unit = meta.get("unit") or ""
        self.label = QLabel(self.name)
        fm = self.label.fontMetrics()
        self.label.setMinimumWidth(min(fm.horizontalAdvance(self.name) + 6, fm.averageCharWidth() * 14))
        self.label.setToolTip("%s%s%s" % (self.name, (" → %s" % meta["target"]) if meta.get("target") else "",
                                          ("\n" + meta["description"]) if meta.get("description") else ""))
        self.slider: Optional[SafeSlider] = None
        self.editor = None
        if meta.get("dtype") == "str":
            if meta.get("allowed"):
                cb = SafeCombo()
                cb.addItems(meta["allowed"])
                cb.activated.connect(lambda _i: self.panel.request_set(self.name, cb.currentText()))
                self.editor = cb
            else:
                le = QLineEdit()
                le.editingFinished.connect(lambda: le.isModified() and
                                           self.panel.request_set(self.name, le.text()))
                self.editor = le
        else:
            sp = SafeSpinBox()
            lo = self.lo if self.lo is not None else -BIG
            hi = self.hi if self.hi is not None else BIG
            sp.setDecimals(_decimals_for(self.lo, self.hi))
            sp.setRange(lo, hi)
            sp.setStepType(QDoubleSpinBox.AdaptiveDecimalStepType)
            if unit:
                sp.setSuffix(" " + unit)
            sp.setMinimumWidth(90)
            sp.valueChanged.connect(self._spin_changed)
            sp.expressionRequested.connect(self._expression)
            sp.readRequested.connect(lambda: self.panel.read_back(self.name))
            sp.setToolTip("Enter or arrows: set (ramped)\nright click: expression / read back")
            self.editor = sp
            if self.lo is not None and self.hi is not None and self.hi > self.lo:
                sl = SafeSlider()
                sl.setRange(0, SLIDER_STEPS)
                sl.setMinimumWidth(60)
                sl.valueChanged.connect(self._slider_changed)
                sl.sliderReleased.connect(self._slider_released)
                sl.setToolTip("%g … %g %s (applied when released)" % (self.lo, self.hi, unit))
                self.slider = sl
        self.read_btn = QToolButton()
        self.read_btn.setText("↻")
        self.read_btn.setToolTip("read the present value from the instrument")
        self.read_btn.clicked.connect(lambda: self.panel.read_back(self.name))
        self._updating = False

    # ---- value display --------------------------------------------------------
    def show_value(self, v) -> None:
        if v is None:
            return
        self._updating = True
        try:
            if isinstance(self.editor, QDoubleSpinBox):
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    return
                if not math.isfinite(fv):
                    return
                if not self.editor.hasFocus():
                    self.editor.setValue(fv)
                if self.slider is not None and not self.slider.isSliderDown():
                    self.slider.setValue(self._to_slider(fv))
            elif isinstance(self.editor, QComboBox):
                if not self.editor.view().isVisible():
                    i = self.editor.findText(str(v))
                    if i >= 0:
                        self.editor.setCurrentIndex(i)
            elif isinstance(self.editor, QLineEdit) and not self.editor.hasFocus():
                self.editor.setText(str(v))
                self.editor.setModified(False)
        finally:
            self._updating = False

    def set_state(self, state: str, tip: str = "") -> None:
        color = {"busy": "#e39a1b", "error": "#d33c3c", "ok": ""}.get(state, "")
        self.label.setStyleSheet("color: %s" % color if color else "")
        base = self.label.toolTip().split("\n⚠")[0]
        self.label.setToolTip(base + ("\n⚠ " + tip if tip else ""))

    # ---- slider mapping -------------------------------------------------------------
    def _to_slider(self, v: float) -> int:
        f = (v - self.lo) / (self.hi - self.lo)
        return int(round(min(1.0, max(0.0, f)) * SLIDER_STEPS))

    def _from_slider(self, i: int) -> float:
        return self.lo + (self.hi - self.lo) * i / SLIDER_STEPS

    # ---- user input --------------------------------------------------------------------
    def _spin_changed(self, v: float) -> None:
        if self._updating:
            return
        if self.slider is not None:
            self.slider.blockSignals(True)
            self.slider.setValue(self._to_slider(v))
            self.slider.blockSignals(False)
        self.panel.request_set(self.name, v)

    def _slider_changed(self, i: int) -> None:
        if self._updating:
            return
        v = self._from_slider(i)
        self.editor.blockSignals(True)
        self.editor.setValue(v)                      # preview while dragging
        self.editor.blockSignals(False)
        if not self.slider.isSliderDown():           # click on the track / keyboard
            self.panel.request_set(self.name, v)

    def _slider_released(self) -> None:
        self.panel.request_set(self.name, self._from_slider(self.slider.value()))

    def _expression(self) -> None:
        cur = self.editor.value()
        txt, ok = QInputDialog.getText(
            self.panel, "Set %s" % self.name,
            "New value as an expression of the present value x = %g\n"
            "(e.g.  x + 0.01,   x/2,   0.1*pi,   sqrt(2)/10):" % cur,
            text=self.panel.last_expression.get(self.name, "x"))
        if not ok or not txt.strip():
            return
        try:
            v = float(Expression(txt, allowed_names=["x"]).evaluate({"x": cur}))
        except Exception as exc:
            self.panel.message("✘ %s: %s" % (self.name, exc))
            return
        if not math.isfinite(v):
            self.panel.message("✘ %s: expression gave %r" % (self.name, v))
            return
        self.panel.last_expression[self.name] = txt
        self.panel.request_set(self.name, v)


class ManualPanel(QWidget):
    def __init__(self, conn, model, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.model = model
        self.rows: Dict[str, ChannelRow] = {}
        self._signature = None
        self._pending: Dict[str, bool] = {}
        self._queued: Dict[str, object] = {}
        self.last_expression: Dict[str, str] = {}
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter channels")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        self.ramp = QCheckBox("ramp")
        self.ramp.setChecked(True)
        self.ramp.setToolTip("respect the alias ramp rates (recommended)")
        top = QHBoxLayout()
        top.addWidget(self.filter, 1)
        top.addWidget(self.ramp)
        self.host = QWidget()
        self.grid = QGridLayout(self.host)
        self.grid.setContentsMargins(2, 2, 2, 2)
        self.grid.setHorizontalSpacing(4)
        self.grid.setVerticalSpacing(2)
        self.grid.setColumnStretch(2, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.host)
        self.status = shrinkable_label(QLabel(""))
        self.status.setStyleSheet("color: gray")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addLayout(top)
        lay.addWidget(scroll, 1)
        lay.addWidget(self.status)
        model.stateChanged.connect(self._state)

    # ---- building ---------------------------------------------------------------------
    def _channels(self):
        out = []
        for a in self.model.aliases():
            # scalar numbers and text only: vector/tensor channels (waveforms ...)
            # cannot be set from a spin box
            if a.get("writable") and (a.get("dtype") == "str" or not a.get("shape")):
                out.append((a, "alias"))
        for c in self.model.composites():
            out.append((dict(c, dtype="float"), "composite"))
        return out

    def _state(self, st: dict) -> None:
        chans = self._channels()
        sig = tuple((m["name"], m.get("dtype"), m.get("min"), m.get("max"), m.get("unit"),
                     tuple(m.get("allowed") or ()), kind) for m, kind in chans)
        if sig != self._signature:
            self._signature = sig
            self._rebuild(chans)
        for m, _ in chans:
            row = self.rows.get(m["name"])
            if row is not None and not self._pending.get(m["name"]):
                row.show_value(m.get("last"))

    def _rebuild(self, chans) -> None:
        while self.grid.count():
            it = self.grid.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.rows.clear()
        for r, (meta, kind) in enumerate(chans):
            row = ChannelRow(self, meta, kind)
            self.rows[meta["name"]] = row
            self.grid.addWidget(row.label, r, 0)
            self.grid.addWidget(row.editor, r, 1)
            if row.slider is not None:
                self.grid.addWidget(row.slider, r, 2)
            self.grid.addWidget(row.read_btn, r, 3)
        self.grid.setRowStretch(len(chans), 1)
        self._apply_filter(self.filter.text())
        for meta, _ in chans:
            if meta.get("last") is None:
                self.read_back(meta["name"])      # show the real present values
        if not chans:
            self.message("no writable aliases or composites defined")
        else:
            self.message("")

    def _apply_filter(self, text: str) -> None:
        t = text.strip().lower()
        for name, row in self.rows.items():
            vis = not t or t in name.lower()
            for w in (row.label, row.editor, row.slider, row.read_btn):
                if w is not None:
                    w.setVisible(vis)

    def message(self, text: str) -> None:
        self.status.setText(text)

    # ---- backend ------------------------------------------------------------------------
    def request_set(self, name: str, value) -> None:
        """Send a write; while one is in flight for this channel keep only the latest."""
        if self._pending.get(name):
            self._queued[name] = value
            return
        self._pending[name] = True
        row = self.rows.get(name)
        if row is not None:
            row.set_state("busy")
        self.message("setting %s → %s …" % (name, value))

        def done(result, ok=True, err=""):
            self._pending[name] = False
            r = self.rows.get(name)
            if r is not None:
                r.set_state("ok" if ok else "error", err)
                if ok:
                    r.show_value(result)
            self.message(("%s = %s" % (name, result)) if ok else ("✘ %s: %s" % (name, err)))
            if name in self._queued:
                nxt = self._queued.pop(name)
                self.request_set(name, nxt)
            elif not ok:
                self.read_back(name)              # show what the instrument really has
            else:
                self.model.schedule_refresh()

        self.conn.request("write_channel", {"name": name, "value": value,
                                            "ramp": self.ramp.isChecked()},
                          on_ok=lambda v: done(v), on_err=lambda e: done(None, False, e),
                          timeout=3600)

    def read_back(self, name: str) -> None:
        def got(v):
            row = self.rows.get(name)
            if row is not None and not self._pending.get(name):
                row.show_value(v)
        self.conn.request("read_channel", {"name": name}, on_ok=got,
                          on_err=lambda e: self.message("✘ read %s: %s" % (name, e)))
