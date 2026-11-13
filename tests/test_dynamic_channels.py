"""Allowed values and channel lists that follow the instrument state."""

import pytest

from easymeasure.backend.instrument import ChannelSpec, Instrument, coerce_value
from easymeasure.common.errors import ChannelError, SweepDefinitionError


class ModeBox(Instrument):
    """Test driver: a 'source' whose mode decides which channel is forced, plus a
    meter whose reading channel is renamed by its function."""

    DESCRIPTION = "mode switching test box"
    CHANNELS = [
        ChannelSpec("mode", "rw", "str", (), config=True, allowed=["Voltage", "Current"]),
        ChannelSpec("voltage", "rw", "float", (), "V"),
        ChannelSpec("current", "r", "float", (), "A"),
        ChannelSpec("Icompliance", "rw", "float", (), "A", config=True),
        ChannelSpec("func", "rw", "str", (), config=True, allowed=["vdc", "iac"]),
        ChannelSpec("Vdc", "r", "float", (), "V"),
        ChannelSpec("nplc", "rw", "float", (), config=True, allowed=[0.02, 0.2, 1, 10, 100]),
        ChannelSpec("gate", "rw", "str", (), allowed=["Idle", "Run", "Hold"]),
    ]

    def connect(self):
        self.st = getattr(self, "st", None) or {"mode": "Voltage", "func": "vdc", "V": 0.0, "I": 0.0,
                                                 "comp": 0.1, "nplc": 1.0}
        self.writes = []

    def get_channels(self):
        v = self.st["mode"] == "Voltage"
        return [
            self.CHANNELS[0],
            ChannelSpec("voltage", "rw" if v else "r", "float", (), "V"),
            ChannelSpec("current", "r" if v else "rw", "float", (), "A"),
            ChannelSpec("Icompliance" if v else "Vcompliance", "rw", "float", (), "A" if v else "V",
                        config=True),
            self.CHANNELS[4],
            ChannelSpec("Vdc" if self.st["func"] == "vdc" else "Iac", "r", "float", (),
                        "V" if self.st["func"] == "vdc" else "A"),
            self.CHANNELS[6],
            self.CHANNELS[7],
        ]

    def read(self, ch):
        st = self.st
        return {"mode": st["mode"].upper(),                 # instrument spells it differently
                "voltage": st["V"], "current": st["I"], "Icompliance": st["comp"],
                "Vcompliance": st["comp"], "func": st["func"], "Vdc": 1.5, "Iac": 0.25,
                "nplc": st["nplc"], "gate": st.get("gate", "Idle")}[ch]

    def write(self, ch, v):
        self.writes.append((ch, v))
        if ch in ("mode", "func"):
            self.st[ch] = v
        elif ch == "voltage":
            self.st["V"] = v
        elif ch == "current":
            self.st["I"] = v
        elif ch in ("Icompliance", "Vcompliance"):
            self.st["comp"] = v
        elif ch == "nplc":
            self.st["nplc"] = v
        elif ch == "gate":
            self.st["gate"] = v


@pytest.fixture
def box(backend):
    backend.drivers.register("modebox", ModeBox)
    backend.add_instrument("box", "modebox")
    return backend


def test_allowed_values_spec_and_coercion():
    s = ChannelSpec("mode", "rw", "str", (), allowed=["Voltage", "Current"])
    assert coerce_value("  voltage ", s, "write") == "Voltage"
    assert coerce_value("CURRENT", s, "read") == "Current"
    assert coerce_value("weird", s, "read") == "weird"       # reads are not refused
    with pytest.raises(ChannelError, match="one of Voltage, Current"):
        coerce_value("Power", s, "write")
    n = ChannelSpec("nplc", "rw", "float", (), allowed=[0.02, 1, 10])
    assert coerce_value("1", n, "write") == 1.0
    assert coerce_value(0.0200000000001, n, "write") == 0.02
    with pytest.raises(ChannelError):
        coerce_value(2, n, "write")
    assert s.to_dict()["allowed"] == ["Voltage", "Current"]
    assert ChannelSpec.from_dict(n.to_dict()).allowed == [0.02, 1.0, 10.0]
    with pytest.raises(ValueError):
        ChannelSpec("v", "rw", "float", (-1,), allowed=[1, 2])   # vectors cannot have choices
    with pytest.raises(ValueError):
        ChannelSpec("v", "rw", "str", (), allowed=[])


def test_allowed_values_through_backend(box):
    assert box.write_channel("box.mode", "current") == "Current"
    assert box.read_channel("box.mode") == "Current"        # 'CURRENT' normalised
    with pytest.raises(ChannelError, match="one of"):
        box.write_channel("box.mode", "Resistance")
    assert box.write_channel("box.nplc", 10) == 10.0
    with pytest.raises(ChannelError):
        box.write_channel("box.nplc", 3)
    info = box.channel_info("box.nplc")
    assert info["choices"] == [0.02, 0.2, 1, 10, 100]
    desc = {c["name"]: c for c in box.list_instruments()[0]["channels"]}
    assert desc["mode"]["allowed"] == ["Voltage", "Current"]
    # aliases inherit the instrument's choices; an alias list narrows them
    box.define_alias(name="G", target="box.gate")
    box.define_alias(name="G2", target="box.gate", allowed=["run", "hold"])
    al = {a["name"]: a for a in box.list_channels()["aliases"]}
    assert al["G"]["choices"] == ["Idle", "Run", "Hold"]
    assert al["G2"]["choices"] == ["Run", "Hold"] and al["G2"]["allowed"] == ["run", "hold"]
    assert box.write_channel("G", "run") == "Run"
    assert box.write_channel("G2", "HOLD") == "Hold"
    from easymeasure.common.errors import LimitError
    with pytest.raises(LimitError):
        box.write_channel("G2", "Idle")
    with pytest.raises(ChannelError):
        box.write_channel("G", "Stop")
    # the instrument's own channel is written as it is (the alias narrows only itself)
    box.channels.set("box.gate", "idle")
    assert box.read_channel("G") == "Idle"


def test_mode_switch_changes_channel_list(box):
    events = []
    box.add_listener(lambda kind, data: events.append((kind, data)))
    h = box.instruments.get("box")
    assert h.spec("voltage").writable and not h.spec("current").writable
    assert h.spec("Icompliance").unit == "A"
    box.define_alias(name="Vs", target="box.voltage")
    box.define_alias(name="Is", target="box.current")
    box.define_alias(name="M", target="box.Vdc")
    box.write_channel("Vs", 0.5, ramp=False)
    assert box.channels.last_value("Vs") == 0.5
    events.clear()

    box.write_channel("box.mode", "Current")
    assert ("changed", {"what": "instruments"}) in events
    assert not h.spec("voltage").writable and h.spec("current").writable
    assert "Icompliance" not in h.channels
    assert h.spec("Vcompliance").unit == "V"
    assert box.channels.last_value("Vs") is None            # cached value forgotten
    info = {a["name"]: a for a in box.list_channels()["aliases"]}
    assert info["Vs"]["writable"] is False and info["Is"]["writable"] is True
    box.write_channel("Is", 1e-3, ramp=False)
    with pytest.raises(ChannelError, match="read-only"):
        box.write_channel("Vs", 1.0)
    with pytest.raises(ChannelError, match=r"no channel 'Icompliance' \(channels now: .*Vcompliance"):
        box.read_channel("box.Icompliance")
    assert box.write_channel("box.Vcompliance", 2.0) == 2.0

    # a renamed reading channel: the alias reports the problem instead of reading
    # something else under the old name
    events.clear()
    box.write_channel("box.func", "iac")
    assert ("changed", {"what": "instruments"}) in events
    assert h.spec("Iac").unit == "A"
    info = {a["name"]: a for a in box.list_channels()["aliases"]}
    assert "Vdc" in info["M"]["error"]
    with pytest.raises(ChannelError):
        box.read_channel("M")
    # writes that do not change the list emit nothing
    events.clear()
    box.write_channel("box.nplc", 1)
    assert not [e for e in events if e[0] == "changed"]
    # the change survives a reconnect (driver asked again after connect)
    box.reconnect_instrument("box")
    assert "Iac" in h.channels


def test_sweep_after_pre_constant_mode_switch(box):
    box.define_alias(name="Is", target="box.current")
    box.define_alias(name="Vm", target="box.voltage")
    d = {"name": "isweep",
         "dimensions": [{"points": 3, "sweep": [{"channel": "Is", "start": 0, "stop": 1e-3}],
                         "record": ["Vm"]}],
         "pre": {"constants": [{"channel": "box.mode", "value": "Current"},
                               {"channel": "box.Vcompliance", "value": 5.0}]},
         "post": {"constants": [{"channel": "Is", "value": 0.0},
                                {"channel": "box.mode", "value": "Voltage"},
                                {"channel": "box.Icompliance", "value": 0.01}]}}
    # in voltage mode "Is" is read-only, but the pre-sweep constants fix that
    warnings = box.validate_sweep(d)["warnings"]
    assert any("checked again after the pre-sweep constants" in w for w in warnings)
    assert any("Vcompliance" in w and "checked when applied" in w for w in warnings)
    box.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert box.queue.wait_idle(30)
    item = box.queue_state()["history"][-1]
    assert item["status"] == "finished", item
    h = box.instruments.get("box")
    assert h.driver.st["mode"] == "Voltage" and h.driver.st["comp"] == 0.01
    assert ("current", 1e-3) in h.driver.writes

    # without the mode switch the sweep is refused up front ...
    bad = dict(d, pre={"constants": []}, post={"constants": []})
    with pytest.raises(SweepDefinitionError, match="read-only"):
        box.validate_sweep(bad)
    # ... and a pre-sweep constant that does not help stops the run before point 1
    useless = dict(bad, pre={"constants": [{"channel": "box.nplc", "value": 10}]})
    box.queue_add({"kind": "sweep", "definition": useless}, start=True)
    assert box.queue.wait_idle(30)
    item = box.queue_state()["history"][-1]
    assert item["status"] == "error" and "read-only" in item.get("error", ""), item
