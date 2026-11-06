"""Monitor panel: live tiles for every monitored channel, driven by backend push."""

from __future__ import annotations

import collections
import time
from typing import Dict

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from ..plotting import colormaps

from ..widgets import FlowLayout, shrinkable_label

HISTORY = 3000


class MonitorTile(QFrame):
    def __init__(self, name: str, on_remove, parent=None):
        super().__init__(parent)
        self.name = name
        self.setFrameShape(QFrame.StyledPanel)
        self.t = collections.deque(maxlen=HISTORY)
        self.v = collections.deque(maxlen=HISTORY)
        self.latest = None
        self.dirty = False
        self.kind = None
        self.title = shrinkable_label(QLabel("<b>%s</b>" % name), wrap=False)
        self.value = shrinkable_label(QLabel("—"), wrap=False)
        from ..display import register_font
        register_font(self.value, 1.7, monospace=True)
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.err = shrinkable_label(QLabel(""))
        self.err.setStyleSheet("color: #d55")
        rm = QPushButton("✕")
        self.rm = rm
        rm.setFixedWidth(24)
        rm.setToolTip("stop monitoring")
        rm.clicked.connect(lambda: on_remove(name))
        self.span = QComboBox()
        self.span.addItems(["1 min", "10 min", "1 h", "all"])
        self.span.setCurrentIndex(1)
        head = QHBoxLayout()
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.span)
        head.addWidget(rm)
        self.plot = pg.PlotWidget()
        from ..plotting.style import style_axes
        style_axes(self.plot.getPlotItem())
        self.plot.setMinimumHeight(90)
        self.plot.setMinimumWidth(100)
        self.plot.setMaximumHeight(170)
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.curve = self.plot.plot([], [], pen=pg.mkPen("c", width=1.2))
        self.img = pg.ImageItem()
        self.img.setColorMap(colormaps.get("viridis"))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addLayout(head)
        lay.addWidget(self.value)
        lay.addWidget(self.plot)
        lay.addWidget(self.err)
        self.span.currentIndexChanged.connect(lambda *_: setattr(self, "dirty", True))

    def push(self, msg: dict) -> None:
        if "error" in msg:
            self.err.setText("⚠ " + str(msg["error"])[:200])
            return
        self.err.setText("")
        v = msg.get("value")
        t = float(msg.get("t", time.time()))
        if isinstance(v, str):
            self.kind = "str"
            self.latest = v
        elif isinstance(v, np.ndarray) and v.ndim >= 1:
            self.kind = "array"
            self.latest = v
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
            self.plot.setLabel("bottom", "s")
        elif self.kind == "str":
            self.value.setText(str(self.latest)[:80])
            self.plot.setVisible(False)
        elif self.kind == "array":
            a = self.latest
            self.plot.setVisible(True)
            if a.ndim == 1:
                if self.img.scene() is not None:
                    self.plot.removeItem(self.img)
                self.curve.setData(np.arange(a.size), a)
                self.value.setText("[%d]  max %.4g" % (a.size, np.nanmax(a) if a.size else np.nan))
            else:
                self.curve.setData([], [])
                if self.img.scene() is None:
                    self.plot.addItem(self.img)
                img = a if a.ndim == 2 else a.reshape(a.shape[0], -1)
                self.img.setImage(img.T if pg.getConfigOption("imageAxisOrder") == "row-major" else img.T)
                self.value.setText("%s  mean %.4g" % ("x".join(map(str, a.shape)), np.nanmean(a)))
            self.plot.setLabel("bottom", "index")


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
        add = QPushButton("Monitor")
        add.clicked.connect(self._add)
        self.add_btn = add
        self.locked = False
        top = FlowLayout()
        top.addWidget(self.channel)
        top.addWidget(self.interval)
        top.addWidget(add)
        self.host = QWidget()
        self.vbox = QVBoxLayout(self.host)
        self.vbox.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.host)
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
        names = self.model.readable_channels() + self.model.raw_channels()
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
            tile.title.setText("<b>%s</b> <small>every %.3g s</small>" % (m["channel"], m["interval"]))

    def _tile(self, name: str) -> MonitorTile:
        t = self.tiles.get(name)
        if t is None:
            t = MonitorTile(name, self._remove)
            t.rm.setEnabled(not self.locked)
            self.tiles[name] = t
            self.vbox.insertWidget(self.vbox.count() - 1, t)
        return t

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
        for t in self.tiles.values():
            try:
                t.redraw()
            except Exception as exc:
                t.err.setText("display error: %s" % exc)
