"""Offscreen GUI tests: the frontend against an in-process backend server."""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from easymeasure.backend.server import Server  # noqa: E402
from easymeasure.backend.storage import DataSet  # noqa: E402


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    yield a


def pump(app, seconds, until=None):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.005)
    return until() if until else True


@pytest.fixture
def gui(app, demo, tmp_path):
    from PySide6.QtCore import QSettings
    QSettings("easyMeasure", "gui").clear()      # no state from earlier sessions
    srv = Server(demo, port=0, runtime_file=tmp_path / "rt.json")
    srv.start()
    from easymeasure.frontend.connection import BackendConnection
    from easymeasure.frontend.main_window import MainWindow
    conn = BackendConnection(runtime_file=tmp_path / "rt.json")
    w = MainWindow(conn)
    w.resize(1400, 900)
    w.show()
    assert pump(app, 10, lambda: conn.is_connected and w.model.state.get("instruments"))
    yield w, demo, srv, tmp_path
    w.close()
    pump(app, 0.2)
    srv.stop()


SWEEP = {"name": "gui", "dimensions": [
    {"points": 15, "sweep": [{"channel": "Vg", "start": -0.3, "stop": 0.3}],
     "record": ["I", "spectrum", "XY"]},
    {"points": 4, "sweep": [{"channel": "Vb", "start": -0.2, "stop": 0.2}], "record": ["T"]},
    {"points": 2, "sweep": [{"channel": "detuning", "start": 0, "stop": 0.1}], "record": []}],
    "pre": {"constants": [{"channel": "src.output", "value": "ON"}], "record": ["T"],
            "hook": "log('x')"},
    "post": {"constants": [{"channel": "Vg", "value": 0.0}]}, "metadata": {"comment": "c"}}


def test_editor_roundtrip_and_validation(app, gui):
    w, be, _, _ = gui
    ed = w.editor
    ed.load_definition(SWEEP)
    d = ed.definition()
    assert [x["points"] for x in d["dimensions"]] == [15, 4, 2]
    assert d["dimensions"][0]["record"] == ["I", "spectrum", "XY"]
    assert d["pre"]["constants"] == [{"channel": "src.output", "value": "ON"}]
    assert d["metadata"]["comment"] == "c"
    ed.validate()
    assert pump(app, 5, lambda: "✔" in ed.status.text() or "✘" in ed.status.text())
    assert "120 points" in ed.status.text()
    bad = dict(SWEEP, dimensions=[{"points": 3, "sweep": [{"channel": "Vg", "start": 0, "stop": 9}],
                                   "record": ["I"]}])
    ed.load_definition(bad)
    ed.validate()
    assert pump(app, 5, lambda: "✘" in ed.status.text())
    assert "maximum" in ed.status.text()


def test_live_sweep_reopen_and_file_view(app, gui):
    w, be, srv, tmp = gui
    w.editor.load_definition(SWEEP)
    w.editor._queue(True)
    assert pump(app, 10, lambda: w.live.ds is not None and w.live.ds.points > 5)
    w.data_tabs.setCurrentWidget(w.live)
    pump(app, 0.5)
    # cycle through plot modes on the N-D spectrum data
    p = next(p for p in w.live.plots if p.channel == "spectrum")
    for mode in ("Heatmap", "Stacked curves", "Curve", "Auto"):
        p.mode_box.setCurrentText(mode)
        pump(app, 0.3)
        assert "cannot plot" not in p.info.text(), p.info.text()
    assert len(p._sliders) == 2     # two sweep dims besides the selected x/y
    # the GUI goes away and comes back while the sweep runs
    from easymeasure.frontend.connection import BackendConnection
    from easymeasure.frontend.main_window import MainWindow
    w.close()
    pump(app, 0.3)
    conn2 = BackendConnection(runtime_file=tmp / "rt.json")
    w2 = MainWindow(conn2)
    w2.show()
    try:
        assert pump(app, 10, lambda: w2.live.ds is not None and w2.live.ds.points > 0)
        assert be.queue.wait_idle(60)
        assert pump(app, 10, lambda: w2.live.ds.status == "finished")
        item = be.queue_state()["history"][-1]
        assert item["status"] == "finished"
        ref = DataSet.load(item["path"])
        np.testing.assert_allclose(w2.live.ds.data["I"], ref.data["I"])  # mirror == file
        w2.open_data(item["path"])
        view = w2.data_tabs.currentWidget()
        assert pump(app, 10, lambda: view.ds is not None)
        assert view.ds.points == 120
        w2.running.refresh()
        pump(app, 0.5)
    finally:
        w2.close()
        pump(app, 0.2)


def test_gui_survives_backend_restart(app, gui):
    w, be, srv, tmp = gui
    srv.stop()
    assert pump(app, 5, lambda: not w.conn.is_connected)
    # requests while disconnected fail gracefully (callbacks, no exception)
    errs = []
    w.conn.request("ping", on_err=errs.append)
    assert pump(app, 2, lambda: errs)
    srv2 = Server(be, port=0, runtime_file=tmp / "rt.json")
    srv2.start()
    try:
        assert pump(app, 10, lambda: w.conn.is_connected)
    finally:
        srv2.stop()


def test_monitor_panel_and_manual_control(app, gui):
    w, be, _, _ = gui
    w.monitor.channel.setEditText("Vg")
    w.monitor._add()
    assert pump(app, 5, lambda: "Vg" in w.monitor.tiles and w.monitor.tiles["Vg"].latest is not None)
    sp = w.setup_panel
    sp.m_chan.setEditText("Vg")
    sp.m_value.setText("0.05")
    sp._manual_set()
    assert pump(app, 5, lambda: "←" in sp.m_result.text() or "✘" in sp.m_result.text())
    assert be.read_channel("Vg") == pytest.approx(0.05)
    sp.m_value.setText("7")
    sp._manual_set()
    assert pump(app, 5, lambda: "✘" in sp.m_result.text())


def test_ndplot_standalone_modes(app):
    from easymeasure.frontend.plotting.ndplot import NDPlotWidget
    hdr = {"shape": [5, 4, 3], "dims": [{"sweep": ["a"], "record": ["m", "s"]},
                                        {"sweep": ["b"], "record": []}, {"sweep": ["c"], "record": []}],
           "setpoints": {"0": {"a": np.linspace(1, 0, 5)}, "1": {"b": np.arange(4.0)},
                         "2": {"c": np.arange(3.0)}}}
    ds = DataSet(hdr)
    for i in range(5):
        for j in range(4):
            ds.apply_point(0, (i, j, 0), {"m": np.random.rand(6, 7), "s": float(i * j)})
    w = NDPlotWidget()
    w.show()
    w.set_dataset(ds, "m")
    for mode in ("Auto", "Heatmap", "Stacked curves", "Curve"):
        w.mode_box.setCurrentText(mode)
        for xi in range(w.x_box.count()):
            w.x_box.setCurrentIndex(xi)
            for yi in range(w.y_box.count()):
                w.y_box.setCurrentIndex(yi)
                w._maybe_refresh(force=True)
                assert "cannot plot" not in w.info.text(), (mode, xi, yi, w.info.text())
    w.set_dataset(ds, "s")
    w._maybe_refresh(force=True)
    assert "cannot plot" not in w.info.text()
    w.close()


def test_layout_rerun_and_1d_plot(app, gui):
    w, be, _, _ = gui
    # two panes: control tabs left, data tabs right; running view hidden by default
    assert w.splitter.count() == 2
    assert w.left_tabs.indexOf(w.left_widget(w.setup_panel)) >= 0 and w.left_tabs.indexOf(w.left_widget(w.queue_panel)) >= 0
    assert w.data_tabs.indexOf(w.live) == 0
    assert w.left_tabs.indexOf(w.left_widget(w.running)) == -1
    w.running_action.setChecked(True)
    assert w.left_tabs.indexOf(w.left_widget(w.running)) >= 0
    w.running_action.setChecked(False)
    assert w.left_tabs.indexOf(w.left_widget(w.running)) == -1
    # run a 1-D sweep and rerun it from the history
    d = {"name": "one_d", "dimensions": [{"points": 5, "sweep": [{"channel": "Vg", "start": 0,
                                                                   "stop": 0.1}], "record": ["I"]}]}
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert be.queue.wait_idle(30)
    qp = w.queue_panel
    assert pump(app, 5, lambda: qp.history.rowCount() == 1)
    assert not qp.rerun_btn.isEnabled()
    qp.history.selectRow(0)
    pump(app, 0.1)
    assert qp.rerun_btn.isEnabled() and qp.open_btn.isEnabled()
    qp.rerun_btn.click()
    assert pump(app, 10, lambda: len(be.queue_state()["history"]) == 2 and
                be.queue_state()["state"] == "idle")
    hist = be.queue_state()["history"]
    assert hist[-1]["status"] == "finished" and hist[-1]["name"] == "one_d"
    assert hist[-1]["run"] != hist[0]["run"]
    # 1-D data: Y is grayed out, not "(none)"
    p = w.live.plots[0]
    assert pump(app, 5, lambda: p.channel == "I" and len(p._axes) == 1)
    assert not p.y_box.isEnabled() and p.y_box.count() == 0
    assert "cannot plot" not in p.info.text()
    # open the data file from the history
    qp.history.selectRow(0)
    qp.open_btn.click()
    assert pump(app, 5, lambda: w.data_tabs.count() == 2)


def test_tray_controller(app, demo, tmp_path):
    """The tray indicator follows the backend state (tested without a real tray)."""
    from easymeasure.frontend.connection import BackendConnection
    from easymeasure.tray import TrayController, make_icon
    srv = Server(demo, port=0, runtime_file=tmp_path / "rt.json")
    srv.start()
    conn = BackendConnection(runtime_file=tmp_path / "rt.json")
    ctl = TrayController(conn, tray=None)
    conn.start()
    try:
        assert pump(app, 10, lambda: conn.is_connected and ctl.state == "idle")
        assert "idle" in ctl.tooltip() and "pid" in ctl.tooltip()
        d = {"name": "traytest", "dimensions": [{"points": 60, "sweep": [
            {"channel": "Vg", "start": 0, "stop": 0.1}], "record": ["I"], "pre_delay": 0.02}]}
        demo.queue_add({"kind": "sweep", "definition": d}, start=True)
        assert pump(app, 10, lambda: ctl.state == "running" and "traytest" in ctl.status_text())
        assert ctl.pause_act.isEnabled() and ctl.abort_act.isEnabled()
        assert be_idle(app, demo, ctl)
        assert not make_icon("running").isNull()
        srv.stop()
        assert pump(app, 5, lambda: ctl.state == "offline")
        assert "not reachable" in ctl.status_text()
    finally:
        ctl.timer.stop()
        conn.stop()
        srv.stop()


def test_tray_open_in_browser(app, demo, tmp_path):
    """Tray -> Open in browser: offers to start remote access, waits for it, opens the
    page (which is really served there); warns when there is no user account."""
    import socket
    import ssl
    import urllib.request
    from easymeasure.frontend.connection import BackendConnection
    from easymeasure.remote import config as rconfig
    from easymeasure.tray import TrayController
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cfg = rconfig.load(demo.home)
    cfg.update(host="0.0.0.0", port=port)
    rconfig.save(cfg, demo.home)
    srv = Server(demo, port=0, runtime_file=tmp_path / "rt.json")
    srv.start()
    conn = BackendConnection(runtime_file=tmp_path / "rt.json")
    ctl = TrayController(conn, tray=None)
    asked, opened, warned, answers = [], [], [], []
    ctl._ask = lambda t: asked.append(t) or answers.pop(0)
    ctl._open_url = opened.append
    ctl._warn = warned.append
    conn.start()
    try:
        assert pump(app, 10, lambda: conn.is_connected and ctl.browser_act.isEnabled())
        assert ctl.browser_act.text() == "Open in browser"
        # no account yet: warned first; "no" stops there
        answers[:] = [False]
        ctl.open_browser()
        assert pump(app, 5, lambda: asked)
        assert "no remote user accounts" in asked[0] and not opened
        demo.remote_set_user("tina", "Tray-test-password-1", "viewer")
        # off: asks to start it, then opens the page once the gateway is up
        asked.clear()
        answers[:] = [True]
        ctl.open_browser()
        assert pump(app, 30, lambda: opened or warned), (asked, warned)
        assert not warned and "is off" in asked[0] and "network" in asked[0]
        assert opened == ["https://127.0.0.1:%d/" % port]
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        page = urllib.request.urlopen(opened[0], context=ctx, timeout=10).read().decode()
        assert "easyMeasure" in page
        # running: opens straight away
        asked.clear()
        ctl.open_browser()
        assert pump(app, 5, lambda: len(opened) == 2) and not asked
        # offline: the item is greyed out
        srv.stop()
        assert pump(app, 5, lambda: not ctl.browser_act.isEnabled())
    finally:
        demo.remote_stop(persist=False)
        ctl.timer.stop()
        conn.stop()
        srv.stop()
    assert TrayController.browser_url({"host": "10.0.0.5", "port": 8443}) == "https://10.0.0.5:8443/"
    assert TrayController.browser_url({"host": "0.0.0.0", "port": 8443, "self_signed": False,
                                       "urls": ["https://lab.example.org:8443"]}) == \
        "https://lab.example.org:8443/"


def be_idle(app, demo, ctl):
    assert demo.queue.wait_idle(30)
    return pump(app, 5, lambda: ctl.state == "idle" and ctl.run is None)


def test_gui_lock(app, gui):
    """The lock button greys out every control of instruments / sweep / queue /
    manual / monitors and the backend commands; tabs, plots and their settings stay
    usable; the queue buttons come back in the state the queue is in now; the lock
    survives a restart of the GUI."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QAbstractButton, QComboBox, QLineEdit
    from easymeasure.frontend.connection import BackendConnection
    from easymeasure.frontend.main_window import MainWindow
    w, be, _, tmp = gui
    be.monitor_add("I", 0.2)
    assert pump(app, 5, lambda: "I" in w.monitor.tiles)
    btn = w.lock_btn
    assert w.menuBar().cornerWidget(Qt.TopRightCorner) is btn and "Lock" in btn.text()
    btn.click()
    pump(app, 0.1)
    assert w.locked and "Locked" in btn.text() and w.lock_label.isVisible()
    for panel in (w.setup_panel, w.editor, w.queue_panel, w.manual):
        ctrls = panel.findChildren(QAbstractButton) + panel.findChildren(QComboBox) + \
            panel.findChildren(QLineEdit)
        assert ctrls and not any(c.isEnabled() for c in ctrls), panel
    assert not any(a.isEnabled() for a in w.queue_actions.values())
    acts = {a.text(): a for m in w.menuBar().actions() if m.menu() for a in m.menu().actions()}
    for t in ("Settings…", "Driver wizard…", "Remote access…", "Shut down backend…"):
        assert not acts[t].isEnabled(), t
    for t in ("Open data file…", "Reconnect", "Monitor", "Log"):
        assert acts[t].isEnabled(), t
    assert not w.monitor.add_btn.isEnabled() and not w.monitor.tiles["I"].rm.isEnabled()
    assert w.monitor.tiles["I"].span.isEnabled()                       # a graph setting
    # tabs still switch
    w.left_tabs.setCurrentIndex(2)
    assert w.left_tabs.tabBar().isEnabled() and w.left_tabs.currentIndex() == 2
    # a run started elsewhere (script, other GUI): plots and their settings work
    be.queue_add({"kind": "sweep", "definition": {"name": "locked", "dimensions": [
        {"points": 80, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}], "record": ["I"],
         "pre_delay": 0.02}]}}, start=True)
    assert pump(app, 10, lambda: w.live.plots and be.queue_state()["state"] == "running")
    pl = w.live.plots[0]
    assert pl.isEnabled() and pl.channel_box.isEnabled() and pl.mode_box.isEnabled()
    assert w.live.add_btn.isEnabled() and w.live.cols.isEnabled()
    assert not w.live.copy_btn.isEnabled() and not w.live.ppt_btn.isEnabled()
    assert not any(a.isEnabled() for a in w.queue_actions.values())    # still locked
    w._copy_to_editor({"name": "x", "dimensions": []})                  # signals are refused too
    assert w.editor.name.text() != "x"
    # opened data files come up locked
    w.set_locked(False)
    assert w.queue_actions["pause"].isEnabled() and w.queue_actions["abort"].isEnabled()
    assert not w.queue_actions["resume"].isEnabled()
    assert w.setup_panel.findChildren(QAbstractButton)[0].isEnabled()
    assert w.monitor.add_btn.isEnabled() and w.live.copy_btn.isEnabled()
    w.set_locked(True)
    assert be.queue.wait_idle(30)
    path = be.queue_state()["history"][-1]["path"]
    w.open_data(path)
    view = w.data_tabs.currentWidget()
    assert view is not w.live and not view.copy_btn.isEnabled() and view.reload_btn.isEnabled()
    # remembered by the next GUI
    conn2 = BackendConnection(runtime_file=tmp / "rt.json")
    w2 = MainWindow(conn2)
    try:
        assert w2.locked and w2.lock_btn.isChecked() and not w2.setup_panel.isEnabled()
        w2.lock_btn.click()
        assert not w2.locked and w2.setup_panel.isEnabled()
    finally:
        w2.close()
        conn2.stop()
    w.set_locked(False)


def test_font_size_and_narrow_window(app, gui):
    from easymeasure.frontend import display
    from easymeasure.frontend.main_window import SettingsDialog
    w, be, _, _ = gui
    base = app.font().pointSizeF()
    try:
        display.apply_font(base + 4)
        pump(app, 0.2)
        assert abs(app.font().pointSizeF() - (base + 4)) < 0.01
        # monospace editors follow the size too
        assert w.log_panel.text.font().pointSizeF() > base + 2
        # the settings dialog works offline (display section only) and previews live
        dlg = SettingsDialog(None, w)
        dlg.font_pt.setValue(base + 6)
        assert abs(app.font().pointSizeF() - (base + 6)) < 0.01
        dlg.reject()                       # cancel restores the previous size
        pump(app, 0.1)
    finally:
        display.apply_font(0)
    # the window can be made small without widgets fighting it
    assert w.minimumSizeHint().width() < 700 and w.minimumSizeHint().height() < 600
    w.resize(640, 520)
    pump(app, 0.3)
    assert w.width() <= 700


def test_outdated_backend_warning(app, gui):
    w, be, _, _ = gui
    w._on_connected({"version": "0.1.0", "pid": 1})          # old backend: no code stamp
    assert "OLDER" in w.conn_label.text()
    from easymeasure import CODE_STAMP
    w._on_connected({"version": "0.1.0", "pid": 1, "code_stamp": CODE_STAMP})
    errs = []
    w.conn.request("start_tray", on_ok=errs.append, on_err=errs.append)
    assert pump(app, 5, lambda: errs)
    assert errs[0]["running"] is False             # no desktop in the test environment


def test_manual_panel(app, gui):
    w, be, _, _ = gui
    mp = w.manual
    assert pump(app, 5, lambda: "Vg" in mp.rows)
    # aliases/composites that can be written (no read-only ones) + instrument settings
    assert "detuning" in mp.rows and "I" not in mp.rows
    assert "dmm.nplc" in mp.rows and mp.rows["dmm.nplc"].kind == "config"
    mp.settings.setChecked(False)
    assert not any("." in n for n in mp.rows)
    mp.settings.setChecked(True)
    assert "dmm.nplc" in mp.rows
    vg = mp.rows["Vg"]
    assert vg.slider is not None                   # min and max are known
    assert mp.rows["B"].slider is not None
    # spin box -> ramped write
    vg.editor.setValue(0.3)
    last = lambda: be.channels.last_value("Vg") if be.channels.last_value("Vg") is not None else 99
    assert pump(app, 10, lambda: abs(last() - 0.3) < 1e-9 and not mp._pending.get("Vg"))
    # slider click (not dragging) -> write
    vg.slider.setValue(vg._to_slider(-0.5))
    assert pump(app, 10, lambda: abs(last() + 0.5) < 1e-3 and not mp._pending.get("Vg"))
    # coalescing: many requests while busy -> only the last one is applied at the end
    for v in (0.1, 0.2, 0.25, 0.05):
        mp.request_set("Vg", v)
    assert pump(app, 15, lambda: not mp._pending.get("Vg") and not mp._queued)
    assert be.channels.last_value("Vg") == pytest.approx(0.05)
    # limit violation is reported, not applied
    mp.request_set("Vg", 5.0)
    assert pump(app, 5, lambda: "✘" in mp.status.text())
    assert be.channels.last_value("Vg") == pytest.approx(0.05)
    # expression of the present value
    from easymeasure.common.exprs import Expression
    assert Expression("x + 0.01", allowed_names=["x"]).evaluate({"x": 0.05}) == pytest.approx(0.06)
    # filter
    mp.filter.setText("v")
    assert mp.rows["Vg"].label.isVisibleTo(mp) and not mp.rows["B"].label.isVisibleTo(mp)
    mp.filter.clear()


def test_actions_and_config_in_gui(app, gui):
    w, be, _, _ = gui
    sp = w.setup_panel
    for r in range(sp.inst.rowCount()):
        if sp.inst.item(r, 0).text() == "dmm":
            sp.inst.selectRow(r)
    pump(app, 0.2)
    assert sp.act.count() == 2                    # reset, trigger
    names = [sp.raw.item(r, 0).text() for r in range(sp.raw.rowCount())]
    r = names.index("nplc")
    assert sp.raw.item(r, 1).text() == "⚙"
    sp.raw.selectRow(r)
    pump(app, 0.1)
    assert not sp.mk_alias.isEnabled() and sp.raw_set.isEnabled()
    sp.raw.selectRow(names.index("reading"))
    pump(app, 0.1)
    assert sp.mk_alias.isEnabled() and not sp.raw_set.isEnabled()
    be.write_channel("dmm.nplc", 7)
    sp.act.setCurrentRow([sp.act.item(i).text() for i in range(sp.act.count())].index("reset"))
    sp._run_action()
    assert pump(app, 5, lambda: be.read_channel("dmm.nplc") == 1)
    # aliasing a config channel is not offered
    assert "dmm.nplc" not in w.model.aliasable_channels()
    assert "dmm.nplc" in w.model.config_channels(writable=True)
    # sweep editor: config channels in pre/post lists, actions insertable into hooks
    ed = w.editor
    ed.refresh_channels()
    assert "lockin.sensitivity" in ed.pre.channels
    acts = [ed.pre.action_box.itemText(i) for i in range(ed.pre.action_box.count())]
    assert "dmm.reset" in acts and "lockin.auto_phase" in acts
    ed.pre.action_box.setCurrentText("lockin.auto_phase")
    ed.pre.append_action()
    assert "action('lockin.auto_phase')" in ed.definition()["pre"]["hook"]
    # sweep channel lists never contain config channels
    assert not any(n.startswith("dmm.") for n in w.model.writable_channels())


def test_manual_expression_updates_focused_spinbox(app, gui, monkeypatch):
    """Regression: after 'Set from expression' the (still focused) spin box must show
    the new value."""
    from PySide6.QtWidgets import QInputDialog
    w, be, _, _ = gui
    mp = w.manual
    assert pump(app, 5, lambda: "Vb" in mp.rows)
    row = mp.rows["Vb"]
    be.write_channel("Vb", 0.1)
    row.show_value(0.1, force=True)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("x + 0.15", True)))
    monkeypatch.setattr(row.editor, "hasFocus", lambda: True)     # dialog returns focus here
    row._expression()
    assert row.editor.value() == pytest.approx(0.25)               # shown immediately
    assert pump(app, 10, lambda: not mp._pending.get("Vb"))
    assert be.channels.last_value("Vb") == pytest.approx(0.25)
    assert row.editor.value() == pytest.approx(0.25)
    assert row.slider.value() == row._to_slider(0.25)
    # a background refresh does not overwrite text the user is typing
    row.editor.lineEdit().setText("0.3")
    row.editor.lineEdit().setModified(True)
    row.show_value(0.0, force=True)
    assert row.editor.lineEdit().text().startswith("0.3")


def test_remote_access_dialog(app, gui):
    """Backend → Remote access…: accounts are managed and the gateway switched on/off
    from the GUI (local-only commands)."""
    pytest.importorskip("aiohttp")
    from easymeasure.frontend.remote_dialog import RemoteAccessDialog
    from easymeasure.remote import config as rconfig
    w, demo, srv, tmp = gui
    cfg = rconfig.load(demo.home)
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    cfg.update(host="127.0.0.1", port=s.getsockname()[1])
    s.close()
    rconfig.save(cfg, demo.home)
    dlg = RemoteAccessDialog(w.conn, w)
    dlg.show()
    assert pump(app, 5, lambda: dlg.status.text() == "off")
    done = []
    w.conn.request("remote_set_user", {"name": "alice", "password": "a-long-password-1",
                                       "role": "operator"}, on_ok=done.append)
    assert pump(app, 5, lambda: done)
    dlg.refresh()
    assert pump(app, 5, lambda: dlg.users.rowCount() == 1 and dlg.users.item(0, 1).text() == "operator")
    errs = []
    w.conn.request("remote_set_user", {"name": "bob", "password": "short"}, on_err=errs.append)
    assert pump(app, 5, lambda: errs) and "10 characters" in errs[0]
    dlg.port.setValue(cfg["port"])
    dlg._toggle()                                    # switch on
    assert pump(app, 25, lambda: "running" in dlg.status.text())
    assert dlg.fp.text().count(":") >= 31 and "https://" in dlg.urls.text()
    assert demo.settings["remote_access"] is True    # stays on across backend restarts
    dlg._toggle()                                    # switch off
    assert pump(app, 10, lambda: dlg.status.text() == "off")
    assert demo.settings["remote_access"] is False
    dlg.close()


def test_allowed_values_and_mode_switch_in_gui(app, gui):
    """Settings with allowed values get combo boxes; a mode switch rebuilds the rows."""
    from PySide6.QtWidgets import QComboBox, QLineEdit
    w, be, _, _ = gui
    be.add_instrument("smu", "keithley2400", "SIM::guik24")
    be.define_alias(name="Vsmu", target="smu.voltage")
    mp = w.manual
    assert pump(app, 5, lambda: "smu.mode" in mp.rows and "Vsmu" in mp.rows)
    mode = mp.rows["smu.mode"].editor
    assert isinstance(mode, QComboBox)
    assert [mode.itemText(i) for i in range(mode.count())] == ["Voltage", "Current"]
    assert pump(app, 5, lambda: mode.currentText() == "Voltage")      # read back
    assert isinstance(mp.rows["smu.Icompliance"].editor, QLineEdit)   # number, any magnitude
    out = mp.rows["smu.output"].editor
    assert isinstance(out, QComboBox) and out.count() == 2
    # choose "Current" in the combo: backend switches, the panel follows
    mode.setCurrentIndex(1)
    mode.activated.emit(1)
    assert pump(app, 10, lambda: "smu.Vcompliance" in mp.rows and "smu.Icompliance" not in mp.rows)
    assert be.read_channel("smu.mode") == "Current"
    assert "Vsmu" not in mp.rows                    # voltage is measured now: not settable
    # numeric choices (DMM nplc of the demo setup has none; the 34401A has a list)
    be.add_instrument("hp", "hp34401a", "SIM::guihp")
    assert pump(app, 5, lambda: "hp.nplc" in mp.rows)
    nplc = mp.rows["hp.nplc"].editor
    assert isinstance(nplc, QComboBox) and nplc.itemData(0) == 0.02
    assert pump(app, 5, lambda: nplc.currentIndex() == 3)              # 10 PLC after *RST
    nplc.setCurrentIndex(1)
    nplc.activated.emit(1)
    assert pump(app, 5, lambda: be.read_channel("hp.nplc") == 0.2)
    # setup panel: the raw table tells the allowed values
    sp = w.setup_panel
    for r in range(sp.inst.rowCount()):
        if sp.inst.item(r, 0).text() == "hp":
            sp.inst.selectRow(r)
    pump(app, 0.2)
    names = [sp.raw.item(r, 0).text() for r in range(sp.raw.rowCount())]
    assert names[0] == "Vdc"
    hdr = [sp.raw.horizontalHeaderItem(c).text() for c in range(sp.raw.columnCount())]
    col_allowed, col_last = hdr.index("Allowed values"), hdr.index("Last value")
    assert sp.raw.item(names.index("mode"), col_allowed).text().startswith("vdc | vac")
    assert sp.raw.item(names.index("Vdc"), col_allowed).text() == ""
    # last values: nothing is read just to fill the table; get/set update the cell
    # (the settings were read back by the manual panel, the reading was not)
    assert sp.raw.item(names.index("Vdc"), col_last).text() == ""
    assert sp.raw.item(names.index("nplc"), col_last).text() == "0.2"     # set above
    sp.raw.selectRow(names.index("Vdc"))
    sp._raw_get()
    assert pump(app, 5, lambda: sp.raw.item(names.index("Vdc"), col_last).text().startswith("1.2345"))
    be.write_channel("hp.range", "10 V")           # set elsewhere: shown after the refresh
    w.model.refresh()
    assert pump(app, 5, lambda: sp.raw.item(
        [sp.raw.item(r, 0).text() for r in range(sp.raw.rowCount())].index("range"), col_last).text() == "10 V")
    # sweep editor constants know the choices
    w.editor.refresh_channels()
    assert w.editor.pre.choices["smu.mode"] == ["Voltage", "Current"]
    be.write_channel("smu.mode", "Voltage")


def test_dimension_editor_order_hooks_and_queue_controls(app, gui):
    """Dimension editor in point order with optional actions; queue buttons follow the
    queue state; queue items copy back into the sweep / script editor."""
    w, be, _, _ = gui
    ed = w.editor
    ed.load_definition({"name": "h", "dimensions": [
        {"points": 4, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}], "record": ["I"],
         "pre_delay": 0.01,
         "hooks": {"pre_record": {"enabled": True, "code": "state.setdefault('n', 0)\nstate['n'] += 1"},
                   "post_sweep": {"enabled": False, "code": "kept = 1"}}}]})
    d = ed.dims[0]
    lay = d.layout()
    order = [lay.itemAt(i).widget() for i in range(lay.count()) if lay.itemAt(i).widget() is not None]
    pos = {x: order.index(x) for x in (d.hooks["pre_sweep"], d.sweep_box, d.hooks["post_sweep"],
                                       d.hooks["pre_record"], d.rec_box, d.hooks["post_record"])}
    assert pos[d.hooks["pre_sweep"]] < pos[d.sweep_box] < pos[d.hooks["post_sweep"]] < \
        pos[d.hooks["pre_record"]] < pos[d.rec_box] < pos[d.hooks["post_record"]]
    # hidden unless ticked; ticked ones are numbered in sequence
    assert d.hooks["pre_record"].isVisibleTo(d) and not d.hooks["post_sweep"].isVisibleTo(d)
    assert d.sweep_box.title() == "1. Set swept channels"
    assert d.pre_label.text() == "2. Pre-sample delay"
    assert d.hooks["pre_record"].title() == "3. Pre-record action" and d.rec_box.title() == "4. Record"
    d.hook_checks["pre_sweep"].setChecked(True)
    assert d.hooks["pre_sweep"].isVisibleTo(d) and d.sweep_box.title() == "2. Set swept channels"
    d.hook_checks["pre_sweep"].setChecked(False)
    dd = d.to_dict()
    assert dd["hooks"]["post_sweep"] == {"enabled": False, "code": "kept = 1"}   # kept, off
    assert "pre_sweep" not in dd["hooks"]
    assert dd["hooks"]["pre_record"]["enabled"]
    he = d.hooks["pre_record"]
    assert [he.action_box.itemText(n) for n in range(3)] == [      # the special actions first
        "Skip this point", "Skip this line", "Terminate the entire sweep"]
    assert he.action_box.count() > 4                               # then the instruments'
    he.code.setPlainText("if i == 2:")
    he.action_box.setCurrentIndex(0)
    he.add_btn.click()
    assert he.code.toPlainText().startswith("if i == 2:\nskip_point()")
    he.code.setPlainText("state.setdefault('n', 0)\nstate['n'] += 1")

    # queue controls: idle with an empty queue -> nothing to do
    qp = w.queue_panel
    w.model.refresh()
    assert pump(app, 5, lambda: not qp.controls["queue_start"].isEnabled())
    assert not any(b.isEnabled() for b in qp.controls.values())
    assert not any(a.isEnabled() for a in w.queue_actions.values())
    be.queue_add({"kind": "sweep", "definition": dict(ed.definition(), name="hooked")})
    assert pump(app, 5, lambda: qp.controls["queue_start"].isEnabled()
                and w.queue_actions["queue_start"].isEnabled())
    assert not qp.controls["abort"].isEnabled()
    # copy the queued sweep back into the editor; a script goes to the script editor
    be.queue_add({"kind": "script", "code": "log('from the queue')", "name": "s1"})
    assert pump(app, 5, lambda: qp.queued.rowCount() == 2)
    qp.queued.selectRow(1)
    assert qp.copy_queued.text() == "Copy to script editor"
    qp.copy_queued.click()
    assert qp.script.toPlainText() == "log('from the queue')" and qp.script_name.text() == "s1"
    qp.queued.selectRow(0)
    assert qp.copy_queued.text() == "Copy to sweep editor"
    ed.load_definition({"name": "other", "dimensions": [{"points": 2}]})
    qp.copy_queued.click()
    assert ed.definition()["name"] == "hooked"
    assert ed.dims[0].hook_checks["pre_record"].isChecked()
    # run it: the pre-record action ran at every point; afterwards back to idle
    be.scripts.state.clear()
    qp.controls["queue_start"].click()
    assert pump(app, 30, lambda: be.queue_state()["state"] == "idle"
                and not be.queue_state()["queued"] and len(be.queue_state()["history"]) >= 2)
    assert be.scripts.state["n"] == 4
    # live view after a sweep that skips a point: complete, that point NaN
    d2 = {"name": "skipping", "dimensions": [
        {"points": 5, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}], "record": ["I"],
         "hooks": {"post_sweep": {"enabled": True, "code": "if i == 3: skip_point()"}}}]}
    be.queue_add({"kind": "sweep", "definition": d2}, start=True)
    assert pump(app, 30, lambda: w.live.ds is not None and w.live.ds.header.get("name") == "skipping"
                and w.live.ds.points == 5 and be.queue_state()["state"] == "idle")
    import numpy as np
    I = w.live.ds.data["I"]
    assert np.isnan(I[3]) and not np.isnan(I[[0, 1, 2, 4]]).any() and w.live.ds.skipped == 1
    assert pump(app, 5, lambda: not qp.controls["queue_start"].isEnabled()
                and not qp.controls["abort"].isEnabled())
    hist = [qp.history.item(r, 2).text() for r in range(qp.history.rowCount())]
    qp.history.selectRow(hist.index("s1"))
    assert qp.copy_hist.text() == "Copy to script editor"
    qp.history.selectRow(hist.index("hooked"))
    assert qp.copy_hist.text() == "Copy to sweep editor" and qp.copy_hist.isEnabled()


def test_driver_wizard_end_to_end(app, gui):
    """Template -> pages -> try on the (emulated) instrument -> save -> use the driver;
    then reopen it in the wizard."""
    from PySide6.QtWidgets import QComboBox, QWizard
    from easymeasure.frontend.driver_wizard import DriverWizard
    from easymeasure.drivers.emulators import ensure_emulator
    from easymeasure.drivers.emulators.smu import Keithley2400Emulator
    w, be, _, _ = gui
    ensure_emulator("SIM::guiwiz", 0, lambda: Keithley2400Emulator({"load_ohm": 1000.0}))
    wiz = DriverWizard(w.conn, w)
    wiz.show()
    tp = wiz.pages[0]
    assert pump(app, 5, lambda: tp.list.count() >= 4)
    tp.list.setCurrentRow(1)                                   # SCPI source-measure unit
    wiz.next()
    bp = wiz.pages[1]
    assert bp.name.text() == "my_smu" and bp.idn_query.text() == "*IDN?"
    bp.name.setText("gui_smu")
    bp.desc.setText("made in the GUI test")
    wiz.next()
    cp = wiz.pages[2]
    assert cp.table.rowCount() == 6 and cp.table.item(0, 0).text() == "mode"
    assert cp.table.item(0, 8).text() == "Voltage=VOLT | Current=CURR"
    cp.add_row({"config": True, "dtype": "str", "access": "rw"})
    r = cp.table.rowCount() - 1
    cp.table.item(r, 0).setText("sense")
    cp.table.item(r, 5).setText(":SYST:RSEN?")
    cp.table.item(r, 6).setText(":SYST:RSEN {value}")
    cp.table.item(r, 7).setText("ON | OFF")
    assert isinstance(cp.table.cellWidget(r, 1), QComboBox)
    wiz.next()
    ap = wiz.pages[3]
    assert ap.table.rowCount() == 1
    wiz.next()
    ip = wiz.pages[4]
    assert "*CLS" in ip.cmds.toPlainText()
    ip.script.setPlainText("log(query('*IDN?'))")
    wiz.next()
    fp = wiz.pages[5]
    assert pump(app, 5, lambda: fp.ok)
    assert "✔ ready to save" in fp.problems.text()
    fp.show_source.setChecked(True)
    assert "class GuiSmu(CommandInstrument)" in fp.source.toPlainText()
    fp.address.setText("SIM::guiwiz")
    fp.try_btn.click()
    assert pump(app, 10, lambda: fp.results.rowCount() == 7)
    vals = {fp.results.item(k, 0).text(): fp.results.item(k, 1).text() for k in range(7)}
    assert vals["mode"] == "Voltage " and vals["sense"] == "OFF " and "KEITHLEY" in fp.idn.text()
    wiz.button(QWizard.FinishButton).click()                    # save
    assert pump(app, 10, lambda: wiz.saved is not None)
    assert wiz.saved["driver"] == "gui_smu"
    assert "gui_smu" in be.list_drivers()["drivers"]
    h = be.add_instrument("gs", "gui_smu", "SIM::guiwiz")
    assert h["state"] == "connected", h["last_error"]
    be.write_channel("gs.sense", "on")
    assert be.read_channel("gs.sense") == "ON"
    # the wizard offers the saved driver for editing next time
    wiz2 = DriverWizard(w.conn, w)
    wiz2.show()
    tp2 = wiz2.pages[0]
    assert pump(app, 5, lambda: tp2.list.count() >= 5)
    tp2.list.setCurrentRow(tp2.list.count() - 1)
    assert tp2.list.currentItem().text().startswith("✎ Edit gui_smu")
    wiz2.next()
    assert wiz2.pages[1].name.text() == "gui_smu" and wiz2.pages[1].name.isReadOnly()
    wiz2.next()
    assert wiz2.pages[2].table.rowCount() == 7
    wiz2.close()
    wiz.close()
