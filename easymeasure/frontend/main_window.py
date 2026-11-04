"""Main window of the easyMeasure GUI."""

from __future__ import annotations

import os
import time
from typing import Optional

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QDockWidget, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton,
                               QTabBar, QTabWidget, QToolBar, QWidget)

from .. import __version__
from .connection import BackendConnection
from .model import BackendModel
from .panels.data_panel import FileDataView, LiveDataView
from .panels.log_panel import LogPanel
from .panels.monitor_panel import MonitorPanel
from .panels.queue_panel import QueuePanel
from .panels.setup_panel import SetupPanel
from .panels.sweep_editor import RunningSweepView, SweepEditor
from .plotting import colormaps
from .workers import run_in_background


class SettingsDialog(QDialog):
    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Backend settings")
        self.data_dir = QLineEdit(settings.get("data_dir") or "")
        self.ppt = QLineEdit(settings.get("ppt_path") or "")
        browse = QPushButton("…")
        browse.clicked.connect(self._browse_ppt)
        ppt_row = QHBoxLayout()
        ppt_row.addWidget(self.ppt, 1)
        ppt_row.addWidget(browse)
        self.auto_ppt = QCheckBox("append a slide automatically when a sweep ends (also on error/abort)")
        self.auto_ppt.setChecked(bool(settings.get("auto_ppt", True)))
        self.h5 = QCheckBox("export an HDF5 copy of every run")
        self.h5.setChecked(bool(settings.get("export_hdf5", True)))
        self.fsync = QDoubleSpinBox()
        self.fsync.setRange(0.0, 60.0)
        self.fsync.setDecimals(2)
        self.fsync.setValue(float(settings.get("fsync_interval", 0.5)))
        self.fsync.setSuffix(" s")
        self.fsync.setToolTip("maximum data loss window on power failure")
        self.drivers = QLineEdit(os.pathsep.join(settings.get("driver_paths") or []))
        self.drivers.setPlaceholderText("extra driver directories, separated by '%s'" % os.pathsep)
        self.cmap = QLineEdit(settings.get("ppt_colormap", "viridis"))
        form = QFormLayout(self)
        form.addRow("Data directory", self.data_dir)
        form.addRow("PowerPoint file", ppt_row)
        form.addRow("", self.auto_ppt)
        form.addRow("", self.h5)
        form.addRow("fsync interval", self.fsync)
        form.addRow("Driver paths", self.drivers)
        form.addRow("PPT colormap", self.cmap)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _browse_ppt(self):
        p, _ = QFileDialog.getSaveFileName(self, "PowerPoint file", self.ppt.text() or "results.pptx",
                                           "PowerPoint (*.pptx)", options=QFileDialog.DontConfirmOverwrite)
        if p:
            self.ppt.setText(p)

    def values(self) -> dict:
        return {"data_dir": self.data_dir.text().strip() or None,
                "ppt_path": self.ppt.text().strip(), "auto_ppt": self.auto_ppt.isChecked(),
                "export_hdf5": self.h5.isChecked(), "fsync_interval": self.fsync.value(),
                "driver_paths": [p for p in self.drivers.text().split(os.pathsep) if p.strip()],
                "ppt_colormap": self.cmap.text().strip() or "viridis"}


class MainWindow(QMainWindow):
    def __init__(self, conn: Optional[BackendConnection] = None, autostart_backend: bool = False):
        super().__init__()
        self.setWindowTitle("easyMeasure %s" % __version__)
        self.resize(1500, 950)
        self.qsettings = QSettings("easyMeasure", "gui")
        self.conn = conn or BackendConnection(parent=self)
        self.model = BackendModel(self.conn, self)
        self._autostart = autostart_backend
        self._started_backend = False

        # central tabs
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.setup_panel = SetupPanel(self.conn, self.model)
        self.editor = SweepEditor(self.conn, self.model)
        self.running = RunningSweepView(self.conn, self.model)
        self.queue_panel = QueuePanel(self.conn, self.model)
        self.live = LiveDataView(self.conn)
        for w, name in ((self.setup_panel, "Instruments && channels"), (self.editor, "Sweep editor"),
                        (self.running, "Running sweep"), (self.queue_panel, "Queue"),
                        (self.live, "Live data")):
            self.tabs.addTab(w, name)
        self._fixed_tabs = self.tabs.count()
        for i in range(self._fixed_tabs):
            self.tabs.tabBar().setTabButton(i, QTabBar.ButtonPosition.RightSide, None)
        self.setCentralWidget(self.tabs)
        self.live.copyDefinition.connect(self._copy_to_editor)
        self.running.copyDefinition.connect(self._copy_to_editor)
        self.live.pptRequested.connect(self._ppt_for_view)

        # docks
        self.monitor = MonitorPanel(self.conn, self.model)
        md = QDockWidget("Monitor", self)
        md.setObjectName("monitor_dock")
        md.setWidget(self.monitor)
        self.addDockWidget(Qt.RightDockWidgetArea, md)
        self.log_panel = LogPanel(self.conn)
        ld = QDockWidget("Log", self)
        ld.setObjectName("log_dock")
        ld.setWidget(self.log_panel)
        self.addDockWidget(Qt.BottomDockWidgetArea, ld)
        self.resizeDocks([md], [380], Qt.Horizontal)
        self.resizeDocks([ld], [170], Qt.Vertical)

        # toolbar
        tb = QToolBar("main")
        tb.setObjectName("main_toolbar")
        self.addToolBar(tb)
        self.conn_label = QLabel(" not connected ")
        self.start_btn = QPushButton("Start backend")
        self.start_btn.clicked.connect(self.start_backend)
        tb.addWidget(self.conn_label)
        self.start_action = tb.addWidget(self.start_btn)   # toolbar widgets hide via their action
        tb.addSeparator()
        for text, cmd in (("▶ Start queue", "queue_start"), ("⏸ Pause", "pause"),
                          ("Resume", "resume"), ("■ Abort", "abort")):
            a = QAction(text, self)
            a.triggered.connect(lambda _=False, c=cmd: self.conn.request(c))
            tb.addAction(a)
        tb.addSeparator()
        a = QAction("Add slide to PPT", self)
        a.setToolTip("append a slide for the running / last sweep to the configured PPT file")
        a.triggered.connect(lambda: self._ppt_backend(None))
        tb.addAction(a)
        a = QAction("Open data…", self)
        a.triggered.connect(self.open_data)
        tb.addAction(a)

        # menus
        m = self.menuBar().addMenu("&File")
        for text, fn, key in (("Open data file…", self.open_data, QKeySequence.Open),
                              ("Browse recent runs…", self.browse_runs, None),
                              ("Settings…", self.edit_settings, None),
                              ("Quit (backend keeps running)", self.close, QKeySequence.Quit)):
            act = QAction(text, self)
            if key:
                act.setShortcut(key)
            act.triggered.connect(fn)
            m.addAction(act)
        b = self.menuBar().addMenu("&Backend")
        for text, fn in (("Start backend", self.start_backend), ("Reconnect", self._reconnect),
                         ("Shut down backend…", self.shutdown_backend)):
            act = QAction(text, self)
            act.triggered.connect(fn)
            b.addAction(act)
        v = self.menuBar().addMenu("&View")
        v.addAction(md.toggleViewAction())
        v.addAction(ld.toggleViewAction())

        # status bar
        self.state_label = QLabel("")
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.state_label)
        self.statusBar().addPermanentWidget(self.progress)

        self.conn.status.connect(self._conn_status)
        self.conn.connected.connect(self._on_connected)
        self.conn.disconnected.connect(self._on_disconnected)
        self.conn.event.connect(self._on_event)
        self.model.message.connect(self._message)
        self.model.queueChanged.connect(self._queue_changed)
        geo = self.qsettings.value("geometry")
        if geo is not None:
            try:
                self.restoreGeometry(geo)
                self.restoreState(self.qsettings.value("windowState"))
            except Exception:
                pass
        self.conn.start()
        if autostart_backend:
            QTimer.singleShot(1500, self._autostart_if_needed)

    # --------------------------------------------------------------- backend --
    def _autostart_if_needed(self):
        if not self.conn.is_connected:
            self.start_backend()

    def start_backend(self):
        if self.conn.is_connected:
            self._message("info", "backend already running")
            return
        from ..client import start_backend_detached
        try:
            start_backend_detached(["--restore"])
            self._started_backend = True
            self._message("info", "backend starting (detached; it keeps running when the GUI closes)")
        except Exception as exc:
            QMessageBox.critical(self, "Backend", "Cannot start backend: %s" % exc)

    def _reconnect(self):
        self.conn.stop()
        self.conn.start()

    def shutdown_backend(self):
        if QMessageBox.question(self, "Shut down backend",
                                "Shut down the backend? Instruments will be disconnected.") \
                != QMessageBox.Yes:
            return
        self.conn.request("shutdown", {"force": False},
                          on_err=lambda e: QMessageBox.warning(self, "Shutdown", e))

    def _conn_status(self, text):
        self.conn_label.setText(" %s " % text)
        ok = self.conn.is_connected
        self.conn_label.setStyleSheet("color: %s" % ("#3a3" if ok else "#d55"))
        self.start_action.setVisible(not ok)

    def _on_connected(self, welcome):
        self.start_action.setVisible(False)
        self._message("info", "connected to backend %s" % welcome.get("version"))

    def _on_disconnected(self):
        self._message("warning", "lost connection to backend – retrying")
        self.start_action.setVisible(True)

    def _message(self, level, text):
        self.statusBar().showMessage(("⚠ " if level in ("error", "warning") else "") + str(text), 8000)

    def _queue_changed(self, q):
        self.state_label.setText("queue: %s  " % q.get("state", "?"))

    def _on_event(self, kind, data):
        if kind == "sweep_point" and isinstance(data, dict):
            p = data.get("progress") or {}
            if p.get("total"):
                self.progress.setVisible(True)
                self.progress.setMaximum(int(p["total"]))
                self.progress.setValue(int(p["done"]))
        elif kind == "sweep_end":
            self.progress.setVisible(False)
            if isinstance(data, dict):
                self._message("info" if data.get("status") == "finished" else "warning",
                              "run %s %s %s" % (data.get("run"), data.get("status"),
                                                data.get("error") or ""))
        elif kind == "sweep_start":
            self.tabs.setCurrentWidget(self.live) if self.tabs.currentWidget() is self.editor else None

    # ------------------------------------------------------------------ data --
    def open_data(self, path: Optional[str] = None):
        if not path:
            start = self.qsettings.value("last_data_dir", "")
            path, _ = QFileDialog.getOpenFileName(self, "Open data file", start,
                                                  "easyMeasure data (*.emj *.h5);;All (*)")
            if not path:
                return
        self.qsettings.setValue("last_data_dir", os.path.dirname(path))
        view = FileDataView(path)
        view.copyDefinition.connect(self._copy_to_editor)
        view.pptRequested.connect(self._ppt_for_view)
        i = self.tabs.addTab(view, os.path.basename(path))
        self.tabs.setCurrentIndex(i)

    def browse_runs(self):
        def got(runs):
            if not runs:
                self._message("info", "no runs found")
                return
            items = ["%s   (%.1f kB, %s)" % (r["name"], r["size"] / 1024,
                                            time.strftime("%Y-%m-%d %H:%M", time.localtime(r["mtime"])))
                     for r in runs]
            item, ok = QInputDialog.getItem(self, "Recent runs", "Open run:", items, 0, False)
            if ok:
                self.open_data(runs[items.index(item)]["path"])
        self.conn.request("list_runs", {"limit": 300}, on_ok=got,
                          on_err=lambda e: self._message("error", e))

    def _copy_to_editor(self, definition):
        self.editor.load_definition(definition)
        self.tabs.setCurrentWidget(self.editor)

    def _close_tab(self, i):
        if i < self._fixed_tabs:
            return
        w = self.tabs.widget(i)
        self.tabs.removeTab(i)
        w.deleteLater()

    # ------------------------------------------------------------------- PPT --
    def _ppt_backend(self, path: Optional[str]):
        args = {"path": path} if path else {}
        self.conn.request("make_ppt", args, on_ok=lambda p: self._message("info", "slide added to %s" % p),
                          on_err=lambda e: QMessageBox.warning(self, "PowerPoint", e))

    def _ppt_for_view(self, view):
        ds = view.ds
        if ds is None:
            return
        f = ds.header.get("file")
        if isinstance(view, LiveDataView) or not f:
            self._ppt_backend(None)
            return
        if self.conn.is_connected:
            self._ppt_backend(f)
            return
        # offline: render locally in a worker thread
        ppt = self.qsettings.value("ppt_path", "")
        if not ppt:
            ppt, _ = QFileDialog.getSaveFileName(self, "PowerPoint file", "results.pptx",
                                                 "PowerPoint (*.pptx)",
                                                 options=QFileDialog.DontConfirmOverwrite)
            if not ppt:
                return
            self.qsettings.setValue("ppt_path", ppt)
        from ..report.pptx_report import append_run_slide
        run_in_background(lambda: str(append_run_slide(ppt, ds)),
                          lambda p: self._message("info", "slide added to %s" % p),
                          lambda e: QMessageBox.warning(self, "PowerPoint", e))

    # -------------------------------------------------------------- settings --
    def edit_settings(self):
        def got(settings):
            dlg = SettingsDialog(settings, self)
            if dlg.exec():
                vals = dlg.values()
                if vals.get("data_dir") is None:
                    vals.pop("data_dir")
                self.qsettings.setValue("ppt_path", vals.get("ppt_path", ""))
                self.conn.request("set_settings", vals,
                                  on_ok=lambda _: self._message("info", "settings saved"),
                                  on_err=lambda e: QMessageBox.warning(self, "Settings", e))
        self.conn.request("get_settings", on_ok=got,
                          on_err=lambda e: QMessageBox.warning(self, "Settings", e))

    def closeEvent(self, ev):
        # Only the GUI goes away; the backend (and any running sweep) is untouched.
        self.qsettings.setValue("geometry", self.saveGeometry())
        self.qsettings.setValue("windowState", self.saveState())
        self.conn.stop()
        super().closeEvent(ev)
