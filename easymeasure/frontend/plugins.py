"""Plugins in the desktop GUI: the Plugins menu, the dialog to manage them, and the
panels and data tabs of the running plugins.

A plugin describes its panels as data (:mod:`easymeasure.plugins.ui`); this module
draws them with Qt - seven-segment displays, LEDs, bar graphs and knobs are
painted here - applies the property changes the plugin sends (``plugin_ui``
events) and sends back what the user does (``plugin_event``).
"""

from __future__ import annotations

import json
import math
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QSplitter, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

COLORS = {"red": "#ff3b1f", "green": "#38e05a", "amber": "#ffb000", "blue": "#3aa0ff",
          "white": "#f2f2f2", "yellow": "#ffe14a"}


def qcolor(name: Optional[str], default: str = "red") -> QColor:
    return QColor(COLORS.get(name or default, name or COLORS[default]))


# ---------------------------------------------------------------------------
# seven segments
# ---------------------------------------------------------------------------
#: character -> lit segments (a top, b top right, c bottom right, d bottom,
#: e bottom left, f top left, g middle)
SEGMENTS = {
    "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc", "5": "afgcd",
    "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abcdfg", "-": "g", " ": "", "_": "d",
    "A": "abcefg", "a": "abcdeg", "b": "cdefg", "C": "adef", "c": "deg", "d": "bcdeg",
    "E": "adefg", "e": "abdefg", "F": "aefg", "H": "bcefg", "h": "cefg", "L": "def",
    "n": "ceg", "o": "cdeg", "O": "abcdef", "P": "abefg", "r": "eg", "t": "defg",
    "U": "bcdef", "u": "cde", "y": "bcdfg", "°": "abfg",
}


def seg_cells(text: str, digits: int):
    """``"-1.234"`` -> [(char, dot)] cells, right-aligned in *digits* cells."""
    cells = []
    for ch in str(text):
        if ch == "." and cells and not cells[-1][1]:
            cells[-1] = (cells[-1][0], True)
        elif ch == ".":
            cells.append((" ", True))
        else:
            cells.append((ch, False))
    cells = cells[-digits:]
    return [(" ", False)] * (digits - len(cells)) + cells


class Seg7(QWidget):
    """Seven-segment digits, painted (lit segments bright, the others faint)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.value, self.digits, self.color, self.h = "", 6, qcolor("red"), 32
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set_props(self, p: dict) -> None:
        self.value = str(p.get("value", self.value) or "")
        self.digits = int(p.get("digits", self.digits))
        if p.get("color"):
            self.color = qcolor(p["color"])
        self.h = int(p.get("size", self.h))
        self.setFixedSize(self.sizeHint())
        self.update()

    CELL = 0.7                            # digit cell width / height

    def sizeHint(self):
        w = int(self.digits * self.h * self.CELL + self.h * 0.3)
        return QSize(w, int(self.h * 1.2))

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#0b0b0b"))
        off = QColor(self.color)
        off.setAlpha(16)
        h = self.h
        cw = h * self.CELL
        x0 = self.width() - self.digits * cw - h * 0.05
        y0 = h * 0.1
        for i, (ch, dot) in enumerate(seg_cells(self.value, self.digits)):
            lit = SEGMENTS.get(ch, SEGMENTS.get(ch.upper(), "g"))
            self._digit(p, x0 + i * cw, y0, h, lit, dot, self.color, off)
        p.end()

    @staticmethod
    def _digit(p, x, y, h, lit, dot, on, off):
        w = h * 0.46                      # the digit's width
        t = h * 0.1                       # segment thickness
        sk = h * 0.08                     # slant
        half = h / 2

        def hseg(cx, cy):
            return QPolygonF([QPointF(cx + t / 2, cy), QPointF(cx + t, cy - t / 2),
                              QPointF(cx + w - t, cy - t / 2), QPointF(cx + w - t / 2, cy),
                              QPointF(cx + w - t, cy + t / 2), QPointF(cx + t, cy + t / 2)])

        def vseg(cx, cy, length):
            return QPolygonF([QPointF(cx, cy + t / 2), QPointF(cx + t / 2, cy + t),
                              QPointF(cx + t / 2, cy + length - t), QPointF(cx, cy + length - t / 2),
                              QPointF(cx - t / 2, cy + length - t), QPointF(cx - t / 2, cy + t)])

        shapes = {"a": hseg(x, y), "g": hseg(x, y + half), "d": hseg(x, y + h),
                  "f": vseg(x, y, half), "b": vseg(x + w, y, half),
                  "e": vseg(x, y + half, half), "c": vseg(x + w, y + half, half)}
        p.setPen(Qt.NoPen)
        for s, poly in shapes.items():
            # slant: shift by height
            poly = QPolygonF([QPointF(pt.x() + sk * (1 - (pt.y() - y) / h), pt.y())
                              for pt in poly])
            p.setBrush(on if s in lit else off)
            p.drawPolygon(poly)
        p.setBrush(on if dot else off)
        r = t * 0.65
        p.drawEllipse(QPointF(x + w + t * 1.1, y + h - r / 2), r, r)


class Led(QWidget):
    """A round LED, painted."""

    def __init__(self, size: int = 12, parent=None):
        super().__init__(parent)
        self.on, self.color, self.d = False, qcolor("red"), size
        self.setFixedSize(size + 2, size + 2)

    def set_state(self, on: bool, color: Optional[str] = None) -> None:
        self.on = bool(on)
        if color:
            self.color = qcolor(color)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(self.color)
        if not self.on:
            c = QColor(c.red() // 5 + 20, c.green() // 5 + 10, c.blue() // 5 + 10)
        p.setBrush(c)
        p.setPen(QPen(QColor("#111"), 1))
        p.drawEllipse(QRectF(1, 1, self.d, self.d))
        if self.on:
            glow = QColor(255, 255, 255, 120)
            p.setBrush(glow)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QRectF(self.d * 0.3, self.d * 0.25, self.d * 0.3, self.d * 0.25))
        p.end()


class LedList(QWidget):
    """One LED per option; the selected one(s) lit."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.options: List[str] = []
        self.orient, self.color = "vertical", "red"
        self.leds: List[Led] = []
        self.texts: List[QLabel] = []
        self.title = QLabel()
        self.title.setObjectName("plugin_caption")
        self.lay = QGridLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setHorizontalSpacing(4)
        self.lay.setVerticalSpacing(2)

    def set_props(self, p: dict) -> None:
        opts = [str(o) for o in p.get("options", self.options)]
        orient = p.get("orient", self.orient)
        self.color = p.get("color", self.color)
        if opts != self.options or orient != self.orient:
            self.options, self.orient = opts, orient
            self._build()
        if "label" in p:
            self.title.setText(p.get("label") or "")
            self.title.setVisible(bool(p.get("label")))
        sel = p.get("selected")
        lit = set(sel if isinstance(sel, list) else [] if sel is None else [sel])
        for i, led in enumerate(self.leds):
            led.set_state(i in lit, self.color)

    def _build(self):
        while self.lay.count():
            it = self.lay.takeAt(0)
            if it.widget() is not None and it.widget() is not self.title:
                it.widget().deleteLater()
        self.leds, self.texts = [], []
        self.lay.addWidget(self.title, 0, 0, 1, max(1, len(self.options) * 2))
        for i, o in enumerate(self.options):
            led, t = Led(), QLabel(o)
            t.setObjectName("plugin_ledtext")
            if self.orient == "horizontal":
                self.lay.addWidget(led, 1, i, Qt.AlignHCenter)
                self.lay.addWidget(t, 2, i, Qt.AlignHCenter)
            else:
                self.lay.addWidget(led, i + 1, 0)
                self.lay.addWidget(t, i + 1, 1)
            self.leds.append(led)
            self.texts.append(t)


class BarGraph(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.value, self.lo, self.hi, self.color = 0.0, 0.0, 1.0, qcolor("red")
        self.setMinimumSize(120, 12)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_props(self, p: dict) -> None:
        self.value = float(p.get("value", self.value) or 0.0)
        self.lo, self.hi = float(p.get("min", self.lo)), float(p.get("max", self.hi))
        if p.get("color"):
            self.color = qcolor(p["color"])
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        r = self.rect().adjusted(0, 2, -1, -2)
        p.fillRect(r, QColor("#0b0b0b"))
        span = (self.hi - self.lo) or 1.0
        zero = 0.0 if self.lo < 0 < self.hi else self.lo
        xz = r.left() + (zero - self.lo) / span * r.width()
        xv = r.left() + (min(max(self.value, self.lo), self.hi) - self.lo) / span * r.width()
        n = 40                           # segments, like the LED bar of a panel
        seg = r.width() / n
        off = QColor(self.color)
        off.setAlpha(30)
        for i in range(n):
            x = r.left() + i * seg
            mid = x + seg / 2
            lit = min(xz, xv) <= mid <= max(xz, xv)
            p.fillRect(QRectF(x + 1, r.top() + 1, seg - 2, r.height() - 2),
                       self.color if lit else off)
        p.end()


class Knob(QWidget):
    """A knob without end stops: the wheel, dragging up / down or the arrow keys turn
    it by steps."""

    turned = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.angle = 0.0
        self._drag: Optional[float] = None
        self.setFixedSize(56, 56)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.SizeVerCursor)
        self.setToolTip("turn: mouse wheel, drag up / down, or the arrow keys")

    def _turn(self, steps: int) -> None:
        if steps and self.isEnabled():
            self.angle = (self.angle + 15 * steps) % 360
            self.update()
            self.turned.emit(int(steps))

    def wheelEvent(self, ev):
        d = ev.angleDelta().y()
        self._turn(1 if d > 0 else -1 if d < 0 else 0)
        ev.accept()

    def mousePressEvent(self, ev):
        self._drag = ev.position().y()

    def mouseMoveEvent(self, ev):
        if self._drag is None:
            return
        d = self._drag - ev.position().y()
        if abs(d) >= 8:
            self._turn(int(d / 8))
            self._drag = ev.position().y()

    def mouseReleaseEvent(self, ev):
        self._drag = None

    def keyPressEvent(self, ev):
        k = ev.key()
        if k in (Qt.Key_Up, Qt.Key_Right):
            self._turn(1)
        elif k in (Qt.Key_Down, Qt.Key_Left):
            self._turn(-1)
        else:
            super().keyPressEvent(ev)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(4, 4, self.width() - 8, self.height() - 8)
        p.setBrush(QColor("#3a3a3a"))
        p.setPen(QPen(QColor("#111"), 2))
        p.drawEllipse(r)
        p.setBrush(QColor("#2a2a2a"))
        p.drawEllipse(r.adjusted(6, 6, -6, -6))
        c = r.center()
        a = math.radians(self.angle - 90)
        p.setPen(QPen(QColor("#e8e8e8"), 3, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(c.x() + math.cos(a) * 6, c.y() + math.sin(a) * 6),
                   QPointF(c.x() + math.cos(a) * (r.width() / 2 - 8),
                           c.y() + math.sin(a) * (r.width() / 2 - 8)))
        p.end()


# ---------------------------------------------------------------------------
# a panel
# ---------------------------------------------------------------------------
DARK = """
QGroupBox#plugin_dark { background: #26282b; color: #e6e6e6; border: 1px solid #111;
    border-radius: 4px; margin-top: 0px; padding: 22px 8px 8px 8px; font-weight: bold }
QGroupBox#plugin_dark::title { subcontrol-origin: padding; subcontrol-position: top left;
    left: 8px; top: 4px; padding: 0 2px; color: #f0f0f0 }
QGroupBox#plugin_dark QLabel { color: #dcdcdc }
QGroupBox#plugin_dark QLabel#plugin_ledtext { font-size: 9pt; font-weight: normal }
QGroupBox#plugin_dark QLabel#plugin_caption { font-size: 8pt; color: #bbbbbb;
    font-weight: normal }
QGroupBox#plugin_dark QPushButton { background: #4a4d52; color: #f4f4f4; border: 1px solid #111;
    border-radius: 3px; padding: 4px 8px; min-width: 34px; font-weight: bold }
QGroupBox#plugin_dark QPushButton:pressed { background: #6a6e74 }
QGroupBox#plugin_dark QPushButton:disabled { color: #888 }
"""


class PanelView(QScrollArea):
    """One panel (or data tab) of a plugin, drawn from its description."""

    def __init__(self, plugin: str, panel: dict, send: Callable[[str, str, str, object], None]):
        super().__init__()
        self.plugin = plugin
        self.panel_id = panel["id"]
        self.signature = structure(panel["root"])
        self._send = send
        self._apply: Dict[str, Callable[[dict], None]] = {}
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        inner.setObjectName("plugin_panel")
        inner.setStyleSheet(DARK)
        lay = QVBoxLayout(inner)
        if has_plot(panel["root"]):
            lay.addWidget(self._build(panel["root"]), 1)     # plots take the room there is
        else:
            lay.addWidget(self._build(panel["root"]))
            lay.addStretch(1)
        self.setWidget(inner)

    def apply(self, widget_id: str, props: dict) -> None:
        f = self._apply.get(widget_id)
        if f is not None:
            f(props)

    def set_locked(self, on: bool) -> None:
        for w in self.findChildren(QPushButton) + self.findChildren(Knob) + \
                self.findChildren(QComboBox) + self.findChildren(QDoubleSpinBox):
            if on:
                w.setProperty("_locked", True)
                w.setEnabled(False)
            elif w.property("_locked"):
                w.setProperty("_locked", False)
                w.setEnabled(w.property("_enabled") is not False)

    def _event(self, wid, event, value=None):
        self._send(self.plugin, wid, event, value)

    def _build(self, w: dict) -> QWidget:
        kind, wid, p = w["type"], w.get("id"), dict(w.get("props") or {})
        make = getattr(self, "_make_" + kind, None)
        if make is None:
            return QLabel("(%s)" % kind)
        widget, apply = make(w, wid, p)
        widget.setObjectName(widget.objectName() or ("pw_%s" % wid if wid else ""))
        if wid:
            def full(props, apply=apply, widget=widget):
                if "visible" in props:
                    widget.setVisible(props["visible"] is not False)
                apply(props)
            self._apply[wid] = full
        if p.get("visible") is False:
            widget.setVisible(False)
        return widget

    # -- layout ---------------------------------------------------------------------
    def _box(self, w, horizontal: bool, holder: QWidget):
        p = w.get("props") or {}
        lay = QHBoxLayout(holder) if horizontal else QVBoxLayout(holder)
        lay.setSpacing(int(p.get("gap", 6)))
        grows = False
        align = {"center": Qt.AlignCenter, "end": Qt.AlignRight if horizontal else Qt.AlignBottom}
        for c in w.get("children") or []:
            if c["type"] == "spacer":
                size = int((c.get("props") or {}).get("size", 0))
                if size:
                    lay.addSpacing(size)
                else:
                    lay.addStretch(1)
                continue
            cw = self._build(c)
            a = align.get(p.get("align"))
            if has_plot(c):
                lay.addWidget(cw, 1)
                grows = True
            elif a is not None:
                lay.addWidget(cw, 0, a)
            elif p.get("align") == "stretch":
                lay.addWidget(cw, 1)
            else:
                lay.addWidget(cw, 0, Qt.AlignTop if horizontal else Qt.AlignLeft)
        if p.get("align") not in ("stretch",) and not grows:
            lay.addStretch(1)
        return lay

    def _make_row(self, w, wid, p):
        holder = QWidget()
        self._box(w, True, holder).setContentsMargins(0, 0, 0, 0)
        return holder, lambda props: None

    def _make_column(self, w, wid, p):
        holder = QWidget()
        self._box(w, False, holder).setContentsMargins(0, 0, 0, 0)
        return holder, lambda props: None

    def _make_group(self, w, wid, p):
        g = QGroupBox(p.get("title") or "")
        if p.get("dark"):
            g.setObjectName("plugin_dark")
        self._box(w, False, g)
        return g, lambda props: g.setTitle(props.get("title", g.title()))

    # -- display ----------------------------------------------------------------------
    def _make_label(self, w, wid, p):
        lab = QLabel()

        def apply(props):
            if "text" in props:
                lab.setText(str(props.get("text") or ""))
            f = lab.font()
            size = props.get("size")
            if size:
                base = QFont().pointSizeF()
                f.setPointSizeF(base * {"small": 0.85, "large": 1.4}.get(size, 1.0))
            if "bold" in props:
                f.setBold(bool(props["bold"]))
            lab.setFont(f)
            if props.get("color"):
                lab.setStyleSheet("color: %s" % qcolor(props["color"]).name())
        apply(p)
        return lab, apply

    def _make_seg7(self, w, wid, p):
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        row = QHBoxLayout()
        seg = Seg7()
        unit = QLabel()
        row.addWidget(seg)
        row.addWidget(unit)
        lay.addLayout(row)
        cap = QLabel()
        cap.setObjectName("plugin_caption")
        lay.addWidget(cap)

        def apply(props):
            seg.set_props(props)
            if "unit" in props:
                unit.setText(props.get("unit") or "")
            if "label" in props:
                cap.setText(props.get("label") or "")
                cap.setVisible(bool(props.get("label")))
        seg.set_props(p)
        unit.setText(p.get("unit") or "")
        cap.setText(p.get("label") or "")
        cap.setVisible(bool(p.get("label")))
        holder.seg = seg
        return holder, apply

    def _make_led(self, w, wid, p):
        holder = QWidget()
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        led, lab = Led(), QLabel(p.get("label") or "")
        lab.setObjectName("plugin_ledtext")
        lay.addWidget(led)
        lay.addWidget(lab)
        led.set_state(p.get("on"), p.get("color"))

        def apply(props):
            led.set_state(props.get("on", led.on), props.get("color"))
            if "label" in props:
                lab.setText(props.get("label") or "")
        holder.led = led
        return holder, apply

    def _make_ledlist(self, w, wid, p):
        ll = LedList()
        ll.set_props(p)
        return ll, ll.set_props

    def _make_bar(self, w, wid, p):
        b = BarGraph()
        b.set_props(p)
        return b, b.set_props

    def _make_plot(self, w, wid, p):
        import pyqtgraph as pg
        pw = pg.PlotWidget()
        pw.setMinimumHeight(260)
        pw.addLegend()
        pw.showGrid(x=True, y=True, alpha=0.3)
        curves: Dict[str, object] = {}
        pens = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b"]

        def apply(props):
            if "title" in props:
                pw.setTitle(props.get("title") or "")
            if "xlabel" in props:
                pw.setLabel("bottom", props.get("xlabel") or "")
            if "ylabel" in props:
                pw.setLabel("left", props.get("ylabel") or "")
            if "series" in props:
                seen = set()
                for i, s in enumerate(props.get("series") or []):
                    name = str(s.get("name", i))
                    seen.add(name)
                    x = [float("nan") if v is None else v for v in s.get("x") or []]
                    y = [float("nan") if v is None else v for v in s.get("y") or []]
                    if name not in curves:
                        curves[name] = pw.plot([], [], name=name,
                                               pen=pg.mkPen(pens[len(curves) % len(pens)], width=2))
                    curves[name].setData(x, y, connect="finite")
                for name in list(curves):
                    if name not in seen:
                        pw.removeItem(curves.pop(name))
        apply(p)
        return pw, apply

    # -- controls -----------------------------------------------------------------------
    def _make_button(self, w, wid, p):
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        cap = QLabel(p.get("label") or "")
        cap.setObjectName("plugin_caption")
        cap.setVisible(bool(p.get("label")))
        b = QPushButton(p.get("text") or "")
        b.setObjectName("pw_%s" % wid)
        b.clicked.connect(lambda: self._event(wid, "click"))
        lay.addWidget(cap, 0, Qt.AlignHCenter)
        lay.addWidget(b)

        def apply(props):
            if "text" in props:
                b.setText(props.get("text") or "")
                f = QFont(b.font())
                f.setBold(True)                   # the panel style: bold keys
                b.setMinimumWidth(QFontMetrics(f).horizontalAdvance(b.text()) + 24)
            if "label" in props:
                cap.setText(props.get("label") or "")
                cap.setVisible(bool(props.get("label")))
            if "enabled" in props:
                b.setProperty("_enabled", props["enabled"] is not False)
                if not b.property("_locked"):
                    b.setEnabled(props["enabled"] is not False)
            if props.get("color"):
                b.setStyleSheet("color: %s" % qcolor(props["color"]).name())
        apply(p)
        holder.button = b
        return holder, apply

    def _make_knob(self, w, wid, p):
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        k = Knob()
        k.setObjectName("pw_%s" % wid)
        k.turned.connect(lambda steps: self._event(wid, "turn", steps))
        cap = QLabel(p.get("label") or "")
        cap.setObjectName("plugin_caption")
        lay.addWidget(k, 0, Qt.AlignHCenter)
        lay.addWidget(cap, 0, Qt.AlignHCenter)

        def apply(props):
            if "label" in props:
                cap.setText(props.get("label") or "")
            if "enabled" in props:
                k.setProperty("_enabled", props["enabled"] is not False)
                if not k.property("_locked"):
                    k.setEnabled(props["enabled"] is not False)
        holder.knob = k
        return holder, apply

    def _make_select(self, w, wid, p):
        holder = QWidget()
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        cap = QLabel(p.get("label") or "")
        cb = QComboBox()
        cb.setObjectName("pw_%s" % wid)
        cb.setMinimumWidth(120)
        lay.addWidget(cap)
        lay.addWidget(cb)
        state = {"busy": False}

        def changed(i):
            if not state["busy"] and i >= 0:
                self._event(wid, "change", cb.itemText(i))
        cb.currentIndexChanged.connect(changed)

        def apply(props):
            state["busy"] = True
            try:
                if "options" in props:
                    cur = cb.currentText()
                    cb.clear()
                    cb.addItems([str(o) for o in props.get("options") or []])
                    cb.setCurrentText(cur)
                if "value" in props:
                    cb.setCurrentText(str(props.get("value") or ""))
                if "label" in props:
                    cap.setText(props.get("label") or "")
                if "enabled" in props:
                    cb.setProperty("_enabled", props["enabled"] is not False)
                    if not cb.property("_locked"):
                        cb.setEnabled(props["enabled"] is not False)
            finally:
                state["busy"] = False
        apply(p)
        return holder, apply

    def _make_number(self, w, wid, p):
        holder = QWidget()
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        cap = QLabel(p.get("label") or "")
        sb = QDoubleSpinBox()
        sb.setObjectName("pw_%s" % wid)
        sb.setKeyboardTracking(False)
        unit = QLabel(p.get("unit") or "")
        lay.addWidget(cap)
        lay.addWidget(sb)
        lay.addWidget(unit)
        state = {"busy": False}
        sb.editingFinished.connect(lambda: None if state["busy"] else
                                   self._event(wid, "change", sb.value()))

        def apply(props):
            state["busy"] = True
            try:
                if "decimals" in props:
                    sb.setDecimals(int(props["decimals"]))
                lo = props.get("min", p.get("min"))
                hi = props.get("max", p.get("max"))
                sb.setRange(-1e12 if lo is None else float(lo), 1e12 if hi is None else float(hi))
                if "step" in props:
                    sb.setSingleStep(float(props["step"]))
                if props.get("value") is not None:
                    sb.setValue(float(props["value"]))
                if "label" in props:
                    cap.setText(props.get("label") or "")
                if "unit" in props:
                    unit.setText(props.get("unit") or "")
                if "enabled" in props:
                    sb.setProperty("_enabled", props["enabled"] is not False)
                    if not sb.property("_locked"):
                        sb.setEnabled(props["enabled"] is not False)
            finally:
                state["busy"] = False
        apply(p)
        return holder, apply


def structure(w: dict) -> str:
    """The shape of a widget tree (types, ids, the options of lists): a panel is drawn
    again when it changes, otherwise only properties are applied."""
    def s(x):
        return [x.get("type"), x.get("id"), [s(c) for c in x.get("children") or []]]
    return json.dumps(s(w))


def has_plot(w: dict) -> bool:
    return w.get("type") == "plot" or any(has_plot(c) for c in w.get("children") or [])


def widgets_of(root: dict):
    yield root
    for c in root.get("children") or []:
        yield from widgets_of(c)


# ---------------------------------------------------------------------------
# the plugins of the main window
# ---------------------------------------------------------------------------
class PluginTabs:
    """Keeps the panels and data tabs of the running plugins in the main window."""

    def __init__(self, window):
        self.w = window
        self.conn = window.conn
        self.views: Dict[tuple, PanelView] = {}      # (plugin, panel id) -> view
        self.plugins: List[dict] = []
        self.menu = window.menuBar().addMenu("&Plugins")
        self.menu.aboutToShow.connect(self._fill_menu)
        window.model.stateChanged.connect(self._state)
        self.conn.event.connect(self._event)
        self.conn.disconnected.connect(lambda: self.update([]))

    # -- the menu ---------------------------------------------------------------------
    def _fill_menu(self) -> None:
        self.menu.clear()
        a = QAction("Manage plugins…", self.menu)
        a.triggered.connect(self.manage)
        self.menu.addAction(a)
        if self.w.locked:
            a.setEnabled(False)
        shown = [p for p in self.plugins if p.get("panels")]
        if shown:
            self.menu.addSeparator()
        for p in shown:
            for panel in p["panels"]:
                act = QAction("%s – %s" % (p["title"], panel["title"]), self.menu)
                act.triggered.connect(lambda _=False, k=(p["name"], panel["id"]): self.show(k))
                self.menu.addAction(act)
        if not self.plugins:
            na = QAction("(no plugins)", self.menu)
            na.setEnabled(False)
            self.menu.addAction(na)

    def manage(self) -> None:
        dlg = PluginDialog(self.conn, self.plugins, self.w)
        dlg.exec()

    def show(self, key) -> None:
        v = self.views.get(key)
        if v is None:
            return
        for tabs in (self.w.left_tabs, self.w.data_tabs):
            i = tabs.indexOf(v)
            if i >= 0:
                tabs.setCurrentIndex(i)

    # -- updates ----------------------------------------------------------------------
    def _state(self, st: dict) -> None:
        self.update(((st or {}).get("plugins") or {}).get("plugins") or [])

    def _event(self, kind: str, data) -> None:
        if kind == "plugins" and isinstance(data, dict):
            self.update(data.get("plugins") or [])
        elif kind == "plugin_ui" and isinstance(data, dict):
            name = data.get("plugin")
            for (pl, _), v in self.views.items():
                if pl == name:
                    for wid, props in (data.get("set") or {}).items():
                        v.apply(wid, props)

    def update(self, plugins: List[dict]) -> None:
        self.plugins = list(plugins)
        want = {}
        for p in self.plugins:
            if p.get("state") not in ("running", "starting"):
                continue
            for panel in p.get("panels") or []:
                want[(p["name"], panel["id"])] = (p, panel)
        for key in list(self.views):
            if key not in want or self.views[key].signature != structure(want[key][1]["root"]):
                self._drop(key)
        for key, (p, panel) in want.items():
            if key not in self.views:
                self._add(p, panel)
            else:
                v = self.views[key]
                for x in widgets_of(panel["root"]):
                    if x.get("id"):
                        v.apply(x["id"], x.get("props") or {})

    def _add(self, p: dict, panel: dict) -> None:
        v = PanelView(p["name"], panel, self._send)
        v.setObjectName("plugin_%s_%s" % (p["name"], panel["id"]))
        v.set_locked(self.w.locked)
        self.views[(p["name"], panel["id"])] = v
        title = panel["title"]
        tip = "%s (plugin %s)" % (title, p["title"])
        if panel.get("where") == "data":
            i = self.w.data_tabs.addTab(v, title)
            self.w.data_tabs.tabBar().setTabButton(i, self.w.data_tabs.tabBar().ButtonPosition.RightSide,
                                                    None)
            self.w.data_tabs.setTabToolTip(i, tip)
        else:
            i = self.w.left_tabs.addTab(v, title)
            self.w.left_tabs.setTabToolTip(i, tip)

    def _drop(self, key) -> None:
        v = self.views.pop(key)
        for tabs in (self.w.left_tabs, self.w.data_tabs):
            i = tabs.indexOf(v)
            if i >= 0:
                tabs.removeTab(i)
        v.deleteLater()

    def _send(self, plugin: str, widget: str, event: str, value) -> None:
        args = {"plugin": plugin, "widget": widget, "event": event}
        if value is not None:
            args["value"] = value
        self.conn.request("plugin_event", args)

    def set_locked(self, on: bool) -> None:
        for v in self.views.values():
            v.set_locked(on)


# ---------------------------------------------------------------------------
# managing the plugins
# ---------------------------------------------------------------------------
class PluginDialog(QDialog):
    """Plugins → Manage plugins…: which plugins there are, enable or disable them,
    their options and errors."""

    COLS = ["Enabled", "Plugin", "State", "Version", "From"]

    def __init__(self, conn, plugins: List[dict], parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Plugins")
        self.resize(900, 560)
        self.data: dict = {"plugins": plugins, "errors": {}, "folders": []}
        lay = QVBoxLayout(self)
        intro = QLabel("Plugins add panels, data tabs, channels and actions. Each runs on its "
                       "own thread; disabling one removes all of it at once.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        split = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setObjectName("plugin_table")
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._details)
        split.addWidget(self.table)
        det = QWidget()
        dl = QVBoxLayout(det)
        dl.setContentsMargins(0, 0, 0, 0)
        self.about = QLabel()
        self.about.setWordWrap(True)
        self.about.setTextInteractionFlags(Qt.TextSelectableByMouse)
        dl.addWidget(self.about)
        ol = QHBoxLayout()
        ol.addWidget(QLabel("Options (JSON):"))
        self.apply_opts = QPushButton("Apply options")
        self.apply_opts.clicked.connect(self._set_options)
        ol.addStretch(1)
        ol.addWidget(self.apply_opts)
        dl.addLayout(ol)
        self.options = QPlainTextEdit()
        self.options.setObjectName("plugin_options")
        self.options.setMaximumHeight(110)
        dl.addWidget(self.options)
        self.error = QPlainTextEdit()
        self.error.setReadOnly(True)
        self.error.setObjectName("plugin_error")
        self.error.setPlaceholderText("no errors")
        self.error.setMaximumHeight(140)
        dl.addWidget(self.error)
        split.addWidget(det)
        lay.addWidget(split, 1)
        self.folders = QLabel()
        self.folders.setWordWrap(True)
        self.folders.setStyleSheet("color: gray")
        self.folders.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.folders)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        self.rescan = bb.addButton("Look for plugins again", QDialogButtonBox.ActionRole)
        self.rescan.clicked.connect(lambda: self.conn.request("rescan_plugins", on_ok=self._got,
                                                              on_err=self._err))
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.msg = QLabel()
        lay.addWidget(self.msg)
        conn.event.connect(self._event)
        self._fill()
        self.conn.request("list_plugins", on_ok=self._got, on_err=self._err)

    def _event(self, kind, data):
        if kind == "plugins" and isinstance(data, dict):
            self._got(data)

    def _got(self, data) -> None:
        if isinstance(data, dict):
            self.data = data
            self._fill()

    def _err(self, e) -> None:
        self.msg.setText("<span style='color:#c0392b'>%s</span>" % e)

    def _selected(self) -> Optional[dict]:
        r = self.table.currentRow()
        ps = self.data.get("plugins") or []
        return ps[r] if 0 <= r < len(ps) else None

    def _fill(self) -> None:
        cur = self._selected()
        ps = self.data.get("plugins") or []
        self.table.blockSignals(True)
        self.table.setRowCount(len(ps))
        for r, p in enumerate(ps):
            box = QCheckBox()
            box.setObjectName("plugin_enable_%s" % p["name"])
            box.setChecked(p.get("state") in ("running", "starting"))
            box.setToolTip("enable / disable %s" % p["title"])
            box.toggled.connect(lambda on, n=p["name"]: self._toggle(n, on))
            cell = QWidget()
            cl = QHBoxLayout(cell)
            cl.setContentsMargins(6, 0, 0, 0)
            cl.addWidget(box)
            self.table.setCellWidget(r, 0, cell)
            state = p.get("state", "off")
            items = [p["title"], state + (" (%d errors)" % p["errors"] if p.get("errors") else ""),
                     p.get("version") or "", "built in" if p.get("builtin") else p.get("source", "")]
            for c, t in enumerate(items, 1):
                it = QTableWidgetItem(t)
                if c == 2 and state == "error":
                    it.setForeground(QBrush(QColor("#c0392b")))
                self.table.setItem(r, c, it)
        self.table.resizeColumnToContents(0)
        self.table.blockSignals(False)
        if ps:
            names = [p["name"] for p in ps]
            r = names.index(cur["name"]) if cur and cur["name"] in names else 0
            self.table.selectRow(r)
        self._details()
        errs = self.data.get("errors") or {}
        self.folders.setText("Plugin API %s. Plugins are looked for in installed packages and "
                             "in: %s%s" % (
            self.data.get("api") or "?", ", ".join(self.data.get("folders") or []),
            ("<br><span style='color:#c0392b'>Not loaded: %s</span>" % ", ".join(errs))
            if errs else ""))
        self.folders.setTextFormat(Qt.RichText)

    def _details(self) -> None:
        p = self._selected()
        if p is None:
            self.about.setText("No plugins found.")
            self.options.setPlainText("")
            self.error.setPlainText("\n\n".join("%s:\n%s" % kv for kv in
                                                (self.data.get("errors") or {}).items()))
            return
        inst = (" · channels: instrument <b>%s</b>" % p["instrument"]) if p.get("instrument") else ""
        self.about.setText("<b>%s</b> (%s) %s · plugin API %s<br>%s%s" % (
            p["title"], p["name"], p.get("version") or "", p.get("api") or "?",
            p.get("description") or "", inst))
        self.options.setPlainText(json.dumps(p.get("options") or {}, indent=1, ensure_ascii=False))
        self.options.setEnabled(bool(p.get("defaults")))
        self.apply_opts.setEnabled(bool(p.get("defaults")))
        self.error.setPlainText("\n".join(p.get("recent_errors") or []) or p.get("last_error") or "")

    def _toggle(self, name: str, on: bool) -> None:
        self.msg.setText("%s %s…" % ("starting" if on else "stopping", name))
        self.conn.request("enable_plugin" if on else "disable_plugin", {"name": name},
                          on_ok=lambda _: (self.msg.setText(""), self.conn.request(
                              "list_plugins", on_ok=self._got)),
                          on_err=lambda e: (self._err(e), self.conn.request(
                              "list_plugins", on_ok=self._got)))

    def _set_options(self) -> None:
        p = self._selected()
        if p is None:
            return
        try:
            opts = json.loads(self.options.toPlainText() or "{}")
            if not isinstance(opts, dict):
                raise ValueError("options are a JSON object: {\"name\": value, …}")
        except ValueError as exc:
            QMessageBox.warning(self, "Options", str(exc))
            return
        self.conn.request("set_plugin_options", {"name": p["name"], "options": opts},
                          on_ok=lambda _: self.msg.setText("options of %s saved" % p["name"]),
                          on_err=self._err)
