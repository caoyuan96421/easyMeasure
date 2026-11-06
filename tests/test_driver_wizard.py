"""Driver Wizard: definitions, generated driver files, templates on emulated instruments."""

import copy
import math

import numpy as np
import pytest

from easymeasure.common.errors import ChannelError, DriverError
from easymeasure.drivers import _command
from easymeasure.drivers.emulators import SCPIEmulator, ensure_emulator
from easymeasure.drivers.emulators.dmm import HP34401Emulator
from easymeasure.drivers.emulators.smu import Keithley2400Emulator
from easymeasure.drivers.simbus import SimDevice, get_bus


def template(title, **changes):
    t = next(t for t in _command.templates() if t["title"] == title)
    d = copy.deepcopy(t["definition"])
    d.update(changes)
    return d


def emu(addr):
    return get_bus(addr.split("::", 1)[1]).devices[0]


def test_templates_are_valid_and_render(backend):
    titles = [t["title"] for t in backend.wizard_templates()]
    assert titles[:3] == ["Generic SCPI", "SCPI source-measure unit", "SCPI multimeter"]
    for t in backend.wizard_templates():
        r = backend.wizard_check(t["definition"])
        assert r["errors"] == [], (t["title"], r["errors"])
        src = r["source"]
        assert src.startswith(_command.GENERATED_MARK)
        compile(src, "x.py", "exec")
        ns = {}
        exec(src, ns)
        cls = ns["DRIVER"]
        assert [c.name for c in cls.CHANNELS] == [c["name"] for c in t["definition"]["channels"]]


def test_check_reports_problems(backend):
    base = template("SCPI multimeter")
    cases = [
        (dict(base, name="My-DMM"), "driver name"),
        (dict(base, name="keithley2400"), "taken by a built-in driver"),
        (dict(base, channels=base["channels"] + [base["channels"][0]]), "duplicate name"),
        (dict(base, channels=[{"name": "x", "access": "r", "dtype": "float"}]), "needs a get"),
        (dict(base, channels=[{"name": "x", "access": "w", "dtype": "float", "set": "X 1"}]),
         "needs {value}"),
        (dict(base, channels=[{"name": "x", "access": "rw", "dtype": "str", "get": "X?",
                               "set": "X {value}", "allowed": ["A"], "map": {"B": "b"}}]),
         "not among the allowed"),
        (dict(base, channels=[{"name": "v", "access": "rw", "dtype": "vector", "get": "V?",
                               "set": "V {value}"}]), "vectors can only be read"),
        (dict(base, channels=[{"name": "x", "access": "r", "dtype": "float", "get": "X?",
                               "scale": 0}]), "scale"),
        (dict(base, init={"commands": [], "script": "if:"}), "init script: syntax error"),
        (dict(base, actions=[{"name": "go", "commands": []}]), "at least one command"),
        (dict(base, channels=[{"name": "x", "access": "rw", "dtype": "float", "get": "X?",
                               "set": "X {val}"}]), "needs {value}"),
    ]
    for d, msg in cases:
        errs = backend.wizard_check(d)["errors"]
        assert any(msg in e for e in errs), (msg, errs)
    warn = backend.wizard_check(dict(base, channels=[
        {"name": "x", "access": "r", "dtype": "float", "get": "READ"}]))["warnings"]
    assert any("does not end in '?'" in w for w in warn)
    with pytest.raises(DriverError):
        backend.wizard_save(dict(base, name="Bad Name"))


def test_smu_template_on_an_emulated_2400(backend):
    be = backend
    d = template("SCPI source-measure unit", name="wiz_smu")
    ensure_emulator("SIM::wsmu", 0, lambda: Keithley2400Emulator({"load_ohm": 1000.0}))
    r = be.wizard_save(d)
    assert r["driver"] == "wiz_smu" and r["path"].endswith("drivers/wiz_smu.py")
    h = be.add_instrument("smu", "wiz_smu", "SIM::wsmu")
    assert h["state"] == "connected", h["last_error"]
    e = emu("SIM::wsmu")
    assert ":SOUR:VOLT:MODE FIX" in e.log and "*CLS" in e.log        # init commands
    ch = {c["name"]: c for c in h["channels"]}
    assert ch["mode"]["allowed"] == ["Voltage", "Current"] and ch["mode"]["config"]
    assert be.read_channel("smu.mode") == "Voltage"                   # reply VOLT -> Voltage
    assert be.read_channel("smu.output") == "OFF"                     # reply 0 -> OFF
    be.write_channel("smu.output", "on")
    assert e.out.on and be.read_channel("smu.output") == "ON"
    be.write_channel("smu.compliance", 0.01)
    be.define_alias(name="V", target="smu.voltage", max=5)
    be.define_alias(name="I", target="smu.current")
    be.write_channel("V", 1.5, ramp=False)
    assert be.read_channel("I") == pytest.approx(1.5e-3)
    # the error queue: a refused value comes back with the instrument's message
    with pytest.raises(ChannelError, match="Data out of range"):
        be.write_channel("smu.compliance", 5)
    with pytest.raises(ChannelError, match="one of"):
        be.write_channel("smu.mode", "Power")
    be.write_channel("smu.output", "OFF")
    be.write_channel("smu.mode", "Current")
    assert e.out.func == "CURR" and e.commands(":SOUR:FUNC CURR")
    assert e.unknown == []


def test_dmm_template_on_an_emulated_34401a(backend):
    be = backend
    d = template("SCPI multimeter", name="wiz_dmm")
    ensure_emulator("SIM::wdmm", 0, lambda: HP34401Emulator({}))
    be.wizard_save(d)
    be.add_instrument("dmm", "wiz_dmm", "SIM::wdmm")
    e = emu("SIM::wdmm")
    assert be.read_channel("dmm.function") == "DC V"                  # "VOLT" -> DC V
    assert be.read_channel("dmm.reading") == pytest.approx(1.234567, rel=1e-4)
    for f, func in (("AC V", "VOLT:AC"), ("DC I", "CURR:DC"), ("4W R", "FRES"), ("DC V", "VOLT:DC")):
        be.write_channel("dmm.function", f)
        assert e.func == func and be.read_channel("dmm.function") == f
    be.write_channel("dmm.nplc", 100)
    assert be.read_channel("dmm.nplc") == 100
    with pytest.raises(ChannelError, match="one of"):
        be.write_channel("dmm.nplc", 3)
    be.write_channel("dmm.autorange", "OFF")
    assert be.read_channel("dmm.autorange") == "OFF"
    assert 'FUNC "VOLT:AC"' in e.log
    assert e.unknown == []


class LineBox(SimDevice):
    """A non-SCPI test instrument: 'V?' / 'V 1.5', 'I?' in mA, 'T?' a trace, 'ID?'."""

    def __init__(self):
        super().__init__()
        self.v = 0.0
        self.log = []

    def handle(self, cmd):
        self.log.append(cmd)
        if cmd == "ID?":
            return "LINEBOX 2.1"
        if cmd == "V?":
            return "V=%+.4f" % self.v
        if cmd.startswith("V "):
            self.v = float(cmd[2:])
            return None
        if cmd == "I?":
            return "%.3f mA" % (self.v * 2.0)
        if cmd == "T?":
            return "1.0,2.5,-3e-1"
        if cmd == "M?":
            return "FAST"
        return None


def test_line_protocol_scale_vector_map_init_and_shutdown(backend):
    be = backend
    bus = get_bus("wline")
    with bus.lock:
        bus.devices[0] = box = LineBox()
    d = template("Simple line protocol (non-SCPI)", name="wiz_line", idn_query="ID?",
                 idn_match="LINEBOX")
    d["options"]["write_termination"] = "\n"
    d["options"]["query_delay"] = 0.01
    d["channels"] += [
        {"name": "current", "access": "r", "dtype": "float", "unit": "A", "get": "I?",
         "scale": 1e-3},
        {"name": "trace", "access": "r", "dtype": "vector", "unit": "V", "get": "T?"},
        {"name": "speed", "access": "r", "dtype": "str", "config": True, "get": "M?",
         "map": {"fast": "FAST", "slow": "SLOW"}},
    ]
    d["init"] = {"commands": ["V 0.25", "ID?"],
                 "script": "if not query('ID?').startswith('LINEBOX'):\n    raise IOError('x')\n"
                           "write('V %g' % (float(query('V?')[2:]) * 2))\nlog('doubled')"}
    d["shutdown"] = {"commands": ["V 0"]}
    d["actions"] = [{"name": "zero", "commands": ["V 0", "V?"]}]
    be.wizard_save(d)
    h = be.add_instrument("lb", "wiz_line", "SIM::wline")
    assert h["state"] == "connected", h["last_error"]
    assert box.v == pytest.approx(0.5)                                 # init commands + script
    assert be.read_channel("lb.current") == pytest.approx(1e-3)        # 1.000 mA
    np.testing.assert_allclose(be.read_channel("lb.trace"), [1.0, 2.5, -0.3])
    assert be.read_channel("lb.speed") == "fast"
    be.define_alias(name="S", target="lb.setpoint")
    be.write_channel("S", 1.25, ramp=False)
    assert box.v == 1.25 and be.read_channel("S") == pytest.approx(1.25)
    be.run_action("lb.zero")
    assert box.v == 0.0
    be.write_channel("S", 2.0, ramp=False)
    be.disconnect_instrument("lb")
    assert box.v == 0.0 and box.log[-1] == "V 0"                       # shutdown commands
    be.remove_alias("S")
    be.remove_instrument("lb")                                         # frees the port
    # a script that refuses the instrument
    d2 = copy.deepcopy(d)
    d2["name"] = "wiz_line2"
    d2["init"]["script"] = "raise IOError('wrong firmware')"
    be.wizard_save(d2)
    h = be.add_instrument("lb2", "wiz_line2", "SIM::wline")
    assert h["state"] == "error" and "wrong firmware" in h["last_error"]
    # identification mismatch
    d3 = dict(copy.deepcopy(d), name="wiz_line3", idn_match="OTHERBOX")
    be.wizard_save(d3)
    be.remove_instrument("lb2")
    h = be.add_instrument("lb3", "wiz_line3", "SIM::wline")
    assert h["state"] == "error" and "unexpected instrument" in h["last_error"]


def test_save_reopen_edit_reload_delete(backend):
    be = backend
    d = template("SCPI multimeter", name="wiz_edit")
    ensure_emulator("SIM::wedit", 0, lambda: HP34401Emulator({}))
    be.wizard_save(d)
    with pytest.raises(DriverError, match="exists already"):
        be.wizard_save(d)
    assert [x["name"] for x in be.wizard_list()] == ["wiz_edit"]
    loaded = be.wizard_load("wiz_edit")
    assert loaded["channels"][1]["map"]["DC V"] == "VOLT:DC"
    be.add_instrument("m", "wiz_edit", "SIM::wedit")
    # edit: rename a channel and add one; the instrument using it is reloaded
    loaded["channels"][0]["name"] = "volts"
    loaded["channels"].append({"name": "zero", "access": "rw", "dtype": "str", "config": True,
                               "get": "ZERO:AUTO?", "set": "ZERO:AUTO {value}",
                               "allowed": ["ON", "OFF"]})
    r = be.wizard_save(loaded, overwrite=True)
    assert r["reloaded"] == ["m"]
    h = be.instruments.get("m")
    assert "volts" in h.channels and "reading" not in h.channels and h.state == "connected"
    be.write_channel("m.zero", "OFF")
    assert be.read_channel("m.zero") == "OFF"                          # "0" -> OFF
    # a hand-written file of the same name is never replaced
    (be.user_driver_dir / "hand_made.py").write_text("x = 1\n")
    assert "not made with the wizard" in be.wizard_check(dict(d, name="hand_made"))["errors"][0]
    with pytest.raises(DriverError, match="used by m"):
        be.wizard_delete("wiz_edit")
    be.remove_instrument("m")
    be.wizard_delete("wiz_edit")
    assert be.wizard_list() == [] and "wiz_edit" not in be.list_drivers()["drivers"]


def test_try_reports_per_channel_and_leaves_nothing_behind(backend):
    be = backend
    ensure_emulator("SIM::wtry", 0, lambda: Keithley2400Emulator({}))
    d = template("SCPI source-measure unit", name="wiz_try")
    d["channels"].append({"name": "bogus", "access": "r", "dtype": "float", "get": ":NOPE?"})
    r = be.wizard_try(d, "SIM::wtry", {"retries": 0, "timeout": 0.3})
    assert r["connected"] and "KEITHLEY" in r["idn"]
    assert r["readings"]["mode"]["value"] == "Voltage"
    assert math.isfinite(r["readings"]["voltage"]["value"])
    assert "error" in r["readings"]["bogus"]                           # undefined header
    assert "error" in r["readings"]["current"]                         # 2400: output off
    assert "wizard_try" not in be.instruments.names()
    assert "__wizard_try__" not in be.list_drivers()["drivers"]
    r = be.wizard_try(d, "SIM::nobody_here", {"retries": 0, "timeout": 0.2})
    assert not r["connected"] and r["error"]
    r = be.wizard_try(dict(d, name="Bad Name"), "SIM::wtry")
    assert not r["connected"] and "driver name" in r["error"]
    # nothing was written to the instrument by trying
    assert not [c for c in emu("SIM::wtry").log if c.startswith((":SOUR:VOLT ", ":OUTP ON"))]


def test_wizard_commands_are_admin_only():
    from easymeasure.remote import policy
    for cmd in ("wizard_save", "wizard_try", "wizard_check", "wizard_load", "wizard_delete"):
        policy.check("admin", cmd, {}, [])
        with pytest.raises(policy.Denied):
            policy.check("operator", cmd, {}, [])
