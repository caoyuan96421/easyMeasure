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
    "pre": {"constants": [{"channel": "output", "value": "ON"}], "record": ["T"], "hook": "log('x')"},
    "post": {"constants": [{"channel": "Vg", "value": 0.0}]}, "metadata": {"comment": "c"}}


def test_editor_roundtrip_and_validation(app, gui):
    w, be, _, _ = gui
    ed = w.editor
    ed.load_definition(SWEEP)
    d = ed.definition()
    assert [x["points"] for x in d["dimensions"]] == [15, 4, 2]
    assert d["dimensions"][0]["record"] == ["I", "spectrum", "XY"]
    assert d["pre"]["constants"] == [{"channel": "output", "value": "ON"}]
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
    assert w.left_tabs.indexOf(w.setup_panel) >= 0 and w.left_tabs.indexOf(w.queue_panel) >= 0
    assert w.data_tabs.indexOf(w.live) == 0
    assert w.left_tabs.indexOf(w.running) == -1
    w.running_action.setChecked(True)
    assert w.left_tabs.indexOf(w.running) >= 0
    w.running_action.setChecked(False)
    assert w.left_tabs.indexOf(w.running) == -1
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
