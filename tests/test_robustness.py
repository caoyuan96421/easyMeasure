"""Robustness tests against simulated buggy hardware."""

import threading
import time

import numpy as np
import pytest

from easymeasure.backend.storage import DataSet

from conftest import EXAMPLES


@pytest.fixture
def faulty(backend):
    problems = backend.load_setup(path=str(EXAMPLES / "faulty_setup.json"))
    # connection itself may fail for some faulty instruments at random; retry those
    for h in backend.instruments.handles():
        for _ in range(5):
            if h.state == "connected":
                break
            try:
                h.connect()
            except Exception:
                pass
    return backend


def test_faulty_instruments_full_sweep(faulty):
    be = faulty
    d = {"name": "faulty", "dimensions": [
        {"points": 20, "sweep": [{"channel": "Vg", "start": -0.4, "stop": 0.4}],
         "record": ["I", "X", "XY", "spectrum", "image", "chaos_garbage", "chaos_vector",
                    "chaos.text"]},
        {"points": 4, "sweep": [{"channel": "Vb", "start": -0.1, "stop": 0.1}], "record": ["T"]}],
        "on_read_error": "nan", "on_set_error": "skip"}
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    # hammer the backend from other threads at the same time
    stop = threading.Event()
    errors = []

    def hammer(name):
        while not stop.is_set():
            try:
                be.read_channel(name)
            except Exception:
                pass
            except BaseException as exc:  # nothing may escape as BaseException
                errors.append(exc)
            time.sleep(0.02)   # ~50 Hz polling per thread, like aggressive GUIs/monitors
    ts = [threading.Thread(target=hammer, args=(n,), daemon=True)
          for n in ("I", "X", "chaos_crash", "spectrum", "chaos_garbage")]
    for t in ts:
        t.start()
    assert be.queue.wait_idle(240)
    stop.set()
    for t in ts:
        t.join(10)
    assert not errors
    item = be.queue_state()["history"][-1]
    assert item["status"] == "finished", item
    ds = DataSet.load(item["path"])
    assert ds.points == 80
    assert np.isfinite(ds.data["I"]).any()    # lost / garbled replies are retried
    stats = {h.name: h.stats for h in be.instruments.handles()}
    assert sum(s["errors"] for s in stats.values()) > 0   # faults really happened
    # an instrument that hung or lost its port stays so (its points are NaN) until
    # Reconnect: nothing reconnects behind the user's back
    for h in be.instruments.handles():
        assert h.state in ("connected", "error", "hung"), (h.name, h.state)
        if h.state != "connected":
            assert h.last_error
    for h in be.instruments.handles():
        for _ in range(5):
            if h.state == "connected":
                break
            try:
                be.reconnect_instrument(h.name)
            except Exception:
                pass
    # backend still fully functional afterwards (the faulty source may silently drop
    # a command, like real flaky hardware, so only responsiveness is checked)
    from easymeasure.common.errors import EasyMeasureError
    for _ in range(5):
        try:
            be.write_channel("Vg", 0.0)
            assert np.isfinite(be.read_channel("Vg"))
        except EasyMeasureError:
            pass
    assert "pong" in be.ping()


def test_chaos_channels_never_crash(faulty):
    be = faulty
    t0 = time.time()
    for name in ("chaos_garbage", "chaos_vector", "chaos_crash", "chaos.text", "chaos_slow",
                 "chaos_hang"):
        for _ in range(3):
            try:
                v = be.read_channel(name)
                if name == "chaos_garbage":
                    assert isinstance(v, float)
            except Exception:
                pass
    # the hang left the chaos instrument hung: refused at once until Reconnect
    h = be.instruments.get("chaos")
    if h.state != "connected":
        t = time.monotonic()
        with pytest.raises(Exception, match="Reconnect it"):
            be.read_channel("chaos_value")
        assert time.monotonic() - t < 0.5
    # backend alive and responsive.  The chaos driver raises at random on 10 % of its
    # calls (connect too), so like a user: Reconnect again until it works
    from easymeasure.common.errors import EasyMeasureError
    ok = False
    for _ in range(20):
        try:
            if h.state != "connected":
                be.reconnect_instrument("chaos")
            assert be.read_channel("chaos_value") is not None
            be.write_channel("chaos_value", 1.0)
            ok = True
            break
        except EasyMeasureError:
            pass
    assert ok


def test_monitor_survives_chaos(faulty):
    be = faulty
    got = []
    be.monitor.listeners.append(got.append)
    be.monitor_add("chaos_crash", 0.1)
    be.monitor_add("chaos_hang", 0.2)
    be.monitor_add("Vg", 0.1)
    time.sleep(8)
    vg = [m for m in got if m["channel"] == "Vg" and "value" in m]
    # a hanging channel on another instrument must not stall Vg monitoring
    assert len(vg) >= 30
    # errors of the chaos instrument are reported as monitor messages, not crashes;
    # once it is hung its channels are paused
    assert any("error" in m or m.get("paused") for m in got
               if m["channel"].startswith("chaos_"))


def test_hung_instrument_during_sweep(backend):
    be = backend
    be.add_instrument("src", "sim_source", "SIM::H1::1", {"call_timeout": 0.5})
    be.add_instrument("dmm", "sim_dmm", "SIM::H1::2", {"call_timeout": 0.5,
                                                    "gate": "src.V1", "bias": "src.V2"})
    be.define_alias(name="Vg", target="src.V1", min=-1, max=1)
    be.define_alias(name="I", target="dmm.reading")
    # make the dmm hang hard every few calls
    from easymeasure.drivers._simbase import _DEVICES
    dev = _DEVICES[("H1", 2)]
    dev.faults.hang_rate = 0.15
    dev.faults.hang_time = 3.0
    dev.faults.rng.seed(1)
    d = {"dimensions": [{"points": 30, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.5}],
                         "record": ["I"]}]}
    t0 = time.time()
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert be.queue.wait_idle(120)
    item = be.queue_state()["history"][-1]
    assert item["status"] == "finished", item
    ds = DataSet.load(item["path"])
    assert ds.points == 30
    assert be.instruments.get("dmm").stats["timeouts"] > 0
    # the source on the same shared bus kept working through the dmm hangs
    assert be.read_channel("Vg") == pytest.approx(0.5)


def test_storage_failure_does_not_stop_measurement(demo, monkeypatch):
    from easymeasure.backend import storage

    calls = {"n": 0}
    orig = storage.JournalWriter.write

    def failing(self, ftype, obj, sync=False):
        calls["n"] += 1
        if calls["n"] > 5:
            raise storage.StorageError("disk full (simulated)")
        return orig(self, ftype, obj, sync)
    monkeypatch.setattr(storage.JournalWriter, "write", failing)
    d = {"dimensions": [{"points": 20, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}],
                         "record": ["I"]}]}
    demo.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert demo.queue.wait_idle(30)
    assert demo.queue_state()["history"][-1]["status"] == "finished"
    assert demo.last_dataset.points == 20     # kept in memory


def test_setup_save_load_roundtrip(demo, tmp_path):
    p = tmp_path / "s.json"
    demo.save_setup(str(p))
    demo.remove_composite("dot")
    demo.load_setup(path=str(p))
    names = {c["name"] for c in demo.list_channels()["composites"]}
    assert "dot" in names
    assert (demo.home / "autosave_setup.json").exists()


def test_ppt_generation(demo, tmp_path):
    ppt = tmp_path / "report.pptx"
    demo.set_settings(ppt_path=str(ppt), auto_ppt=True)
    d = {"name": "for-ppt", "dimensions": [
        {"points": 10, "sweep": [{"channel": "Vg", "start": -0.3, "stop": 0.3}], "record": ["I", "spectrum"]},
        {"points": 4, "sweep": [{"channel": "Vb", "start": -0.2, "stop": 0.2}], "record": []}]}
    demo.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert demo.queue.wait_idle(60)
    for _ in range(100):
        if ppt.exists():
            break
        time.sleep(0.1)
    demo.make_ppt()                        # manual button
    from pptx import Presentation
    assert len(Presentation(str(ppt)).slides) == 2


def test_ppt_append_while_file_locked(demo, tmp_path, monkeypatch):
    """A .pptx open (= locked) in PowerPoint is updated through PowerPoint itself;
    if that is impossible the slide goes to a _pending file instead of being lost."""
    from easymeasure.report import pptx_report
    d = {"name": "locked", "dimensions": [
        {"points": 5, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}], "record": ["I"]}]}
    demo.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert demo.queue.wait_idle(30)
    ds = demo.last_dataset
    ppt = tmp_path / "open.pptx"
    pptx_report.append_run_slide(ppt, ds)                     # create it normally
    real_replace = pptx_report._replace

    def locked(src, dst):
        if dst == ppt:
            raise PermissionError(13, "file is open in PowerPoint")
        real_replace(src, dst)
    monkeypatch.setattr(pptx_report, "_replace", locked)
    monkeypatch.setattr(pptx_report, "_sleep", lambda s: None)
    inserted = []

    def fake_powerpoint(target, single):
        from pptx import Presentation
        assert target == ppt and Presentation(str(single)).slides.__len__() == 1
        inserted.append(single)
        return True
    monkeypatch.setattr(pptx_report, "_insert_via_powerpoint", fake_powerpoint)
    assert pptx_report.append_run_slide(ppt, ds) == ppt and len(inserted) == 1
    # PowerPoint not available: slide kept in a pending file, temp files cleaned up
    monkeypatch.setattr(pptx_report, "_insert_via_powerpoint", lambda t, s: False)
    out = pptx_report.append_run_slide(ppt, ds)
    assert out.name == "open_pending.pptx" and out.exists()
    assert sorted(p.name for p in tmp_path.glob("open*")) == ["open.pptx", "open_pending.pptx"]


def test_monitor_pauses_while_an_instrument_behind_a_channel_is_not_connected(backend):
    """Disconnect (or a failed connection) pauses every monitored channel that reads
    the instrument, directly or through aliases and composites; it is not polled -
    so not reconnected behind the user's back - and goes on once it is connected."""
    be = backend
    be.add_instrument("dmm", "sim_dmm", "SIM::mp1")
    be.add_instrument("src", "sim_source", "SIM::mp2")
    be.define_alias(name="I", target="dmm.reading")
    be.define_alias(name="Vs", target="src.V1", min=-1, max=1)
    be.define_composite(type="multi", name="g", variables=["u"], targets={"Vs": "u"})
    got = []
    be.monitor.listeners.append(got.append)
    for ch in ("I", "dmm.reading", "g.u", "Vs"):
        be.monitor_add(ch, 0.05)

    def values(ch, since=0):
        return [m for m in got[since:] if m["channel"] == ch and "value" in m]

    def until(fn, t=5.0):
        end = time.time() + t
        while time.time() < end:
            if fn():
                return True
            time.sleep(0.05)
        return False
    assert until(lambda: all(values(c) for c in ("I", "dmm.reading", "g.u")))
    dmm = be.instruments.get("dmm")
    be.disconnect_instrument("dmm")
    calls = dmm.stats["calls"]
    assert until(lambda: all(any(m.get("paused") == "dmm is disconnected" for m in got
                                 if m["channel"] == c) for c in ("I", "dmm.reading")))
    n = len(got)
    time.sleep(0.6)
    assert not values("I", n) and not values("dmm.reading", n)
    assert dmm.stats["calls"] == calls and dmm.state == "disconnected"   # not polled
    assert len(values("g.u", n)) >= 3                                   # the other goes on
    st = {e["channel"]: e for e in be.monitor.status()}
    assert st["I"]["paused"] == "dmm is disconnected" and st["Vs"]["paused"] == ""
    # paused once, not at every interval
    assert sum(1 for m in got if m["channel"] == "I" and m.get("paused")) == 1
    # through a composite: the source disconnected pauses g.e and Vs
    be.disconnect_instrument("src")
    assert until(lambda: be.monitor.status() and {e["channel"]: e for e in
                 be.monitor.status()}["g.u"]["paused"] == "src is disconnected")
    # connected again: it goes on by itself
    be.reconnect_instrument("dmm")
    n = len(got)
    assert until(lambda: values("I", n) and values("dmm.reading", n))
    assert {e["channel"]: e for e in be.monitor.status()}["I"]["paused"] == ""
    # an instrument in error (a failed connection) pauses it as well
    dmm.state, dmm.last_error = "error", "no reply to *IDN?"
    assert until(lambda: {e["channel"]: e for e in be.monitor.status()}["I"]["paused"]
                 .startswith("dmm is in error: no reply"))


def test_every_front_end_hears_of_a_state_change(backend):
    """A hang inside a call (not a user action) is announced at once: instrument_state."""
    be = backend
    events = []
    be.add_listener(lambda k, d: events.append((k, d)))
    be.add_instrument("chaos", "sim_chaos", options={"call_timeout": 0.3})
    assert ("instrument_state", {"instrument": "chaos", "state": "connected",
                                 "last_error": ""}) in events
    with pytest.raises(Exception):
        be.read_channel("chaos.hang")
    st = [d for k, d in events if k == "instrument_state"]
    assert st[-1]["instrument"] == "chaos" and st[-1]["state"] == "hung"
    assert be.list_instruments()[0]["state"] == "hung"
    be.reconnect_instrument("chaos")
    st = [d for k, d in events if k == "instrument_state"]
    assert [d["state"] for d in st[-2:]] == ["disconnected", "connected"]
