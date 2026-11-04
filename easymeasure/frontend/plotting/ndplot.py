"""N-dimensional data plot widget.

* 1-D data: curve.
* 2-D data: heat map (default) or stacked curves.
* N-D data: heat map / curve along one or two selectable axes, all other axes
  are controlled by sliders ("follow latest" keeps them on the newest data).

Heat maps can be panned / zoomed with the mouse; colour levels and the colour
gradient are adjusted by dragging the histogram on the right.  Redraws are
coalesced by a timer so even very fast sweeps keep the GUI responsive.
"""

from __future__ import annotations

import warnings
from typing import Dict, List, Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout,
                               QLabel, QSizePolicy, QSlider, QVBoxLayout, QWidget)

from ...common import ndview
from . import colormaps

pg.setConfigOptions(antialias=False, imageAxisOrder="col-major")

MODES = ["Auto", "Curve", "Heatmap", "Stacked curves"]
MAX_STACKED = 150


def _edges(c: np.ndarray):
    c = np.asarray(c, dtype=float)
    if c.size == 0:
        return 0.0, 1.0
    if c.size == 1:
        return float(c[0]) - 0.5, float(c[0]) + 0.5
    d = (c[-1] - c[0]) / (c.size - 1)
    if d == 0 or not np.isfinite(d):
        d = 1.0
    return float(c[0]) - d / 2, float(c[-1]) + d / 2


class NDPlotWidget(QWidget):
    closeRequested = Signal(object)

    def __init__(self, parent=None, closable: bool = False):
        super().__init__(parent)
        self.ds = None
        self.channel: Optional[str] = None
        self._axes: List[dict] = []
        self._sliders: Dict[str, QSlider] = {}
        self._slider_labels: Dict[str, QLabel] = {}
        self._dirty = False
        self._auto_levels = True
        self._building = False
        self._last_z = None
        self._last_xy = None
        self._setting_levels = False

        # ---- controls -------------------------------------------------------
        self.channel_box = QComboBox()
        self.channel_box.setMinimumContentsLength(10)
        self.mode_box = QComboBox()
        self.mode_box.addItems(MODES)
        self.x_box = QComboBox()
        self.y_box = QComboBox()
        self.cmap_box = QComboBox()
        self.cmap_box.addItems(colormaps.available())
        self.follow = QCheckBox("follow latest")
        self.follow.setChecked(True)
        self.autolevel = QCheckBox("auto levels")
        self.autolevel.setChecked(True)
        self.offset = QDoubleSpinBox()
        self.offset.setDecimals(6)
        self.offset.setRange(-1e12, 1e12)
        self.offset.setToolTip("vertical offset between stacked curves (0 = automatic)")
        self.offset.setPrefix("offset ")
        top = QHBoxLayout()
        self._labels = {}
        for w, label in ((self.channel_box, "Channel"), (self.mode_box, None), (self.x_box, "X"),
                         (self.y_box, "Y"), (self.cmap_box, None)):
            if label:
                self._labels[label] = QLabel(label)
                top.addWidget(self._labels[label])
            top.addWidget(w)
        self.y_box.setMinimumContentsLength(6)
        top.addWidget(self.offset)
        top.addWidget(self.follow)
        top.addWidget(self.autolevel)
        top.addStretch(1)
        if closable:
            from PySide6.QtWidgets import QToolButton
            close = QToolButton()
            close.setText("✕")
            close.setToolTip("remove this plot")
            close.clicked.connect(lambda: self.closeRequested.emit(self))
            top.addWidget(close)

        # ---- plot area ------------------------------------------------------
        self.glw = pg.GraphicsLayoutWidget()
        self.plot = self.glw.addPlot(row=0, col=0)
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.image = pg.ImageItem()
        self.plot.addItem(self.image)
        self.hist = pg.HistogramLUTItem()
        self.hist.setImageItem(self.image)
        self.glw.addItem(self.hist, row=0, col=1)
        self.hist.sigLevelChangeFinished.connect(self._levels_dragged)
        self.curves: List[pg.PlotDataItem] = []
        self.readout = QLabel(" ")
        self.readout.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.info = QLabel(" ")
        self.info.setStyleSheet("color: gray")

        self.slider_box = QGridLayout()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addLayout(top)
        lay.addWidget(self.glw, 1)
        lay.addLayout(self.slider_box)
        bottom = QHBoxLayout()
        bottom.addWidget(self.readout, 1)
        bottom.addWidget(self.info)
        lay.addLayout(bottom)

        self.channel_box.currentTextChanged.connect(self._channel_changed)
        self.mode_box.currentIndexChanged.connect(self._layout_changed)
        self.x_box.currentIndexChanged.connect(self._layout_changed)
        self.y_box.currentIndexChanged.connect(self._layout_changed)
        self.cmap_box.currentTextChanged.connect(self._cmap_changed)
        self.offset.valueChanged.connect(self.mark_dirty)
        self.autolevel.toggled.connect(self._autolevel_toggled)
        self.plot.scene().sigMouseMoved.connect(self._mouse_moved)
        self._cmap_changed(self.cmap_box.currentText())

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._maybe_refresh)
        self._timer.start()
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ------------------------------------------------------------------ public --
    def set_dataset(self, ds, channel: Optional[str] = None) -> None:
        self.ds = ds
        self._auto_levels = self.autolevel.isChecked()
        names = self._plottable_names()
        self._building = True
        self.channel_box.clear()
        self.channel_box.addItems(names)
        self._building = False
        if channel and channel in names:
            self.channel_box.setCurrentText(channel)
        elif names:
            self.channel_box.setCurrentIndex(0)
        self._channel_changed(self.channel_box.currentText())

    def refresh_channel_list(self) -> None:
        """New channels may appear while a sweep runs (first value defines dtype)."""
        if self.ds is None:
            return
        names = self._plottable_names()
        current = [self.channel_box.itemText(i) for i in range(self.channel_box.count())]
        if names != current:
            keep = self.channel_box.currentText()
            self._building = True
            self.channel_box.clear()
            self.channel_box.addItems(names)
            self._building = False
            if keep in names:
                self.channel_box.setCurrentText(keep)
            else:
                self._channel_changed(self.channel_box.currentText())

    def mark_dirty(self, *_) -> None:
        self._dirty = True

    # --------------------------------------------------------------- internals --
    def _plottable_names(self) -> List[str]:
        if self.ds is None:
            return []
        out = []
        for n in self.ds.record_names():
            arr = self.ds.data.get(n)
            if arr is None or arr.dtype != object:
                out.append(n)
        return out

    def _channel_changed(self, name: str) -> None:
        if self._building:
            return
        self.channel = name or None
        self._rebuild_axes()

    def _rebuild_axes(self) -> None:
        self._building = True
        try:
            self.x_box.clear()
            self.y_box.clear()
            self._axes = []
            if self.ds is not None and self.channel and self.channel in self.ds.data:
                self._axes = ndview.axes_for(self.ds, self.channel)
            elif self.ds is not None and self.channel:
                # no data yet: we only know the sweep axes
                k = self.ds.dim_of.get(self.channel, 0)
                self._axes = [{"id": "d%d" % d, "label": self.ds.sweep_axis(d)[0],
                               "size": self.ds.shape[d], "kind": "sweep"}
                              for d in range(k, self.ds.ndim)]
            for a in self._axes:
                self.x_box.addItem(a["label"], a["id"])
            if len(self._axes) >= 2:
                # a second axis exists: Y can be any axis, or none (curve + sliders)
                for a in self._axes:
                    self.y_box.addItem(a["label"], a["id"])
                self.y_box.insertItem(0, "(none)", None)
            # (1-D data: Y stays empty and is grayed out in _layout_changed)
            if self._axes and self.ds is not None and self.channel in self.ds.data:
                x, y = ndview.default_view(self.ds, self.channel)
                self.x_box.setCurrentIndex(max(0, self.x_box.findData(x)))
                self.y_box.setCurrentIndex(max(0, self.y_box.findData(y)) if y else 0)
            elif len(self._axes) >= 2:
                self.x_box.setCurrentIndex(0)
                self.y_box.setCurrentIndex(2)
            elif self.y_box.count():
                self.y_box.setCurrentIndex(0)
        finally:
            self._building = False
        self._auto_levels = self.autolevel.isChecked()
        self._layout_changed()

    def _selected(self):
        x = self.x_box.currentData()
        y = self.y_box.currentData()
        mode = self.mode_box.currentText()
        if mode == "Curve":
            y = None
        if y == x:
            y = None
        if mode == "Auto":
            mode = "Heatmap" if y is not None else "Curve"
        elif mode in ("Heatmap", "Stacked curves") and y is None:
            mode = "Curve"
        return x, y, mode

    def _layout_changed(self, *_) -> None:
        if self._building:
            return
        x, y, mode = self._selected()
        # rebuild sliders for the remaining axes
        while self.slider_box.count():
            item = self.slider_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._sliders.clear()
        self._slider_labels.clear()
        row = 0
        for a in self._axes:
            if a["id"] in (x, y):
                continue
            lab = QLabel(a["label"])
            s = QSlider(Qt.Horizontal)
            s.setRange(0, max(0, a["size"] - 1))
            s.valueChanged.connect(self._slider_moved)
            val = QLabel("")
            val.setMinimumWidth(110)
            self.slider_box.addWidget(lab, row, 0)
            self.slider_box.addWidget(s, row, 1)
            self.slider_box.addWidget(val, row, 2)
            self._sliders[a["id"]] = s
            self._slider_labels[a["id"]] = val
            row += 1
        # Y is meaningless for 1-D data and in curve mode: gray it out
        y_usable = len(self._axes) >= 2 and self.mode_box.currentText() != "Curve"
        self.y_box.setEnabled(y_usable)
        self._labels["Y"].setEnabled(y_usable)
        self.y_box.setToolTip("" if y_usable else
                              ("the data has a single axis" if len(self._axes) < 2
                               else "not used in curve mode"))
        heat = mode == "Heatmap"
        self.hist.setVisible(heat)
        self.image.setVisible(heat)
        self.offset.setVisible(mode == "Stacked curves")
        self.cmap_box.setVisible(mode != "Curve")
        self.autolevel.setVisible(heat)
        self._auto_levels = self.autolevel.isChecked()
        self._clear_curves()
        self.plot.enableAutoRange()
        self._dirty = True
        self._maybe_refresh(force=True)

    def _slider_moved(self, *_):
        s = self.sender()
        if not self._building and s is not None and (s.hasFocus() or s.isSliderDown()):
            self.follow.setChecked(False)   # the user is browsing: stop following
        self._update_slider_labels()
        self._dirty = True

    def _update_slider_labels(self):
        for aid, s in self._sliders.items():
            a = next((a for a in self._axes if a["id"] == aid), None)
            if a is None:
                continue
            i = s.value()
            coords = a.get("coords")
            if coords is not None and i < len(coords):
                self._slider_labels[aid].setText("%d: %.6g" % (i, coords[i]))
            else:
                self._slider_labels[aid].setText(str(i))

    def _cmap_changed(self, name: str) -> None:
        try:
            cm = colormaps.get(name)
            self.hist.gradient.setColorMap(cm)
        except Exception:
            pass
        self._dirty = True

    def _autolevel_toggled(self, on: bool) -> None:
        self._auto_levels = on
        self._dirty = True

    def _levels_dragged(self, *_):
        # the user tweaked the levels by hand: stop overriding them
        if self._setting_levels:
            return
        if self.autolevel.isChecked():
            self.autolevel.blockSignals(True)
            self.autolevel.setChecked(False)
            self.autolevel.blockSignals(False)
            self._auto_levels = False

    def _clear_curves(self):
        for c in self.curves:
            self.plot.removeItem(c)
        self.curves = []

    def _maybe_refresh(self, force: bool = False) -> None:
        if not (self._dirty or force):
            return
        if not self.isVisible() and not force:
            return
        self._dirty = False
        try:
            self._refresh()
        except Exception as exc:  # never let a bad slice break the GUI
            self.info.setText("cannot plot: %s" % exc)

    def _refresh(self) -> None:
        ds = self.ds
        if ds is None or not self.channel:
            self.info.setText("no data")
            return
        if self.channel not in ds.data:
            self.info.setText("waiting for data of %s" % self.channel)
            return
        if not self._axes or "coords" not in self._axes[0]:
            self._rebuild_axes()
            return
        x, y, mode = self._selected()
        if x is None:
            return
        if self.follow.isChecked():
            latest = ndview.latest_indices(ds, self.channel)
            self._building = True
            for aid, s in self._sliders.items():
                if aid in latest:
                    s.setValue(latest[aid])
            self._building = False
        self._update_slider_labels()
        fixed = {aid: s.value() for aid, s in self._sliders.items()}
        xs, ys, z = ndview.extract(ds, self.channel, x, y, fixed)
        self._last_xy = (xs, ys)
        self._last_z = z
        xa = next(a for a in self._axes if a["id"] == x)
        self.plot.setLabel("bottom", xa.get("name", xa["label"]), units=xa.get("unit") or None)
        unit = (ds.header.get("units") or {}).get(self.channel, "")
        n_ok = int(np.isfinite(z).sum())
        self.info.setText("%s  %s  (%d/%d finite)" % (self.channel, "x".join(map(str, z.shape)),
                                                      n_ok, z.size))
        if mode == "Curve":
            if y is not None:
                z = z[0]
            self.plot.setLabel("left", self.channel, units=unit or None)
            if len(self.curves) != 1:
                self._clear_curves()
                self.curves.append(self.plot.plot([], [], pen=pg.mkPen("y", width=1.5),
                                                  symbol="o", symbolSize=3, symbolBrush="y",
                                                  connect="finite"))
            self.curves[0].setData(xs, z, connect="finite")
            return
        ya = next(a for a in self._axes if a["id"] == y)
        if mode == "Stacked curves":
            self.plot.setLabel("left", self.channel + " (offset by %s)" % ya["label"])
            nrows = min(z.shape[0], MAX_STACKED)
            step = self.offset.value()
            if step == 0:
                finite = z[np.isfinite(z)]
                step = float(np.ptp(finite)) * 0.5 if finite.size else 1.0
                step = step or 1.0
            cols = colormaps.colors(self.cmap_box.currentText(), nrows)
            if len(self.curves) != nrows:
                self._clear_curves()
                for j in range(nrows):
                    self.curves.append(self.plot.plot([], [], pen=pg.mkPen(cols[j], width=1)))
            for j in range(nrows):
                self.curves[j].setData(xs, z[j] + j * step, connect="finite")
            return
        # heat map
        self.plot.setLabel("left", ya.get("name", ya["label"]), units=ya.get("unit") or None)
        img = z
        if xs.size > 1 and xs[-1] < xs[0]:
            xs = xs[::-1]
            img = img[:, ::-1]
        if ys.size > 1 and ys[-1] < ys[0]:
            ys = ys[::-1]
            img = img[::-1, :]
        x0, x1 = _edges(xs)
        y0, y1 = _edges(ys)
        finite = img[np.isfinite(img)]
        levels = None
        if self._auto_levels or self.image.image is None:
            if finite.size:
                lo, hi = (np.percentile(finite, [0.5, 99.5]) if finite.size > 50
                          else (finite.min(), finite.max()))
                if hi <= lo:
                    hi = lo + (abs(lo) * 1e-6 or 1e-12)
                levels = (float(lo), float(hi))
        if not finite.size:
            self.image.clear()
            return
        data = img.T  # col-major: data[x, y]
        self._setting_levels = True
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)   # all-NaN rows are normal
                if levels is not None:
                    self.image.setImage(data, levels=levels, autoLevels=False)
                    self.hist.setLevels(*levels)
                else:
                    self.image.setImage(data, autoLevels=False, levels=self.hist.getLevels())
        finally:
            self._setting_levels = False
        self.image.setRect(QRectF(x0, y0, x1 - x0, y1 - y0))

    def _mouse_moved(self, pos) -> None:
        try:
            if not self.plot.sceneBoundingRect().contains(pos):
                return
            p = self.plot.vb.mapSceneToView(pos)
            txt = "x=%.6g  y=%.6g" % (p.x(), p.y())
            if self._last_z is not None and self._last_xy is not None:
                xs, ys = self._last_xy
                z = self._last_z
                if ys is not None and z.ndim == 2 and self.image.isVisible():
                    i = int(np.argmin(np.abs(xs - p.x())))
                    j = int(np.argmin(np.abs(ys - p.y())))
                    txt += "   z[%d,%d]=%.6g" % (j, i, z[j, i])
            self.readout.setText(txt)
        except Exception:
            pass

    def current_view(self) -> dict:
        x, y, mode = self._selected()
        return {"channel": self.channel, "x": x, "y": y, "mode": mode,
                "fixed": {aid: s.value() for aid, s in self._sliders.items()},
                "cmap": self.cmap_box.currentText()}
