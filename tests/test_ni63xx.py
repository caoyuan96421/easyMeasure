"""NI USB-63xx X Series driver against the NI-DAQmx emulator."""

import time

import pytest

from easymeasure.common.errors import ChannelError
from easymeasure.drivers import _nidaq_models as models
from easymeasure.drivers.emulators import nidaqmx_sim
from easymeasure.drivers.ni63xx import NI63xx

# model: (analog inputs RSE, DIFF, outputs, port-0 lines, AI ranges, AO ranges)
EXPECTED = {
    "USB-6341": (16, 8, 2, 8, 4, 1), "USB-6343": (32, 16, 4, 32, 4, 1),
    "USB-6346": (None, 8, 2, 8, 4, 1), "USB-6349": (None, 32, 2, 8, 4, 1),
    "USB-6351": (16, 8, 2, 8, 7, 2), "USB-6353": (32, 16, 4, 32, 7, 2),
    "USB-6356": (None, 8, 2, 8, 4, 2), "USB-6361": (16, 8, 2, 8, 7, 2),
    "USB-6363": (32, 16, 4, 32, 7, 2), "USB-6366": (None, 8, 2, 8, 4, 2),
}


def names(chs, prefix):
    import re
    return [c.name for c in chs if re.fullmatch(prefix + r"\d+", c.name)]


@pytest.mark.parametrize("model", sorted(EXPECTED))
def test_channel_counts_follow_the_model(model):
    se, diff, ao, p0, n_air, n_aor = EXPECTED[model]
    term = "RSE" if se else "DIFF"
    d = NI63xx("x", "DAQ::Dev1", {"model": model, "terminal": term})
    chs = d.get_channels()
    assert len(names(chs, "ai")) == (se or diff)
    assert len(names(chs, "ao")) == ao
    assert len(names(chs, "dio")) == p0 and len(names(chs, "pfi")) == 16
    assert names(chs, "ctr") == ["ctr0", "ctr1", "ctr2", "ctr3"]
    spec = {c.name: c for c in chs}
    assert len(spec["ai_range"].allowed) == n_air and len(spec["ao_range"].allowed) == n_aor
    assert spec["terminal"].allowed == (["RSE", "NRSE", "DIFF"] if se else ["DIFF"])
    assert spec["model"].allowed[0] == "auto" and model in spec["model"].allowed
    if se:                                        # differential pairs aiK / aiK+8
        d2 = NI63xx("x", "DAQ::Dev1", {"model": model, "terminal": "DIFF"})
        want = [k for k in range(se) if k % 16 < 8]
        assert names(d2.get_channels(), "ai") == ["ai%d" % k for k in want]


def test_product_type_lookup():
    assert models.lookup("USB-6343 (BNC)").product == "USB-6343"
    assert models.lookup("USB-6363 (Mass Termination)").ai == 32
    assert models.lookup("NI USB-6211").family == "M"
    assert models.lookup("PCIe-6363") is None and models.lookup("") is None


@pytest.fixture
def x(backend):
    nidaqmx_sim.reset()
    h = backend.add_instrument("x", "ni63xx", "DAQ::SIM")
    assert h["state"] == "connected", h["last_error"]
    return backend


def test_model_switch_repopulates_channels(x):
    be = x
    h = be.instruments.get("x")
    assert be.read_channel("x.model") == "USB-6363"           # auto in simulation
    assert "ai31" in h.channels and "ao3" in h.channels and "dio31" in h.channels
    be.write_channel("x.ai_range", "±0.1 V")
    be.write_channel("x.model", "USB-6341")
    h = be.instruments.get("x")
    assert be.read_channel("x.model") == "USB-6341"
    assert "ai15" in h.channels and "ai16" not in h.channels and h.spec("ai").shape == (16,)
    assert "ao1" in h.channels and "ao2" not in h.channels
    assert "dio7" in h.channels and "dio8" not in h.channels and "pfi15" in h.channels
    assert be.read_channel("x.ai_range") == "±10 V"            # ±0.1 V: not on a 6341
    with pytest.raises(ChannelError):
        be.write_channel("x.ai_range", "±0.1 V")
    with pytest.raises(ChannelError):
        be.write_channel("x.ao_range", "±5 V")
    be.write_channel("x.model", "USB-6346")                   # simultaneous: DIFF only
    h = be.instruments.get("x")
    assert be.read_channel("x.terminal") == "DIFF" and h.spec("ai").shape == (8,)
    be.write_channel("x.rate", 500e3)                          # per input, all 8 at once
    with pytest.raises(ChannelError, match="too fast"):
        be.write_channel("x.rate", 600e3)
    with pytest.raises(ChannelError):
        be.write_channel("x.terminal", "RSE")
    be.write_channel("x.model", "USB-6363")
    be.write_channel("x.rate", 2e6)                           # one input of a 6363
    be.write_channel("x.model", "USB-6341")                   # rate comes down to fit
    assert be.read_channel("x.rate") == pytest.approx(500e3)
    with pytest.raises(ChannelError):
        be.write_channel("x.model", "USB-6211")
    # the option sets it from the start, and the setup keeps it
    be.update_instrument("x", options={"model": "USB-6343"})
    h = be.instruments.get("x")
    assert h.state == "connected" and be.read_channel("x.model") == "USB-6343"
    assert "ai31" in h.channels and "ao3" in h.channels
    setup = be.get_setup()
    assert [i for i in setup["instruments"] if i["name"] == "x"][0]["options"]["model"] == "USB-6343"


def test_analog_loopback_ranges_and_rates(x):
    be = x
    h = be.instruments.get("x")
    be.channels.set_raw("x.ao3", 3.5)
    assert be.read_channel("x.ai3") == pytest.approx(3.5, abs=1e-3)
    assert be.read_channel("x.ai20") == pytest.approx(2.0, abs=1e-3)
    assert be.read_channel("x.ai").shape == (32,)
    # one input is acquired alone: up to 2 MS/s on a 6363, even with all 32 inputs exposed
    be.write_channel("x.rate", 2e6)
    assert be.read_channel("x.ai7") == pytest.approx(0.7, abs=1e-3)
    with pytest.raises(ChannelError, match="too fast"):
        be.write_channel("x.rate", 2.5e6)
    be.write_channel("x.rate", 40000)
    with pytest.raises(ChannelError, match="32 inputs at 40000 S/s each is too fast.*31250"):
        be.read_channel("x.ai")                               # 32 × 40 kS/s > 1 MS/s
    assert be.read_channel("x.ai7") == pytest.approx(0.7, abs=1e-3)
    be.write_channel("x.terminal", "DIFF")
    assert "ai8" not in h.channels and "ai16" in h.channels and "ai24" not in h.channels
    be.write_channel("x.rate", 60000)                         # 16 inputs × 60 k < 1 MS/s
    assert be.read_channel("x.ai").shape == (16,)
    assert be.read_channel("x.ai16") == pytest.approx(1.6, abs=1e-3)
    be.update_instrument("x", options={"ai_channels": "5", "rate": 2e6, "samples": 4})
    h = be.instruments.get("x")
    assert be.read_channel("x.ai5") == pytest.approx(0.5, abs=1e-3)   # 2 MS/s: one input
    # output range
    with pytest.raises(ChannelError, match="±10"):
        be.channels.set_raw("x.ao0", 11)
    be.channels.set_raw("x.ao0", 7)
    with pytest.raises(ChannelError, match="ao0 is at 7"):
        be.write_channel("x.ao_range", "±5 V")
    be.channels.set_raw("x.ao0", 1)
    be.write_channel("x.ao_range", "±5 V")
    with pytest.raises(ChannelError, match="±5"):
        be.channels.set_raw("x.ao0", 6)
    be.channels.set_raw("x.ao0", -4.5)
    assert be.read_channel("x.ao0") == -4.5


def test_digital_lines_and_counters(x):
    be = x
    # line K is wired to line K+16 of the 32-line port 0 (and PFI 0 to PFI 4)
    assert be.read_channel("x.dio16") == 0.0
    be.channels.set_raw("x.dio0", 1)
    assert be.read_channel("x.dio16") == 1.0 and be.read_channel("x.dio0") == 1.0
    be.channels.set_raw("x.pfi4", 1)
    assert be.read_channel("x.pfi0") == 1.0
    be.channels.set_raw("x.dio16", 0)                # now an output too: drives its own level
    assert be.read_channel("x.dio16") == 0.0
    be.run_action("x.release_digital")
    assert be.instruments.get("x").driver.dout == {}
    assert be.read_channel("x.dio0") == 0.0 and be.read_channel("x.pfi0") == 0.0
    with pytest.raises(ChannelError):
        be.channels.set_raw("x.dio3", 2)
    # counters start on first reading and reserve their source PFI
    c = be.read_channel("x.ctr0")
    time.sleep(0.03)
    assert be.read_channel("x.ctr0") > c
    with pytest.raises(ChannelError, match="source of ctr0"):
        be.channels.set_raw("x.pfi8", 1)          # counter 0's source
    assert be.read_channel("x.ctr0") > c          # still counting: no retry / recovery
    be.channels.set_raw("x.pfi9", 1)                 # the others are free
    be.run_action("x.reset_counters")
    assert be.read_channel("x.ctr0") < 20
    # a recovery closes every task: driven lines come back at their level
    be.instruments.get("x").driver.recover()
    assert be.read_channel("x.pfi9") == 1.0 and be.read_channel("x.pfi13") == 1.0
    assert be.instruments.get("x").state == "connected"


def test_model_checked_against_the_device(backend, monkeypatch):
    """With a real device the reported product decides (auto) or is checked."""
    nidaqmx_sim.reset()
    monkeypatch.setattr(NI63xx, "_module", lambda self: nidaqmx_sim)   # stands in for nidaqmx
    nidaqmx_sim.define("Dev7", "USB-6343")
    nidaqmx_sim.define("Dev8", "USB-6211")
    for dev in ("Dev9", "Dev10", "Dev11"):
        nidaqmx_sim.define(dev, "USB-6343")
    be = backend
    h = be.add_instrument("a", "ni63xx", "DAQ::Dev7")
    assert h["state"] == "connected" and be.read_channel("a.model") == "USB-6343"
    with pytest.raises(ChannelError, match="Dev7 is a USB-6343, not a USB-6363"):
        be.write_channel("a.model", "USB-6363")
    h = be.add_instrument("b", "ni63xx", "DAQ::Dev9", {"model": "USB-6361"})
    assert h["state"] == "error" and "not a USB-6361" in h["last_error"]
    h = be.add_instrument("c", "ni63xx", "DAQ::Dev8")
    assert h["state"] == "error" and "ni6211" in h["last_error"]
    # a device type the table does not know: the model setting is trusted
    monkeypatch.setattr(nidaqmx_sim.Device, "product_type", property(lambda self: "USB-63xx OEM"))
    h = be.add_instrument("d", "ni63xx", "DAQ::Dev10", {"model": "USB-6343"})
    assert h["state"] == "connected" and "ai31" in be.instruments.get("d").channels
    h = be.add_instrument("e", "ni63xx", "DAQ::Dev11")
    assert h["state"] == "error" and "set the model" in h["last_error"]


def test_without_ni_software(backend):
    h = backend.add_instrument("daq", "ni63xx", "DAQ::Dev1")
    assert h["state"] == "error"
    assert "Dev1" in h["last_error"] or "nidaqmx" in h["last_error"]
