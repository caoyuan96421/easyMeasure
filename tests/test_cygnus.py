"""Cygnus 16430 8-channel supply driver against the emulated firmware."""

import time

import numpy as np
import pytest

from easymeasure.common.errors import ChannelError
from easymeasure.drivers.cygnus16430 import Cygnus16430, adc_period
from easymeasure.drivers.emulators import cygnus as emu_mod
from easymeasure.drivers.simbus import get_bus


def emu(name="cyg"):
    return get_bus(name).devices[0]


@pytest.fixture
def cyg(backend):
    h = backend.add_instrument("cyg", "cygnus16430", "SIM::cyg", {"load_ohm": 1e4})
    assert h["state"] == "connected", h["last_error"]
    return backend


def test_channels_and_interfaces(cyg):
    be = cyg
    h = be.instruments.get("cyg")
    assert h.driver.idn == "Cygnus-16430,8CH,0,1.0.0"
    names = set(h.channels)
    for k in range(1, 9):
        assert {"voltage%d" % k, "Vmeas%d" % k, "Imeas%d" % k, "Ilimit%d" % k,
                "output%d" % k} <= names
    assert {"voltAll", "IlimitAll", "VmeasAll", "ImeasAll"} <= names
    assert h.spec("voltAll").shape == (8,) and h.spec("ImeasAll").shape == (8,)
    assert not ({"voltages", "Vmeas", "Imeas"} & names)
    assert h.spec("output3").allowed == ["ON", "OFF"] and h.spec("output3").config
    assert Cygnus16430.INTERFACES == ("serial", "sim")
    with pytest.raises(ChannelError, match="does not take GPIB"):
        be.add_instrument("x", "cygnus16430", "GPIB0::5::INSTR")


def test_outputs_limits_and_readback(cyg):
    be = cyg
    raw = be.channels.set_raw
    raw("cyg.Ilimit1", 0.005)
    raw("cyg.voltage1", 10.0)
    assert be.read_channel("cyg.Vmeas1") == pytest.approx(10.0)       # relay open: the node
    assert be.read_channel("cyg.Imeas1") == pytest.approx(0.0)
    raw("cyg.output1", "ON")
    assert be.read_channel("cyg.output1") == "ON" and be.read_channel("cyg.output2") == "OFF"
    assert be.read_channel("cyg.Imeas1") == pytest.approx(1e-3)        # 10 V / 10 kΩ
    raw("cyg.voltage1", 100.0)                                         # 10 mA > 5 mA limit
    assert be.read_channel("cyg.Imeas1") == pytest.approx(0.005)
    assert be.read_channel("cyg.Vmeas1") == pytest.approx(50.0)        # constant current
    assert be.read_channel("cyg.voltage1") == pytest.approx(100.0)     # the setpoint
    assert be.read_channel("cyg.Ilimit1") == pytest.approx(0.005)
    # the other outputs are independent
    raw("cyg.Ilimit8", 0.04)
    raw("cyg.voltage8", 150.0)
    raw("cyg.output8", "ON")
    assert be.read_channel("cyg.Imeas8") == pytest.approx(0.015)
    assert be.read_channel("cyg.Imeas1") == pytest.approx(0.005)
    # all at once
    raw("cyg.voltAll", [1, 2, 3, 4, 5, 6, 7, 8])
    assert list(be.read_channel("cyg.voltAll")) == [1, 2, 3, 4, 5, 6, 7, 8]
    vm = be.read_channel("cyg.VmeasAll")
    assert np.allclose(vm, [1, 2, 3, 4, 5, 6, 7, 8])
    im = be.read_channel("cyg.ImeasAll")
    assert im[0] == pytest.approx(1e-4) and im[7] == pytest.approx(8e-4) and im[3] == 0.0
    e = emu()
    assert "VOLTA 1.000000,2.000000,3.000000,4.000000,5.000000,6.000000,7.000000,8.000000" in e.log
    # limits: refused before anything is sent
    for ch, val in (("voltage2", 151), ("voltage2", -1), ("Ilimit2", 0.041)):
        with pytest.raises(ChannelError, match="outside"):
            raw("cyg." + ch, val)
    with pytest.raises(ChannelError, match="nothing was set"):
        raw("cyg.voltAll", [1, 2, 3, 4, 5, 6, 7, 200])
    assert list(be.read_channel("cyg.voltAll")) == [1, 2, 3, 4, 5, 6, 7, 8]
    raw("cyg.IlimitAll", [0.001] * 7 + [0.04])
    assert list(be.read_channel("cyg.IlimitAll")) == [0.001] * 7 + [0.04]
    assert "CURRA " + ",".join(["0.00100000"] * 7 + ["0.04000000"]) in e.log
    assert be.read_channel("cyg.ImeasAll")[0] == pytest.approx(1e-4)   # 1 V: under 1 mA
    with pytest.raises(ChannelError, match="outside 0 … 40 mA"):
        raw("cyg.IlimitAll", [0.05] * 8)
    assert e.unknown == [] and e.err == ""            # only commands the firmware knows


def test_refusal_reports_the_firmware_message(cyg, monkeypatch):
    monkeypatch.setattr(emu_mod, "VMAX", 100.0)       # a firmware with a lower maximum
    with pytest.raises(ChannelError, match=r"refused VOLT 3,120.000000: VOLT: > 150.0 V max"):
        cyg.channels.set_raw("cyg.voltage3", 120.0)
    assert cyg.instruments.get("cyg").state == "connected"
    assert cyg.read_channel("cyg.voltage3") == 0.0


def test_readings_wait_for_fresh_conversions(cyg):
    be = cyg
    d = be.instruments.get("cyg").driver
    assert d.settle_time() == pytest.approx(2 * adc_period(13))       # 2 × 76 ms
    be.channels.set_raw("cyg.Ilimit2", 0.04)
    be.channels.set_raw("cyg.output2", "ON")
    t0 = time.monotonic()
    be.channels.set_raw("cyg.voltage2", 20.0)
    assert be.read_channel("cyg.Imeas2") == pytest.approx(2e-3)       # not the old 0
    assert time.monotonic() - t0 >= adc_period(13)
    # without settling the reading right after a change is the stale one
    be.update_instrument("cyg", options={"load_ohm": 1e4, "settle": False})
    be.channels.set_raw("cyg.voltage2", 30.0)
    assert be.read_channel("cyg.Imeas2") == pytest.approx(2e-3)
    time.sleep(2 * adc_period(13))
    assert be.read_channel("cyg.Imeas2") == pytest.approx(3e-3)
    be.update_instrument("cyg", options={"load_ohm": 1e4})
    # slower ADC and a filter lengthen the wait; both reach the firmware
    be.write_channel("cyg.Vrate", 20)
    assert be.read_channel("cyg.Vrate") == 20.0 and emu().sf["V"] == 20
    be.write_channel("cyg.Ifilter", 0.5)
    assert emu().alpha["CURR"] == [0.5] * 8 and be.read_channel("cyg.Ifilter") == 0.5
    d = be.instruments.get("cyg").driver
    assert d.settle_time() == pytest.approx(8 * adc_period(13))       # ceil(ln.01/ln.5)+1
    for bad in (("Vrate", 12), ("Vrate", 13.5), ("Vfilter", 0), ("Vfilter", 1.5)):
        with pytest.raises(ChannelError):
            be.write_channel("cyg." + bad[0], bad[1])


def test_actions(cyg):
    be = cyg
    e = emu()
    for k in (1, 4):
        be.channels.set_raw("cyg.output%d" % k, "ON")
    with pytest.raises(ChannelError, match="switch every output off first.*1, 4"):
        be.run_action("cyg.auto_calibrate")
    be.run_action("cyg.all_off")
    assert e.on == [False] * 8
    be.run_action("cyg.auto_calibrate")
    assert e.cal_runs == 1
    be.channels.set_raw("cyg.voltage5", 42.0)
    be.write_channel("cyg.Vfilter", 0.3)
    be.run_action("cyg.reset")
    assert e.v_set == [0.0] * 8 and be.read_channel("cyg.Vfilter") == 1.0
    assert e.unknown == []


def test_sweep_over_one_output(cyg):
    be = cyg
    be.channels.set_raw("cyg.Ilimit3", 0.04)
    be.channels.set_raw("cyg.output3", "ON")
    be.define_alias(name="V3", target="cyg.voltage3", min=0, max=20)
    be.define_alias(name="I3", target="cyg.Imeas3")
    be.queue_add({"kind": "sweep", "definition": {"name": "cyg", "dimensions": [
        {"points": 5, "sweep": [{"channel": "V3", "start": 0, "stop": 20}], "record": ["I3"]}],
        "post": {"constants": [{"channel": "V3", "value": 0}]}}}, start=True)
    assert be.queue.wait_idle(30)
    item = be.queue_state()["history"][-1]
    assert item["status"] == "finished", item
    from easymeasure.backend.storage import DataSet
    ds = DataSet.load(item["path"])
    np.testing.assert_allclose(ds.data["I3"], np.linspace(0, 20, 5) / 1e4, atol=1e-9)
