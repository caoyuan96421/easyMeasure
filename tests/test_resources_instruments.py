import threading
import time

import numpy as np
import pytest

from easymeasure.backend.registry import DriverRegistry
from easymeasure.backend.instrument import (ActionSpec, ChannelSpec, Instrument,
                                            InstrumentManager, coerce_value)
from easymeasure.backend.resources import FairLock, ResourceManager
from easymeasure.common.errors import (ChannelError, InstrumentError, InstrumentNotConnected,
                                       InstrumentTimeout, ResourceBusyError, ResourceError)


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
    for ch in ("boom", "exit"):                   # a driver bug: the call fails, not the link
        with pytest.raises(InstrumentError):
            h.read(ch)
    assert h.state == "connected" and h.read("v") == 1.0
    with pytest.raises(InstrumentError):           # garbled replies: the link is not right
        h.read("garble")
    assert h.state == "error"
    # in error it stays so: refused at once, nothing reconnected behind the user's back
    n = FlakyDriver.connects
    with pytest.raises(InstrumentNotConnected, match="Reconnect it"):
        h.read("v")
    assert FlakyDriver.connects == n
    h.reconnect()
    assert h.read("v") == 1.0


def test_hung_driver_is_abandoned(mgr):
    h = mgr.add("f", "flaky", options={"retries": 0, "call_timeout": 0.3})
    t = time.monotonic()
    with pytest.raises(InstrumentTimeout):
        h.read("hang")
    assert time.monotonic() - t < 2
    assert h.stats["timeouts"] == 1 and h.state == "hung"
    with pytest.raises(InstrumentNotConnected, match="hung"):
        h.read("v")                    # stays hung until Reconnect
    before = FlakyDriver.connects
    h.reconnect()
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


def test_a_time_out_is_not_retried(mgr):
    h = mgr.add("f", "flaky", options={"retries": 2, "call_timeout": 0.2})
    t = time.monotonic()
    with pytest.raises(InstrumentTimeout):
        h.read("hang")
    assert time.monotonic() - t < 1 and h.stats["retries"] == 0      # once, not 3 times
    assert h.state == "hung"


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
    assert h.state == "error" and Shy.tries == 1
    with pytest.raises(InstrumentNotConnected):
        h.read("x")                    # not tried again by a call
    assert Shy.tries == 1
    with pytest.raises(InstrumentError):
        h.reconnect()                  # Reconnect tries (and fails once more)
    h.reconnect()
    assert h.read("x") == 5.0 and h.state == "connected"


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
    h.reconnect()
    assert h.read("y") == 2.0


def test_review_hanging_open_does_not_deadlock():
    """A port whose open() hangs must not block invalidate()/other callers forever."""
    from easymeasure.drivers import simbus
    from easymeasure.backend.registry import DriverRegistry
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
                                                   "connect_timeout": 0.5, "retries": 1})
        h.resource.invalidate("glitch")
        hang["on"] = True
        t = time.monotonic()
        with pytest.raises(InstrumentError):
            h.read("V1")
        assert time.monotonic() - t < 6
        hang["on"] = False
        time.sleep(0.2)
        h.reconnect()
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
    from easymeasure.backend.registry import DriverRegistry
    from easymeasure.drivers.sim_source import SimVoltageSource
    from easymeasure.drivers._simbase import _DEVICES

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


def test_disconnect_holds_until_connected_again(mgr):
    """The user's Disconnect: no read, retry or reconnect connects it again, also
    when it was in error; its port is freed for other programs."""
    class Shy(Instrument):
        CHANNELS = [ChannelSpec("x", "r")]
        fail = True
        connects = 0

        def connect(self):
            Shy.connects += 1
            if Shy.fail:
                raise IOError("no reply to *IDN?")

        def _read_x(self):
            return 5.0
    mgr.drivers.register("shy", Shy)
    h = mgr.add("s", "shy", "SIM::held", options={"retries": 3, "retry_delay": 0})
    assert h.state == "error"
    n = Shy.connects
    h.disconnect(release=True, hold=True)
    assert h.state == "disconnected" and h.held and h.describe()["held"]
    assert not any(r["address"] == "SIM::held" for r in mgr.resources.list())  # port closed
    Shy.fail = False
    for call in (lambda: h.read("x"), lambda: h.run_action("nope"),
                 lambda: h.raw_query("*IDN?")):
        with pytest.raises((InstrumentNotConnected, ChannelError)):
            call()
    assert Shy.connects == n and h.state == "disconnected"     # nothing reconnected it
    assert h.stats["retries"] == 0 and h.stats["reconnects"] == 0
    h.connect()                                                 # the user's Connect
    assert not h.held and h.read("x") == 5.0
    # the same after a successful connection, and Reconnect ends it too
    h.disconnect(release=True, hold=True)
    with pytest.raises(InstrumentNotConnected):
        h.read("x")
    h.reconnect()
    assert h.read("x") == 5.0 and h.state == "connected"


def test_shutdown_closes_the_ports_even_of_a_stuck_call(tmp_path):
    """Every port is closed when the backend stops, also one whose instrument is
    in the middle of a call that never ends."""
    import socket
    import threading
    from easymeasure.backend.service import Backend
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    conns = []

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            conns.append(c)

            def talk(c=c):
                buf = b""
                while True:
                    try:
                        d = c.recv(1024)
                    except OSError:
                        return
                    if not d:
                        c.close()
                        conns.append("closed")
                        return
                    buf += d
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if line.strip() == b"*IDN?":
                            c.sendall(b"ACME,X1,0,1.0\n")
                        # anything else: never answered (a stuck instrument)
            threading.Thread(target=talk, daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    be = Backend(home=tmp_path / "h", log_to_console=False,
                 settings={"folders_confirmed": True})
    try:
        for name, host in (("a", "127.0.0.1"), ("b", "localhost")):
            be.add_instrument(name, "scpi_generic", "TCP::%s::%d" % (host, port),
                              {"timeout": 60, "call_timeout": 120, "retries": 0,
                               "channels": [{"name": "v", "access": "r", "get": "V?"}]})
            assert be.instruments.get(name).state == "connected"
        stuck = threading.Thread(target=lambda: _ignore(lambda: be.read_channel("a.v")),
                                 daemon=True)
        stuck.start()
        time.sleep(0.5)
        assert be.instruments.get("a")._call_lock.locked
    finally:
        t0 = time.monotonic()
        be.close()
        took = time.monotonic() - t0
    deadline = time.monotonic() + 5
    while conns.count("closed") < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    srv.close()
    assert conns.count("closed") == 2, conns          # both ports closed
    assert took < 15, took


def _ignore(fn):
    try:
        fn()
    except Exception:
        pass


def test_a_port_in_use_is_reported(mgr):
    mgr.add("one", "flaky", "SIM::busy")
    h = mgr.add("two", "flaky", "SIM::busy")
    assert h.state == "error" and "SIM::busy" in h.last_error


def test_a_waiting_action_polls_without_holding_the_instrument(mgr):
    """A waiting action is polled (each poll a short call): reads go on meanwhile;
    it ends when its condition holds, on *check* raising, or after its timeout."""
    import threading

    class Slow(Instrument):
        CHANNELS = [ChannelSpec("x", "r")]
        ACTIONS = [ActionSpec("wait_done", "", 5.0, 0.05), ActionSpec("never", "", 0.3, 0.05)]
        done_at = 0.0
        polls = 0

        def _read_x(self):
            return 1.0

        def _action_wait_done(self):
            Slow.polls += 1
            return time.monotonic() >= Slow.done_at

        def _action_never(self):
            return False
    mgr.drivers.register("slow", Slow)
    h = mgr.add("w", "slow", "SIM::wait")
    Slow.done_at = time.monotonic() + 0.6
    t = threading.Thread(target=h.run_action, args=("wait_done",))
    t0 = time.monotonic()
    t.start()
    time.sleep(0.1)
    r0 = time.monotonic()
    assert h.read("x") == 1.0 and time.monotonic() - r0 < 0.2     # not blocked by the wait
    t.join(5)
    assert not t.is_alive() and 0.55 < time.monotonic() - t0 < 2 and Slow.polls >= 5
    with pytest.raises(InstrumentTimeout, match="did not finish in 0 s"):
        h.run_action("never")

    class Stop(Exception):
        pass
    n = []

    def check():
        n.append(1)
        if len(n) > 3:
            raise Stop()
    with pytest.raises(Stop):
        h.run_action("never", check)


def test_a_busy_gpib_bus_is_waited_for_not_a_hang(mgr):
    """Instruments on one GPIB card take turns; a call waiting for the bus (another
    instrument's transfer) is not timed: it neither times out nor makes it hung."""
    from easymeasure.backend.resources import bus_lock
    o = {"bus": "GPIB7", "call_timeout": 0.4, "gpib_addr": 3}
    h = mgr.add("d1", "sim_dmm", "SIM::bus_a", dict(o))
    assert h.state == "connected" and h.resource.bus == "GPIB7"
    lk = bus_lock("GPIB7")
    got = {}

    def other_instrument():                       # e.g. a 10 s read timeout elsewhere
        lk.acquire()
        got["held"] = True
        time.sleep(1.2)
        lk.release()
    th = threading.Thread(target=other_instrument)
    th.start()
    while "held" not in got:
        time.sleep(0.01)
    t0 = time.monotonic()
    v = h.read("reading")
    assert time.monotonic() - t0 > 1.0 and isinstance(v, float)
    assert h.state == "connected" and h.stats["timeouts"] == 0
    th.join()
    # held for longer than bus_wait: a "busy" error - the instrument stays connected
    h2 = mgr.add("d2", "sim_dmm", "SIM::bus_b", dict(o, bus_wait=0.3))
    lk.acquire()
    try:
        with pytest.raises(ResourceBusyError, match="GPIB7 bus is busy"):
            h2.read("reading")
    finally:
        lk.release()
    assert h2.state == "connected" and h2.read("reading") is not None
    # an address on another card is not affected
    assert bus_lock("GPIB8") is not lk


def test_no_listener_fails_at_once_and_stays_in_error(mgr):
    from easymeasure.backend.resources import NoListener, Resource, bus_of
    assert bus_of("GPIB0::17::INSTR", {}) == "GPIB0" and bus_of("GPIB::5", {}) == "GPIB0"
    assert bus_of("GPIB2::5::INSTR", {}) == "GPIB2" and bus_of("COM3", {}) is None
    r = Resource("SIM::nobody_here", False, {"bus": "GPIB9"})
    with r.transaction("t"):
        t0 = time.monotonic()
        with pytest.raises(NoListener, match="switched off, or the address is wrong"):
            r.query("*IDN?")
        assert time.monotonic() - t0 < 0.1
        # a temporary timeout (the identification's) is put back
        be = r._io()
        old = be.timeout
        with r.timeout_for(0.05):
            assert be.timeout == 0.05
        assert be.timeout == old
