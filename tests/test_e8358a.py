"""Agilent E8358A PNA network analyzer: measurements created on the analyzer, one
sweep per recorded point, settings and their refusals, averaging, and a power
sweep recording whole traces.  Against the emulator (drivers/emulators/pna.py),
which refuses every command the PNA programming guide does not define."""

import time

import numpy as np
import pytest

from easymeasure.common.errors import ChannelError, InstrumentTimeout
from easymeasure.drivers.emulators import ensure_emulator


@pytest.fixture(autouse=True)
def _fresh_bus():
    from easymeasure.drivers.simbus import reset_buses
    reset_buses()
    yield
    reset_buses()


def ours(emu):
    """The measurements the driver made (the user's own stay on the analyzer)."""
    return {k: v for k, v in emu.meas.items() if k.startswith("EM_")}


def add_vna(be, name="vna", **opts):
    o = dict({"time_scale": 100.0, "poll_interval": 0.005}, **opts)
    be.add_instrument(name, "e8358a", "SIM::pna_" + name, o)
    h = be.instruments.get(name)
    assert h.state == "connected", h.last_error
    return h, ensure_emulator("SIM::pna_" + name, 0, lambda: None)


def test_identification_and_one_sweep_per_point(backend):
    be = backend
    h, emu = add_vna(be)
    assert "E8358A" in be.read_channel("vna.idn")
    be.write_channel("vna.f_start", 4.99e9)
    be.write_channel("vna.f_stop", 5.01e9)
    be.write_channel("vna.points", 201)
    be.write_channel("vna.if_bandwidth", 1000)
    # the first reading creates the measurement on the analyzer and shows it
    db = np.asarray(be.read_channel("vna.S21"))
    assert ours(emu) == {"EM_S21": "S21"} and "EM_S21" in emu.feeds.values()
    assert emu.triggers == 1
    # the other channels of S21 come from the same sweep
    ph = np.asarray(be.read_channel("vna.S21_phase"))
    re, im = np.asarray(be.read_channel("vna.S21_re")), np.asarray(be.read_channel("vna.S21_im"))
    assert emu.triggers == 1
    assert np.allclose(db, 20 * np.log10(np.hypot(re, im)))
    assert np.allclose(ph, np.degrees(np.arctan2(im, re)))
    # a channel read again: the next sweep
    be.read_channel("vna.S21")
    assert emu.triggers == 2
    f = np.asarray(be.read_channel("vna.frequencies"))
    assert len(f) == len(db) == 201 and f[0] == 4.99e9 and f[-1] == 5.01e9
    assert abs(f[np.argmin(db)] - 5e9) < 2e5                  # the resonance
    assert -33 < db.min() < -29 and -26 < db[0] < -24
    # the analyzer is left in HOLD after a single sweep
    assert emu.mode == "SING"
    assert not emu.unknown, emu.unknown
    assert not emu.errors, list(emu.errors)


def test_settings_and_refusals(backend):
    be = backend
    h, emu = add_vna(be)
    for ch, v in (("f_span", 1e8), ("f_center", 2e9), ("points", 1601), ("if_bandwidth", 100),
                  ("power", -20), ("average_count", 16), ("averaging", "ON"),
                  ("sweep_type", "LOG"), ("output", "OFF"), ("correction", "OFF")):
        be.write_channel("vna." + ch, v)
        assert be.read_channel("vna." + ch) == v, ch
    assert be.read_channel("vna.f_start") == pytest.approx(1.95e9)
    f = np.asarray(be.read_channel("vna.frequencies"))
    assert f[0] == pytest.approx(1.95e9) and f[-1] == pytest.approx(2.05e9)
    assert f[1] / f[0] == pytest.approx(f[-1] / f[-2])          # logarithmic
    assert be.read_channel("vna.sweep_time") > 1
    with pytest.raises(ChannelError, match="9 GHz"):
        be.write_channel("vna.f_stop", 12e9)
    with pytest.raises(ChannelError, match="1 … 16001"):
        be.write_channel("vna.points", 20000)
    with pytest.raises(ChannelError, match="must be one of"):
        be.write_channel("vna.if_bandwidth", 4000)
    # the analyzer's own refusal, with its message
    with pytest.raises(ChannelError, match="no calibration"):
        be.write_channel("vna.correction", "ON")
    with pytest.raises(ChannelError, match="out of range"):
        be.write_channel("vna.power", 30)
    assert be.read_channel("vna.power") == -20
    assert not emu.unknown, emu.unknown


def test_averaging_reverse_parameters_and_preset(backend):
    be = backend
    h, emu = add_vna(be, parameters="S21,S12")
    assert ours(emu) == {"EM_S21": "S21", "EM_S12": "S12"}      # created when connecting
    be.write_channel("vna.points", 101)
    be.write_channel("vna.power", -40)
    noisy = np.asarray(be.read_channel("vna.S21_re"))
    s12 = np.asarray(be.read_channel("vna.S12_re"))
    assert emu.triggers == 1                                     # both in one sweep
    be.write_channel("vna.averaging", "ON")
    be.write_channel("vna.average_count", 64)
    quiet = np.asarray(be.read_channel("vna.S21_re"))
    assert "SENS1:SWE:GRO:COUN 64" in emu.log and "SENS1:AVER:CLE" in emu.log
    ref = np.asarray(be.read_channel("vna.S12_re"))              # same averaged sweep
    assert emu.triggers == 2
    assert np.std(quiet - ref) < np.std(noisy - s12) / 3         # averaged: less noise
    # delete_traces: every measurement goes (the user's too), the settings stay; then
    # only what is read is measured - S21 alone: no reverse sweep
    be.write_channel("vna.delete_traces")
    assert emu.meas == {} and be.read_channel("vna.points") == 101
    be.read_channel("vna.S21")
    assert emu.meas == {"EM_S21": "S21"} and emu._directions() == 1
    # ... also as the first pre-sweep constant
    emu.meas["CH1_S22_1"] = "S22"                                # made at the front panel
    be.queue_add({"kind": "sweep", "definition": {"name": "s21", "dimensions": [
        {"points": 2, "record": ["vna.S21"]}],
        "pre": {"constants": [{"channel": "vna.delete_traces"}]}}}, start=True)
    assert be.queue.wait_idle(30)
    assert be.queue_state()["history"][-1]["status"] == "finished"
    assert emu.meas == {"EM_S21": "S21"}
    # preset: the analyzer starts over; the measurements are created again
    be.write_channel("vna.preset")
    assert emu.meas == {}
    be.read_channel("vna.S22")
    assert emu.meas == {"EM_S22": "S22"}
    # continuous for the front panel; the next reading is a single sweep again
    be.write_channel("vna.continuous")
    assert emu.mode == "CONT"
    be.read_channel("vna.S22")
    assert emu.mode == "SING"
    with pytest.raises(ChannelError, match="not one of"):
        be.add_instrument("vna2", "e8358a", "SIM::pna_x", {"parameters": "S31"})
        raise ChannelError(be.instruments.get("vna2").last_error)
    assert not emu.unknown, emu.unknown


def test_a_sweep_that_does_not_end(backend):
    be = backend
    h, emu = add_vna(be, time_scale=1e-4, sweep_timeout=0.3)    # a sweep of hours
    be.write_channel("vna.points", 3)
    t0 = time.monotonic()
    with pytest.raises(Exception, match="did not end"):
        h.read("S21")
    assert time.monotonic() - t0 < 5


def test_power_sweep_records_traces(backend):
    be = backend
    h, emu = add_vna(be)
    be.write_channel("vna.f_start", 4.995e9)
    be.write_channel("vna.f_stop", 5.005e9)
    be.write_channel("vna.points", 51)
    be.define_alias(name="P", target="vna.power", min=-60, max=0, unit="dBm")
    item = be.queue_add({"kind": "sweep", "definition": {"name": "power", "dimensions": [
        {"points": 4, "sweep": [{"channel": "P", "start": -40, "stop": -10}],
         "record": ["vna.S21", "vna.S21_phase"]}]}}, start=True)
    assert be.queue.wait_idle(60)
    last = be.queue_state()["history"][-1]
    assert last["status"] == "finished", last
    assert emu.triggers == 4                                     # one sweep per point
    from easymeasure.backend.storage import DataSet
    ds = DataSet.load(last["path"])
    assert ds.data["vna.S21"].shape == (4, 51) and ds.data["vna.S21_phase"].shape == (4, 51)
    f = ds.value_axes["vna.S21"][0]["coords"]
    assert f[0] == pytest.approx(4.995e9) and f[-1] == pytest.approx(5.005e9)
    # more power, less noise: the off-resonance baseline is smoother
    rough = [np.std(np.diff(ds.data["vna.S21"][i][:10])) for i in (0, 3)]
    assert rough[1] < rough[0]
