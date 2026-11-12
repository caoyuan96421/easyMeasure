"""AMI 430 / 420 magnet programmer: field ramps, the persistent-switch procedures in
their safe order, refusals, and a quench aborting a running sweep.

The emulator (drivers/emulators/ami430.py) quenches the magnet when the switch is
heated while supply and magnet currents differ, so a driver that heated too early
would make these tests fail."""

import time

import pytest

from easymeasure.common.errors import ChannelError, InstrumentFault
from easymeasure.drivers.emulators import ensure_emulator

FAST = {"time_scale": 200.0, "poll_interval": 0.05, "status_interval": 0.0,
        "settle_time": 0.3, "coil": 0.1, "limit": 90.0, "rate": 1.0, "psw_rate": 5.0,
        "heat_time": 20.0, "cool_time": 30.0}


def add_magnet(be, name="mag", **opts):
    o = dict(FAST, **opts)
    be.add_instrument(name, "ami430", "SIM::ami_" + name, o)
    h = be.instruments.get(name)
    assert h.state == "connected", h.last_error
    emu = ensure_emulator("SIM::ami_" + name, 0, lambda: None)
    return h, emu


def wait(fn, timeout=20.0, what=""):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("timed out waiting for %s" % (what or fn))


@pytest.fixture(autouse=True)
def _fresh_bus():
    from easymeasure.drivers.simbus import reset_buses
    reset_buses()
    yield
    reset_buses()


def test_identification_units_and_a_field_ramp(backend):
    h, emu = add_magnet(backend, greeting=True, units=0, rate_units=0)   # kG, per second
    assert "MODEL 430" in h.driver.idn
    assert h.read("IsPersistentSwitchInstalled") == "Yes"        # auto: from the programmer
    assert h.read("ramp_rate") == pytest.approx(1.0 * 0.1 * 60)  # 1 A/s * 0.1 T/A in T/min
    assert h.read("switch") == "ON" and h.read("status") == "PAUSED"
    h.write("field", 1.5)
    assert h.read("target") == pytest.approx(1.5)
    assert emu.target == pytest.approx(15.0)                     # A: kG converted
    assert "CONFIGURE:FIELD:TARGET 15" in " ".join(emu.log).upper() or \
        "CONF:FIELD:TARG 15" in " ".join(emu.log).upper()
    wait(lambda: h.read("status") == "HOLDING", what="HOLDING")
    assert h.read("field") == pytest.approx(1.5)
    assert h.read("current") == pytest.approx(15.0)
    assert not emu.unknown, emu.unknown                          # only manual commands
    with pytest.raises(ChannelError, match="current limit"):
        h.write("field", 9.5)


def test_enter_and_exit_persistent_in_the_safe_order(backend):
    h, emu = add_magnet(backend)
    h.write("field", 2.0)
    wait(lambda: h.read("status") == "HOLDING")
    h.run_action("enter_persistent")
    assert h.read("status") == "ENTERING PERSISTENT"
    with pytest.raises(ChannelError, match="busy"):
        h.write("field", 1.0)                                    # nothing in between
    with pytest.raises(ChannelError, match="busy"):
        h.run_action("exit_persistent")
    wait(lambda: h.read("status") == "PERSISTENT", what="PERSISTENT")
    assert "persistent at 2" in h.read("operation")
    assert h.read("switch") == "OFF" and h.read("field") == pytest.approx(2.0)
    assert h.read("supply_current") == pytest.approx(0.0, abs=1e-6)   # leads at zero
    assert emu.Im == pytest.approx(20.0)                         # still in the magnet
    ev = [e for _, e in emu.history]
    i_off = next(i for i, e in enumerate(ev) if e.startswith("PSwitch 0"))
    i_cold = ev.index("switch cold")
    i_zero = ev.index("ZERO")
    assert i_off < i_cold < i_zero                               # cool first, then leads down
    # persistent: a field write would only move the leads - refused
    with pytest.raises(ChannelError, match="persistent mode"):
        h.write("field", 1.0)
    with pytest.raises(ChannelError, match="already"):
        h.run_action("enter_persistent")
    # exit: supply up to the magnet current BEFORE heating
    h.run_action("exit_persistent")
    assert h.read("status") == "EXITING PERSISTENT"
    wait(lambda: h.read("status") == "HOLDING", what="driven again")
    assert "driven at 2" in h.read("operation")
    ev = [e for _, e in emu.history]
    heat = next(e for e in ev if e.startswith("PSwitch 1"))
    assert "supply 20.0000 A, magnet 20.0000 A" in heat          # matched when heated
    assert emu.quench_count == 0 and h.fault is None
    h.write("field", 1.0)                                        # driven: allowed again
    wait(lambda: h.read("status") == "HOLDING")
    assert h.read("field") == pytest.approx(1.0)


def test_exit_never_heats_a_mismatched_switch(backend):
    h, emu = add_magnet(backend)
    h.write("field", 1.0)
    wait(lambda: h.read("status") == "HOLDING")
    h.run_action("enter_persistent")
    wait(lambda: h.read("status") == "PERSISTENT")
    # the supply cannot reach the magnet current: someone lowered the limit on the
    # front panel (the programmer refuses the target)
    emu.limit = 5.0
    with pytest.raises(ChannelError, match="refused"):
        h.run_action("exit_persistent")
    assert h.read("switch") == "OFF" and h.read("status") == "PERSISTENT"
    emu.limit = 90.0
    # the ramp to the magnet current is paused from the front panel
    h.run_action("exit_persistent")
    emu.handle("PAUSE")
    wait(lambda: h.read("operation").startswith("stopped"), what="stopped")
    assert "NOT heated" in h.read("operation")
    assert h.read("switch") == "OFF" and emu.quench_count == 0
    assert not any(e.startswith("PSwitch 1") for _, e in emu.history)
    # a tolerance the supply never meets: it holds, but does not match
    h.driver.options["match_tolerance"] = -1.0
    h.run_action("exit_persistent")
    t0 = time.monotonic()
    # (the driver gives up after 30 s of holding; make that quick)
    while h.read("status") == "EXITING PERSISTENT" and time.monotonic() - t0 < 2:
        time.sleep(0.05)
    seq = h.driver.seq
    if seq is not None and "held_since" in seq:
        seq["held_since"] -= 60
    wait(lambda: h.read("operation").startswith("stopped"))
    assert "NOT heated" in h.read("operation") and emu.quench_count == 0
    h.driver.options["match_tolerance"] = 0.05
    h.run_action("exit_persistent")
    wait(lambda: h.read("status") == "HOLDING")
    assert emu.quench_count == 0


def test_the_emulator_quenches_a_naive_sequence(backend):
    """What the driver avoids: heating the switch at the wrong supply current."""
    h, emu = add_magnet(backend)
    h.write("field", 1.0)
    wait(lambda: h.read("status") == "HOLDING")
    h.run_action("enter_persistent")
    wait(lambda: h.read("status") == "PERSISTENT")
    emu.handle("PSwitch 1")                       # straight from the front panel: 0 A vs 10 A
    wait(lambda: emu.quench_count == 1, what="emulator quench")


def test_refusals_without_switch_and_config_switch(backend):
    h, emu = add_magnet(backend, switch=False)
    assert h.read("IsPersistentSwitchInstalled") == "No" and h.read("switch") == "NONE"
    with pytest.raises(ChannelError, match="no persistent switch"):
        h.run_action("enter_persistent")
    with pytest.raises(ChannelError, match="without a persistent switch"):
        h.write("IsPersistentSwitchInstalled", "Yes")
    h.write("field", 0.5)
    wait(lambda: h.read("status") == "HOLDING")
    # the programmer has a switch but the setting says No: still protected
    h2, emu2 = add_magnet(backend, "m2", IsPersistentSwitchInstalled="No")
    assert h2.read("IsPersistentSwitchInstalled") == "No"
    with pytest.raises(ChannelError):
        h2.run_action("enter_persistent")
    h2.write("IsPersistentSwitchInstalled", "Yes")
    h2.write("field", 0.2)
    wait(lambda: h2.read("status") == "HOLDING")
    h2.run_action("enter_persistent")
    wait(lambda: h2.read("status") == "PERSISTENT")
    with pytest.raises(ChannelError, match="exit persistent mode first"):
        h2.write("IsPersistentSwitchInstalled", "No")
    emu2.has_switch = True
    h2.driver.switch_installed = False             # as if changed behind its back
    with pytest.raises(ChannelError, match="persistent mode"):
        h2.write("field", 0.5)


def test_heating_or_cooling_refuses_ramps(backend):
    h, emu = add_magnet(backend, time_scale=20.0)
    h.write("field", 0.5)
    wait(lambda: h.read("status") == "HOLDING")
    h.run_action("enter_persistent")
    wait(lambda: h.read("status") == "ENTERING PERSISTENT" and emu.switch == "cooling",
         what="cooling")
    with pytest.raises(ChannelError):
        h.write("field", 1.0)
    h.run_action("pause")                          # stops the procedure, not the cooling
    assert h.read("operation").startswith("stopped by pause")
    assert h.read("status") == "COOLING SWITCH"
    with pytest.raises(ChannelError, match="cooling"):
        h.write("field", 1.0)
    with pytest.raises(ChannelError, match="cooling"):
        h.run_action("exit_persistent")


def test_quench_during_a_ramp_aborts_the_sweep_and_the_queue(backend):
    h, emu = add_magnet(backend, time_scale=5.0, rate=0.5)
    backend.define_alias(name="B", target="mag.field", min=-5, max=5)
    backend.add_instrument("dmm", "sim_dmm", "SIM::dmm")
    backend.define_alias(name="I", target="dmm.reading")
    events = []
    backend.add_listener(lambda k, d: events.append((k, d)))
    sweep = {"name": "fan", "dimensions": [
        {"points": 400, "sweep": [], "record": ["I", "@elapsed"], "post_delay": 0.01}],
        "pre": {"constants": [{"channel": "B", "value": 3.0}]}}
    sweep2 = dict(sweep, name="next")
    backend.queue_add({"kind": "sweep", "definition": sweep})
    backend.queue_add({"kind": "sweep", "definition": sweep2}, start=True)
    wait(lambda: emu.mode == "ramp" and emu.Is > 0.5, what="ramping")
    emu.quench("test")
    wait(lambda: any(k == "instrument_fault" for k, _ in events), what="fault event")
    wait(lambda: any(k == "sweep_end" for k, _ in events), what="sweep end")
    end = next(d for k, d in events if k == "sweep_end")
    assert end["status"] == "aborted" and "QUENCH" in end["error"], end
    fault = next(d for k, d in events if k == "instrument_fault")
    assert fault["instrument"] == "mag" and fault["aborted"]
    assert backend.instruments.get("mag").fault
    assert backend.instruments.describe()[0]["fault"]
    # the queue was stopped: the next sweep does not start by itself
    time.sleep(0.5)
    assert sum(1 for k, _ in events if k == "sweep_end") == 1
    # unsafe things are refused until the quench is cleared; reading works
    with pytest.raises(InstrumentFault):
        h.write("field", 1.0)
    with pytest.raises(InstrumentFault):
        h.run_action("exit_persistent")
    assert h.read("status") == "QUENCH" and h.read("quench") == "Yes"
    h.read("field")
    n_faults = sum(1 for k, _ in events if k == "instrument_fault")
    with pytest.raises(InstrumentFault):
        h.write("field", 1.0)
    assert sum(1 for k, _ in events if k == "instrument_fault") == n_faults   # reported once
    h.run_action("clear_quench")
    assert h.read("quench") == "No" and h.fault is None and h.driver.fault is None
    h.write("field", 0.1)
    wait(lambda: h.read("status") == "HOLDING")


def test_quench_during_persistent_operations_is_caught_by_the_watchdog(backend):
    h, emu = add_magnet(backend, time_scale=20.0)
    events = []
    backend.add_listener(lambda k, d: events.append((k, d)))
    h.write("field", 0.5)
    wait(lambda: h.read("status") == "HOLDING")
    h.run_action("enter_persistent")
    wait(lambda: emu.switch == "cooling", what="cooling")
    emu.quench("during cooling")
    # nobody reads the magnet: the backend's watchdog notices
    wait(lambda: any(k == "instrument_fault" for k, _ in events), what="watchdog fault")
    assert h.driver.seq is None and "quench" in h.driver.operation
    msg = next(d for k, d in events if k == "instrument_fault")["message"]
    assert "during entering persistent mode" in msg


def test_quench_in_effect_when_connecting_is_reported(backend):
    h, emu = add_magnet(backend)
    events = []
    backend.add_listener(lambda k, d: events.append((k, d)))
    emu.quench("before")
    backend.reconnect_instrument("mag")
    wait(lambda: any(k == "instrument_fault" for k, _ in events), what="fault after connect")


def test_a_user_action_waiting_on_the_magnet_is_aborted(backend):
    h, emu = add_magnet(backend, time_scale=5.0, rate=0.5)
    backend.define_user_action("ramp up", "set_raw('mag.field', 3.0)\n"
                                          "wait_until(lambda: get('mag.status') == 'HOLDING', "
                                          "timeout=60)\n")
    import threading
    out = {}
    t = threading.Thread(target=lambda: out.update(r=backend.run_user_action("ramp up")))
    t.start()
    wait(lambda: emu.mode == "ramp", what="ramp")
    emu.quench("test")
    t.join(20)
    assert not t.is_alive() and out["r"]["status"] in ("aborted", "error"), out


def test_quench_while_a_sweep_reads_the_magnet_is_not_a_nan(backend):
    h, emu = add_magnet(backend, time_scale=5.0, rate=0.5, poll_interval=30.0)
    backend.define_alias(name="B", target="mag.field")
    events = []
    backend.add_listener(lambda k, d: events.append((k, d)))
    sweep = {"name": "trace", "on_read_error": "nan", "dimensions": [
        {"points": 2000, "sweep": [], "record": ["B", "@elapsed"], "post_delay": 0.01}],
        "pre": {"constants": [{"channel": "B", "value": 3.0}]}}
    backend.queue_add({"kind": "sweep", "definition": sweep}, start=True)
    wait(lambda: emu.mode == "ramp" and emu.Is > 0.5, what="ramping")
    emu.quench("test")
    wait(lambda: any(k == "sweep_end" for k, _ in events), what="sweep end")
    end = next(d for k, d in events if k == "sweep_end")
    assert end["status"] in ("error", "aborted") and "QUENCH" in end["error"], end
    assert end["footer"]["points"] < 2000


def test_model_420_without_the_newer_queries(backend):
    h, emu = add_magnet(backend, model=420)
    assert "MODEL 420" in h.driver.idn
    assert h.read("IsPersistentSwitchInstalled") == "No"     # unknown: not assumed
    assert h.read("voltage") != h.read("voltage")              # NaN: no voltage taps query
    h.write("IsPersistentSwitchInstalled", "Yes")
    h.write("field", 0.4)
    wait(lambda: h.read("status") == "HOLDING")
    h.run_action("enter_persistent")
    wait(lambda: h.read("status") == "PERSISTENT")
    h.run_action("exit_persistent")
    wait(lambda: h.read("status") == "HOLDING")
    assert emu.quench_count == 0
