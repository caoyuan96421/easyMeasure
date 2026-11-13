"""Hardware drivers against their emulators (command syntax from the manuals)."""

import math
import time

import numpy as np
import pytest

from easymeasure.common.errors import ChannelError, LimitError
from easymeasure.drivers.emulators import SCPIEmulator, compile_pattern, split_args
from easymeasure.drivers.simbus import get_bus

HW = ["keithley2400", "keysight_b2901", "keysight_b2902", "sr830", "hp34401a", "ni6211", "ni63xx", "cygnus16430",
      "yokogawa7651", "hp4145", "e8358a"]


def emu(address: str, addr: int = 0):
    return get_bus(address.split("::", 1)[1]).devices[addr]


def clean(e):
    """Every command a driver sent exists in the manual's command table."""
    assert e.unknown == [], e.unknown
    return True


# ---------------------------------------------------------------------------
def test_registry_and_static_channels(backend):
    d = backend.list_drivers()
    assert not d["errors"], d["errors"]
    for name in HW:
        info = d["drivers"][name]
        names = [c["name"] for c in info["channels"]]
        assert len(names) == len(set(names)) and names, name
        assert info["description"]
    assert "Icompliance" in [c["name"] for c in d["drivers"]["keithley2400"]["channels"]]
    assert "voltage2" in [c["name"] for c in d["drivers"]["keysight_b2902"]["channels"]]


def test_scpi_pattern_matcher():
    rx = compile_pattern("[:SOURce{n}]:VOLTage[:LEVel][:IMMediate][:AMPLitude]")
    for ok in (":SOUR:VOLT", "SOUR2:VOLT:LEV", ":VOLT", ":SOURCE:VOLTAGE:LEVEL:IMMEDIATE:AMPLITUDE",
               ":sour:volt:lev"):
        assert rx.match(ok if ok.startswith(":") else ":" + ok), ok
    for bad in (":SOURC:VOLT", ":SOUR:VOL", ":SOUR:VOLTAGES", ":SOUR:VOLT?"):
        assert not rx.match(bad), bad
    assert rx.match(":SOUR2:VOLT").group(1) == "2"
    assert split_args('1, "VOLT,CURR", (@1,2)') == ["1", '"VOLT,CURR"', "(@1,2)"]
    e = SCPIEmulator("x,y,z,1")
    assert e.handle("*IDN?;*IDN?") == "x,y,z,1;x,y,z,1"
    assert e.handle(":BOGUS 1") is None and e.unknown == [":BOGUS 1"]
    assert e.handle(":SYST:ERR?").startswith("-113")
    assert e.handle(":SYST:ERR?") == '+0,"No error"'


# ---------------------------------------------------------------------------
@pytest.fixture
def k24(backend):
    backend.add_instrument("smu", "keithley2400", "SIM::k24", {"load_ohm": 1000.0})
    backend.define_alias(name="Vs", target="smu.voltage", min=-20, max=20)
    backend.define_alias(name="Is", target="smu.current")
    return backend


def test_keithley2400_voltage_mode(k24):
    be, e = k24, emu("SIM::k24")
    h = be.instruments.get("smu")
    assert h.state == "connected", h.last_error
    assert e.commands(":FORM:ELEM") == [":FORM:ELEM CURR"]
    assert ":SENS:FUNC:CONC OFF" in e.log and ":SENS:FUNC 'CURR'" in e.log
    # output off: nothing is measured (the 2400 would answer :READ? with error 803)
    assert math.isnan(be.read_channel("Is"))
    assert not e.commands(":READ?")
    be.write_channel("smu.Icompliance", 1e-3)
    assert be.read_channel("smu.Icompliance") == pytest.approx(1e-3)
    be.write_channel("smu.output", "on")
    assert be.read_channel("smu.output") == "ON"
    be.write_channel("Vs", 0.5, ramp=False)
    assert be.read_channel("Vs") == pytest.approx(0.5)
    assert be.read_channel("Is") == pytest.approx(0.5e-3)           # 0.5 V / 1 kΩ
    be.write_channel("Vs", 5.0, ramp=False)
    assert be.read_channel("Is") == pytest.approx(1e-3)             # clamped at compliance
    with pytest.raises(LimitError):
        be.write_channel("Vs", 50)                                   # alias limit
    # the instrument's own refusal is reported (error queue) and not retried
    with pytest.raises(ChannelError, match="Data out of range"):
        be.write_channel("smu.Icompliance", 5)                   # the 2400 limit is 1.05 A
    assert be.read_channel("smu.Icompliance") == pytest.approx(1e-3)
    with pytest.raises(ChannelError, match="NPLC must be within"):
        be.write_channel("smu.nplc", 20)
    be.write_channel("smu.nplc", 0.1)
    assert be.read_channel("smu.nplc") == pytest.approx(0.1)
    assert e.commands(":SENS:CURR:NPLC 0.1")
    be.write_channel("smu.sense", "4-wire")
    assert be.read_channel("smu.sense") == "4-wire" and e.out.rsen
    be.write_channel("smu.terminals", "rear")
    assert be.read_channel("smu.terminals") == "REAR"
    be.write_channel("smu.beep")
    assert clean(e)


def test_keithley2400_mode_switch(k24):
    be, e = k24, emu("SIM::k24")
    h = be.instruments.get("smu")
    be.write_channel("smu.output", "ON")
    with pytest.raises(ChannelError, match="OFF before changing the mode"):
        be.write_channel("smu.mode", "Current")
    assert e.out.func == "VOLT"
    be.write_channel("smu.output", "OFF")
    be.write_channel("smu.mode", "current")
    assert be.read_channel("smu.mode") == "Current" and e.out.func == "CURR"
    chans = h.channels
    assert chans["current"].writable and not chans["voltage"].writable
    assert "Icompliance" not in chans and chans["Vcompliance"].unit == "V"
    assert e.commands(":FORM:ELEM")[-1] == ":FORM:ELEM VOLT"
    be.write_channel("smu.Vcompliance", 2.0)
    be.write_channel("smu.output", "ON")
    be.write_channel("Is", 1e-3, ramp=False)
    assert be.read_channel("Vs") == pytest.approx(1.0)               # 1 mA × 1 kΩ
    be.write_channel("Is", 5e-3, ramp=False)
    assert be.read_channel("Vs") == pytest.approx(2.0)               # voltage compliance
    with pytest.raises(ChannelError, match="read-only"):
        be.write_channel("Vs", 1.0)
    with pytest.raises(ChannelError, match="Vcompliance"):
        be.read_channel("smu.Icompliance")
    # output switched on/off at the front panel is noticed
    e.out.on = False
    be.instruments.get("smu").driver.st["1"]["on"] = True
    v = be.read_channel("Vs")       # :READ? fails (803) -> retried after recover -> NaN
    assert math.isnan(v)
    assert clean(e)


def test_keithley2400_iv_sweep_with_mode_from_pre_constants(k24):
    be = k24
    d = {"name": "iv", "dimensions": [
        {"points": 5, "sweep": [{"channel": "Is", "start": 0, "stop": 2e-3}], "record": ["Vs"]}],
         "pre": {"constants": [{"channel": "smu.mode", "value": "Current"},
                               {"channel": "smu.Vcompliance", "value": 10},
                               {"channel": "smu.output", "value": "ON"}]},
         "post": {"constants": [{"channel": "Is", "value": 0},
                                {"channel": "smu.output", "value": "OFF"},
                                {"channel": "smu.mode", "value": "Voltage"}]}}
    assert be.validate_sweep(d)["warnings"]
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert be.queue.wait_idle(60)
    item = be.queue_state()["history"][-1]
    assert item["status"] == "finished", item
    from easymeasure.backend.storage import DataSet
    ds = DataSet.load(item["path"])
    np.testing.assert_allclose(ds.data["Vs"], np.linspace(0, 2e-3, 5) * 1000, rtol=1e-6)
    assert be.read_channel("smu.mode") == "Voltage"
    assert clean(emu("SIM::k24"))


def test_keithley2400_survives_a_noisy_line(backend):
    be = backend
    be.add_instrument("smu", "keithley2400", "SIM::noisy",
                      {"error_rate": 0.05, "garble_rate": 0.05, "seed": 3, "retries": 4,
                       "timeout": 0.2, "verify_writes": True})
    be.define_alias(name="V", target="smu.voltage")
    h = be.instruments.get("smu")
    for i in range(40):
        be.write_channel("V", i * 0.01, ramp=False)
        assert be.read_channel("V") == pytest.approx(i * 0.01)
    assert h.stats["retries"] > 0


# ---------------------------------------------------------------------------
def test_keysight_b2901(backend):
    be = backend
    be.add_instrument("smu", "keysight_b2901", "SIM::b1", {"load_ohm": 2000.0})
    e = emu("SIM::b1")
    be.define_alias(name="V", target="smu.voltage")
    be.define_alias(name="I", target="smu.current")
    # measuring with the output off would switch it on by itself (OUTP:ON:AUTO):
    # the driver must not measure then
    assert math.isnan(be.read_channel("I"))
    assert not e.outs["1"].on and not e.commands(":MEAS")
    be.write_channel("smu.Icompliance", 1e-3)
    be.write_channel("smu.nplc", 0.01)
    be.write_channel("smu.output", "ON")
    be.write_channel("V", 1.0, ramp=False)
    assert be.read_channel("I") == pytest.approx(5e-4)
    assert e.commands(":MEAS:CURR? (@1)")
    with pytest.raises(ChannelError, match="Settings conflict"):
        be.write_channel("smu.low", "FLOAT")                       # needs the output off
    be.write_channel("smu.output", "OFF")
    be.write_channel("smu.low", "float")
    assert be.read_channel("smu.low") == "FLOAT"
    be.write_channel("smu.mode", "Current")
    assert "Vcompliance" in be.instruments.get("smu").channels
    be.write_channel("smu.nplc", 50)          # the B2900 integrates up to 100 PLC
    with pytest.raises(ChannelError):
        be.write_channel("smu.nplc", 200)
    assert be.read_channel("smu.sense") == "2-wire"
    assert clean(e)


def test_keysight_b2902_two_independent_outputs(backend):
    be = backend
    be.add_instrument("smu", "keysight_b2902", "SIM::b2", {"load_ohm": 1000.0})
    e = emu("SIM::b2")
    h = be.instruments.get("smu")
    assert "voltage1" in h.channels and "voltage2" in h.channels and "voltage" not in h.channels
    be.write_channel("smu.mode2", "Current")
    ch = h.channels
    assert ch["voltage1"].writable and not ch["current1"].writable and "Icompliance1" in ch
    assert ch["current2"].writable and not ch["voltage2"].writable and "Vcompliance2" in ch
    be.define_alias(name="Vg", target="smu.voltage1")
    be.define_alias(name="Ig", target="smu.current1")
    be.define_alias(name="Isd", target="smu.current2")
    be.define_alias(name="Vsd", target="smu.voltage2")
    be.write_channel("smu.Icompliance1", 1e-2)
    be.write_channel("smu.Vcompliance2", 5)
    be.write_channel("smu.output1", "ON")
    be.write_channel("smu.output2", "ON")
    be.write_channel("Vg", 2.0, ramp=False)
    be.write_channel("Isd", 1e-3, ramp=False)
    assert be.read_channel("Ig") == pytest.approx(2e-3)
    assert be.read_channel("Vsd") == pytest.approx(1.0)
    assert e.commands(":MEAS:VOLT? (@2)") and e.commands(":SOUR2:CURR 0.001")
    with pytest.raises(ChannelError):
        be.read_channel("smu.Icompliance2")
    assert clean(e)


# ---------------------------------------------------------------------------
@pytest.mark.parametrize("model", ["SR830", "SR850"])
def test_sr830_family(backend, model):
    be = backend
    addr = "SIM::li_" + model
    be.add_instrument("li", "sr830", addr, {"emulate_model": model})
    e = emu(addr)
    h = be.instruments.get("li")
    assert h.state == "connected", h.last_error
    assert e.outx == 1                                         # GPIB: replies to GPIB
    x, y, r, th = (be.read_channel("li." + c) for c in ("X", "Y", "R", "theta"))
    assert math.hypot(x, y) == pytest.approx(r) and th == pytest.approx(30.0)
    assert be.read_channel("li.Xn") == pytest.approx(12e-9)
    assert be.read_channel("li.Yn") == pytest.approx(11e-9)
    if model == "SR830":
        assert e.ddef[1] == (2, 0) and e.ddef[2] == (2, 0)
    else:
        assert e.trcd[3][0] == 5 and e.trcd[4][0] == 6
    # sources (sweepable)
    be.define_alias(name="Vac", target="li.sine_out", min=0.004, max=2)
    be.define_alias(name="f", target="li.frequency")
    be.define_alias(name="G", target="li.aux_out2", min=-1, max=1)
    be.write_channel("Vac", 0.5, ramp=False)
    assert be.read_channel("li.R") == pytest.approx(0.5e-3)
    be.write_channel("f", 77.7, ramp=False)
    assert be.read_channel("f") == pytest.approx(77.7)
    be.write_channel("G", -0.8, ramp=False)
    assert be.read_channel("li.aux_in2") == pytest.approx(-0.4)   # loop-back cable
    be.channels.set("li.phase", 400)           # raw write (scripts); wraps to ±180
    assert be.read_channel("li.phase") == pytest.approx(40.0)
    # every setting round-trips through all of its values
    for c in ("sensitivity", "time_constant", "rolloff", "coupling", "ground", "line_filter",
              "reserve", "sync_filter"):
        for v in h.spec(c).allowed:
            assert be.write_channel("li." + c, v) == v
            assert be.read_channel("li." + c) == v, (c, v)
    assert be.write_channel("li.time_constant", "300ms") == "300 ms"   # typed casually
    be.write_channel("li.harmonic", 3)
    assert be.read_channel("li.harmonic") == 3
    with pytest.raises(ChannelError):
        be.write_channel("li.harmonic", 2.5)
    # current input: units and sensitivity labels follow
    be.write_channel("li.input", "I (1 MOhm)")
    assert h.spec("X").unit == "A" and h.spec("Xn").unit == "A/√Hz"
    assert h.spec("sensitivity").allowed[-1] == "1 µA"
    assert be.read_channel("li.R") == pytest.approx(0.5e-3 * 1e-6)    # A now
    be.write_channel("li.sensitivity", "20 pA")
    assert be.read_channel("li.sensitivity") == "20 pA"
    be.write_channel("li.input", "I (100 MΩ)")
    assert be.read_channel("li.input") == "I (100 MΩ)"
    be.write_channel("li.input", "A")
    assert h.spec("R").unit == "V"
    # external reference: frequency becomes read-only
    be.write_channel("li.reference", "External")
    assert not h.spec("frequency").writable
    with pytest.raises(ChannelError, match="read-only"):
        be.write_channel("f", 10.0)
    be.write_channel("li.reference", "Internal")
    be.write_channel("f", 10.0, ramp=False)
    # auto functions wait until the instrument is done
    be.write_channel("li.auto_phase")
    assert abs(be.read_channel("li.Y")) < 1e-12
    be.write_channel("li.auto_gain")
    be.write_channel("li.auto_reserve")
    assert be.read_channel("li.sensitivity") == "1 mV"
    with pytest.raises(ChannelError, match="sine_out"):
        be.channels.set("li.sine_out", 6)
    assert clean(e)


def test_sr830_rs232_interface_option(backend):
    be = backend
    be.add_instrument("li", "sr830", "SIM::li232", {"interface": "RS232"})
    assert emu("SIM::li232").outx == 0


# ---------------------------------------------------------------------------
def test_hp34401a_modes_rename_the_reading(backend):
    be = backend
    be.add_instrument("dmm", "hp34401a", "SIM::dmm")
    e = emu("SIM::dmm")
    h = be.instruments.get("dmm")
    assert ":TRIG:SOUR IMM" in [":" + c.lstrip(":") for c in e.log]
    be.define_alias(name="Vsample", target="dmm.Vdc")
    assert be.read_channel("Vsample") == pytest.approx(1.234567, rel=1e-4)
    expect = {"vdc": ("Vdc", "V"), "vac": ("Vac", "V"), "idc": ("Idc", "A"),
              "iac": ("Iac", "A"), "ohm": ("R2w", "Ω"), "ohm4w": ("R4w", "Ω")}
    for mode, (name, unit) in expect.items():
        be.write_channel("dmm.mode", mode)
        assert be.read_channel("dmm.mode") == mode
        readings = [c for c in h.channels.values() if not c.config]
        assert [c.name for c in readings] == [name] and readings[0].unit == unit
        assert math.isfinite(be.read_channel("dmm." + name))
        for rng in h.spec("range").allowed:
            be.write_channel("dmm.range", rng)
            assert be.read_channel("dmm.range") == rng
        be.write_channel("dmm.range", "AUTO")
        if mode in ("vac", "iac"):
            assert "nplc" not in h.channels
            for f in ("3 Hz", "20 Hz", "200 Hz"):
                be.write_channel("dmm.ac_filter", f)
                assert be.read_channel("dmm.ac_filter") == f
            assert h.spec(name).timeout == 5.0           # 200 Hz filter settles fast
        else:
            assert "ac_filter" not in h.channels
            for n in (0.02, 0.2, 1, 10, 100):
                be.write_channel("dmm.nplc", n)
                assert be.read_channel("dmm.nplc") == n
            assert h.spec(name).timeout == pytest.approx(15.0)   # follows NPLC 100
            be.write_channel("dmm.autozero", "OFF")
            assert be.read_channel("dmm.autozero") == "OFF"
    # the alias now points at a channel that does not exist in this mode
    al = {a["name"]: a for a in be.list_channels()["aliases"]}
    assert "no channel 'Vdc'" in al["Vsample"]["error"]
    with pytest.raises(ChannelError, match="channels now: R4w"):
        be.read_channel("Vsample")
    with pytest.raises(ChannelError, match="one of"):
        be.write_channel("dmm.nplc", 5)
    be.write_channel("dmm.mode", "vdc")
    assert be.read_channel("Vsample") == pytest.approx(1.234567, rel=1e-4)
    be.write_channel("dmm.range", "100 mV")
    assert be.read_channel("dmm.Vdc") == math.inf                  # overload
    assert clean(e)


# ---------------------------------------------------------------------------
@pytest.fixture
def daq(backend):
    from easymeasure.drivers.emulators import nidaqmx_sim
    nidaqmx_sim.reset()
    backend.add_instrument("daq", "ni6211", "DAQ::SIM1")
    return backend


def test_ni6211_channels_and_loopback(daq):
    be = daq
    h = be.instruments.get("daq")
    assert h.state == "connected", h.last_error
    names = list(h.channels)
    for n in ["ai%d" % k for k in range(16)] + ["ai", "ao0", "ao1", "di0", "di3", "do0", "do3",
                                                 "ctr0", "ctr1", "rate", "samples", "terminal",
                                                 "ai_range"]:
        assert n in names, n
    assert be.read_channel("daq.ao1") == 0.0           # read back on the internal channel
    be.define_alias(name="Vg", target="daq.ao0", min=-5, max=5, ramp_rate=50)
    be.write_channel("Vg", 2.5)
    assert be.read_channel("daq.ai0") == pytest.approx(2.5, abs=1e-3)
    assert be.read_channel("daq.ai7") == pytest.approx(0.7, abs=1e-3)
    v = be.read_channel("daq.ai")
    assert v.shape == (16,) and v[0] == pytest.approx(2.5, abs=1e-3)
    with pytest.raises(LimitError):
        be.write_channel("Vg", 8)
    with pytest.raises(ChannelError, match="±10"):
        be.channels.set("daq.ao1", 11)
    be.define_alias(name="Trig", target="daq.do1")
    be.write_channel("Trig", 1)
    assert be.read_channel("daq.di1") == 1.0 and be.read_channel("daq.do1") == 1.0
    be.write_channel("Trig", 0)
    assert be.read_channel("daq.di1") == 0.0
    c0 = be.read_channel("daq.ctr0")
    time.sleep(0.03)
    assert be.read_channel("daq.ctr0") > c0
    be.write_channel("daq.reset_counters")
    assert be.read_channel("daq.ctr0") < 20


def test_ni6211_speed_settings(daq):
    be = daq
    h = be.instruments.get("daq")
    with pytest.raises(ChannelError, match="250 kS/s"):
        be.write_channel("daq.rate", 300e3)                 # more than the converter
    be.write_channel("daq.rate", 250e3)                     # one input alone: full speed
    assert be.read_channel("daq.ai3") == pytest.approx(0.3, abs=1e-3)
    with pytest.raises(ChannelError, match="16 inputs at 250000 S/s each is too fast"):
        be.read_channel("daq.ai")                           # 16 inputs share 250 kS/s
    assert be.instruments.get("daq").state == "connected"    # refused, not a fault
    be.write_channel("daq.rate", 15000)
    assert be.read_channel("daq.ai").shape == (16,)
    # switching between channel sets hands the converter over each time
    for _ in range(3):
        assert be.read_channel("daq.ai5") == pytest.approx(0.5, abs=1e-3)
        assert be.read_channel("daq.ai")[7] == pytest.approx(0.7, abs=1e-3)
    be.write_channel("daq.samples", 1500)
    assert h.spec("ai3").timeout == pytest.approx(5.1)       # follows samples / rate
    be.read_channel("daq.ai3")
    be.write_channel("daq.samples", 1)                       # on-demand single conversion
    t = time.perf_counter()
    for _ in range(20):
        be.read_channel("daq.ai3")
    assert (time.perf_counter() - t) / 20 < 0.02
    be.write_channel("daq.terminal", "DIFF")
    assert "ai8" not in h.channels and h.spec("ai").shape == (8,)
    be.write_channel("daq.samples", 100)
    be.write_channel("daq.rate", 30000)                      # 8 inputs: 31.25 kS/s each
    assert be.read_channel("daq.ai").shape == (8,)
    be.write_channel("daq.terminal", "RSE")                  # 16 × 30 kS/s: vector too fast
    with pytest.raises(ChannelError, match="too fast"):
        be.read_channel("daq.ai")
    assert be.read_channel("daq.ai9") == pytest.approx(0.9, abs=1e-3)
    be.write_channel("daq.terminal", "DIFF")
    be.write_channel("daq.ai_range", "±0.2 V")
    assert be.read_channel("daq.ai_range") == "±0.2 V"
    be.update_instrument("daq", options={"ai_channels": "0-1", "rate": 100000, "terminal": "RSE"})
    h = be.instruments.get("daq")
    assert [c for c in h.channels if c.startswith("ai") and c != "ai_range"] == ["ai0", "ai1", "ai"]
    assert be.read_channel("daq.rate") == 100000


def test_ni6211_without_ni_software(backend):
    """A real device address needs NI-DAQmx: the error says what is missing."""
    h = backend.add_instrument("daq", "ni6211", "DAQ::Dev1")
    assert h["state"] == "error"
    assert "Dev1" in h["last_error"] or "nidaqmx" in h["last_error"]


def test_drivers_on_one_gpib_bus(backend):
    """Several instruments behind one shared Prologix-style GPIB adapter."""
    be = backend
    be.add_instrument("smu", "keithley2400", "SIM::gpib", {"gpib_addr": 24, "shared": True,
                                                         "load_ohm": 100.0})
    be.add_instrument("dmm", "hp34401a", "SIM::gpib", {"gpib_addr": 22, "shared": True})
    be.add_instrument("li", "sr830", "SIM::gpib", {"gpib_addr": 8, "shared": True})
    for n in ("smu", "dmm", "li"):
        assert be.instruments.get(n).state == "connected", be.instruments.get(n).last_error
    assert len(be.list_resources()) == 1
    be.define_alias(name="V", target="smu.voltage")
    be.write_channel("smu.Icompliance", 0.01)
    be.write_channel("smu.output", "ON")
    for i in range(10):
        be.write_channel("V", 0.01 * i, ramp=False)
        assert be.read_channel("smu.current") == pytest.approx(1e-4 * i)
        assert be.read_channel("dmm.Vdc") == pytest.approx(1.234567, rel=1e-4)
        assert be.read_channel("li.theta") == pytest.approx(30.0)
    for a in (24, 22, 8):
        assert clean(emu("SIM::gpib", a))


def test_example_hardware_setup_and_iv_sweep(backend):
    """examples/hardware_setup.json + sweep_iv_current.json run on the emulators."""
    from conftest import load_example
    from easymeasure.drivers.emulators import nidaqmx_sim
    nidaqmx_sim.reset()
    be = backend
    assert be.load_setup(setup=load_example("hardware_setup.json")) == []
    for h in be.instruments.handles():
        assert h.state == "connected", (h.name, h.last_error)
    d = load_example("sweep_iv_current.json")
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert be.queue.wait_idle(120)
    item = be.queue_state()["history"][-1]
    assert item["status"] == "finished", item
    from easymeasure.backend.storage import DataSet
    ds = DataSet.load(item["path"])
    np.testing.assert_allclose(ds.data["Vsrc"], np.linspace(-2, 2, 41), rtol=1e-6, atol=1e-9)
    assert ds.pre["smu.mode"] == "Current"
    assert be.read_channel("smu.mode") == "Voltage" and be.read_channel("smu.output") == "OFF"
    # the units in the file come from the instrument in its current-source mode
    assert ds.header["units"]["Vsrc"] == "V" and ds.header["units"]["Isrc"] == "A"


# ---------------------------------------------------------------------------
def test_yokogawa7651(backend):
    be = backend
    be.add_instrument("yk", "yokogawa7651", "SIM::yk")
    e = emu("SIM::yk")
    h = be.instruments.get("yk")
    assert h.state == "connected", h.last_error
    # a pure source: one channel named after the mode, no measurement
    assert [c for c in h.channels if not h.channels[c].config] == ["voltage"]
    assert be.read_channel("yk.mode") == "Voltage" and be.read_channel("yk.range") == "AUTO"
    assert be.read_channel("yk.Icompliance") == pytest.approx(0.12)   # from OS
    be.define_alias(name="Vy", target="yk.voltage", min=-20, max=20)
    be.write_channel("Vy", 0.005, ramp=False)      # output off: AUTO goes to the 10 mV range
    assert e.range == 2 and e.value == pytest.approx(0.005)
    be.write_channel("yk.Icompliance", 0.05)
    assert e.la == 50
    with pytest.raises(ChannelError, match="1 mA steps"):
        be.write_channel("yk.Icompliance", 0.0505)
    be.write_channel("yk.output", "ON")
    assert e.on and be.read_channel("yk.output") == "ON"
    ranges = []
    for v in (0.5, 5.0, 11.9, 2.0, 0.001):
        be.write_channel("Vy", v, ramp=False)
        assert be.read_channel("Vy") == pytest.approx(v)
        ranges.append(e.range)
    assert ranges == [4, 5, 5, 5, 5]               # up as needed, never down while ON
    with pytest.raises(LimitError):
        be.write_channel("Vy", 25)                 # alias limit first
    with pytest.raises(ChannelError, match="largest range"):
        h.write("voltage", 33)                     # the driver's own limit (no alias)
    be.write_channel("yk.range", "10 V")
    with pytest.raises(ChannelError, match="does not fit the 10 V range"):
        be.write_channel("Vy", 12.5, ramp=False)
    be.write_channel("Vy", 2.0, ramp=False)
    with pytest.raises(ChannelError, match="does not fit the 100 mV range"):
        be.write_channel("yk.range", "100 mV")     # 2 V would not survive the range change
    assert e.range == 5 and e.value == pytest.approx(2.0)
    with pytest.raises(ChannelError, match="switch the output OFF"):
        be.write_channel("yk.mode", "Current")
    be.write_channel("yk.output", "OFF")
    be.write_channel("yk.mode", "current")
    assert e.func == 5
    assert "current" in h.channels and "voltage" not in h.channels
    assert h.spec("Vcompliance").unit == "V"
    assert h.spec("range").allowed == ["AUTO", "1 mA", "10 mA", "100 mA"]
    be.write_channel("yk.Vcompliance", 5)
    assert e.lv == 5
    be.define_alias(name="Iy", target="yk.current")
    be.write_channel("Iy", 2e-3, ramp=False)
    assert e.range == 5 and be.read_channel("Iy") == pytest.approx(2e-3)
    be.write_channel("yk.zero")
    assert e.value == 0.0
    assert e.unknown == []


def test_yokogawa7651_error_flag_and_lost_commands(backend):
    be = backend
    be.add_instrument("yk", "yokogawa7651", "SIM::yk2")
    e = emu("SIM::yk2")
    be.write_channel("yk.range", "1 V")
    be.raw_write("yk", "S+5.0E+0;E")               # behind the driver's back: too big
    assert e.error
    with pytest.raises(ChannelError, match="reports an error"):
        be.read_channel("yk.voltage")
    # a noisy line: lost / garbled commands are caught by the read-back and retried
    be.add_instrument("yn", "yokogawa7651", "SIM::yk3",
                      {"error_rate": 0.05, "garble_rate": 0.05, "seed": 5, "retries": 5,
                       "timeout": 0.2})
    be.define_alias(name="V", target="yn.voltage")
    for i in range(30):
        be.write_channel("V", 0.1 * i, ramp=False)
        assert emu("SIM::yk3").value == pytest.approx(0.1 * i)
    assert be.instruments.get("yn").stats["retries"] > 0


# ---------------------------------------------------------------------------
@pytest.fixture
def pa(backend):
    backend.add_instrument("pa", "hp4145", "SIM::pa", {"load_ohm": 1000.0})
    return backend


def test_hp4145_user_mode_smus(pa):
    be = pa
    e = emu("SIM::pa")
    h = be.instruments.get("pa")
    assert h.state == "connected", h.last_error
    assert e.log[:3] == ["ID", "US", "IT1"] and e.user_mode
    assert {"voltage1", "current4", "vs1", "vm2", "integration"} <= set(h.channels)
    be.define_alias(name="Vd", target="pa.voltage1")
    be.define_alias(name="Id", target="pa.current1")
    assert math.isnan(be.read_channel("Id"))           # disabled: nothing measured
    with pytest.raises(ChannelError, match="set the compliance"):
        be.write_channel("pa.output1", "ON")
    be.write_channel("pa.Icompliance1", 1e-3)
    be.write_channel("Vd", 0.5, ramp=False)            # remembered while OFF ...
    assert e.smu[1].mode is None and not e.log[-1].startswith("DV")
    be.write_channel("pa.output1", "ON")              # ... programmed when switched ON
    assert e.log[-1] == "DV1,0,+5.0000E-01,+1.0000E-03"
    assert be.read_channel("Vd") == pytest.approx(0.5)   # both read back by TV / TI
    assert be.read_channel("Id") == pytest.approx(0.5e-3)
    be.write_channel("Vd", 5.0, ramp=False)
    assert be.read_channel("Id") == pytest.approx(1e-3)  # compliance
    # the power envelope is checked before anything is sent
    be.write_channel("pa.Icompliance1", 0.1)
    with pytest.raises(ChannelError, match="not available at 30 V"):
        be.write_channel("Vd", 30.0, ramp=False)
    be.write_channel("pa.Icompliance1", 0.02)
    be.write_channel("Vd", 30.0, ramp=False)
    with pytest.raises(ChannelError, match="beyond ±100 V"):
        h.write("voltage1", 120)
    # fixed range
    be.write_channel("Vd", 1.0, ramp=False)
    be.write_channel("pa.range1", "20 V")
    assert e.log[-1].startswith("DV1,1,")
    with pytest.raises(ChannelError, match="does not fit the 20 V range"):
        be.write_channel("Vd", 25.0, ramp=False)
    # current mode on another SMU; mode change needs output OFF
    with pytest.raises(ChannelError, match="OFF before changing the mode"):
        be.write_channel("pa.mode1", "Current")
    be.write_channel("pa.mode2", "Current")
    assert h.spec("current2").writable and not h.spec("voltage2").writable
    assert h.spec("Vcompliance2").unit == "V" and "Icompliance2" not in h.channels
    assert h.spec("range2").allowed[1] == "1 nA"
    be.write_channel("pa.Vcompliance2", 10)
    be.write_channel("pa.output2", "ON")
    be.define_alias(name="Is", target="pa.current2")
    be.write_channel("Is", 2e-3, ramp=False)
    assert be.read_channel("pa.voltage2") == pytest.approx(2.0)
    be.write_channel("Is", 0.05, ramp=False)          # 50 mA with 10 V compliance: fine
    assert be.read_channel("pa.voltage2") == pytest.approx(10.0)   # (clamped at 10 V)
    with pytest.raises(ChannelError, match="not available at 0.05 A"):
        be.write_channel("pa.Vcompliance2", 50)       # at 50 mA only up to 40 V
    # voltage sources and voltmeters
    be.channels.set("pa.vs1", 3.3)
    assert be.read_channel("pa.vm1") == pytest.approx(3.3)
    with pytest.raises(ChannelError, match="±20 V"):
        be.channels.set("pa.vs2", 25)
    be.write_channel("pa.integration", "long")
    assert e.it == 3
    be.write_channel("pa.output2", "OFF")
    assert e.smu[2].mode is None and e.log[-1] == "DS2"
    be.write_channel("pa.disable_all")
    assert e.smu[1].mode is None
    assert e.unknown == [] and e.errors == []


def test_hp4145_reconnect_leaves_outputs_alone(pa):
    be = pa
    e = emu("SIM::pa")
    be.write_channel("pa.Icompliance1", 1e-3)
    be.write_channel("pa.output1", "ON")
    n = len(e.log)
    be.reconnect_instrument("pa")
    assert e.smu[1].mode == "V"                         # bias still applied
    assert not [c for c in e.log[n:] if c.startswith(("DS", "DV", "DI"))]
    assert be.read_channel("pa.output1") == "OFF"       # unknown -> reported OFF
    assert be.read_channel("pa.Icompliance1") == pytest.approx(1e-3)   # remembered


def test_hp4145_models_and_reply_status(backend):
    be = backend
    h = be.add_instrument("a55", "hp4145", "SIM::p55", {"emulate_model": "4155C"})
    assert h["state"] == "connected" and be.instruments.get("a55").driver.model == "4155C"
    assert emu("SIM::p55").log[:2] == ["ID", "CMD?"]          # already there: nothing to do
    # a 4155 left in SCPI or FLEX mode is switched into 4145 syntax by the driver
    for mode, steps in ((0, [":SYST:LANG COMP"]), (1, [":PAGE", ":SYST:LANG COMP"])):
        addr = "SIM::p55m%d" % mode
        h = be.add_instrument("m%d" % mode, "hp4145", addr, {
            "emulate_model": "4155C", "emulate_cmd_mode": mode, "emulate_switch_delay": 0.3})
        assert h["state"] == "connected", h["last_error"]
        e = emu(addr)
        assert e.cmd_mode == 2 and e.user_mode
        assert [c for c in e.log if c.startswith(":")] == steps
        assert e.unknown == []
        be.write_channel("m%d.Icompliance1" % mode, 1e-3)
        be.write_channel("m%d.output1" % mode, "ON")
        assert e.smu[1].mode == "V"
    # only if the analyzer refuses to switch is there an error (and it says so)
    h = be.add_instrument("b55", "hp4145", "SIM::p55s", {
        "emulate_model": "4155C", "emulate_cmd_mode": 0, "emulate_no_lang": True,
        "mode_switch_timeout": 0.5})
    assert h["state"] == "error" and "could not switch" in h["last_error"]
    h = be.add_instrument("b56", "hp4145", "SIM::p56s", {
        "emulate_model": "4156C", "emulate_cmd_mode": 0, "force_4145_syntax": False})
    assert h["state"] == "error" and "force_4145_syntax is off" in h["last_error"]
    be.add_instrument("two", "hp4145", "SIM::p2", {"smus": [1, 3], "vs_vm": False})
    ch = be.instruments.get("two").channels
    assert "voltage3" in ch and "voltage2" not in ch and "vs1" not in ch
    # status characters of a reading
    drv = be.instruments.get("a55").driver
    for reply, want in (("NAI+1.0000E-03", 1e-3), ("CAI-2.0000E-03", -2e-3),
                        ("VAV+1.0000E+02", math.inf), ("XAI+0.0000E+00", math.nan),
                        ("N I +3.5E-06", 3.5e-6)):
        drv.q = lambda c, r=reply: r
        got = drv._measure("I", 1)
        assert (math.isnan(got) and math.isnan(want)) or got == pytest.approx(want), reply


def test_hp4145_iv_sweep(pa):
    be = pa
    be.define_alias(name="Vd", target="pa.voltage1", min=-10, max=10, ramp_rate=50)
    be.define_alias(name="Id", target="pa.current1")
    d = {"name": "iv4145", "dimensions": [
        {"points": 11, "sweep": [{"channel": "Vd", "start": -1, "stop": 1}], "record": ["Id"]}],
         "pre": {"constants": [{"channel": "pa.Icompliance1", "value": 1e-2},
                               {"channel": "pa.output1", "value": "ON"}]},
         "post": {"constants": [{"channel": "Vd", "value": 0},
                                {"channel": "pa.output1", "value": "OFF"}]}}
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert be.queue.wait_idle(60)
    item = be.queue_state()["history"][-1]
    assert item["status"] == "finished", item
    from easymeasure.backend.storage import DataSet
    ds = DataSet.load(item["path"])
    np.testing.assert_allclose(ds.data["Id"], np.linspace(-1, 1, 11) / 1000, rtol=1e-4, atol=1e-9)
    assert emu("SIM::pa").smu[1].mode is None
