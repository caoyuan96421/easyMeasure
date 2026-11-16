"""AMI 420 magnet programmer: its own command set (the emulator beeps at anything
else, and does not answer it), a persistent current the driver has to record
itself, the cooling the 420 does not time, and the persistent-switch procedures in
their safe order.

The emulator (drivers/emulators/ami420.py) quenches the magnet when the switch is
heated while supply and magnet currents differ, and lets a ramp started while the
switch still cools change the magnet current."""

import json
import math
import time

import pytest

from easymeasure.common.errors import ChannelError, InstrumentFault
from easymeasure.drivers._ami420_base import memory_file
from easymeasure.drivers.emulators import ensure_emulator

FAST = {"time_scale": 200.0, "poll_interval": 0.05, "status_interval": 0.0,
        "settle_time": 0.3, "cool_time": 0.4, "coil": 0.1, "limit": 90.0, "rate": 1.0,
        "heat_time": 15.0, "heater": True}


def add_magnet(be, name="mag", driver="ami420_Persistent", **opts):
    o = dict(FAST, **opts)
    be.add_instrument(name, driver, "SIM::ami_" + name, o)
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


def only_the_420s_commands(emu):
    assert not emu.unknown, emu.unknown
    assert emu.beeps == 0, list(emu.errors)


@pytest.fixture(autouse=True)
def _fresh_bus():
    from easymeasure.drivers.simbus import reset_buses
    reset_buses()
    yield
    reset_buses()


def test_identification_units_and_a_field_ramp(backend):
    h, emu = add_magnet(backend, units=0, rate_units=0)          # kG, per second
    assert "MODEL 420" in h.driver.idn
    assert h.read("ramp_rate") == pytest.approx(1.0 * 0.1 * 60)  # 1 A/s * 0.1 T/A in T/min
    assert h.read("switch") == "ON" and h.read("status") == "PAUSED"
    h.write("field", 1.5)
    assert h.read("target") == pytest.approx(1.5)
    assert emu.target == pytest.approx(15.0)                     # A: kG converted
    assert "CONFigure:FIELD:PROGram 15" in emu.log
    wait(lambda: h.read("status") == "HOLDING", what="HOLDING")
    assert h.read("field") == pytest.approx(1.5)
    assert h.read("current") == pytest.approx(15.0) == h.read("supply_current")
    assert h.read("supply_voltage") == pytest.approx(0.15)
    assert math.isnan(h.read("persistent_field"))               # driven
    with pytest.raises(ChannelError, match="current limit"):
        h.write("field", 9.5)
    with pytest.raises(ChannelError, match="driven"):
        h.write("persistent_field", 1.0)
    only_the_420s_commands(emu)


def test_enter_and_exit_persistent_in_the_safe_order(backend):
    h, emu = add_magnet(backend)
    mem = memory_file(h.driver.address)
    h.write("field", 2.0)
    wait(lambda: h.read("status") == "HOLDING")
    h.run_action("enter_persistent")
    assert h.read("status") == "ENTERING PERSISTENT"
    with pytest.raises(ChannelError, match="busy"):
        h.write("field", 1.0)
    # the current is on record before the heater goes off
    wait(lambda: emu.heater is False, what="heater off")
    assert json.loads(mem.read_text())["current"] == pytest.approx(20.0)
    # the 420 does not time the cooling: the driver does, and refuses meanwhile
    assert h.read("switch") == "COOLING"
    wait(lambda: h.read("status") == "PERSISTENT", what="PERSISTENT")
    assert "persistent at 2" in h.read("operation")
    assert h.read("switch") == "OFF"
    # the 420 measures the leads (at zero now); the field is the recorded one
    assert h.read("supply_current") == pytest.approx(0.0, abs=1e-6)
    assert h.read("field") == pytest.approx(2.0) == h.read("persistent_field")
    assert h.read("current") == pytest.approx(20.0) and emu.Im == pytest.approx(20.0)
    ev = [e for _, e in emu.history]
    i_off = next(i for i, e in enumerate(ev) if e.startswith("PSwitch 0"))
    i_cold = ev.index("switch cold")
    i_zero = next(i for i, e in enumerate(ev) if e.startswith("ZERO"))
    assert i_off < i_cold < i_zero                               # cooled, then leads down
    with pytest.raises(ChannelError, match="persistent mode"):
        h.write("field", 1.0)
    with pytest.raises(ChannelError, match="already"):
        h.run_action("enter_persistent")
    # exit: the supply to the recorded current BEFORE heating
    h.run_action("exit_persistent")
    assert "CONFigure:CURRent:PROGram 20" in emu.log
    wait(lambda: h.read("status") == "HOLDING", what="driven again")
    assert "driven at 2" in h.read("operation")
    heat = next(e for _, e in emu.history if e.startswith("PSwitch 1"))
    assert "supply 20.0000 A, magnet 20.0000 A" in heat
    assert emu.quench_count == 0 and h.fault is None
    assert not mem.exists() and math.isnan(h.read("persistent_field"))
    h.write("field", 1.0)
    wait(lambda: h.read("status") == "HOLDING")
    assert h.read("field") == pytest.approx(1.0)
    only_the_420s_commands(emu)


def test_the_recorded_current_survives_a_restart(backend):
    h, emu = add_magnet(backend)
    h.write("field", 1.2)
    wait(lambda: h.read("status") == "HOLDING")
    h.run_action("enter_persistent")
    wait(lambda: h.read("status") == "PERSISTENT")
    # a new backend session: the instrument is added again, the 420 knows nothing
    backend.remove_instrument("mag")
    h, emu2 = add_magnet(backend)
    assert emu2 is emu and emu.Is == 0
    assert h.read("status") == "PERSISTENT" and h.read("field") == pytest.approx(1.2)
    assert "recorded" in h.read("operation")
    h.run_action("exit_persistent")
    wait(lambda: h.read("status") == "HOLDING")
    assert emu.quench_count == 0 and h.read("field") == pytest.approx(1.2)


def test_a_magnet_left_persistent_without_the_driver(backend):
    """At power-on the heater is off: persistent, at a field the 420 does not know."""
    h, emu = add_magnet(backend, heater=False)
    assert h.read("status") == "PERSISTENT" and h.read("switch") == "OFF"
    assert math.isnan(h.read("field")) and "unknown" in h.read("operation")
    with pytest.raises(ChannelError, match="persistent mode"):
        h.write("field", 0.5)
    with pytest.raises(ChannelError, match="unknown"):
        h.run_action("exit_persistent")
    assert not emu.heater
    with pytest.raises(ChannelError, match="not a field"):
        h.write("persistent_field", 99.0)
    h.write("persistent_field", 0.0)                              # it was at zero
    assert h.read("field") == 0.0
    h.run_action("exit_persistent")
    wait(lambda: h.read("status") == "HOLDING")
    assert emu.heater and emu.quench_count == 0
    only_the_420s_commands(emu)


def test_exit_never_heats_a_mismatched_switch(backend):
    h, emu = add_magnet(backend)
    h.write("field", 1.0)
    wait(lambda: h.read("status") == "HOLDING")
    h.run_action("enter_persistent")
    wait(lambda: h.read("status") == "PERSISTENT")
    # the limit was lowered on the front panel: the 420 refuses the programmed current
    emu.limit = 5.0
    with pytest.raises(ChannelError, match="refused"):
        h.run_action("exit_persistent")
    assert h.read("switch") == "OFF" and h.read("status") == "PERSISTENT"
    emu.limit = 90.0
    # the ramp to the recorded current is paused from the front panel
    h.run_action("exit_persistent")
    emu.handle("PAUSE")
    wait(lambda: h.read("operation").startswith("stopped"), what="stopped")
    assert "NOT heated" in h.read("operation")
    assert h.read("switch") == "OFF" and emu.quench_count == 0
    assert not any(e.startswith("PSwitch 1") for _, e in emu.history)
    h.run_action("exit_persistent")
    wait(lambda: h.read("status") == "HOLDING")
    assert emu.quench_count == 0


def test_cooling_is_waited_for(backend):
    """The 420 would ramp while the switch still cools (and so change the magnet
    current); the driver refuses until ``cool_time`` has passed."""
    h, emu = add_magnet(backend, cool_time=1.0)
    h.write("field", 0.5)
    wait(lambda: h.read("status") == "HOLDING")
    h.run_action("enter_persistent")
    wait(lambda: emu.heater is False, what="heater off")
    h.run_action("pause")                          # stops the procedure, not the cooling
    assert h.read("operation").startswith("stopped by pause")
    assert h.read("status") == "COOLING SWITCH"
    with pytest.raises(ChannelError, match="cooling"):
        h.write("field", 1.0)
    with pytest.raises(ChannelError, match="cooling"):
        h.run_action("exit_persistent")
    wait(lambda: h.read("status") == "PERSISTENT", what="cooled")
    assert h.read("field") == pytest.approx(0.5)   # recorded before the heater went off


def test_without_a_switch(backend):
    """ami420: no persistent channels or actions, the measured field, nothing probed."""
    h, emu = add_magnet(backend, driver="ami420_noPersistent", switch=False)
    assert "switch" not in h.channels and "persistent_field" not in h.channels
    assert "enter_persistent" not in h.actions
    with pytest.raises(ChannelError, match="no action"):
        h.run_action("enter_persistent")
    h.write("field", 0.5)
    wait(lambda: h.read("status") == "HOLDING")
    assert h.read("field") == pytest.approx(0.5) == h.read("target")
    assert h.read("current") == pytest.approx(5.0)
    assert not any("PSwitch" in c for c in emu.log)                # never asked
    only_the_420s_commands(emu)


def test_the_plain_driver_ignores_the_switch_completely(backend):
    """ami420_noPersistent: no PSwitch command or query at all, nothing on record is
    believed - whatever the programmer says about a heater, the magnet is driven."""
    import json
    mem = memory_file("SIM::ami_a")
    mem.parent.mkdir(parents=True, exist_ok=True)
    mem.write_text(json.dumps({"current": 12.0, "field": 1.2}))   # stale, ignored
    # the programmer claims a heater that is off: that would be "persistent"
    h, emu = add_magnet(backend, "a", driver="ami420_noPersistent", heater=False)
    assert h.state == "connected" and h.read("status") == "PAUSED"
    h.write("field", 0.7)
    wait(lambda: h.read("status") == "HOLDING")
    assert h.read("field") == pytest.approx(0.7) == h.read("target")
    assert h.read("current") == pytest.approx(7.0)
    assert not any("PSwitch" in c for c in emu.log), [c for c in emu.log if "PSwitch" in c]
    assert mem.exists()                                            # left alone
    # a STATE? of 8 (heating) blocks nothing here: the programmer decides
    emu.has_switch, emu.heater, emu.switch = True, True, "warm"
    h.write("field", 0.2)
    wait(lambda: h.read("status") == "HOLDING")
    assert h.read("field") == pytest.approx(0.2)
    assert not any("PSwitch" in c for c in emu.log)


def test_quench_aborts_and_is_cleared(backend):
    h, emu = add_magnet(backend, time_scale=5.0, rate=0.5)
    events = []
    backend.add_listener(lambda k, d: events.append((k, d)))
    h.write("field", 3.0)
    wait(lambda: emu.mode == "ramp" and emu.Is > 0.5, what="ramping")
    emu.quench("test")
    wait(lambda: any(k == "instrument_fault" for k, _ in events), what="watchdog fault")
    with pytest.raises(InstrumentFault):
        h.write("field", 1.0)
    with pytest.raises(InstrumentFault):
        h.run_action("enter_persistent")
    assert h.read("status") == "QUENCH" and h.read("quench") == "Yes"
    h.run_action("clear_quench")
    assert h.read("quench") == "No" and h.driver.fault is None
    h.write("field", 0.1)
    wait(lambda: h.read("status") == "HOLDING")


def test_each_driver_refuses_the_other_model(backend):
    """A 430 driver on a 420 would send queries the 420 does not answer (a beep and
    a timeout each): it stops at the identification instead."""
    from easymeasure.drivers.emulators.ami420 import AMI420Emulator
    emu = ensure_emulator("SIM::ami_x", 0, lambda: AMI420Emulator(FAST))
    backend.add_instrument("x", "ami430", "SIM::ami_x", dict(FAST))
    h = backend.instruments.get("x")
    assert h.state != "connected" and "use the ami420_noPersistent or ami420_Persistent" in h.last_error
    assert emu.beeps == 0 and not emu.unknown
    from easymeasure.drivers.emulators.ami430 import AMI430Emulator
    ensure_emulator("SIM::ami_y", 0, lambda: AMI430Emulator(FAST))
    backend.add_instrument("y", "ami420_noPersistent", "SIM::ami_y", dict(FAST))
    assert "use the ami430 driver" in backend.instruments.get("y").last_error
