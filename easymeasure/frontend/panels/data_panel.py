"""Data presentation: live view of the running sweep and viewers for data files."""

from __future__ import annotations

import time
from typing import List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QFileDialog, QGridLayout, QHBoxLayout, QLabel, QMessageBox,
                               QProgressBar, QPushButton, QSpinBox, QSplitter, QTextEdit,
                               QVBoxLayout, QWidget)

from ...backend.storage import DataSet
from ..plotting.ndplot import NDPlotWidget
from ..widgets import FitArea, FlowLayout, shrinkable_label
from ..workers import run_in_background


def _fmt_t(s: Optional[float]) -> str:
    if s is None:
        return "?"
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return "%d:%02d:%02d" % (h, m, sec) if h else "%d:%02d" % (m, sec)


def auto_columns(n_plots: int, area, min_size, spacing: int = 6, max_cols: int = 4) -> int:
    """Columns for *n_plots* in *area* (QSize): among the arrangements in which every
    plot keeps at least *min_size*, the one with the largest plots (judged by the
    shorter of width and 1.5 × height, plots being wider than tall); when none fits,
    the one that overflows least - preferring any that needs no sideways scrolling."""
    if n_plots <= 1:
        return 1
    W, H = max(area.width(), 1), max(area.height(), 1)
    best, best_score, least, least_over = 1, None, 1, None
    for n in range(1, min(max_cols, n_plots) + 1):
        rows = -(-n_plots // n)
        cw = (W - spacing * (n - 1)) / n
        ch = (H - spacing * (rows - 1)) / rows
        hover = max(0.0, min_size.width() - cw) * n
        vover = max(0.0, min_size.height() - ch) * rows
        over = (hover > 0, hover + vover)
        if over == (False, 0.0):
            score = min(cw, 1.5 * ch)
            if best_score is None or score > best_score + 1e-9:
                best, best_score = n, score
        elif least_over is None or over < least_over:
            least, least_over = n, over
    return best if best_score is not None else least


class DataView(QWidget):
    """Shows one dataset with any number of N-D plots."""

    copyDefinition = Signal(dict)
    pptRequested = Signal(object)      # the DataView

    def __init__(self, parent=None, live: bool = False):
        super().__init__(parent)
        self.ds: Optional[DataSet] = None
        self.live = live
        self.plots: List[NDPlotWidget] = []
        self.title = shrinkable_label(QLabel("<b>no data</b>"))
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.progress = QProgressBar()
        self.progress.setVisible(live)
        self.progress.setFormat("%p%")
        self.eta = QLabel("")
        self.add_btn = QPushButton("Add plot")
        self.cols = QSpinBox()
        self.cols.setRange(0, 4)
        self.cols.setValue(0)
        self.cols.setPrefix("columns: ")
        self.cols.setSpecialValueText("columns: auto")
        self.cols.setToolTip("number of plot columns (auto = the arrangement that shows every "
                             "plot largest in the available space)")
        self._eff_cols = None
        self.ppt_btn = QPushButton("Add slide to PPT")
        self.copy_btn = QPushButton("Copy sweep to editor")
        self.info_btn = QPushButton("Details")
        self.info_btn.setCheckable(True)
        self.progress.setMaximumWidth(160)
        bar = FlowLayout()
        self.bar = bar
        bar.addWidget(self.eta)
        bar.addWidget(self.progress)
        bar.addWidget(self.cols)
        bar.addWidget(self.add_btn)
        bar.addWidget(self.copy_btn)
        bar.addWidget(self.ppt_btn)
        bar.addWidget(self.info_btn)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setVisible(False)
        # every plot in view; scroll bars only below the plots' minimum sizes
        grid_scroll = FitArea(self.grid_host)
        self.grid_scroll = grid_scroll
        split = QSplitter(Qt.Vertical)
        split.addWidget(grid_scroll)
        split.addWidget(self.details)
        split.setStretchFactor(0, 5)
        lay = QVBoxLayout(self)
        lay.addWidget(self.title)
        lay.addLayout(bar)
        lay.addWidget(split, 1)
        self.add_btn.clicked.connect(lambda: self.add_plot())
        self.cols.valueChanged.connect(self._relayout)
        self.info_btn.toggled.connect(self.details.setVisible)
        self.copy_btn.clicked.connect(self._copy)
        self.ppt_btn.clicked.connect(lambda: self.pptRequested.emit(self))
        self._header_timer = QTimer(self)
        self._header_timer.setInterval(500)
        self._header_timer.timeout.connect(self._update_header)
        self._header_timer.start()

    def set_locked(self, on: bool) -> None:
        """GUI lock: plots and their settings stay usable; what acts on the sweep
        editor or writes files does not."""
        for w in (self.copy_btn, self.ppt_btn):
            w.setEnabled(not on)

    # ------------------------------------------------------------------------
    def set_dataset(self, ds: Optional[DataSet]) -> None:
        old_channels = [p.channel for p in self.plots]
        self.ds = ds
        if ds is None:
            for p in self.plots:
                p.set_dataset(None)
            self._update_header()
            return
        names = ds.record_names()
        if not self.plots:
            # sensible default: one plot per record channel (max 4)
            numeric = [n for n in names if not (n in ds.data and ds.data[n].dtype == object)]
            for n in (numeric[:4] or [None]):
                self.add_plot(n)
        else:
            for p, ch in zip(self.plots, old_channels):
                p.set_dataset(ds, ch if ch in names else None)
        self._update_header()
        self._update_details()

    def add_plot(self, channel: Optional[str] = None) -> NDPlotWidget:
        p = NDPlotWidget(closable=True)
        p.closeRequested.connect(self._remove_plot)
        self.plots.append(p)
        if self.ds is not None:
            names = self.ds.record_names()
            if channel is None and names:
                channel = names[min(len(self.plots) - 1, len(names) - 1)]
            p.set_dataset(self.ds, channel)
        self._relayout()
        return p

    def _remove_plot(self, p) -> None:
        if p in self.plots:
            self.plots.remove(p)
            p.setParent(None)
            p.deleteLater()
            self._relayout()

    def _effective_cols(self) -> int:
        n = self.cols.value()
        if n > 0:
            return n
        return auto_columns(len(self.plots), self.grid_scroll.maximumViewportSize(),
                            self._plot_min_size(), self.grid.spacing())

    def _plot_min_size(self):
        from PySide6.QtCore import QSize
        w = max([p.minimumSizeHint().width() for p in self.plots] + [160])
        h = max([max(p.minimumHeight(), p.minimumSizeHint().height()) for p in self.plots] + [150])
        return QSize(w, h)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.cols.value() == 0 and self._effective_cols() != self._eff_cols:
            self._relayout()

    def _relayout(self, *_):
        while self.grid.count():
            self.grid.takeAt(0)
        n = self._effective_cols()
        self._eff_cols = n
        rows = (len(self.plots) + n - 1) // n
        for i, p in enumerate(self.plots):
            self.grid.addWidget(p, i // n, i % n)
        # every cell the same size (not the first plot taking what the others leave)
        for r in range(max(rows, self.grid.rowCount())):
            self.grid.setRowStretch(r, 1 if r < rows else 0)
        for c in range(max(n, self.grid.columnCount())):
            self.grid.setColumnStretch(c, 1 if c < n else 0)
        self.grid_scroll.fit()

    def data_changed(self) -> None:
        for p in self.plots:
            p.refresh_channel_list()
            p.mark_dirty()

    def _update_header(self) -> None:
        if self.cols.value() == 0 and self.plots and self.isVisible() and \
                self._effective_cols() != self._eff_cols:
            self._relayout()                 # the plots' minimum sizes changed
        ds = self.ds
        if ds is None:
            self.title.setText("<b>no data</b>")
            self.progress.setValue(0)
            self.eta.setText("")
            return
        h = ds.header
        total = ds.total_points or 1
        self.title.setText("<b>Run %s – %s</b> &nbsp; [%s] &nbsp; %d/%d points%s" % (
            h.get("run", "?"), h.get("name", ""), ds.status, ds.points, ds.total_points,
            ("  &nbsp; %d read errors" % sum(ds.errors.values())) if ds.errors else ""))
        if self.live:
            self.progress.setValue(int(100 * ds.points / total))
            # a continued run: the rate since it was continued
            aborts = h.get("aborts") or []
            p0 = aborts[-1].get("points", 0) if aborts else 0
            started = h.get("continued") or h.get("started")
            if started and ds.status == "running" and ds.points > p0:
                el = time.time() - started
                rem = el / (ds.points - p0) * (total - ds.points)
                self.eta.setText("elapsed %s, remaining ~%s" % (_fmt_t(el), _fmt_t(rem)))
            elif ds.status != "running":
                self.eta.setText("")

    def _update_details(self) -> None:
        ds = self.ds
        if ds is None:
            return
        import json
        from ...common.codec import to_jsonable
        d = ds.header.get("definition", {})
        lines = ["file: %s" % ds.header.get("file", "(live)"),
                 "shape: %s" % ds.shape, "status: %s" % ds.status]
        if ds.pre:
            lines.append("pre: %s" % {k: (v if not hasattr(v, "shape") else "array%s" % (v.shape,))
                                      for k, v in ds.pre.items()})
        if ds.post:
            lines.append("post: %s" % {k: (v if not hasattr(v, "shape") else "array%s" % (v.shape,))
                                       for k, v in ds.post.items()})
        if ds.footer:
            lines.append("footer: %s" % ds.footer)
        for n, a in enumerate(ds.header.get("aborts") or [], 1):
            lines.append("abort %d: run %s %s%s at %s after %s of %s points; continued %s "
                         "(wait %g s) as run %s" % (
                             n, a.get("run"), a.get("status"),
                             (" (%s)" % a["error"]) if a.get("error") else "",
                             time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a["ended"]))
                             if a.get("ended") else "?", a.get("points"), a.get("total"),
                             time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a["continued"]))
                             if a.get("continued") else "?", a.get("wait") or 0,
                             a.get("continued_as")))
        lines.append("")
        lines.append(json.dumps(to_jsonable(d), indent=2))
        self.details.setPlainText("\n".join(lines))

    def _copy(self) -> None:
        if self.ds is not None:
            d = self.ds.header.get("definition")
            if d:
                self.copyDefinition.emit(d)


class LiveDataView(DataView):
    """Mirrors the dataset of the currently running (or last) sweep."""

    def __init__(self, conn, parent=None):
        super().__init__(parent, live=True)
        self.conn = conn
        self.run: Optional[int] = None
        conn.connected.connect(lambda _: self.resync())
        conn.event.connect(self._on_event)

    def resync(self) -> None:
        self.conn.request("run_snapshot", on_ok=self._got_snapshot)

    def _got_snapshot(self, snap) -> None:
        if not snap:
            return
        try:
            ds = DataSet.from_snapshot(snap)
        except Exception as exc:
            self.title.setText("cannot load snapshot: %s" % exc)
            return
        self.run = ds.header.get("run")
        if snap.get("running"):
            ds.header["status"] = "running"
        self.set_dataset(ds)
        self.data_changed()

    def _on_event(self, kind: str, data) -> None:
        if not isinstance(data, dict) and kind != "resync":
            return
        try:
            if kind == "sweep_start":
                hdr = data["header"]
                hdr["file"] = data.get("path")
                self.run = data["run"]
                self.set_dataset(DataSet(hdr))
            elif kind == "sweep_point":
                if self.ds is None or data.get("run") != self.run:
                    self.resync()
                    return
                self.ds.apply_point(data["k"], data["i"], data.get("v", {}), data.get("e"),
                                    skip=bool(data.get("skip")))
                self.data_changed()
            elif kind == "sweep_phase" and self.ds is not None and data.get("run") == self.run:
                self.ds.apply_phase(data.get("phase"), data.get("v", {}))
                if data.get("phase") == "axes":
                    self.data_changed()
            elif kind == "sweep_end" and self.ds is not None and data.get("run") == self.run:
                self.ds.footer = data.get("footer") or {"status": data.get("status")}
                self.ds.header["status"] = data.get("status")
                self._update_header()
                self._update_details()
                self.data_changed()
            elif kind == "resync":
                self.resync()
        except Exception as exc:
            self.title.setText("live data error: %s (resyncing)" % exc)
            self.resync()


class FileDataView(DataView):
    """A data file loaded from disk (complete or partial)."""

    def __init__(self, path: str, parent=None):
        super().__init__(parent, live=False)
        self.path = path
        self.title.setText("loading %s ..." % path)
        self.reload_btn = QPushButton("Reload")
        self.bar.insertWidget(0, self.reload_btn)
        self.reload_btn.clicked.connect(self.load)
        self.load()

    def load(self) -> None:
        run_in_background(lambda: DataSet.load(self.path), self._loaded,
                          lambda err: self.title.setText("cannot load %s: %s" % (self.path, err)))

    def _loaded(self, ds) -> None:
        self.set_dataset(ds)
        self.data_changed()
