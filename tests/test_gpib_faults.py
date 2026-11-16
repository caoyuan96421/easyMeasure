"""Faulty instruments on one shared GPIB bus.

Four emulated instruments sit on one (simulated) GPIB bus.  One of them fails -
switched off, stops answering, or blocks the bus - and the others must go on
working without hanging; a sweep goes on or stops as its settings say, and a sweep
that does not use the failed instrument is not affected at all.
"""

import math
import threading
import time

import numpy as np
import pytest

from easymeasure.backend.storage import DataSet
from easymeasure.common.errors import InstrumentError, InstrumentNotConnected
from easymeasure.drivers.simbus import get_bus

#: (name, driver, GPIB address, a channel to read)
INSTRUMENTS = (("smu", "keithley2400", 24, "smu.voltage"),
               ("dmm", "hp34401a", 22, "dmm.Vdc"),
               ("li", "sr830", 8, "li.X"),
               ("yk", "yokogawa7651", 3, "yk.voltage"))
FAST = {"timeout": 0.3, "retry_delay": 0.01, "call_timeout": 2.0}


@pytest.fixture
def lab(backend):
    for name, driver, addr, _ in INSTRUMENTS:
        backend.add_instrument(name, driver, "SIM::lab::%d" % addr, dict(FAST))
        h = backend.instruments.get(name)
        assert h.state == "connected", (name, h.last_error)
    backend.define_alias(name="Vy", target="yk.voltage", min=-10, max=10)
    return backend


def device(addr):
    return get_bus("lab").devices[addr]


def switch_off(addr):
    """The instrument is switched off: nothing listens at its address any more."""
    return get_bus("lab").devices.pop(addr)


def others(failed):
    return [(n, ch) for n, _, _, ch in INSTRUMENTS if n != failed]


def use_the_others(be, failed, rounds=10, bound=1.0):
    """Read every other instrument; each call must be quick and succeed."""
    worst = 0.0
    for _ in range(rounds):
        for name, ch in others(failed):
            t = time.monotonic()
            v = be.read_channel(ch)
            worst = max(worst, time.monotonic() - t)
            assert isinstance(v, float), (ch, v)
            assert be.instruments.get(name).state == "connected", name
    assert worst < bound, "a working instrument waited %.2f s" % worst
    return worst


def hammer(be, ch, stop, results):
    """Keep using a failing instrument from another thread (a monitor, a GUI)."""
    while not stop.is_set():
        try:
            be.read_channel(ch)
            results.append("ok")
        except InstrumentError as exc:
            results.append(type(exc).__name__)
        time.sleep(0.01)


# ------------------------------------------------------------------ failures --
@pytest.mark.parametrize("how", ["switched_off", "stops_answering"])
def test_one_instrument_fails_the_others_carry_on(lab, how):
    be = lab
    dmm = device(22)
    if how == "switched_off":
        switch_off(22)                       # no listener: fails at once
    else:
        dmm.faults.error_rate = 1.0          # still on the bus, but never answers
    stop, results = threading.Event(), []
    t = threading.Thread(target=hammer, args=(be, "dmm.Vdc", stop, results), daemon=True)
    t.start()
    try:
        use_the_others(be, "dmm")            # while the dmm is failing and retried
    finally:
        stop.set()
        t.join(10)
    h = be.instruments.get("dmm")
    assert h.state == "error" and "ok" not in results
    # it stays in error, refused at once, without touching the bus
    t0 = time.monotonic()
    with pytest.raises(InstrumentNotConnected, match="Reconnect it"):
        be.read_channel("dmm.Vdc")
    assert time.monotonic() - t0 < 0.1
    for name, _ in others("dmm"):
        assert be.instruments.get(name).stats["errors"] == 0, name
    # back on (or answering again) and reconnected: it works
    if how == "switched_off":
        get_bus("lab").devices[22] = dmm
    else:
        dmm.faults.error_rate = 0.0
    be.reconnect_instrument("dmm")
    assert isinstance(be.read_channel("dmm.Vdc"), float)


def test_an_instrument_that_blocks_the_bus_is_cut_off(lab):
    """A device that stops in the middle of a transfer holds the bus (as a GPIB device
    that never finishes its handshake).  The others wait for the bus, not fail; the
    blocking call is cut off at its deadline and the bus is free again."""
    be = lab
    device(22).faults.hang_rate, device(22).faults.hang_time = 1.0, 60.0
    errs = []
    t = threading.Thread(target=lambda: errs.append(pytest.raises(InstrumentError,
                                                                  be.read_channel, "dmm.Vdc")),
                         daemon=True)
    t0 = time.monotonic()
    t.start()
    time.sleep(0.2)                          # the dmm holds the bus now
    # they wait for it (the dmm's reading has a 6 s deadline, plus its transfer's own
    # 1.6 s), then go on - without an error
    use_the_others(be, "dmm", rounds=3, bound=9.0)
    t.join(10)
    assert time.monotonic() - t0 < 12
    assert be.instruments.get("dmm").state == "hung"
    use_the_others(be, "dmm", rounds=5, bound=1.0)   # the bus is free again
    for name, _ in others("dmm"):
        assert be.instruments.get(name).stats["errors"] == 0, name


def test_disconnect_ends_a_blocked_call_at_once(lab):
    """Disconnect (and Remove) does not wait for the call in progress: it is cut off,
    the port released, the bus freed, and the cut-off thread does no more I/O."""
    be = lab
    dmm = device(22)
    dmm.faults.hang_rate, dmm.faults.hang_time = 1.0, 60.0
    got = []

    def call():
        t = time.monotonic()
        try:
            be.read_channel("dmm.Vdc")
        except InstrumentError as exc:
            got.append((type(exc).__name__, time.monotonic() - t))

    callers = [threading.Thread(target=call, daemon=True) for _ in range(3)]
    for c in callers:                        # one blocked on the bus, two queued behind
        c.start()
        time.sleep(0.05)
    time.sleep(0.3)
    t0 = time.monotonic()
    be.disconnect_instrument("dmm")
    took = time.monotonic() - t0
    for c in callers:
        c.join(5)
    assert took < 1.0, "Disconnect waited %.2f s" % took
    assert len(got) == 3 and all(n == "InstrumentNotConnected" for n, _ in got), got
    assert all(dt < 1.5 for _, dt in got), got
    h = be.instruments.get("dmm")
    assert h.state == "disconnected" and h.resource is None
    assert not any(r["address"] == "SIM::lab::22" for r in be.list_resources())
    n = len(dmm.log) if hasattr(dmm, "log") else None
    use_the_others(be, "dmm", rounds=5, bound=1.0)   # the bus is free at once
    time.sleep(0.5)
    if n is not None:
        assert len(dmm.log) == n             # the cut-off thread sent nothing more
    # Remove is just as quick
    dmm.faults.hang_rate = 0.0
    be.connect_instrument("dmm")
    dmm.faults.hang_rate = 1.0
    t = threading.Thread(target=call, daemon=True)
    t.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    be.remove_instrument("dmm")
    assert time.monotonic() - t0 < 1.0
    t.join(5)
    use_the_others(be, "dmm", rounds=3, bound=1.0)


# --------------------------------------------------------------------- sweeps --
def switch_off_after(addr, commands):
    """Switch the instrument off when it has handled *commands* more commands."""
    dev = device(addr)
    orig, n = dev.handle, [0]

    def handle(cmd):
        n[0] += 1
        if n[0] == commands:
            get_bus("lab").devices.pop(addr, None)
        return orig(cmd)
    dev.handle = handle
    return dev


def run(be, record, points=20, **opts):
    d = {"name": "bus", "dimensions": [
        {"points": points, "sweep": [{"channel": "Vy", "start": 0, "stop": 1}],
         "record": record, "pre_delay": 0.0}]}
    d.update(opts)
    t = time.monotonic()
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert be.queue.wait_idle(120)
    item = be.queue_state()["history"][-1]
    return item, time.monotonic() - t


def test_a_dropped_instrument_in_a_sweep_gives_nan_and_the_sweep_goes_on(lab):
    be = lab
    switch_off_after(22, 12)                 # the dmm goes off part-way
    item, _ = run(be, ["dmm.Vdc", "li.X"], on_read_error="nan")
    assert item["status"] == "finished", item
    ds = DataSet.load(item["path"])
    dmm, li = ds.data["dmm.Vdc"], ds.data["li.X"]
    assert ds.points == 20 and np.isfinite(li).all()
    assert np.isfinite(dmm[:3]).all() and np.isnan(dmm[-5:]).all()
    assert be.instruments.get("dmm").state == "error"
    assert be.instruments.get("li").state == "connected"


def test_a_dropped_instrument_in_a_sweep_stops_it_when_asked(lab):
    be = lab
    switch_off_after(22, 12)
    item, took = run(be, ["dmm.Vdc", "li.X"], on_read_error="abort")
    assert item["status"] == "error" and "dmm" in item["error"], item
    assert took < 15
    ds = DataSet.load(item["path"])
    assert np.isfinite(ds.data["li.X"][:3]).all()


@pytest.mark.parametrize("on_set_error", ["abort", "skip"])
def test_a_dropped_swept_instrument(lab, on_set_error):
    be = lab
    switch_off_after(3, 15)                  # the swept source goes off part-way
    item, _ = run(be, ["li.X"], on_set_error=on_set_error)
    if on_set_error == "abort":
        assert item["status"] == "error" and "yk" in item["error"], item
    else:
        assert item["status"] == "finished", item
        assert DataSet.load(item["path"]).points == 20
    assert be.instruments.get("yk").state == "error"
    use_the_others(be, "yk", rounds=2)


@pytest.mark.parametrize("how", ["switched_off", "stops_answering"])
def test_a_sweep_that_does_not_use_the_failed_instrument_is_not_affected(lab, how):
    be = lab
    base, base_t = run(be, ["li.X", "smu.voltage"])
    assert base["status"] == "finished"
    dmm = device(22)
    stop, results = threading.Event(), []
    # the failed instrument is still being used (a monitor, a GUI) during the sweep
    t = threading.Thread(target=hammer, args=(be, "dmm.Vdc", stop, results), daemon=True)
    if how == "switched_off":
        switch_off(22)
    else:
        dmm.faults.error_rate = 1.0
    t.start()
    try:
        item, took = run(be, ["li.X", "smu.voltage"])
    finally:
        stop.set()
        t.join(10)
    assert item["status"] == "finished", item
    ds = DataSet.load(item["path"])
    assert ds.points == 20
    assert np.isfinite(ds.data["li.X"]).all() and np.isfinite(ds.data["smu.voltage"]).all()
    assert not item.get("error")
    # no slower than the run before, but for the few transfers the failing
    # instrument holds the bus while it is tried (before it is in error)
    assert took < base_t + 3.0, (took, base_t)
    for name in ("li", "smu", "yk"):
        h = be.instruments.get(name)
        assert h.state == "connected" and h.stats["errors"] == 0, name
    assert be.instruments.get("dmm").state == "error"
    assert not math.isnan(be.read_channel("li.X"))
