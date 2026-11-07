"""System-tray indicator for a running backend.

Started automatically by ``python -m easymeasure.backend`` (disable with
``--no-tray``).  It runs as a separate process and talks to the backend like any
other client, so the tray can never affect the backend; it exits on its own when
the backend goes away.

The icon colour shows the state (green idle, blue running, orange paused, red
error/aborting, gray not reachable); the tooltip shows the current run and its
progress.  Menu: open the GUI, open the web page (starting remote access if it is off),
pause / resume / abort, open the data folder or the log, shut the backend down.  A notification pops up when a run ends.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from .common import paths

COLORS = {"idle": "#2e9d4a", "running": "#2f7fe0", "paused": "#e39a1b", "aborting": "#d33c3c",
          "error": "#d33c3c", "offline": "#8a8a8a"}


def make_icon(state: str, size: int = 64) -> QIcon:
    """A rounded square in the state colour with a white sine trace."""
    icon = QIcon()
    for s in (16, 24, 32, 48, size):
        pm = QPixmap(s, s)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor(COLORS.get(state, COLORS["offline"]))))
        p.setPen(Qt.NoPen)
        r = s * 0.18
        p.drawRoundedRect(QRectF(0.5, 0.5, s - 1, s - 1), r, r)
        path = QPainterPath()
        import math
        n = 24
        for i in range(n + 1):
            x = s * (0.14 + 0.72 * i / n)
            y = s * (0.5 - 0.26 * math.sin(2 * math.pi * i / n))
            if i == 0:
                path.moveTo(QPointF(x, y))
            else:
                path.lineTo(QPointF(x, y))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor("white"), max(1.5, s / 12), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)
        p.end()
        icon.addPixmap(pm)
    return icon


def _gui_command() -> list:
    exe = Path(sys.executable)
    if os.name == "nt":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            exe = w
    return [str(exe), "-m", "easymeasure.frontend"]


def _detached_popen(cmd: list) -> None:
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True)
    if os.name == "nt":
        kw["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)


class TrayController:
    """State, tooltip and menu of the tray icon (testable without a system tray)."""

    OFFLINE_EXIT_S = 20.0

    def __init__(self, conn, tray: Optional[QSystemTrayIcon] = None):
        self.conn = conn
        self.tray = tray
        self.state = "offline"
        self.info = None
        self.queue = {}
        self.run = None
        self.progress = None
        self.settings = {}
        self.last_seen = time.monotonic()
        self.ever_connected = False
        self.menu = QMenu()
        self.title_act = self.menu.addAction("easyMeasure backend")
        self.title_act.setEnabled(False)
        self.status_act = self.menu.addAction("connecting …")
        self.status_act.setEnabled(False)
        self.menu.addSeparator()
        self.menu.addAction("Open GUI", self.open_gui)
        self.browser_act = self.menu.addAction("Open in browser", self.open_browser)
        self.browser_act.setToolTip("the web page of remote access on this computer")
        self.menu.addSeparator()
        self.pause_act = self.menu.addAction("Pause", lambda: self.conn.request("pause"))
        self.resume_act = self.menu.addAction("Resume", lambda: self.conn.request("resume"))
        self.abort_act = self.menu.addAction("Abort current item", lambda: self.conn.request("abort"))
        self.menu.addSeparator()
        self.menu.addAction("Open data folder", self.open_data_folder)
        self.menu.addAction("Open log file", self.open_log)
        self.menu.addSeparator()
        self.menu.addAction("Shut down backend…", self.shutdown)
        self.menu.addAction("Hide this icon", self.quit)
        if tray is not None:
            tray.setContextMenu(self.menu)
            tray.activated.connect(self._activated)
        conn.connected.connect(self._on_connected)
        conn.disconnected.connect(lambda: self._set_state("offline"))
        conn.event.connect(self._on_event)
        self.timer = QTimer()
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._update()

    # ---- backend -------------------------------------------------------------
    def _on_connected(self, welcome: dict) -> None:
        self.ever_connected = True
        self.last_seen = time.monotonic()
        self.info = welcome
        self.conn.request("get_state", on_ok=self._got_state)

    def _got_state(self, st) -> None:
        if not isinstance(st, dict):
            return
        self.settings = st.get("settings") or {}
        self.queue = st.get("queue") or {}
        cur = st.get("current_run")
        if cur:
            self.run = cur
            self.progress = cur.get("progress")
        self._set_state(self.queue.get("state", "idle"))

    def _on_event(self, kind: str, data) -> None:
        self.last_seen = time.monotonic()
        if kind == "queue" and isinstance(data, dict):
            self.queue = data
            self._set_state(data.get("state", "idle"))
        elif kind == "sweep_start" and isinstance(data, dict):
            hdr = data.get("header") or {}
            self.run = {"run": data.get("run"), "name": hdr.get("name", "")}
            self.progress = None
            self._update()
        elif kind in ("sweep_point", "sweep_progress") and isinstance(data, dict):
            self.progress = data.get("progress")
        elif kind == "sweep_end" and isinstance(data, dict):
            status = data.get("status", "")
            name = (self.run or {}).get("name", "")
            msg = "Run %s %s %s" % (data.get("run"), name, status)
            if data.get("error"):
                msg += "\n" + str(data["error"])[:200]
            icon = QSystemTrayIcon.Information if status in ("finished", "terminated") else QSystemTrayIcon.Warning
            if self.tray is not None:
                self.tray.showMessage("easyMeasure", msg, icon, 8000)
            self.run = None
            self.progress = None
            self._update()

    def _tick(self) -> None:
        if self.conn.is_connected:
            self.conn.request("ping", on_ok=lambda _: setattr(self, "last_seen", time.monotonic()))
            self._update()
            return
        # backend gone for good (or never came up): leave quietly
        limit = self.OFFLINE_EXIT_S if self.ever_connected else 3 * self.OFFLINE_EXIT_S
        if time.monotonic() - self.last_seen > limit:
            self.quit()

    def _set_state(self, state: str) -> None:
        self.state = state if state in COLORS else "idle"
        self._update()

    # ---- presentation ------------------------------------------------------------
    def status_text(self) -> str:
        if self.state == "offline":
            return "backend not reachable"
        txt = "Queue: %s" % self.state
        if self.run:
            txt += " — run %s %s" % (self.run.get("run", "?"), self.run.get("name", ""))
            p = self.progress or {}
            if p.get("total"):
                txt += " (%d%%)" % int(100 * p.get("done", 0) / max(1, p["total"]))
        n = len(self.queue.get("queued", []) or [])
        if n:
            txt += ", %d queued" % n
        return txt

    def tooltip(self) -> str:
        head = "easyMeasure backend"
        if self.info:
            head += " (pid %s)" % self.info.get("pid")
        return head + "\n" + self.status_text()

    def _update(self) -> None:
        self.status_act.setText(self.status_text())
        if self.info:
            self.title_act.setText("easyMeasure backend — pid %s" % self.info.get("pid"))
        busy = self.state in ("running", "paused", "aborting")
        self.pause_act.setEnabled(self.state == "running")
        self.resume_act.setEnabled(self.state == "paused")
        self.abort_act.setEnabled(busy)
        self.browser_act.setEnabled(self.state != "offline")
        if self.tray is not None:
            self.tray.setIcon(make_icon(self.state))
            self.tray.setToolTip(self.tooltip())

    # ---- actions -----------------------------------------------------------------
    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self.open_gui()

    def open_gui(self) -> None:
        try:
            _detached_popen(_gui_command())
        except Exception as exc:
            QMessageBox.warning(None, "easyMeasure", "Cannot start the GUI: %s" % exc)

    # ---- web page ---------------------------------------------------------------
    @staticmethod
    def browser_url(status: dict) -> str:
        """Address of the web page from this computer.  With the self-signed
        certificate: 127.0.0.1 (in the certificate; the gateway listens on IPv4).
        With an own certificate: the computer's name, which that certificate is for."""
        host = str(status.get("host") or "")
        urls = status.get("urls") or []
        if status.get("self_signed") is False and urls and host in ("", "0.0.0.0"):
            return urls[0].rstrip("/") + "/"
        if host in ("", "0.0.0.0", "::", "localhost", "::1"):
            host = "127.0.0.1"
        elif ":" in host:
            host = "[%s]" % host
        return "https://%s:%s/" % (host, status.get("port"))

    def _ask(self, text: str) -> bool:                       # replaced in tests
        return QMessageBox.question(None, "easyMeasure", text) == QMessageBox.Yes

    def _warn(self, text: str) -> None:
        QMessageBox.warning(None, "easyMeasure", text)

    def _open_url(self, url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    def open_browser(self) -> None:
        """Open the web page; offers to switch remote access on when it is off."""
        self.conn.request("remote_status", on_ok=self._browser_status,
                          on_err=lambda e: self._warn("Cannot ask the backend: %s" % e))

    def _browser_status(self, st) -> None:
        if not isinstance(st, dict):
            return
        if not st.get("users") and not self._ask(
                "There are no remote user accounts yet, so the web page cannot log in.\n"
                "Add one in the GUI (Backend \u2192 Remote access\u2026).\n\nOpen the page anyway?"):
            return
        if st.get("running"):
            self._open_url(self.browser_url(st))
            return
        where = "this computer only" if str(st.get("host")) in ("127.0.0.1", "localhost", "::1") \
            else "the network (port %s)" % st.get("port")
        if not self._ask("Remote access (the web page) is off.\nStart it now? It will be "
                         "reachable from %s until switched off again." % where):
            return
        self.conn.request("remote_start", on_ok=lambda r: self._wait_gateway(r, time.monotonic()),
                          on_err=lambda e: self._warn("Cannot start remote access: %s" % e))

    def _wait_gateway(self, st, t0: float) -> None:
        if isinstance(st, dict) and st.get("running"):
            self._open_url(self.browser_url(st))
        elif time.monotonic() - t0 > 20:
            self._warn("Remote access did not come up; see its log:\n%s"
                       % (st or {}).get("log", "remote.log"))
        else:
            QTimer.singleShot(400, lambda: self.conn.request(
                "remote_status", on_ok=lambda r: self._wait_gateway(r, t0),
                on_err=lambda e: self._warn("Cannot ask the backend: %s" % e)))

    def open_data_folder(self) -> None:
        d = self.settings.get("data_dir") or str(paths.default_data_dir())
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    def open_log(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.log_dir() / "backend.log")))

    def shutdown(self) -> None:
        busy = self.state in ("running", "paused", "aborting")
        text = "Shut down the easyMeasure backend? Instruments will be disconnected."
        if busy:
            text = "A measurement is running.\nAbort it and shut down the backend?"
        if QMessageBox.question(None, "easyMeasure", text) != QMessageBox.Yes:
            return
        if busy:
            self.conn.request("abort_all")
        self.conn.request("shutdown", {"force": busy},
                          on_err=lambda e: QMessageBox.warning(None, "easyMeasure", e))

    def quit(self) -> None:
        if self.tray is not None:
            self.tray.hide()
        self.conn.stop()
        QApplication.quit()


def _tray_log(msg: str) -> None:
    try:
        with open(paths.log_dir() / "tray.log", "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="easymeasure-tray")
    ap.add_argument("--home", default=None)
    args = ap.parse_args(argv)
    if args.home:
        os.environ["EASYMEASURE_HOME"] = args.home
    import faulthandler
    try:
        _fh = open(paths.log_dir() / "tray.log", "a", encoding="utf-8")
        faulthandler.enable(_fh)
    except OSError:
        pass

    def _excepthook(t, v, tb):
        import traceback
        _tray_log("unhandled error:\n" + "".join(traceback.format_exception(t, v, tb)))
    sys.excepthook = _excepthook
    if os.name == "nt":
        try:   # own identity for notifications (instead of "Python")
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("easyMeasure.Backend")
        except Exception:
            pass
    from .frontend.display import prepare_environment
    prepare_environment()              # the current UI scale (not one inherited from a parent)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("easyMeasure backend")
    app.setApplicationDisplayName("easyMeasure backend")
    app.setQuitOnLastWindowClosed(False)
    # one tray icon per backend state directory
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    import hashlib
    key = "easymeasure-tray-" + hashlib.sha1(str(paths.home_dir()).encode()).hexdigest()[:12]
    probe = QLocalSocket()
    probe.connectToServer(key)
    if probe.waitForConnected(300):
        _tray_log("another tray icon is already running; exiting")
        return 0
    QLocalServer.removeServer(key)
    lock = QLocalServer()
    lock.listen(key)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        _tray_log("no system tray available on this desktop; exiting")
        return 0
    from .frontend.connection import BackendConnection
    from .backend.server import DEFAULT_EVENTS
    conn = BackendConnection(client_name="tray", events=list(DEFAULT_EVENTS) + ["sweep_progress"])
    tray = QSystemTrayIcon()
    ctl = TrayController(conn, tray)
    tray.show()
    _tray_log("tray icon shown (pid %d)" % os.getpid())
    first = {"done": False}

    def announce(_welcome):
        if not first["done"]:
            first["done"] = True
            # visible even when Windows hides the icon in the overflow area
            tray.showMessage("easyMeasure", "Backend is running (pid %s). Its icon is in the "
                             "notification area; if hidden, click ^ on the taskbar."
                             % _welcome.get("pid"), make_icon("idle"), 6000)
    conn.connected.connect(announce)
    conn.start()
    app._tray_controller = ctl
    app._tray_lock = lock
    return app.exec()


def spawn(home: Optional[str] = None, log_path: Optional[Path] = None):
    """Start the tray helper for this backend.

    Returns ``(Popen or None, reason)``.  The helper's own output goes to
    *log_path* so problems are never silent."""
    if os.environ.get("EASYMEASURE_NO_TRAY"):
        return None, "disabled by EASYMEASURE_NO_TRAY"
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or
                                                 os.environ.get("WAYLAND_DISPLAY")):
        return None, "no desktop session"
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return None, "PySide6 not installed"
    exe = Path(sys.executable)
    if os.name == "nt" and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    cmd = [str(exe), "-m", "easymeasure.tray"]
    env = dict(os.environ)
    if home:
        env["EASYMEASURE_HOME"] = home
    if env.get("QT_QPA_PLATFORM") == "offscreen":
        env.pop("QT_QPA_PLATFORM")
    pkg_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.DEVNULL
    if log_path is not None:
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            out = open(log_path, "a", encoding="utf-8")
            out.write("%s starting tray helper: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                         " ".join(cmd)))
            out.flush()
        except OSError:
            out = subprocess.DEVNULL
    kw = dict(stdin=subprocess.DEVNULL, stdout=out, stderr=out, close_fds=True, env=env)
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        return subprocess.Popen(cmd, **kw), "started"
    except Exception as exc:
        return None, "cannot start tray helper: %s" % exc
    finally:
        if out is not subprocess.DEVNULL:
            try:
                out.close()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
