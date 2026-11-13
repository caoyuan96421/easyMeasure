"""Monitor panel: live tiles for every monitored channel, driven by backend push.

Scalars are drawn against time.  A vector channel (e.g. the eight readings of a
multi-channel source) is drawn as one curve per element against time ("vs
time"), or as its latest value against the element index ("vs index"); long
vectors (spectra …) start in the index view.  Tiles are laid out in a grid
whose number of columns is set at the top (remembered between sessions)."""

from __future__ import annotations

import collections
import time
from typing import Dict

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ..plotting import colormaps
from .data_panel import auto_columns

from ..widgets import ElidedLabel, FitArea, FlowLayout, shrinkable_label

HISTORY = 3000
MAX_TRACES = 64        # vectors up to this length keep a per-element history
TIME_VIEW_MAX = 16     # ... and start in the "vs time" view up to this length
MAX_COLUMNS = 8


def trace_color(i: int, n: int):
    return pg.intColor(i, hues=max(n, 1), values=1, maxValue=255, minValue=150)


def _fmt_vector(a: np.ndarray) -> str:
    if a.size <= 8:
        return "[" + ", ".join("%.4g" % x for x in a) + "]"
    return "[%d]  min %.4g  max %.4g" % (a.size, np.nanmin(a), np.nanmax(a))


class MonitorTile(QFrame):
    def __init__(self, name: str, on_remove, parent=None):
        super().__init__(parent)
        self.name = name
        self.setFrameShape(QFrame.StyledPanel)
        self.t = collections.deque(maxlen=HISTORY)
        self.v = collections.deque(maxlen=HISTORY)
        self.ta = collections.deque(maxlen=HISTORY)      # 1-D vectors: time ...
        self.va = collections.deque(maxlen=HISTORY)      # ... and the values
        self.traces = []
        self.latest = None
        self.dirty = False
        self.kind = None
        self.title = ElidedLabel(name, bold=True)
        self.every = QLabel("")                  # short: the big value gives way first
        self.every.setStyleSheet("color: gray")
        self.value = shrinkable_label(QLabel("—"), wrap=False)
        from ..display import register_font
        register_font(self.value, 1.7, monospace=True)
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.err = shrinkable_label(QLabel(""))
        self.err.setStyleSheet("color: #d55")
        self.err.setVisible(False)              # takes room only when there is an error
        rm = QPushButton("✕")
        self.rm = rm
        rm.setFixedWidth(24)
        rm.setToolTip("stop monitoring")
        rm.clicked.connect(lambda: on_remove(name))
        self.span = QComboBox()
        self.span.addItems(["1 min", "10 min", "1 h", "all"])
        self.span.setCurrentIndex(1)
        self.view = QComboBox()
        self.view.addItems(["vs time", "vs index"])
        for cb in (self.span, self.view):
            cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(4)
        self.view.setToolTip("vector channel: every element against time, or the latest "
                             "value against the element index")
        self.view.setVisible(False)
        self._view_set = False
        head = QHBoxLayout()
        head.addWidget(self.title, 1)                 # the name takes what the row has
        head.addWidget(self.view)
        head.addWidget(self.span)
        head.addWidget(rm)
        self.plot = pg.PlotWidget()
        from ..plotting.style import style_axes
        style_axes(self.plot.getPlotItem())
        self.plot.setMinimumHeight(50)          # grows with the space the panel has
        self.plot.setMinimumWidth(100)
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.curve = self.plot.plot([], [], pen=pg.mkPen("c", width=1.2))
        self.img = pg.ImageItem()
        self.img.setColorMap(colormaps.get("viridis"))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addLayout(head)
        vrow = QHBoxLayout()
        vrow.addWidget(self.value, 1)
        vrow.addWidget(self.every, 0, Qt.AlignRight | Qt.AlignBottom)
        lay.addLayout(vrow)
        lay.addWidget(self.plot, 1)
        lay.addWidget(self.err)
        self.span.currentIndexChanged.connect(lambda *_: setattr(self, "dirty", True))
        self.view.currentIndexChanged.connect(lambda *_: setattr(self, "dirty", True))

    def push(self, msg: dict) -> None:
        if "error" in msg:
            self.err.setText("⚠ " + str(msg["error"])[:200])
            self.err.setVisible(True)
            return
        if self.err.isVisible():
            self.err.setText("")
            self.err.setVisible(False)
        v = msg.get("value")
        t = float(msg.get("t", time.time()))
        if isinstance(v, str):
            self.kind = "str"
            self.latest = v
        elif isinstance(v, np.ndarray) and v.ndim >= 1:
            self.kind = "array"
            self.latest = v
            if v.ndim == 1 and 0 < v.size <= MAX_TRACES:
                if self.va and self.va[-1].shape != v.shape:
                    self.ta.clear()                      # the length changed: start over
                    self.va.clear()
                self.ta.append(t)
                try:
                    self.va.append(np.asarray(v, dtype=float).copy())
                except (TypeError, ValueError):
                    self.ta.pop()
            if not self._view_set:
                self.view.setCurrentIndex(0 if v.ndim == 1 and v.size <= TIME_VIEW_MAX else 1)
                self._view_set = True
        else:
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return
            self.kind = "scalar"
            self.latest = fv
            self.t.append(t)
            self.v.append(fv)
        self.dirty = True

    def redraw(self) -> None:
        if not self.dirty:
            return
        self.dirty = False
        if self.kind == "scalar":
            self.value.setText("%.7g" % self.latest)
            self.plot.setVisible(True)
            if self.img.scene() is not None:
                self.plot.removeItem(self.img)
            t = np.fromiter(self.t, float)
            v = np.fromiter(self.v, float)
            span = {0: 60, 1: 600, 2: 3600}.get(self.span.currentIndex())
            if span and t.size:
                m = t >= t[-1] - span
                t, v = t[m], v[m]
            if t.size:
                self.curve.setData(t - t[-1], v)
            self._set_traces(0)
            self.plot.setLabel("bottom", "s")
        elif self.kind == "str":
            self.value.setText(str(self.latest)[:80])
            self.plot.setVisible(False)
        elif self.kind == "array":
            a = self.latest
            self.plot.setVisible(True)
            self.view.setVisible(a.ndim == 1 and a.size <= MAX_TRACES)
            if a.ndim == 1:
                if self.img.scene() is not None:
                    self.plot.removeItem(self.img)
                self.value.setText(_fmt_vector(a) if a.size else "[]")
                if self.view.isVisible() and self.view.currentIndex() == 0 and self.va:
                    self._draw_history()
                    return
                self._set_traces(0)
                self.curve.setData(np.arange(a.size), a)
            else:
                self._set_traces(0)
                self.curve.setData([], [])
                if self.img.scene() is None:
                    self.plot.addItem(self.img)
                img = a if a.ndim == 2 else a.reshape(a.shape[0], -1)
                self.img.setImage(img.T if pg.getConfigOption("imageAxisOrder") == "row-major" else img.T)
                self.value.setText("%s  mean %.4g" % ("x".join(map(str, a.shape)), np.nanmean(a)))
            self.plot.setLabel("bottom", "index")

    def _set_traces(self, n: int) -> None:
        """Keep exactly *n* per-element curves (and a legend for a few)."""
        while len(self.traces) > n:
            self.plot.removeItem(self.traces.pop())
        if n == 0 and self.plot.getPlotItem().legend is not None:
            self.plot.getPlotItem().legend.clear()
        while len(self.traces) < n:
            i = len(self.traces)
            self.traces.append(self.plot.plot([], [], pen=pg.mkPen(trace_color(i, n), width=1.2)))
        for i, c in enumerate(self.traces):
            c.setPen(pg.mkPen(trace_color(i, n), width=1.2))

    def _draw_history(self) -> None:
        t = np.fromiter(self.ta, float)
        v = np.vstack(list(self.va))                     # (time, element)
        span = {0: 60, 1: 600, 2: 3600}.get(self.span.currentIndex())
        if span and t.size:
            m = t >= t[-1] - span
            t, v = t[m], v[m]
        n = v.shape[1]
        self.curve.setData([], [])
        if len(self.traces) != n:
            self._set_traces(n)
            pi = self.plot.getPlotItem()
            if n <= 8:
                leg = pi.legend or pi.addLegend(offset=(2, 2), labelTextSize="7pt")
                leg.clear()
                for i, c in enumerate(self.traces):
                    leg.addItem(c, "[%d]" % i)
            elif pi.legend is not None:
                pi.legend.clear()
        for i, c in enumerate(self.traces):
            c.setData(t - t[-1], v[:, i])
        self.plot.setLabel("bottom", "s")


class MonitorPanel(QWidget):
    def __init__(self, conn, model, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.model = model
        self.tiles: Dict[str, MonitorTile] = {}
        self.channel = QComboBox()
        self.channel.setEditable(True)
        self.channel.setMinimumContentsLength(6)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.05, 3600)
        self.interval.setDecimals(2)
        self.interval.setValue(1.0)
        self.interval.setSuffix(" s")
        self.columns = QSpinBox()
        self.columns.setRange(0, MAX_COLUMNS)
        self.columns.setPrefix("columns ")
        self.columns.setSpecialValueText("columns auto")
        self.columns.setToolTip("number of monitor tiles side by side (auto = the arrangement "
                                "that shows every tile largest in the available space)")
        self._eff_cols = None
        try:
            self.columns.setValue(int(QSettings("easyMeasure", "gui").value("monitor_columns", 0)))
        except (TypeError, ValueError):
            pass
        self.columns.valueChanged.connect(self._columns_changed)
        add = QPushButton("Monitor")
        add.clicked.connect(self._add)
        self.add_btn = add
        self.locked = False
        top = FlowLayout()
        top.addWidget(self.channel)
        top.addWidget(self.interval)
        top.addWidget(add)
        top.addWidget(self.columns)
        self.host = QWidget()
        self.grid = QGridLayout(self.host)
        self.grid.setContentsMargins(2, 2, 2, 2)
        self.grid.setSpacing(4)
        scroll = FitArea(self.host)          # every tile in view; scrolls only when it must
        self.scroll = scroll
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(scroll, 1)
        conn.event.connect(self._on_event)
        conn.connected.connect(lambda _: self._load_history())
        model.stateChanged.connect(self._state)
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._redraw)
        self._timer.start()

    def _state(self, st: dict) -> None:
        names = self.model.readable_channels()
        cur = self.channel.currentText()
        self.channel.blockSignals(True)
        self.channel.clear()
        self.channel.addItems(names)
        self.channel.setEditText(cur)
        self.channel.blockSignals(False)
        monitored = {m["channel"] for m in st.get("monitor", [])}
        for name in list(self.tiles):
            if name not in monitored:
                self._drop_tile(name)
        for m in st.get("monitor", []):
            tile = self._tile(m["channel"])
            tile.every.setText("every %.3g s" % m["interval"])

    def _tile(self, name: str) -> MonitorTile:
        t = self.tiles.get(name)
        if t is None:
            t = MonitorTile(name, self._remove)
            t.rm.setEnabled(not self.locked)
            self.tiles[name] = t
            self._relayout()
        return t

    def _relayout(self) -> None:
        """Place the tiles row by row in the chosen number of columns."""
        while self.grid.count():
            self.grid.takeAt(0)
        n = self._effective_cols()
        self._eff_cols = n
        for c in range(MAX_COLUMNS):
            self.grid.setColumnStretch(c, 1 if c < n else 0)
        k = 0
        for k, t in enumerate(self.tiles.values()):
            self.grid.addWidget(t, k // n, k % n)
        rows = (len(self.tiles) + n - 1) // n
        for r in range(max(rows, self.grid.rowCount())):
            self.grid.setRowStretch(r, 1 if r < rows else 0)   # the tiles share the height
        self.scroll.fit()

    def _effective_cols(self) -> int:
        n = self.columns.value()
        if n > 0 or not self.tiles:
            return max(n, 1)
        from PySide6.QtCore import QSize
        tiles = list(self.tiles.values())
        m = QSize(max(t.minimumSizeHint().width() for t in tiles),
                  max(t.minimumSizeHint().height() for t in tiles))
        return auto_columns(len(tiles), self.scroll.maximumViewportSize(), m,
                            self.grid.spacing(), MAX_COLUMNS)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.columns.value() == 0 and self.tiles and self._effective_cols() != self._eff_cols:
            self._relayout()

    def _columns_changed(self, n: int) -> None:
        QSettings("easyMeasure", "gui").setValue("monitor_columns", n)
        self._relayout()

    def set_locked(self, on: bool) -> None:
        """GUI lock: no adding / removing monitors; the plots and their time span
        stay usable."""
        self.locked = on
        for w in [self.channel, self.interval, self.add_btn] + [t.rm for t in self.tiles.values()]:
            w.setEnabled(not on)

    def _drop_tile(self, name: str) -> None:
        t = self.tiles.pop(name, None)
        if t is not None:
            t.setParent(None)
            t.deleteLater()
            self._relayout()

    def _add(self) -> None:
        name = self.channel.currentText().strip()
        if not name:
            return
        self.conn.request("monitor_add", {"channel": name, "interval": self.interval.value()},
                          on_ok=lambda _: self.model.schedule_refresh(),
                          on_err=lambda e: self.model.message.emit("error", e))

    def _remove(self, name: str) -> None:
        self.conn.request("monitor_remove", {"channel": name},
                          on_ok=lambda _: self.model.schedule_refresh())
        self._drop_tile(name)

    def _load_history(self) -> None:
        def got(hist):
            for name, items in (hist or {}).items():
                tile = self._tile(name)
                for t, v in items:
                    tile.push({"channel": name, "t": t, "value": v})
        self.conn.request("monitor_history", on_ok=got)

    def _on_event(self, kind, data) -> None:
        if kind == "monitor" and isinstance(data, dict):
            name = data.get("channel")
            if name:
                self._tile(name).push(data)

    def _redraw(self) -> None:
        if not self.isVisible():
            return
        if self.columns.value() == 0 and self.tiles and self._effective_cols() != self._eff_cols:
            self._relayout()                 # tiles grew (a value, a vector view) or shrank
        for t in self.tiles.values():
            try:
                t.redraw()
            except Exception as exc:
                t.err.setText("display error: %s" % exc)
                t.err.setVisible(True)
