import threading
import time

import numpy as np
import pytest

from easymeasure.backend.drivers import DriverRegistry
from easymeasure.backend.instrument import (ChannelSpec, Instrument, InstrumentManager,
                                            coerce_value)
from easymeasure.backend.resources import FairLock, ResourceManager
from easymeasure.common.errors import (ChannelError, InstrumentError, InstrumentTimeout,
                                       ResourceBusyError, ResourceError)


# ---------------------------------------------------------------- resources --
def test_exclusive_and_shared_resources():
    rm = ResourceManager()
    r1 = rm.acquire("COM5", "a")
    with pytest.raises(ResourceBusyError):
        rm.acquire("com5", "b")            # same port, different spelling
    rm.release(r1, "a")
    s1 = rm.acquire("SIM::bus", "x", shared=True)
    s2 = rm.acquire("SIM::bus", "y", shared=True)
    assert s1 is s2 and s1.owners == {"x", "y"}
    with pytest.raises(ResourceBusyError):
        rm.acquire("SIM::bus", "z", shared=False)
    n1 = rm.acquire("NONE", "p")
    n2 = rm.acquire("NONE", "q")
    assert n1 is not n2


def test_io_outside_transaction_refused():
    rm = ResourceManager()
    r = rm.acquire("SIM::b2", "a", shared=True)
    with pytest.raises(ResourceError):
        r.write("x")
    with r.transaction("a"):
        r.write("*IDN?")   # nobody listening; just must not raise


def test_fairlock_fifo_timeout_reentrant():
    lk = FairLock()
    assert lk.acquire()
    assert lk.acquire()          # re-entrant
    order = []

    def waiter(i):
        assert lk.acquire(5)
        order.append(i)
        lk.release()

    ts = []
    for i in range(5):
        t = threading.Thread(target=waiter, args=(i,))
        t.start()
        ts.append(t)
        time.sleep(0.02)
    got = []
    t = threading.Thread(target=lambda: got.append(lk.acquire(0.05)))
    t.start()
    t.join()
    assert got == [False]        # timed out while held
    lk.release()
    lk.release()
    for t in ts:
        t.join()
    assert order == [0, 1, 2, 3, 4]


def test_selector_called_on_owner_switch():
    rm = ResourceManager()
    r = rm.acquire("SIM::sel", "a", shared=True)
    rm.acquire("SIM::sel", "b", shared=True)
    calls = []
    r.set_selector("a", lambda res: calls.append("a"))
    r.set_selector("b", lambda res: calls.append("b"))
    for who in "aabba":
        with r.transaction(who):
            pass
    assert calls == ["a", "b", "a"]


# --------------------------------------------------------------- coercion --
def test_coerce():
    s = ChannelSpec("x", "rw", "float", ())
    assert coerce_value("1.5E-3\n", s) == 1.5e-3
    assert coerce_value(np.array([2.0]), s) == 2.0
    for bad in [None, {"a": 1}, "abc", [1, 2], object(), [[1, [2]]]]:
        with pytest.raises(ChannelError):
            coerce_value(bad, s)
    v = ChannelSpec("v", "r", "float", (-1,))
    assert coerce_value("1,2,3", v).shape == (3,)
    with pytest.raises(ChannelError):
        coerce_value(np.ones((2, 2)), v)
    m = ChannelSpec("m", "r", "float", (3, -1))
    assert coerce_value(np.ones((3, 7)), m).shape == (3, 7)
    with pytest.raises(ChannelError):
        coerce_value(np.ones((2, 7)), m)
    t = ChannelSpec("t", "rw", "str", ())
    assert coerce_value(b"ON", t) == "ON"
    assert coerce_value(3, t) == "3"


# ---------------------------------------------------------- guarded handle --
class FlakyDriver(Instrument):
    CHANNELS = [ChannelSpec("v", "rw", "float"), ChannelSpec("hang", "r", "float"),
                ChannelSpec("boom", "r", "float"), ChannelSpec("exit", "r", "float"),
                ChannelSpec("garble", "r", "float"), ChannelSpec("wrong", "r", "float")]
    fail_next = 0
    connects = 0

    def connect(self):
        FlakyDriver.connects += 1

    def _read_v(self):
        if FlakyDriver.fail_next > 0:
            FlakyDriver.fail_next -= 1
            raise IOError("transient")
        return 1.0

    def _write_v(self, v):
        self.val = v

    def _read_hang(self):
        time.sleep(60)

    def _read_boom(self):
        raise ZeroDivisionError("bug")

    def _read_exit(self):
        raise SystemExit(1)

    def _read_garble(self):
        return "@@@"


@pytest.fixture
def mgr():
    reg = DriverRegistry()
    reg.register("flaky", FlakyDriver)
    FlakyDriver.fail_next = 0
    FlakyDriver.connects = 0
    m = InstrumentManager(ResourceManager(), reg)
    yield m
    m.shutdown()


def test_retry_recovers_transient(mgr):
    h = mgr.add("f", "flaky", options={"retries": 3, "retry_delay": 0.0})
    FlakyDriver.fail_next = 2
    assert h.read("v") == 1.0
    assert h.stats["errors"] == 2 and h.state == "connected"


def test_exhausted_retries_raise_instrument_error(mgr):
    h = mgr.add("f", "flaky", options={"retries": 1, "retry_delay": 0.0})
    for ch in ("boom", "exit", "garble"):
        with pytest.raises(InstrumentError):
            h.read(ch)
    # instrument still usable afterwards
    assert h.read("v") == 1.0


def test_hung_driver_is_abandoned(mgr):
    h = mgr.add("f", "flaky", options={"retries": 0, "call_timeout": 0.3})
    t = time.monotonic()
    with pytest.raises(InstrumentTimeout):
        h.read("hang")
    assert time.monotonic() - t < 2
    assert h.stats["timeouts"] == 1
    before = FlakyDriver.connects
    assert h.read("v") == 1.0          # fresh worker, reconnected
    assert FlakyDriver.connects == before + 1


def test_bad_user_values_not_retried(mgr):
    h = mgr.add("f", "flaky", options={"retries": 3})
    with pytest.raises(ChannelError):
        h.write("v", "not a number")
    with pytest.raises(ChannelError):
        h.write("hang", 1.0)            # read-only
    with pytest.raises(ChannelError):
        h.read("nope")
    assert h.stats["retries"] == 0


def test_connect_failure_keeps_handle(mgr):
    class Bad(Instrument):
        CHANNELS = [ChannelSpec("x", "r")]

        def connect(self):
            raise IOError("no device")
    mgr.drivers.register("bad", Bad)
    h = mgr.add("b", "bad")
    assert h.state == "error" and "no device" in h.last_error
    assert "b" in mgr.names()


def test_registry_lists_sim_drivers():
    reg = DriverRegistry()
    d = reg.describe()
    for name in ("sim_source", "sim_dmm", "sim_lockin", "sim_spectrum", "sim_camera", "sim_awg",
                 "sim_magnet", "sim_temperature", "sim_chaos", "scpi_generic"):
        assert name in d["drivers"], name
    assert d["errors"] == {}


def test_user_driver_directory(tmp_path):
    (tmp_path / "my_meter.py").write_text(
        "from easymeasure.backend.instrument import Instrument, ChannelSpec\n"
        "class MyMeter(Instrument):\n"
        "    CHANNELS = [ChannelSpec('x', 'r')]\n"
        "    def _read_x(self):\n        return 42\n")
    (tmp_path / "broken.py").write_text("import nonexisting_module_xyz\n")
    reg = DriverRegistry([tmp_path])
    reg.scan()
    assert "my_meter" in reg.describe()["drivers"]
    assert "broken" in reg.describe()["errors"]
    m = InstrumentManager(ResourceManager(), reg)
    assert m.add("m", "my_meter").read("x") == 42.0


def test_fairlock_priority():
    from easymeasure.backend.resources import PRIO_MEASUREMENT, PRIO_NORMAL
    lk = FairLock()
    lk.acquire()
    order = []

    def w(name, prio):
        lk.acquire(5, priority=prio)
        order.append(name)
        lk.release()
    ts = [threading.Thread(target=w, args=("gui1", PRIO_NORMAL)),
          threading.Thread(target=w, args=("gui2", PRIO_NORMAL)),
          threading.Thread(target=w, args=("sweep", PRIO_MEASUREMENT))]
    for t in ts:
        t.start()
        time.sleep(0.03)
    lk.release()
    for t in ts:
        t.join()
    assert order == ["sweep", "gui1", "gui2"]


def test_circuit_breaker(mgr):
    h = mgr.add("f", "flaky", options={"retries": 2, "call_timeout": 0.2, "breaker_threshold": 2,
                                       "breaker_cooldown": 30})
    for _ in range(2):
        with pytest.raises(InstrumentTimeout):
            h.read("hang")
    t = time.monotonic()
    with pytest.raises(InstrumentTimeout, match="quarantined"):
        h.read("hang")
    assert time.monotonic() - t < 0.05        # fails fast
    assert h.read("v") == 1.0                 # other channels unaffected
    assert "read hang" in h.describe()["quarantined"]


def test_recovers_after_failed_initial_connect(mgr):
    class Shy(Instrument):
        CHANNELS = [ChannelSpec("x", "r")]
        tries = 0

        def connect(self):
            Shy.tries += 1
            if Shy.tries < 3:
                raise IOError("no reply to *IDN?")

        def _read_x(self):
            return 5.0
    mgr.drivers.register("shy", Shy)
    h = mgr.add("s", "shy", options={"retries": 2, "retry_delay": 0})
    assert h.state == "error"
    assert h.read("x") == 5.0          # reconnects on demand
    assert h.state == "connected"


def test_dynamic_channels_after_failed_connect(mgr):
    class Dyn(Instrument):
        ok = False

        def connect(self):
            if not Dyn.ok:
                raise IOError("offline")

        def get_channels(self):
            return [ChannelSpec("y", "r")]

        def _read_y(self):
            return 2.0
    mgr.drivers.register("dyn", Dyn)
    h = mgr.add("d", "dyn")
    assert h.state == "error"
    Dyn.ok = True
    assert h.read("y") == 2.0


def test_review_hanging_open_does_not_deadlock():
    """A port whose open() hangs must not block invalidate()/other callers forever."""
    from easymeasure.backend.drivers import simbus, DriverRegistry
    hang = {"on": False}
    orig = simbus.SimPortBackend.open

    def hanging_open(self):
        if hang["on"]:
            time.sleep(30)
        orig(self)
    simbus.SimPortBackend.open = hanging_open
    try:
        m = InstrumentManager(ResourceManager(), DriverRegistry())
        h = m.add("src", "sim_source", "SIM::HO", {"gpib_addr": 1, "call_timeout": 0.5,
                                                   "connect_timeout": 0.5, "retries": 1,
                                                   "breaker_cooldown": 0.1})
        h.resource.invalidate("glitch")
        hang["on"] = True
        t = time.monotonic()
        with pytest.raises(InstrumentError):
            h.read("V1")
        assert time.monotonic() - t < 6
        hang["on"] = False
        time.sleep(0.2)
        assert h.read("V1") == 0.0
    finally:
        simbus.SimPortBackend.open = orig


def test_review_hanging_constructor(mgr):
    class HangInit(Instrument):
        CHANNELS = [ChannelSpec("x", "r")]

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            time.sleep(30)
    mgr.drivers.register("hanginit", HangInit)
    t = time.monotonic()
    h = mgr.add("hi", "hanginit", options={"call_timeout": 0.3, "connect_timeout": 0.3})
    assert time.monotonic() - t < 3 and h.state in ("error", "hung")


def test_review_busy_instrument_bounded_wait(mgr):
    h = mgr.add("f", "flaky", options={"retries": 0, "call_timeout": 2.0, "lock_wait": 0.3})
    th = threading.Thread(target=lambda: pytest.raises(InstrumentTimeout, h.read, "hang"))
    th.start()
    time.sleep(0.1)
    t = time.monotonic()
    with pytest.raises(InstrumentTimeout, match="busy"):
        h.read("v")
    assert time.monotonic() - t < 1.0
    th.join()


def test_review_shared_bus_not_invalidated_by_other_instrument():
    from easymeasure.backend.drivers import DriverRegistry
    from easymeasure.backend.drivers.sim_source import SimVoltageSource
    from easymeasure.backend.drivers._simbase import _DEVICES

    class Src2(SimVoltageSource):
        def get_channels(self):
            return super().get_channels() + [ChannelSpec("pair", "w")]

        def _write_pair(self, v):
            self.resource.write("SOUR1:VOLT %g" % v)
            time.sleep(0.4)
            self.resource.write("SOUR2:VOLT %g" % v)
    reg = DriverRegistry()
    reg.register("src2", Src2)
    m = InstrumentManager(ResourceManager(), reg)
    src = m.add("src", "src2", "SIM::SB", {"gpib_addr": 1})
    dmm = m.add("dmm", "sim_dmm", "SIM::SB", {"gpib_addr": 2})
    t = threading.Thread(target=lambda: src.write("pair", 0.7))
    t.start()
    time.sleep(0.15)
    dmm._abandon_worker("dmm timed out elsewhere")   # dmm does not hold the bus
    t.join()
    assert _DEVICES[("SB", 1)].volts[:2] == [0.7, 0.7]
    # and if the bus *is* reopened mid-transaction the owner is re-selected
    t = threading.Thread(target=lambda: src.write("pair", 0.3))
    t.start()
    time.sleep(0.15)
    src.resource.invalidate("forced reopen")
    t.join()
    assert _DEVICES[("SB", 1)].volts[:2] == [0.3, 0.3]
