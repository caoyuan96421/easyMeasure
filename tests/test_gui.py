"""Offscreen GUI tests: the frontend against an in-process backend server."""

import math
import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

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
    assert not hasattr(sp, "m_chan")                 # setting by hand is the Manual panel
    sp._write("dmm.nplc", "1")
    assert pump(app, 5, lambda: "←" in sp.msg.text() or "✘" in sp.msg.text())
    assert be.read_channel("dmm.nplc") == pytest.approx(1)
    sp._write("Vg", "7")
    assert pump(app, 5, lambda: "✘" in sp.msg.text())


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


def test_remote_dialog_certificate(app, gui, monkeypatch):
    """Backend → Remote access: get a Let's Encrypt certificate with one button (against a
    stand-in ACME server), open the Windows firewall, go back to self-signed."""
    import socket
    from PySide6.QtWidgets import QMessageBox
    from acme_fake import FakeAcme
    from easymeasure.frontend.remote_dialog import RemoteAccessDialog
    w, be, _, _ = gui
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port80 = s.getsockname()[1]
    s.close()
    ca = FakeAcme(port80)
    be._acme_extra_args = ["--directory", ca.directory]
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: warned.append(a[2]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.Yes)
    dlg = RemoteAccessDialog(w.conn, w)
    dlg.show()
    try:
        assert pump(app, 5, lambda: "self-signed" in dlg.cert_state.text())
        dlg.le_domain.setText("lab.example.org")
        dlg.le_http.setValue(port80)
        dlg.le_get.click()                                  # not agreed yet: nothing happens
        assert warned and "Subscriber Agreement" in warned[-1]
        dlg.le_agree.setChecked(True)
        dlg.le_get.click()
        assert pump(app, 60, lambda: "Let's Encrypt" in dlg.cert_state.text()), \
            dlg.cert_progress.text()
        assert "lab.example.org" in dlg.cert_state.text() and "✔" in dlg.cert_progress.text()
        assert dlg.cert_log.isVisible() and "verified" in dlg.cert_log.toPlainText()
        assert dlg.le_get.isEnabled()
        # Windows firewall (the elevation itself is Windows-only)
        runs = []
        dlg._run_elevated = lambda prog, params: runs.append((prog, params)) or True
        dlg.port.setValue(443)
        dlg._open_firewall()
        assert runs[0][0] == "cmd.exe" and "localport=80,443" in runs[0][1]
        assert "allowed" in dlg.msg.text()
        dlg._run_elevated = lambda prog, params: False
        dlg._open_firewall()
        assert "declined" in dlg.msg.text()
        dlg.use_self.click()
        assert pump(app, 5, lambda: "self-signed" in dlg.cert_state.text())
    finally:
        dlg.close()
        ca.close()


def test_instrument_dialog_address_list(app, gui, monkeypatch):
    """Add instrument: the address list shows the ports the driver accepts, the
    identified matches first (and picked), the driver's suggestions last; Refresh
    scans again (with or without *IDN?)."""
    from easymeasure.frontend.panels.setup_panel import InstrumentDialog
    w, be, _, _ = gui
    calls = []

    def fake(interfaces=None, identify=False):
        calls.append(identify)
        return {"found": [
            {"address": "COM3", "interface": "serial", "description": "USB Serial Port (FTDI)"},
            {"address": "GPIB0::24::INSTR", "interface": "gpib", "description": "",
             "idn": "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1,C32" if identify else ""},
            {"address": "GPIB0::5::INSTR", "interface": "gpib", "description": "", "idn": ""},
            {"address": "USB0::1::2::3::INSTR", "interface": "usb", "description": "",
             "idn": "Keysight Technologies,B2902A,MY1,3.4" if identify else ""},
            {"address": "DAQ::Dev3", "interface": "daq", "description": "USB-6343",
             "idn": "USB-6343"},
            {"address": "GPIB0::8::INSTR", "interface": "gpib", "description": "",
             "in_use": ["li"]}], "notes": ["VISA: just a note"]}
    monkeypatch.setattr(be, "discover_resources", fake)
    InstrumentDialog.scan_cache = None
    assert pump(app, 5, lambda: w.setup_panel.drivers)
    dlg = InstrumentDialog(w.setup_panel.drivers, parent=w, conn=w.conn)
    dlg.show()
    assert pump(app, 5, lambda: InstrumentDialog.scan_cache is not None) and calls == [True]
    pump(app, 0.1)

    def items():
        return [(dlg.address.itemData(i), dlg.address.itemText(i)) for i in range(dlg.address.count())]
    dlg.driver.setCurrentText("keithley2400")
    assert [a for a, _ in items()] == ["GPIB0::24::INSTR", "COM3", "GPIB0::5::INSTR",
                                       "GPIB0::8::INSTR", "SIM::keithley2400"]
    labels = dict(items())
    assert "MODEL 2400" in labels["GPIB0::24::INSTR"] and labels["GPIB0::24::INSTR"].endswith("✓")
    assert "(in use: li)" in labels["GPIB0::8::INSTR"] and "emulator" in labels["SIM::keithley2400"]
    assert dlg.address_text() == "GPIB0::24::INSTR"                    # the match, picked
    st = dlg.scan_status.text()
    assert "takes: GPIB" in st and "4 port(s) found" in st and "1 identified" in st
    assert "VISA: just a note" in st
    dlg.address.activated.emit(1)
    assert dlg.address.currentText() == "COM3" and dlg.address_text() == "COM3"
    dlg.driver.setCurrentText("keysight_b2902")
    assert items()[0][0] == "USB0::1::2::3::INSTR" and dlg.address_text() == "USB0::1::2::3::INSTR"
    dlg.driver.setCurrentText("ni63xx")
    assert [a for a, _ in items()] == ["DAQ::Dev3", "DAQ::SIM"] and dlg.address_text() == "DAQ::Dev3"
    dlg.driver.setCurrentText("sim_magnet")
    assert [a for a, _ in items()] == ["NONE"] and dlg.address_text() == "NONE"
    # Refresh without identification: no match marks
    dlg.identify.setChecked(False)
    dlg.refresh.click()
    assert pump(app, 5, lambda: len(calls) == 2 and dlg.refresh.isEnabled()) and calls[1] is False
    dlg.driver.setCurrentText("keithley2400")
    assert not any(t.endswith("✓") for _, t in items())
    # a label in the box still means its address; OK gives the address
    dlg.name.setText("k")
    dlg.address.setEditText(dict(items())["COM3"])
    dlg._ok()
    assert dlg.result_value["address"] == "COM3" and dlg.result_value["driver"] == "keithley2400"
    # the next dialog reuses the scan
    dlg2 = InstrumentDialog(w.setup_panel.drivers, parent=w, conn=w.conn)
    pump(app, 0.3)
    assert len(calls) == 2 and dlg2.address.count() > 0
    dlg2.close()
    dlg.close()
    InstrumentDialog.scan_cache = None


def test_first_start_asks_for_data_folder_and_ppt(app, gui, tmp_path):
    """A backend whose folders were never confirmed: the GUI asks once (prefilled with
    the current data folder and a PowerPoint file in it); 'Use these' stores them,
    'Ask me later' leaves everything as it was."""
    w, be, _, _ = gui
    be.settings["folders_confirmed"] = False
    w._first_start_asked = False
    w.model.refresh()
    assert pump(app, 5, lambda: getattr(w, "first_start_dialog", None) is not None
                and w.first_start_dialog.isVisible())
    dlg = w.first_start_dialog
    assert dlg.data_dir.text() == be.settings["data_dir"]
    assert dlg.ppt.text() == os.path.join(be.settings["data_dir"], "results.pptx")
    dlg.later.click()                                       # ask me later: nothing changes
    pump(app, 0.3)
    assert be.settings["folders_confirmed"] is False
    w.model.refresh()
    pump(app, 0.3)
    assert not w.first_start_dialog.isVisible()             # not again in this session
    # next GUI session: asked again, and confirmed this time
    w._first_start_asked = False
    w.model.refresh()
    assert pump(app, 5, lambda: w.first_start_dialog is not dlg and w.first_start_dialog.isVisible())
    dlg = w.first_start_dialog
    new = tmp_path / "measurements"
    dlg.data_dir.setText(str(new))
    dlg.ppt.setText(str(tmp_path / "slides"))
    dlg.auto_ppt.setChecked(False)
    dlg.ok.click()
    assert pump(app, 5, lambda: be.settings["folders_confirmed"] is True)
    assert be.settings["data_dir"] == str(new) and new.is_dir() and be.store.root == new
    assert be.settings["ppt_path"] == str(tmp_path / "slides.pptx")
    assert be.settings["auto_ppt"] is False


def test_data_file_dialogs_start_in_the_data_folder(app, gui, monkeypatch, tmp_path):
    """Open data file… and Load from data file start in the backend's data folder (or
    the folder last used inside it), never in the working directory."""
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QFileDialog
    w, be, _, _ = gui
    data = be.get_settings()["data_dir"]
    os.makedirs(data, exist_ok=True)
    assert pump(app, 5, lambda: w.model.data_dir() == data)
    QSettings("easyMeasure", "gui").setValue("last_data_dir", str(tmp_path))   # outside it
    starts = []
    answer = [""]
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        lambda parent, title, start, filt: (starts.append(start), (answer[0], ""))[1])
    w.open_data()
    w.editor.load_from_data()
    assert starts == [data, data]
    # a file picked in a subfolder of the data folder: the next dialogs start there
    sub = os.path.join(data, "2026-11-07")
    os.makedirs(sub, exist_ok=True)
    w.model.remember_data_dir(os.path.join(sub, "run_0001.emj"))
    w.open_data()
    w.editor.load_from_data()
    assert starts[2:] == [sub, sub]


def test_ui_scale_not_stuck_on_an_inherited_value(app, monkeypatch):
    """A GUI (or tray) started by another easyMeasure process inherits its QT_SCALE_FACTOR:
    the current setting must still win; only a QT_SCALE_FACTOR the user set is kept."""
    from easymeasure.frontend import display
    st = display.settings()
    old = st.value("ui_scale", 100)
    try:
        # inherited from a process that started with 150 %; the setting is now 125 %
        monkeypatch.setenv("QT_SCALE_FACTOR", "1.5")
        monkeypatch.setenv(display.SCALE_MARK, "1.5")
        st.setValue("ui_scale", 125)
        display.prepare_environment()
        assert os.environ["QT_SCALE_FACTOR"] == "1.25" == os.environ[display.SCALE_MARK]
        st.setValue("ui_scale", 100)                     # back to 100 %: no factor at all
        display.prepare_environment()
        assert "QT_SCALE_FACTOR" not in os.environ and display.SCALE_MARK not in os.environ
        st.setValue("ui_scale", 150)
        display.prepare_environment()
        assert os.environ["QT_SCALE_FACTOR"] == "1.5"
        # the user's own (no mark) is respected
        monkeypatch.setenv("QT_SCALE_FACTOR", "2")
        monkeypatch.delenv(display.SCALE_MARK)
        display.prepare_environment()
        assert os.environ["QT_SCALE_FACTOR"] == "2" and display.SCALE_MARK not in os.environ
    finally:
        st.setValue("ui_scale", old)


def test_monitor_tile_shows_long_names(app, gui):
    """A long channel name uses the width of its tile and is cut with "…" only when the
    tile is too narrow (full name in the tooltip)."""
    w, be, _, _ = gui
    be.define_alias(name="sample_temperature_long", target="dmm.reading")
    be.monitor_add("sample_temperature_long", 0.5)
    assert pump(app, 5, lambda: "sample_temperature_long" in w.monitor.tiles)
    t = w.monitor.tiles["sample_temperature_long"]
    w.monitor.resize(520, 400)
    t.resize(500, 220)
    pump(app, 0.5, lambda: t.every.text())
    assert t.every.text() == "every 0.5 s"
    assert t.title.text() == "sample_temperature_long"          # whole, not cut at ~10
    assert t.title.toolTip() == "sample_temperature_long"
    t.title.resize(90, t.title.height())                   # a narrow tile
    shown = t.title.text()
    assert shown.endswith("…") and "sample_temperature_long".startswith(shown[:-1])
    be.monitor_remove("sample_temperature_long")


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


def test_parameters_and_actions_in_the_gui(app, gui):
    """Parameters: the Instruments tab's table and dialog, the Manual panel's group
    (a value is edited there, a formula follows); user actions as Manual buttons
    (run, and abort while running)."""
    from easymeasure.frontend.panels.setup_panel import ParameterDialog
    w, be, _, _ = gui
    sp, mp = w.setup_panel, w.manual
    be.define_parameter(name="Cb", value=0.6, unit="1e12/cm2/V", description="back gate")
    be.define_parameter(name="half", formula="Cb/2")
    assert pump(app, 5, lambda: sp.param.rowCount() == 2 and "half" in mp.params)
    assert [sp.param.item(r, 0).text() for r in range(2)] == ["Cb", "half"]
    assert sp.param.item(1, 2).text() == "Cb/2" and sp.param.item(0, 4).text() == "half"
    # the dialog: value or formula
    dlg = ParameterDialog({"name": "half", "formula": "Cb/2"}, ["Cb", "half"])
    assert dlg.kind.currentData() == "formula" and not dlg.value.isVisibleTo(dlg)
    dlg.kind.setCurrentIndex(0)
    dlg.value.setText("0.25")
    assert dlg.value_dict() == {"name": "half", "unit": "", "description": "", "value": 0.25}
    # Manual: the formula parameter is shown, not editable; the value one is set there
    assert isinstance(mp.params["half"]["editor"], QLabel)
    assert "0.3" in mp.params["half"]["editor"].text()
    ed = mp.params["Cb"]["editor"]
    ed.setText("0.8")
    ed.returnPressed.emit()
    assert pump(app, 5, lambda: be.read_channel(name="half") == pytest.approx(0.4))
    assert pump(app, 5, lambda: "0.4" in mp.params["half"]["editor"].text())
    mp.filter.setText("Vg")
    assert not mp.params["Cb"]["label"].isVisibleTo(mp)
    mp.filter.setText("")
    # user actions as buttons
    be.define_user_action(name="gate_to_Cb", code="set('Vg', Cb)", description="Vg = Cb")
    be.define_user_action(name="slow", code="sleep(30)")
    assert pump(app, 5, lambda: set(mp.action_buttons) == {"gate_to_Cb", "slow"})
    assert mp.actions_box.isVisibleTo(mp)
    mp.action_buttons["gate_to_Cb"].click()
    assert pump(app, 10, lambda: abs(be.channels.get("Vg") - 0.8) < 1e-9)
    mp.action_buttons["slow"].click()
    assert pump(app, 5, lambda: mp.action_buttons["slow"].text().startswith("■"))
    mp.action_buttons["slow"].click()                    # aborts
    assert pump(app, 10, lambda: mp.action_buttons["slow"].text().startswith("▶"))
    last = next(a for a in be.list_user_actions() if a["name"] == "slow")["last"]
    assert last["status"] == "aborted"


def test_manual_stop_ramps_button(app, gui):
    """"Stop ramps" is dark until a ramp runs (also one started elsewhere), stops it
    where it is, and goes dark again; an instant write never lights it."""
    import threading
    w, be, _, _ = gui
    mp = w.manual
    assert pump(app, 5, lambda: "Vg" in mp.rows)
    be.define_alias(name="Vg", target="src.V1", min=-2, max=2, ramp_rate=0.2, unit="V", replace=True)
    be.write_channel(name="Vg", value=0.0, ramp=False)
    pump(app, 0.5)
    assert not mp.stop_ramps.isEnabled()
    mp.rows["Vg"].editor.setValue(1.5)                     # from this window
    assert pump(app, 5, lambda: mp.stop_ramps.isEnabled())
    assert "Vg" in mp.stop_ramps.text()
    pump(app, 0.5)
    mp.stop_ramps.click()
    assert pump(app, 5, lambda: not mp.stop_ramps.isEnabled() and not mp._pending.get("Vg"))
    v = be.channels.last_value("Vg")
    assert 0.05 < v < 0.6                                   # stopped on the way to 1.5
    # a ramp started by another client lights it too
    th = threading.Thread(target=lambda: be.write_channel(name="Vg", value=-1.0))
    th.start()
    assert pump(app, 5, lambda: mp.stop_ramps.isEnabled())
    mp.stop_ramps.click()
    th.join(5)
    assert pump(app, 5, lambda: not mp.stop_ramps.isEnabled())
    # instant writes never light it
    mp.ramp.setChecked(False)
    mp.request_set("Vg", 0.0)
    assert pump(app, 5, lambda: not mp._pending.get("Vg"))
    assert not mp.stop_ramps.isEnabled()
    mp.ramp.setChecked(True)


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


def test_dimension_editor_inner_loop_step_and_compact_lists(app, gui):
    """An outer dimension lists 'Run the Dim k loop' where the engine runs it: last, or
    before recording with 'record after inner dims'; the innermost has none. The swept
    channel table and the record list are a few rows tall and scroll."""
    w, be, _, _ = gui
    ed = w.editor
    ed.load_definition({"name": "inner", "dimensions": [
        {"points": 5, "sweep": [{"channel": "Vg", "start": 0, "stop": 1}], "record": ["I"]},
        {"points": 3, "sweep": [{"channel": "Vb", "start": 0, "stop": 1}], "record": ["I"]}]})
    inner, outer = ed.dims
    assert not inner.inner_mid.isVisibleTo(inner) and not inner.inner_end.isVisibleTo(inner)
    assert not inner.after.isEnabled()                          # nothing inside the innermost
    assert outer.after.isEnabled()
    assert outer.inner_end.isVisibleTo(outer) and not outer.inner_mid.isVisibleTo(outer)
    assert outer.inner_end.text() == "5. Run the Dim 1: Vg ×5 loop (all its points)"
    assert outer.post_label.text() == "4. Post-sample delay"
    outer.after.setChecked(True)                                # record after the inner loop
    assert outer.inner_mid.isVisibleTo(outer) and not outer.inner_end.isVisibleTo(outer)
    assert outer.pre_label.text() == "2. Pre-sample delay"
    assert outer.inner_mid.text().startswith("3. Run the Dim 1")
    assert outer.rec_box.title() == "4. Record" and outer.post_label.text() == "5. Post-sample delay"
    lay = outer.layout()
    widgets = [lay.itemAt(i).widget() for i in range(lay.count())]
    assert widgets.index(outer.inner_mid) < widgets.index(outer.rec_box) < \
        widgets.index(outer.inner_end)
    inner.points.setValue(7)                                    # follows the inner dimension
    assert "Vg ×7" in outer.inner_mid.text()
    ed.move_dimension(1) if ed.dim_list.currentRow() == 0 else ed.move_dimension(-1)
    a, b = ed.dims
    assert not a.inner_end.isVisibleTo(a) and not a.inner_mid.isVisibleTo(a)
    assert "Dim 1" in (b.inner_mid.text() + b.inner_end.text())
    # compact lists with scrolling
    cur = ed.dims[0]
    w.left_tabs.setCurrentWidget(w.left_tabs.widget(1))
    ed.dim_list.setCurrentRow(0)
    fm_rows = cur.record.maximumHeight() / max(1, cur.record.fontMetrics().height())
    assert fm_rows < 10 and cur.table.maximumHeight() < 200
    cur.record.set_names(["ch%d" % k for k in range(30)])
    assert pump(app, 2, lambda: cur.record.verticalScrollBar().maximum() > 0)   # scrolls


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


def test_composite_editor_and_manual_groups(app, gui):
    """The composite dialog (three types, Check), the composites table and the
    manual panel's variable groups (hold + Set all → set_many)."""
    w, be, _, _ = gui
    from easymeasure.frontend.panels.setup_panel import CompositeDialog
    sp, m = w.setup_panel, w.model
    # the demo's three kinds in the table
    assert pump(app, 5, lambda: sp.comp.rowCount() == 3)
    types = {sp.comp.item(r, 0).text(): sp.comp.item(r, 1).text() for r in range(3)}
    assert types == {"detuning": "Multivariate write", "Vdiff": "Composite read",
                     "Tset_C": "Simple mapping"}
    # a multivariate write, built in the dialog
    dlg = CompositeDialog(m, w.conn, None, parent=w)
    dlg.name.setText("gates")
    dlg.vars.item(0, 0).setText("eps")
    for col, txt in ((1, "-0.4"), (2, "0.4"), (3, "V")):
        dlg.vars.item(0, col).setText(txt)
    dlg._add_var({"name": "U", "min": 0, "max": 0.5, "ramp_rate": 1})
    dlg.targets.setPlainText("Vx = U + eps\nVy = U - eps")
    assert dlg.out_of_range.currentData() == "skip"          # the default
    dlg.out_of_range.setCurrentIndex(dlg.out_of_range.findData("clamp"))
    assert dlg.value()["out_of_range"] == "clamp"
    dlg.out_of_range.setCurrentIndex(dlg.out_of_range.findData("skip"))
    v = dlg.value()
    assert v["type"] == "multi" and [x["name"] for x in v["variables"]] == ["eps", "U"]
    assert v["variables"][1]["ramp_rate"] == 1 and v["targets"]["Vy"] == "U - eps"
    dlg.check()
    assert pump(app, 5, lambda: "✔ defines eps, U" in dlg.info.text())
    assert "gates" not in [c["name"] for c in be.list_channels()["composites"]]
    be.define_composite(**v)
    # a mapping whose formulas are not inverse: Check reports where
    dlg = CompositeDialog(m, w.conn, None, parent=w)
    dlg.name.setText("bad")
    dlg.type.setCurrentIndex(2)
    assert dlg.pages.currentIndex() == 2
    dlg.m_target.setEditText("Vb")
    dlg.forward.setText("x**2")
    dlg.backward.setText("sqrt(y)")
    dlg.m_min.setText("-1")
    dlg.m_max.setText("1")
    dlg.check()
    assert pump(app, 5, lambda: "not the inverse" in dlg.info.text())
    # a composite read, and editing keeps the type
    dlg = CompositeDialog(m, w.conn, next(c for c in m.composites() if c["name"] == "Vdiff"),
                          parent=w)
    assert dlg.type.currentData() == "read" and dlg.expression.text() == "(Vx - Vy)/2"
    dlg.expression.setText("Vx - Vy")
    assert dlg.value() == {"type": "read", "name": "Vdiff", "expression": "Vx - Vy",
                           "unit": dlg.r_unit.text(), "description": dlg.description.text()}
    # the manual panel: the variables, grouped under their composite
    mp = w.manual
    m.schedule_refresh()
    assert pump(app, 5, lambda: "eps" in mp.rows and "gates" in mp.groups)
    assert mp.rows["eps"].kind == "variable" and "Tset_C" in mp.rows and "Vdiff" not in mp.rows
    assert "detuning" not in mp.groups                    # one variable: no group needed
    g = mp.groups["gates"]
    # never set: NaN, sliders greyed out, hold forced on, Set all waits for values
    eps, U = mp.rows["eps"], mp.rows["U"]
    assert eps.editor.is_nan() and "NaN" in eps.editor.text() and U.editor.is_nan()
    assert not eps.slider.isEnabled() and not U.slider.isEnabled()
    assert g["hold"].isChecked() and not g["hold"].isEnabled()
    assert not g["set"].isEnabled() and g["zero"].isEnabled()
    assert g["note"].isVisibleTo(mp)
    # a value typed over NaN (even one equal to the hidden 0) is held
    eps.editor.setFocus()
    eps.editor.lineEdit().selectAll()
    QTest.keyClicks(eps.editor.lineEdit(), "0")
    QTest.keyClick(eps.editor, Qt.Key_Return)
    assert mp._held["gates"] == {"eps": 0.0} and not eps.editor.is_nan()
    assert eps.slider.isEnabled() and U.editor.is_nan() and not g["set"].isEnabled()
    eps.editor.setValue(0.1)
    U.editor.setValue(0.2)
    assert mp._held["gates"] == {"eps": 0.1, "U": 0.2}
    assert g["set"].isEnabled() and not U.editor.is_nan() and "0.2" in U.editor.text()
    assert math.isnan(be.channels.get("eps"))             # nothing sent yet
    mp.set_group("gates")
    assert pump(app, 10, lambda: not mp._pending.get("eps") and
                be.channels.last_value("U") == pytest.approx(0.2))
    assert be.channels.get("Vx") == pytest.approx(0.3) and be.channels.get("Vy") == \
        pytest.approx(0.1)
    # set: hold is released and free again
    assert pump(app, 5, lambda: not g["hold"].isChecked() and g["hold"].isEnabled())
    assert not g["note"].isVisibleTo(mp) and eps.slider.isEnabled()
    mp.rows["eps"].editor.setValue(0.0)                   # now one alone is fine
    assert pump(app, 10, lambda: abs(be.channels.get("Vx") - 0.2) < 1e-9)
    # the variables are indented under their group; "Set all to zero"
    from easymeasure.frontend.panels.manual_panel import GROUP_INDENT
    assert mp.rows["eps"].label.indent() == GROUP_INDENT and mp.rows["Vg"].label.indent() <= 0
    g["zero"].click()
    assert pump(app, 10, lambda: not mp._pending.get("U") and
                be.channels.last_value("U") == pytest.approx(0.0))
    assert be.channels.get("Vx") == pytest.approx(0.0) and be.channels.get("Vy") == \
        pytest.approx(0.0)
    mp.filter.setText("gates")
    assert g["label"].isVisibleTo(mp) and mp.rows["U"].label.isVisibleTo(mp)
    assert not mp.rows["Vg"].label.isVisibleTo(mp)
    mp.filter.clear()
    # the sweep editor offers the variables and the mapping, records the read
    wr, rd = w.editor._channel_lists()
    assert {"eps", "U", "Tset_C", "detuning"} <= set(wr) and "Vdiff" not in wr
    assert "Vdiff" in rd


def test_monitor_vector_curves_and_columns(app, gui):
    """A short vector channel is drawn as one curve per element against time; a long
    one as its latest value against the index; tiles go in a chosen number of columns."""
    w, be, _, _ = gui
    mon = w.monitor
    be.monitor_add("XY", 0.1)
    be.monitor_add("spectrum", 0.2)
    be.monitor_add("I", 0.2)
    assert pump(app, 10, lambda: all(n in mon.tiles and mon.tiles[n].latest is not None
                                     for n in ("XY", "spectrum", "I")))
    xy, sp = mon.tiles["XY"], mon.tiles["spectrum"]
    assert pump(app, 10, lambda: len(xy.va) >= 3)
    xy.dirty = True
    xy.redraw()
    assert xy.view.isVisibleTo(xy) and xy.view.currentText() == "vs time"
    assert len(xy.traces) == 2                                   # X and Y, each over time
    x0, y0 = xy.traces[0].getData()
    assert len(x0) == len(xy.va) and x0[-1] == 0 and np.allclose(y0, [a[0] for a in xy.va])
    assert xy.traces[0].opts["pen"].color() != xy.traces[1].opts["pen"].color()
    assert xy.value.text().startswith("[") and xy.value.text().count(",") == 1
    xy.view.setCurrentIndex(1)                                   # latest vs index
    xy.redraw()
    assert xy.traces == [] and list(xy.curve.getData()[0]) == [0, 1]
    sp.dirty = True
    sp.redraw()
    assert sp.view.currentText() == "vs index" or not sp.view.isVisibleTo(sp)
    assert len(sp.curve.getData()[0]) == 201 and sp.traces == []
    # columns
    names = list(mon.tiles)
    mon.columns.setValue(2)
    pos = {n: mon.grid.getItemPosition(mon.grid.indexOf(t))[:2] for n, t in mon.tiles.items()}
    assert [pos[n] for n in names] == [(k // 2, k % 2) for k in range(len(names))]
    from PySide6.QtCore import QSettings
    assert int(QSettings("easyMeasure", "gui").value("monitor_columns")) == 2
    # nothing scrolls while the tiles fit; the tiles share the panel's height
    mon.scroll.fit()
    assert mon.scroll.verticalScrollBar().maximum() == 0 or \
        mon.host.layout().minimumSize().height() > mon.scroll.maximumViewportSize().height()
    be.monitor_remove("spectrum")
    assert pump(app, 5, lambda: "spectrum" not in mon.tiles)
    names = list(mon.tiles)
    pos = {n: mon.grid.getItemPosition(mon.grid.indexOf(t))[:2] for n, t in mon.tiles.items()}
    assert [pos[n] for n in names] == [(k // 2, k % 2) for k in range(len(names))]
    mon.columns.setValue(0)                                      # auto


def test_live_plots_fit_without_scrolling(app, gui):
    """All plots of the live view are shown at once in the available area: auto
    columns pick the arrangement, and scroll bars only appear below the plots'
    minimum sizes."""
    from easymeasure.frontend.panels.data_panel import auto_columns
    from PySide6.QtCore import QSize
    m = QSize(300, 200)
    assert auto_columns(1, QSize(600, 400), m) == 1
    assert auto_columns(4, QSize(1000, 800), m) == 2           # 2×2, not 4×1 or 1×4
    assert auto_columns(4, QSize(1600, 300), m) == 4           # a wide strip: one row
    assert auto_columns(4, QSize(400, 1000), m) == 1           # tall and narrow: a column
    assert auto_columns(3, QSize(500, 300), m) == 1            # nothing fits: no sideways scroll
    w, be, _, _ = gui
    w.resize(1400, 900)
    be.queue_add({"kind": "sweep", "definition": SWEEP}, start=True)
    lv = w.live
    assert pump(app, 30, lambda: lv.ds is not None and len(lv.plots) == 4)
    pump(app, 1.0)
    area = lv.grid_scroll
    assert lv._eff_cols == 2
    assert area.verticalScrollBar().maximum() == 0 and area.horizontalScrollBar().maximum() == 0
    vp = area.viewport().size()
    for p in lv.plots:                                          # each gets a fair share
        assert p.height() > vp.height() / 2 - 40 and p.width() > vp.width() / 2 - 40
    # a tiny area: scroll bars, never squeezed below the minimum
    lv.cols.setValue(1)
    area.resize(300, 200)
    area.fit()
    assert lv.grid_host.height() >= lv.grid_host.layout().minimumSize().height()
    assert area.verticalScrollBar().maximum() > 0
    lv.cols.setValue(0)
    assert pump(app, 30, lambda: be.queue.wait_idle(0.01))


def test_combo_lists_show_full_channel_names(app, gui):
    """Combo boxes stay narrow, but their drop-down lists show long entries in full
    (also editable ones, which Qt does not widen by itself)."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    w, be, _, _ = gui
    long = "sample_temperature_of_the_mixing_chamber_plate"
    be.define_alias(name=long, target="dmm.reading")
    mon = w.monitor
    assert pump(app, 5, lambda: mon.channel.findText(long) >= 0)
    for cb in (mon.channel, w.live.plots[0].channel_box if w.live.plots else None):
        if cb is None:
            continue
        cb.window().show()
        need = cb.view().fontMetrics().horizontalAdvance(long)
        assert cb.width() < need                                 # the box itself stays narrow
        QTest.mouseClick(cb, Qt.LeftButton, Qt.NoModifier, QPoint(cb.width() - 5, cb.height() // 2))
        assert pump(app, 2, lambda: cb.view().isVisible())
        assert cb.view().width() >= need, (cb.view().width(), need)
        cb.hidePopup()
    # the keyboard opens it as well
    mon.channel.setFocus()
    mon.channel.view().setMinimumWidth(0)
    QTest.keyClick(mon.channel, Qt.Key_F4)
    assert pump(app, 2, lambda: mon.channel.view().isVisible())
    assert mon.channel.view().width() >= mon.channel.view().fontMetrics().horizontalAdvance(long)
    mon.channel.hidePopup()


def test_user_actions_panel(app, gui, monkeypatch):
    """The Actions panel: define (dialog), run, confirm, abort, reorder, remove."""
    from PySide6.QtWidgets import QMessageBox
    from easymeasure.frontend.panels.setup_panel import UserActionDialog
    w, be, _, _ = gui
    sp = w.setup_panel
    dlg = UserActionDialog(None, w.model.readable_channels(), [], parent=w)
    dlg.name.setText("gates to zero")
    dlg.description.setText("Vx, Vy to 0 V")
    dlg.insert.setCurrentIndex(dlg.insert.findText("Vx"))
    dlg._insert(dlg.insert.findText("Vx"))
    assert dlg.code.toPlainText() == "'Vx'"
    dlg.code.setPlainText("set_many({'Vx': 0, 'Vy': 0}, ramp=False)")
    dlg._ok()
    v = dlg.result_value
    assert v == {"name": "gates to zero", "code": "set_many({'Vx': 0, 'Vy': 0}, ramp=False)",
                 "description": "Vx, Vy to 0 V", "confirm": False}
    bad = UserActionDialog(None, [], [], parent=w)
    bad.name.setText("x")
    bad.code.setPlainText("set(")
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    bad._ok()
    assert bad.result_value is None                          # syntax error: not accepted
    be.define_user_action(**v)
    be.define_user_action(name="slow", code="sleep(20)", confirm=True)
    assert pump(app, 5, lambda: sp.uact.rowCount() == 2)
    assert [sp.uact.item(r, 0).text() for r in range(2)] == ["gates to zero", "slow"]
    be.channels.set("Vx", 0.3, ramp=False)
    sp.uact.selectRow(0)
    sp._run_uact()
    assert pump(app, 10, lambda: "done" in sp.msg.text())
    assert be.channels.get("Vx") == 0
    assert pump(app, 5, lambda: sp.uact.item(0, 2).text().startswith("✔"))
    # confirm: "No" does not run it
    sp.uact.selectRow(1)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    sp._run_uact()
    pump(app, 0.3)
    assert not be.user_actions.running()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    sp._run_uact()
    assert pump(app, 5, lambda: sp.uact_abort.isEnabled() and "running" in sp.uact.item(1, 2).text())
    sp._abort_uact()
    assert pump(app, 10, lambda: sp.uact.item(1, 2).text().startswith("■"))
    assert "aborted" in sp.msg.text()
    # order and removal
    sp.uact.selectRow(1)
    sp._move_uact(-1)
    assert pump(app, 5, lambda: sp.uact.item(0, 0).text() == "slow")
    sp.uact.selectRow(0)
    sp._remove_uact()                                       # question answered Yes above
    assert pump(app, 5, lambda: sp.uact.rowCount() == 1)
    assert [a["name"] for a in be.list_user_actions()] == ["gates to zero"]
