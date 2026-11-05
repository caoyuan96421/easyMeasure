"""Main window of the easyMeasure GUI."""

from __future__ import annotations

import os
import time
from typing import Optional

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QDockWidget, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton, QSplitter,
                               QScrollArea, QSpinBox, QTabBar, QTabWidget, QToolBar, QVBoxLayout, QWidget)

from .. import __version__
from .connection import BackendConnection
from .model import BackendModel
from .panels.data_panel import FileDataView, LiveDataView
from .panels.log_panel import LogPanel
from .panels.manual_panel import ManualPanel
from .panels.monitor_panel import MonitorPanel
from .panels.queue_panel import QueuePanel
from .panels.setup_panel import SetupPanel
from .panels.sweep_editor import RunningSweepView, SweepEditor
from .plotting import colormaps
from . import display
from .widgets import install_resize_friendly_defaults, shrinkable_label
from .workers import run_in_background


class SettingsDialog(QDialog):
    """Display settings (local to this computer) + backend settings (if connected)."""

    def __init__(self, settings: Optional[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.backend_settings = settings
        lay = QVBoxLayout(self)

        # ---- display -------------------------------------------------------
        disp = QGroupBox("Display (this computer)")
        df = QFormLayout(disp)
        self._orig_pt = display.font_pt()
        self.font_pt = QDoubleSpinBox()
        self.font_pt.setRange(0, 32)
        self.font_pt.setDecimals(1)
        self.font_pt.setSingleStep(0.5)
        self.font_pt.setSpecialValueText("system default")
        self.font_pt.setSuffix(" pt")
        self.font_pt.setValue(self._orig_pt)
        self.font_pt.setToolTip("font size of all widgets and plot axes, applied immediately")
        self.font_pt.valueChanged.connect(display.apply_font)
        self.scale = QSpinBox()
        self.scale.setRange(50, 300)
        self.scale.setSingleStep(10)
        self.scale.setSuffix(" %")
        self.scale.setValue(display.ui_scale_percent())
        self.scale.setToolTip("scales everything (widgets, icons, plots); takes effect after "
                              "restarting the GUI")
        note = shrinkable_label(QLabel("Font size applies immediately. The overall scale takes "
                                       "effect when the GUI is restarted (the backend keeps running)."))
        note.setStyleSheet("color: gray")
        df.addRow("Font size", self.font_pt)
        df.addRow("Overall scale", self.scale)
        df.addRow(note)
        lay.addWidget(disp)

        # ---- backend -------------------------------------------------------
        bk = QGroupBox("Backend")
        form = QFormLayout(bk)
        settings = settings or {}
        self.data_dir = QLineEdit(settings.get("data_dir") or "")
        self.ppt = QLineEdit(settings.get("ppt_path") or "")
        browse = QPushButton("…")
        browse.clicked.connect(self._browse_ppt)
        ppt_row = QHBoxLayout()
        ppt_row.addWidget(self.ppt, 1)
        ppt_row.addWidget(browse)
        self.auto_ppt = QCheckBox("append a slide automatically when a sweep ends")
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
        form.addRow("Data directory", self.data_dir)
        form.addRow("PowerPoint file", ppt_row)
        form.addRow("", self.auto_ppt)
        form.addRow("", self.h5)
        form.addRow("fsync interval", self.fsync)
        form.addRow("Driver paths", self.drivers)
        form.addRow("PPT colormap", self.cmap)
        if self.backend_settings is None:
            bk.setEnabled(False)
            bk.setTitle("Backend (not connected)")
        lay.addWidget(bk)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def reject(self):
        display.apply_font(self._orig_pt)          # undo the live preview
        super().reject()

    def save_display(self) -> bool:
        """Persist display settings; returns True if a restart is needed."""
        st = display.settings()
        st.setValue("ui_font_pt", self.font_pt.value())
        restart = self.scale.value() != display.ui_scale_percent()
        st.setValue("ui_scale", self.scale.value())
        return restart

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
        app = QApplication.instance()
        install_resize_friendly_defaults(app)
        display.apply_saved_font()
        self.setWindowTitle("easyMeasure %s" % __version__)
        self.resize(1500, 950)
        self.qsettings = QSettings("easyMeasure", "gui")
        self.conn = conn or BackendConnection(parent=self)
        self.model = BackendModel(self.conn, self)
        self._autostart = autostart_backend
        self._started_backend = False

        # central area: left = control (instruments / sweep editor / queue),
        # right = data (live view + loaded files); monitor dock on the far right
        self.setup_panel = SetupPanel(self.conn, self.model)
        self.editor = SweepEditor(self.conn, self.model)
        self.running = RunningSweepView(self.conn, self.model)
        self.queue_panel = QueuePanel(self.conn, self.model)
        self.live = LiveDataView(self.conn)
        self.left_tabs = QTabWidget()
        self._scrolled = {}
        for w, name in ((self.setup_panel, "Instruments && channels"), (self.editor, "Sweep editor"),
                        (self.queue_panel, "Queue")):
            self.left_tabs.addTab(self._scroll(w), name)
        self.data_tabs = QTabWidget()
        self.data_tabs.setTabsClosable(True)
        self.data_tabs.tabCloseRequested.connect(self._close_tab)
        self.data_tabs.addTab(self.live, "Live data")
        self.data_tabs.tabBar().setTabButton(0, QTabBar.ButtonPosition.RightSide, None)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.left_tabs)
        self.splitter.addWidget(self.data_tabs)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setChildrenCollapsible(False)
        self.setCentralWidget(self.splitter)
        self.queue_panel.rerunRequested.connect(self._rerun)
        self.queue_panel.copyToEditorRequested.connect(self._copy_to_editor)
        self.queue_panel.openDataRequested.connect(self.open_data)
        self.live.copyDefinition.connect(self._copy_to_editor)
        self.running.copyDefinition.connect(self._copy_to_editor)
        self.live.pptRequested.connect(self._ppt_for_view)

        # docks
        self.monitor = MonitorPanel(self.conn, self.model)
        md = QDockWidget("Monitor", self)
        md.setObjectName("monitor_dock")
        md.setWidget(self.monitor)
        self.addDockWidget(Qt.RightDockWidgetArea, md)
        # manual control, directly below the monitor
        self.manual = ManualPanel(self.conn, self.model)
        mand = QDockWidget("Manual", self)
        mand.setObjectName("manual_dock")
        mand.setWidget(self.manual)
        self.addDockWidget(Qt.RightDockWidgetArea, mand)
        self.splitDockWidget(md, mand, Qt.Vertical)
        self.manual_dock = mand
        self.log_panel = LogPanel(self.conn)
        ld = QDockWidget("Log", self)
        ld.setObjectName("log_dock")
        ld.setWidget(self.log_panel)
        self.addDockWidget(Qt.BottomDockWidgetArea, ld)
        self.resizeDocks([md], [380], Qt.Horizontal)
        self.resizeDocks([ld], [170], Qt.Vertical)
        self.resizeDocks([md, self.manual_dock], [420, 320], Qt.Vertical)

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
        self.queue_actions = {}
        for text, cmd in (("▶ Start queue", "queue_start"), ("⏸ Pause", "pause"),
                          ("Resume", "resume"), ("■ Abort", "abort")):
            a = QAction(text, self)
            a.triggered.connect(lambda _=False, c=cmd: self.conn.request(c))
            a.setEnabled(False)                  # follows the queue state
            tb.addAction(a)
            self.queue_actions[cmd] = a
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
                         ("Restart backend…", self.restart_backend),
                         ("Show tray icon", self.show_tray),
                         ("Remote access…", self.remote_access),
                         ("Shut down backend…", self.shutdown_backend)):
            act = QAction(text, self)
            act.triggered.connect(fn)
            b.addAction(act)
        v = self.menuBar().addMenu("&View")
        v.addAction(md.toggleViewAction())
        v.addAction(mand.toggleViewAction())
        v.addAction(ld.toggleViewAction())
        self.running_action = QAction("Running sweep view", self, checkable=True)
        self.running_action.toggled.connect(self._show_running)
        v.addAction(self.running_action)
        self.running_action.setChecked(self.qsettings.value("show_running", False, type=bool))

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
                self.splitter.restoreState(self.qsettings.value("splitter"))
            except Exception:
                pass
        else:
            QTimer.singleShot(0, lambda: self.splitter.setSizes(
                [int(self.splitter.width() * 0.42), int(self.splitter.width() * 0.58)]))
        self.conn.start()
        if autostart_backend:
            QTimer.singleShot(1500, self._autostart_if_needed)

    # --------------------------------------------------------------- backend --
    def _autostart_if_needed(self):
        if not self.conn.is_connected:
            self.start_backend()

    def start_backend(self, force: bool = False):
        if self.conn.is_connected and not force:
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
        from .. import CODE_STAMP
        stamp = welcome.get("code_stamp")
        if stamp is None or float(stamp) < CODE_STAMP - 1:
            # the backend process was started before the code was updated
            self.conn_label.setText(" connected — backend runs OLDER code: Backend → Restart ")
            self.conn_label.setStyleSheet("color: #e39a1b")
            self._message("warning", "the running backend uses older code than this GUI; "
                                     "use Backend → Restart backend when no sweep is running")

    def remote_access(self):
        if not self.conn.is_connected:
            QMessageBox.information(self, "Remote access", "Connect to a backend first.")
            return
        from .remote_dialog import RemoteAccessDialog
        RemoteAccessDialog(self.conn, self).exec()

    def show_tray(self):
        self.conn.request("start_tray", on_ok=lambda r: self._message(
            "info", "tray icon running" if r.get("running") else "tray icon: %s" % r.get("reason")),
            on_err=lambda e: self._message("error", "this backend cannot show a tray icon "
                                                    "(restart it to update): %s" % e))

    def restart_backend(self):
        """Shut the backend down and start a fresh one (setup restored)."""
        if QMessageBox.question(self, "Restart backend",
                                "Restart the backend? Instruments are reconnected and the current "
                                "setup is restored. Not possible while a sweep is running.") \
                != QMessageBox.Yes:
            return

        def stopped(_):
            self._message("info", "backend stopping, starting a new one…")
            wait = QTimer(self)
            t0 = time.monotonic()

            def check():
                from ..backend.server import backend_alive, read_runtime_file
                info = read_runtime_file()
                if (not info or not backend_alive(info, 0.2)) or time.monotonic() - t0 > 15:
                    wait.stop()
                    self.start_backend(force=True)
            wait.timeout.connect(check)
            wait.start(300)
        self.conn.request("shutdown", {"force": False}, on_ok=stopped,
                          on_err=lambda e: QMessageBox.warning(self, "Restart backend", e))

    def _on_disconnected(self):
        self._message("warning", "lost connection to backend – retrying")
        self.start_action.setVisible(True)
        from .panels.queue_panel import apply_queue_controls
        apply_queue_controls(self.queue_actions, {})          # nothing to control now
        apply_queue_controls(self.queue_panel.controls, {})

    def _message(self, level, text):
        self.statusBar().showMessage(("⚠ " if level in ("error", "warning") else "") + str(text), 8000)

    def _queue_changed(self, q):
        self.state_label.setText("queue: %s  " % q.get("state", "?"))
        from .panels.queue_panel import apply_queue_controls
        apply_queue_controls(self.queue_actions, q)

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
            self.data_tabs.setCurrentWidget(self.live)

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
        i = self.data_tabs.addTab(view, os.path.basename(path))
        self.data_tabs.setTabToolTip(i, path)
        self.data_tabs.setCurrentIndex(i)

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
        self.left_tabs.setCurrentWidget(self.left_widget(self.editor))

    def _close_tab(self, i):
        w = self.data_tabs.widget(i)
        if w is self.live:
            return
        self.data_tabs.removeTab(i)
        w.deleteLater()

    def _scroll(self, w):
        """Wrap a panel so the window can be made smaller than the panel's content
        (scroll bars appear instead of the window refusing to shrink)."""
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QScrollArea.NoFrame)
        sa.setWidget(w)
        sa.setMinimumSize(60, 45)
        self._scrolled[w] = sa
        return sa

    def left_widget(self, w):
        return self._scrolled.get(w, w)

    def _show_running(self, on: bool):
        """The read-only 'Running sweep' view is optional (View menu)."""
        holder = self._scrolled.get(self.running) or self._scroll(self.running)
        idx = self.left_tabs.indexOf(holder)
        if on and idx < 0:
            self.left_tabs.insertTab(2, holder, "Running sweep")
            self.running.refresh()
        elif not on and idx >= 0:
            self.left_tabs.removeTab(idx)
        self.qsettings.setValue("show_running", on)

    def _rerun(self, item: dict):
        """Queue a copy of a finished/failed/aborted item from the history."""
        kind = item.get("kind")
        if kind == "sweep" and item.get("definition"):
            new = {"kind": "sweep", "definition": item["definition"]}
        elif kind == "script" and item.get("code") is not None:
            new = {"kind": "script", "code": item["code"], "name": item.get("name", "script")}
        else:
            self._message("error", "this history item cannot be rerun")
            return
        self.conn.request("queue_add", {"item": new, "start": True},
                          on_ok=lambda i: self._message("info", "rerun queued as item #%s" % i),
                          on_err=lambda e: QMessageBox.warning(self, "Rerun", e))

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
        def open_dialog(settings):
            dlg = SettingsDialog(settings, self)
            if not dlg.exec():
                return
            if dlg.save_display():
                QMessageBox.information(self, "Settings", "The new overall scale takes effect when "
                                        "the GUI is restarted. The backend keeps running.")
            if settings is None:
                return
            vals = dlg.values()
            if vals.get("data_dir") is None:
                vals.pop("data_dir")
            self.qsettings.setValue("ppt_path", vals.get("ppt_path", ""))
            self.conn.request("set_settings", vals,
                              on_ok=lambda _: self._message("info", "settings saved"),
                              on_err=lambda e: QMessageBox.warning(self, "Settings", e))
        if self.conn.is_connected:
            self.conn.request("get_settings", on_ok=open_dialog,
                              on_err=lambda e: open_dialog(None))
        else:
            open_dialog(None)

    def closeEvent(self, ev):
        # Only the GUI goes away; the backend (and any running sweep) is untouched.
        self.qsettings.setValue("geometry", self.saveGeometry())
        self.qsettings.setValue("windowState", self.saveState())
        self.qsettings.setValue("splitter", self.splitter.saveState())
        self.conn.stop()
        super().closeEvent(ev)
