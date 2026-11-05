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


def be_idle(app, demo, ctl):
    assert demo.queue.wait_idle(30)
    return pump(app, 5, lambda: ctl.state == "idle" and ctl.run is None)


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
    # only aliases/composites that can be written, no config channels, no read-only
    assert "detuning" in mp.rows and "I" not in mp.rows
    assert not any("." in n for n in mp.rows)
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
