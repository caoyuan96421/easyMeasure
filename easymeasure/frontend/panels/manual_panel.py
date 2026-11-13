"""Manual panel: set any aliased / composite channel and instrument setting directly.

Instrument settings (configuration channels such as a mode, range or time
constant) are listed below the aliases when "settings" is ticked.  Channels with
a list of allowed values get a combo box instead of a text field / spin box.

Each channel row has a spin box, a slider (when the alias has both a minimum and
a maximum) and a context menu on the spin box (right click) to enter a math
expression of the present value ``x`` (e.g. ``x + 0.01``, ``x/2``, ``0.1*pi``).

Variables of a multivariate composite channel are grouped (indented) under the
composite's name.  A group has "hold", "Set all" and "Set all to zero": while
hold is ticked, edits only mark the variables and "Set all" sends them in one go
(the hardware channels are computed once from all of them).  "Set all to zero"
sets every variable to 0 together, the usual way to start.

Until a group's variables have been set for the first time they have no value:
they show NaN, their sliders are greyed out and hold is forced on.  The group is
started with "Set all to zero", or by typing a value for every variable and
clicking "Set all" (enabled once none shows NaN).

The setup's **parameters** are listed in their own group: a value parameter can be
edited (it is written like any channel; it moves no instrument), a formula
parameter shows its value.  The **user actions** are buttons at the top: ▶ runs one
(writes it), and while it runs its button aborts it.

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
from PySide6.QtWidgets import (QSizePolicy, QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QMenu, QScrollArea, QSlider,
                               QMessageBox, QToolButton, QVBoxLayout, QWidget)

from ...common.exprs import Expression
from ..widgets import FlowLayout, shrinkable_label

SLIDER_STEPS = 1000
GROUP_INDENT = 18      # px: variables under the header of their composite


def _finite(v) -> bool:
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _parse(text: str, numeric: bool):
    """Value typed into a text field: a number for numeric channels if it parses."""
    t = text.strip()
    if numeric:
        try:
            return float(t)
        except ValueError:
            return t                       # let the backend explain what is wrong
    return t
BIG = 1e12


class _NoWheelMixin:
    def wheelEvent(self, ev):                       # noqa: N802 - Qt API
        if self.hasFocus():
            super().wheelEvent(ev)
        else:
            ev.ignore()                              # let the panel scroll instead


class SafeSpinBox(_NoWheelMixin, QDoubleSpinBox):
    """A spin box that never sends on a scroll, and can show "NaN" (no value yet)."""
    expressionRequested = Signal()
    readRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setKeyboardTracking(False)              # send on Enter / arrows, not per digit
        # right click is handled here (not by the embedded line edit)
        self.lineEdit().setContextMenuPolicy(Qt.NoContextMenu)
        self._nan = False
        # a number was entered over "NaN": it must be sent even if it equals the
        # hidden value of the box (valueChanged would not fire then)
        self.entered_over_nan = False

    # ---- "no value yet" -----------------------------------------------------------------
    def is_nan(self) -> bool:
        return self._nan

    def set_nan(self, on: bool) -> None:
        on = bool(on)
        if on == self._nan:
            return
        self._nan = on
        self.blockSignals(True)
        if on:                                     # hidden value: 0, or the nearest limit
            self.setValue(min(self.maximum(), max(self.minimum(), 0.0)))
        self.setValue(self.value())                # redraw the text
        self.lineEdit().setText(self.prefix() + self.textFromValue(self.value()) + self.suffix())
        self.blockSignals(False)

    def textFromValue(self, v: float) -> str:     # noqa: N802 - Qt API
        return "NaN" if self._nan else super().textFromValue(v)

    def valueFromText(self, text: str) -> float:  # noqa: N802 - Qt API
        v = super().valueFromText(text)
        if self._nan:
            self._nan = False
            self.entered_over_nan = True
        return v

    def stepBy(self, steps: int) -> None:         # noqa: N802 - Qt API
        if self._nan:
            self._nan = False
            self.entered_over_nan = True
        super().stepBy(steps)

    def _select_if_nan(self) -> None:
        if self._nan:
            self.selectAll()                       # typing replaces "NaN"

    def focusInEvent(self, ev):                    # noqa: N802 - Qt API
        super().focusInEvent(ev)
        self._select_if_nan()

    def mouseReleaseEvent(self, ev):               # noqa: N802 - Qt API
        super().mouseReleaseEvent(ev)
        self._select_if_nan()

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
        unit = meta.get("display_unit") or meta.get("unit") or ""
        choices = meta.get("choices") or meta.get("allowed")
        self.numeric_choices = bool(choices) and meta.get("dtype") != "str"
        self.label = QLabel(self.name)
        fm = self.label.fontMetrics()
        self.label.setMinimumWidth(min(fm.horizontalAdvance(self.name) + 6, fm.averageCharWidth() * 14))
        self.label.setToolTip("%s%s%s%s" % (
            self.name, (" → %s" % meta["target"]) if meta.get("target") else "",
            (" [%s]" % unit) if unit and kind == "config" else "",
            ("\n" + meta["description"]) if meta.get("description") else ""))
        self._base_style = "font-style: italic" if kind == "config" else ""
        self.label.setStyleSheet(self._base_style)
        self.slider: Optional[SafeSlider] = None
        self.editor = None
        if choices:
            cb = SafeCombo()
            for c in choices:
                cb.addItem(("%g" % c + (" " + unit if unit else "")) if self.numeric_choices else str(c), c)
            cb.setCurrentIndex(-1)
            cb.activated.connect(lambda i: self.panel.request_set(self.name, cb.itemData(i)))
            self.editor = cb
        elif meta.get("dtype") == "str" or kind == "config":
            # text, or a numeric setting without limits (frequency, NPLC ...): a line
            # edit takes any magnitude, a spin box would need a range
            le = QLineEdit()
            numeric = meta.get("dtype") != "str"
            if numeric and unit:
                le.setPlaceholderText(unit)
            le.editingFinished.connect(lambda: le.isModified() and
                                       self.panel.request_set(self.name, _parse(le.text(), numeric)))
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
            sp.editingFinished.connect(self._editing_finished)
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
        self.backend_set = _finite(meta.get("last"))       # refreshed from the state
        self.unset = False

    def set_unset(self, on: bool) -> None:
        """Show "no value yet" (NaN, slider greyed out), or end it."""
        on = bool(on)
        if on == self.unset or not isinstance(self.editor, SafeSpinBox):
            return
        self.unset = on
        self._updating = True
        try:
            self.editor.set_nan(on)
        finally:
            self._updating = False
        if self.slider is not None:
            self.slider.setEnabled(not on)
            if on:
                self.slider.blockSignals(True)
                self.slider.setValue(self._to_slider(self.editor.value()))
                self.slider.blockSignals(False)

    # ---- value display --------------------------------------------------------
    def user_is_typing(self) -> bool:
        """True while the user has typed into the spin box but not committed yet."""
        return isinstance(self.editor, QDoubleSpinBox) and self.editor.hasFocus() and \
            self.editor.lineEdit().isModified()

    def show_value(self, v, force: bool = False) -> None:
        """Display *v*.  Background refreshes (force=False) leave a focused spin box
        alone; values the user just set (force=True) are always shown, unless the
        user is in the middle of typing a new number."""
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
                if (force and not self.user_is_typing()) or not self.editor.hasFocus():
                    self.editor.set_nan(False)
                    self.unset = False
                    if self.slider is not None:
                        self.slider.setEnabled(True)
                    self.editor.setValue(fv)
                    self.editor.lineEdit().setModified(False)
                if self.slider is not None and not self.slider.isSliderDown():
                    self.slider.setValue(self._to_slider(fv))
            elif isinstance(self.editor, QComboBox):
                if not self.editor.view().isVisible():
                    self.editor.setCurrentIndex(self._choice_index(v))
            elif isinstance(self.editor, QLineEdit) and not self.editor.hasFocus():
                self.editor.setText(("%.10g" % v) if isinstance(v, float) else str(v))
                self.editor.setModified(False)
        finally:
            self._updating = False

    def _choice_index(self, v) -> int:
        cb = self.editor
        for i in range(cb.count()):
            d = cb.itemData(i)
            if self.numeric_choices:
                try:
                    if abs(float(v) - d) <= 1e-9 * max(abs(d), 1e-300):
                        return i
                except (TypeError, ValueError):
                    return -1
            elif str(d).casefold() == str(v).casefold():
                return i
        return -1

    def set_state(self, state: str, tip: str = "") -> None:
        color = {"busy": "#e39a1b", "error": "#d33c3c", "held": "#2f7fd0", "ok": ""}.get(state, "")
        self.label.setStyleSheet("; ".join(x for x in (self._base_style,
                                                       "color: %s" % color if color else "") if x))
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
        self.editor.entered_over_nan = False
        if self.unset or self.editor.is_nan():      # a value was entered over "NaN"
            self.set_unset(False)
            self.editor.set_nan(False)
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

    def _editing_finished(self) -> None:
        sp = self.editor
        if sp.entered_over_nan:                     # the number typed equals the hidden value
            self._spin_changed(sp.value())

    def _slider_released(self) -> None:
        self.panel.request_set(self.name, self._from_slider(self.slider.value()))

    def _expression(self) -> None:
        cur = float("nan") if self.unset else self.editor.value()
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
        self.show_value(v, force=True)               # show the requested value at once
        self.panel.request_set(self.name, v)


class ManualPanel(QWidget):
    def __init__(self, conn, model, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.model = model
        self.rows: Dict[str, ChannelRow] = {}
        self._signature = None
        self.groups: Dict[str, dict] = {}       # multivariate composite -> header widgets
        self.params: Dict[str, dict] = {}       # parameter -> {label, editor, meta}
        self.param_header = None
        self._held: Dict[str, Dict[str, float]] = {}
        self._pending: Dict[str, bool] = {}
        self._queued: Dict[str, object] = {}
        # read-backs wait in line per instrument: a rebuild asks for every value at
        # once, and an instrument serves one request at a time anyway
        self._rb_wait: Dict[str, list] = {}
        self._rb_busy: Dict[str, bool] = {}
        self.last_expression: Dict[str, str] = {}
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter channels")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        self.ramp = QCheckBox("ramp")
        self.ramp.setChecked(True)
        self.ramp.setToolTip("respect the alias ramp rates (recommended)")
        self.settings = QCheckBox("settings")
        self.settings.setChecked(True)
        self.settings.setToolTip("also list the instrument settings (configuration channels: "
                                 "modes, ranges, time constants ...)")
        self.settings.toggled.connect(lambda _: self._state(self.model.state))
        self.stop_ramps = QToolButton()
        self.stop_ramps.setText("■ Stop ramps")
        self.stop_ramps.setEnabled(False)          # lit only while a ramp is running
        self.stop_ramps.setToolTip("no ramp is running")
        self.stop_ramps.clicked.connect(self._stop_ramps)
        top = QHBoxLayout()
        top.addWidget(self.filter, 1)
        top.addWidget(self.settings)
        top.addWidget(self.ramp)
        # own row, full width: prominent when lit, and never makes the panel wider
        self.stop_ramps.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
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
        # user actions: one button each (hidden while there are none)
        self.actions_box = QWidget()
        self.actions_flow = FlowLayout(self.actions_box)
        self.actions_flow.setContentsMargins(0, 0, 0, 0)
        self.actions_box.setVisible(False)
        self.action_buttons: Dict[str, QToolButton] = {}
        self._actions_sig = None
        lay.addLayout(top)
        lay.addWidget(self.stop_ramps)
        lay.addWidget(self.actions_box)
        lay.addWidget(scroll, 1)
        lay.addWidget(self.status)
        model.stateChanged.connect(self._state)
        model.rampsChanged.connect(self.show_ramps)

    # ---- ramps ------------------------------------------------------------------------
    def show_ramps(self, active: list) -> None:
        """Light "Stop ramps" while ramps of direct writes run (from any window)."""
        names = sorted({c for r in active or [] for c in r.get("channels", [])})
        self.stop_ramps.setEnabled(bool(names))
        if names:
            self.stop_ramps.setText("■ Stop ramps (%s)" % ", ".join(names) if len(names) <= 3
                                    else "■ Stop ramps (%d)" % len(names))
            self.stop_ramps.setToolTip("stop the ramps of %s where they are now"
                                       % ", ".join(names))
            self.stop_ramps.setStyleSheet("QToolButton { background: #c0392b; color: white; "
                                          "font-weight: bold; padding: 1px 6px; border-radius: 3px }")
        else:
            self.stop_ramps.setText("■ Stop ramps")
            self.stop_ramps.setToolTip("no ramp is running")
            self.stop_ramps.setStyleSheet("")

    def _stop_ramps(self) -> None:
        self.conn.request("stop_ramps", {},
                          on_ok=lambda names: self.message("stopped: %s" % ", ".join(names or [])
                                                           if names else "no ramp was running"),
                          on_err=lambda e: self.message("✘ stop ramps: %s" % e))

    # ---- building ---------------------------------------------------------------------
    def _channels(self):
        out = []
        for a in self.model.aliases():
            # scalar numbers and text only: vector/tensor channels (waveforms ...)
            # cannot be set from a spin box
            if a.get("writable") and (a.get("dtype") == "str" or not a.get("shape")):
                out.append((a, "alias"))
        for v in self.model.virtual_channels():
            if v.get("writable"):
                out.append((v, v.get("kind", "variable")))
        if self.settings.isChecked():
            for i in self.model.instruments():
                for c in i.get("channels", []):
                    if c.get("config") and "w" in c.get("access", "rw") and \
                            (c.get("dtype") == "str" or not c.get("shape")):
                        out.append((dict(c, name="%s.%s" % (i["name"], c["name"]),
                                         choices=c.get("allowed")), "config"))
        return out

    # ---- user actions -------------------------------------------------------------------
    def _show_actions(self) -> None:
        acts = self.model.user_actions()
        sig = tuple((a["name"], a.get("description", ""), bool(a.get("confirm"))) for a in acts)
        if sig != self._actions_sig:
            self._actions_sig = sig
            while self.actions_flow.count():
                it = self.actions_flow.takeAt(0)
                w = it.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
            self.action_buttons.clear()
            if acts:
                lab = QLabel("<b>Actions</b>")
                self.actions_flow.addWidget(lab)
            for a in acts:
                b = QToolButton()
                b.clicked.connect(lambda _=False, n=a["name"]: self._action_clicked(n))
                self.action_buttons[a["name"]] = b
                self.actions_flow.addWidget(b)
            self.actions_box.setVisible(bool(acts))
        for a in acts:
            b = self.action_buttons.get(a["name"])
            if b is None:
                continue
            running = bool(a.get("running"))
            b.setText(("■ " if running else "▶ ") + a["name"])
            b.setProperty("running", running)
            b.setStyleSheet("QToolButton { background: #e39a1b; color: white; }" if running
                            else "")
            tip = a.get("description") or a["name"]
            last = a.get("last") or {}
            if running:
                tip = "running - click to abort\n" + tip
            elif last.get("status") == "error":
                tip += "\nlast run failed: %s" % last.get("error")
            b.setToolTip(tip)

    def _action_clicked(self, name: str) -> None:
        a = next((x for x in self.model.user_actions() if x["name"] == name), None)
        if a is None:
            return
        if a.get("running"):
            self.conn.request("abort_user_action", {"name": name},
                              on_ok=lambda r: self.message("aborting %s" % name),
                              on_err=lambda e: self.message("✘ abort %s: %s" % (name, e)))
            return
        if a.get("confirm") and QMessageBox.question(
                self, "Run action", "Run %s?%s" % (name, ("\n\n" + a["description"])
                                                   if a.get("description") else "")) \
                != QMessageBox.Yes:
            return
        self.message("running %s…" % name)

        def done(r):
            st = r.get("status") if isinstance(r, dict) else "finished"
            if st == "finished":
                self.message("✔ %s done (%.3g s)" % (name, r.get("elapsed") or 0))
            else:
                self.message("✘ %s %s%s" % (name, st, (": " + r["error"]) if r.get("error") else ""))
            self.model.schedule_refresh()

        def failed(e):
            self.message("✘ %s: %s" % (name, e))
            self.model.schedule_refresh()
        self.conn.request("write_channel", {"name": name}, on_ok=done, on_err=failed,
                          timeout=7 * 24 * 3600)
        self.model.schedule_refresh()

    # ---- parameters ---------------------------------------------------------------------
    def _param_rows(self, r: int) -> int:
        ps = self.model.parameters()
        self.params.clear()
        if not ps:
            return r
        lab = QLabel("<b>Parameters</b>")
        lab.setToolTip("constants of the setup, used by composite channels and actions; "
                       "setting one moves no instrument")
        self.grid.addWidget(lab, r, 0, 1, 4)
        self.param_header = lab
        r += 1
        for p in ps:
            name_l = QLabel(p["name"] + (" (%s)" % p["unit"] if p.get("unit") else ""))
            name_l.setIndent(GROUP_INDENT)
            name_l.setToolTip(p.get("description") or p["name"])
            if p.get("formula"):
                ed = QLabel()
                ed.setToolTip("computed: %s = %s" % (p["name"], p["formula"]))
                ed.setStyleSheet("color: #888")
            else:
                ed = QLineEdit()
                ed.setToolTip("type a value and press Enter (moves no instrument)")
                ed.returnPressed.connect(lambda n=p["name"], e=ed: self.set_parameter(n, e.text()))
            self.grid.addWidget(name_l, r, 0)
            self.grid.addWidget(ed, r, 1, 1, 3)
            self.params[p["name"]] = {"label": name_l, "editor": ed, "meta": p}
            r += 1
        self._show_param_values(ps)
        return r

    def _show_param_values(self, ps=None) -> None:
        for p in ps if ps is not None else self.model.parameters():
            row = self.params.get(p["name"])
            if row is None:
                continue
            ed, v = row["editor"], p.get("current")
            text = "" if v is None else "%.6g" % v
            if isinstance(ed, QLabel):
                ed.setText("%s  = %s" % (text, p["formula"]))
            elif not ed.hasFocus():
                ed.setText(text)

    def set_parameter(self, name: str, text: str) -> None:
        try:
            value = float(Expression(str(text)).evaluate({}))
        except Exception:
            self.message("✘ %s: %r is not a number" % (name, text))
            return
        self.message("%s → %g" % (name, value))
        self.conn.request("write_channel", {"name": name, "value": value},
                          on_ok=lambda v: (self.message("%s = %g" % (name, v)),
                                           self.model.schedule_refresh()),
                          on_err=lambda e: (self.message("✘ %s: %s" % (name, e)),
                                            self._show_param_values()))

    def _state(self, st: dict) -> None:
        self._show_actions()
        chans = self._channels()
        sig = tuple((m["name"], m.get("composite"), m.get("dtype"), m.get("min"), m.get("max"),
                     m.get("display_unit") or m.get("unit"),
                     tuple(m.get("choices") or m.get("allowed") or ()), kind) for m, kind in chans)
        sig += tuple(("param", p["name"], p.get("unit", ""), p.get("formula") or "",
                      p.get("description", "")) for p in self.model.parameters())
        if sig != self._signature:
            self._signature = sig
            self._rebuild(chans)
        for m, _ in chans:
            row = self.rows.get(m["name"])
            if row is not None:
                row.backend_set = _finite(m.get("last"))
            if row is not None and not self._pending.get(m["name"]):
                row.show_value(m.get("last"))
        self._update_groups()
        self._show_param_values()

    def _rebuild(self, chans) -> None:
        while self.grid.count():
            it = self.grid.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.rows.clear()
        self.groups.clear()
        multi = {g["name"]: g["variables"] for g in self.model.composite_groups()}
        r = 0
        for meta, kind in chans:
            comp = meta.get("composite") if kind == "variable" else None
            if comp in multi and comp not in self.groups:
                self._group_header(comp, multi[comp], r)
                r += 1
            row = ChannelRow(self, meta, kind)
            self.rows[meta["name"]] = row
            if comp in multi:
                row.label.setIndent(GROUP_INDENT)      # under its group's header
            self.grid.addWidget(row.label, r, 0)
            self.grid.addWidget(row.editor, r, 1)
            if row.slider is not None:
                self.grid.addWidget(row.slider, r, 2)
            self.grid.addWidget(row.read_btn, r, 3)
            r += 1
        self._held = {g: v for g, v in self._held.items() if g in self.groups}
        self._update_groups()
        self.param_header = None
        r = self._param_rows(r)
        self.grid.setRowStretch(r, 1)
        self._apply_filter(self.filter.text())
        for meta, kind in chans:
            if meta.get("last") is None and not (kind == "variable" and
                                                 meta.get("composite") in self.groups):
                self.read_back(meta["name"])      # show the real present values
        if not chans and not self.params:
            self.message("no writable aliases, composites or instrument settings")
        else:
            self.message("")

    def _group_header(self, comp: str, variables, r: int) -> None:
        lab = QLabel("<b>%s</b>  (%s)" % (comp, ", ".join(v.split(".", 1)[-1] for v in variables)))
        lab.setToolTip("variables of the multivariate composite %s" % comp)
        hold = QCheckBox("hold")
        hold.setToolTip("collect edits of these variables and send them with Set all")
        btn = QToolButton()
        btn.setText("Set all")
        btn.setToolTip("set every variable of %s together to the values shown" % comp)
        btn.clicked.connect(lambda: self.set_group(comp))
        zero = QToolButton()
        zero.setText("Set all to zero")
        zero.setToolTip("set every variable of %s to 0 together (limits and ramps apply)" % comp)
        zero.clicked.connect(lambda: self.set_group(comp, zero=True))
        hold.toggled.connect(lambda on: None if on else self._held.pop(comp, None))
        note = QLabel("not set yet")
        note.setStyleSheet("color: #c0392b")
        note.setVisible(False)
        head = QWidget()                           # name, and "not set yet" below it
        vl = QVBoxLayout(head)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)
        vl.addWidget(lab)
        vl.addWidget(note)
        box = QWidget()                            # the controls wrap in a narrow panel
        hl = FlowLayout(box, hspacing=4, vspacing=2)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(hold)
        hl.addWidget(btn)
        hl.addWidget(zero)
        self.grid.addWidget(head, r, 0)
        self.grid.addWidget(box, r, 1, 1, 3)
        self.groups[comp] = {"label": head, "title": lab, "box": box, "hold": hold, "set": btn,
                             "zero": zero, "note": note, "variables": list(variables),
                             "forced": False}

    def _update_groups(self) -> None:
        """Variables never set show NaN (slider greyed out), and their group has hold
        forced on until Set all to zero, or Set all once every one has a value."""
        for comp, g in self.groups.items():
            held = self._held.get(comp, {})
            rows = {n: self.rows[n] for n in g["variables"] if n in self.rows}
            unset = [n for n, r in rows.items() if not r.backend_set]
            for n, r in rows.items():
                if not self._pending.get(n):
                    r.set_unset(not r.backend_set and n not in held)
            hold, setb = g["hold"], g["set"]
            if unset:
                if not g["forced"]:
                    g["forced"] = True
                    hold.blockSignals(True)
                    hold.setChecked(True)
                    hold.blockSignals(False)
                    hold.setEnabled(False)
                    hold.setToolTip("%s not set yet: hold stays on until all variables are set "
                                    "together (Set all, or Set all to zero)" % ", ".join(unset))
                missing = [n for n in unset if n not in held]
                setb.setEnabled(not missing)
                setb.setToolTip(("type a value for %s first (it shows NaN)" % ", ".join(missing))
                                if missing else "set %s together to the values shown"
                                % ", ".join(rows))
                g["note"].setVisible(True)
                g["note"].setToolTip("%s %s never been set: no value yet" % (
                    ", ".join(unset), "has" if len(unset) == 1 else "have"))
            else:
                if g["forced"]:
                    g["forced"] = False
                    hold.setEnabled(True)
                    hold.setChecked(False)
                    hold.setToolTip("collect edits of these variables and send them with Set all")
                setb.setEnabled(True)
                setb.setToolTip("set every variable of %s together to the values shown" % comp)
                g["note"].setVisible(False)

    def _group_of(self, name: str) -> Optional[str]:
        row = self.rows.get(name)
        comp = row.meta.get("composite") if row is not None and row.kind == "variable" else None
        return comp if comp in self.groups else None

    def set_group(self, comp: str, zero: bool = False) -> None:
        """Set all variables of a multivariate composite together (``set_many``): to
        the values shown, or with *zero* all to 0."""
        g = self.groups.get(comp)
        if g is None:
            return
        values = {}
        for n in g["variables"]:
            row = self.rows.get(n)
            if row is None or not isinstance(row.editor, QDoubleSpinBox):
                continue
            held = self._held.get(comp, {})
            if not zero and n not in held and row.unset:
                self.message("✘ %s has no value yet: type one, or use Set all to zero" % n)
                return
            values[n] = 0.0 if zero else held.get(n, row.editor.value())
        for n in values:
            self._pending[n] = True
            self.rows[n].set_state("busy")
        self.message("setting %s together …" % ", ".join(values))

        def done(ok=True, err=""):
            if ok:                                 # on failure the held values stay
                self._held.pop(comp, None)
            for n, v in values.items():
                self._pending[n] = False
                r = self.rows.get(n)
                if r is not None:
                    r.set_state("ok" if ok else
                                "held" if n in self._held.get(comp, {}) else "error", err)
                    if ok:
                        r.backend_set = True
                        r.show_value(v, force=True)
            self._update_groups()
            self.message(("%s set" % ", ".join("%s = %g" % kv for kv in values.items()))
                         if ok else "✘ %s: %s" % (comp, err))
            self.model.schedule_refresh()
        self.conn.request("set_many", {"values": values, "ramp": self.ramp.isChecked()},
                          on_ok=lambda _: done(), on_err=lambda e: done(False, e), timeout=3600)

    def _apply_filter(self, text: str) -> None:
        t = text.strip().lower()
        for name, row in self.rows.items():
            vis = not t or t in name.lower() or t in (row.meta.get("composite") or "").lower()
            for w in (row.label, row.editor, row.slider, row.read_btn):
                if w is not None:
                    w.setVisible(vis)
        for comp, g in self.groups.items():
            vis = not t or t in comp.lower() or any(t in v.lower() for v in g["variables"])
            g["label"].setVisible(vis)
            g["box"].setVisible(vis)
        anyp = False
        for name, prow in self.params.items():
            vis = not t or t in name.lower() or t in "parameters"
            anyp |= vis
            prow["label"].setVisible(vis)
            prow["editor"].setVisible(vis)
        if getattr(self, "param_header", None) is not None:
            self.param_header.setVisible(anyp)

    def message(self, text: str) -> None:
        self.status.setText(text)

    # ---- backend ------------------------------------------------------------------------
    def request_set(self, name: str, value) -> None:
        """Send a write; while one is in flight for this channel keep only the latest."""
        comp = self._group_of(name)
        if comp is not None and self.groups[comp]["hold"].isChecked():
            self._held.setdefault(comp, {})[name] = value
            self.rows[name].set_state("held", "held: sent with Set all")
            self.message("%s = %s held; Set all sends %s" % (
                name, value, ", ".join(self._held[comp])))
            self._update_groups()
            return
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
                    r.show_value(result, force=True)
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

    def _instrument_of(self, name: str) -> str:
        row = self.rows.get(name)
        target = (row.meta.get("target") if row is not None else None) or name
        return target.split(".", 1)[0] if "." in target else "@" + name

    def read_back(self, name: str) -> None:
        key = self._instrument_of(name)
        wait = self._rb_wait.setdefault(key, [])
        if name not in wait:
            wait.append(name)
        self._next_read_back(key)

    def _next_read_back(self, key: str) -> None:
        if self._rb_busy.get(key) or not self._rb_wait.get(key):
            return
        name = self._rb_wait[key].pop(0)
        self._rb_busy[key] = True

        def done(v=None, err=None):
            self._rb_busy[key] = False
            row = self.rows.get(name)
            if err is not None:
                self.message("✘ read %s: %s" % (name, err))
            elif row is not None and not self._pending.get(name):
                row.show_value(v)
            self._next_read_back(key)
        self.conn.request("read_channel", {"name": name}, on_ok=lambda v: done(v),
                          on_err=lambda e: done(err=e))
