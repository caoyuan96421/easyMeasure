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
    be.write_channel("lb.zero")
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


def test_generated_driver_in_a_sweep_after_a_restart_and_extended_by_hand(backend, tmp_path):
    """The saved file is a real driver: an alias on it is limit-checked and swept, it is
    found again by a restarted backend (with the autosaved setup), and a hand-made
    extension of its class works and is protected from being overwritten silently."""
    from easymeasure.backend.service import Backend
    from easymeasure.backend.storage import DataSet
    be = backend
    ensure_emulator("SIM::wpers", 0, lambda: Keithley2400Emulator({"load_ohm": 1000.0}))
    d = template("SCPI source-measure unit", name="wiz_persist", idn_match="MODEL 2400")
    be.wizard_save(d)
    assert be.add_instrument("smu", "wiz_persist", "SIM::wpers")["state"] == "connected"
    be.define_alias(name="Vs", target="smu.voltage", min=-1, max=1)
    be.define_alias(name="Im", target="smu.current")
    sweep = {"name": "wizsweep", "dimensions": [{"points": 11, "sweep": [
                 {"channel": "Vs", "start": 0, "stop": 1}], "record": ["Im"]}],
             "pre": {"constants": [{"channel": "smu.compliance", "value": 0.01},
                                   {"channel": "smu.output", "value": "ON"}]},
             "post": {"constants": [{"channel": "Vs", "value": 0},
                                    {"channel": "smu.output", "value": "OFF"}]}}
    bad = copy.deepcopy(sweep)
    bad["dimensions"][0]["sweep"][0]["stop"] = 2                       # beyond the alias limit
    with pytest.raises(Exception, match="(?i)limit|max|range"):
        be.queue_add({"kind": "sweep", "definition": bad})
    be.queue_add({"kind": "sweep", "definition": sweep}, start=True)
    assert be.queue.wait_idle(30)
    item = be.queue_state()["history"][-1]
    assert item["status"] == "finished", item
    ds = DataSet.load(item["path"])
    np.testing.assert_allclose(ds.data["Im"], np.linspace(0, 1, 11) / 1000.0, rtol=1e-4)
    assert not emu("SIM::wpers").out.on                                 # post constants
    be.close()

    # a restarted backend finds the driver in <home>/drivers and restores the setup
    be2 = Backend(home=tmp_path / "emhome", log_to_console=False)
    try:
        assert "wiz_persist" in be2.list_drivers()["drivers"]
        assert be2.restore_autosave() == []
        assert be2.instruments.get("smu").state == "connected"
        assert be2.read_channel("smu.mode") == "Voltage"
        assert [x["name"] for x in be2.wizard_list()] == ["wiz_persist"]

        # extend the class by hand: a channel computed in Python
        path = be2.user_driver_dir / "wiz_persist.py"
        assert not _command.edited_by_hand(path)
        src = path.read_text()
        src = src.replace("    DEFINITION = DEFINITION\n", """    DEFINITION = DEFINITION

    def read(self, channel):
        if channel == "voltage":
            return 1000.0 * float(super().read(channel))     # in mV, by hand
        return super().read(channel)
""")
        path.write_text(src)
        assert _command.edited_by_hand(path)
        be2.rescan_drivers()
        be2.update_instrument("smu")
        be2.write_channel("Vs", 0.25, ramp=False)
        assert be2.read_channel("smu.voltage") == pytest.approx(250.0)
        # the wizard still reopens it, and warns before replacing the hand-made part
        loaded = be2.wizard_load("wiz_persist")
        assert [c["name"] for c in loaded["channels"]][:2] == ["mode", "voltage"]
        assert any("edited by hand" in w for w in be2.wizard_check(loaded)["warnings"])
        be2.wizard_save(loaded, overwrite=True)
        assert (be2.user_driver_dir / "wiz_persist.py.bak").read_text() == src
        assert not _command.edited_by_hand(path)
        assert be2.read_channel("smu.voltage") == pytest.approx(0.25)    # reloaded, plain again
    finally:
        be2.close()


def test_generic_scpi_template_actions_and_error_queue(backend):
    """The bare template on a real SCPI instrument: actions run, a refused setting
    raises the instrument's own error message, and an unknown query is reported."""
    be = backend
    ensure_emulator("SIM::wgen", 0, lambda: Keithley2400Emulator({}))
    d = template("Generic SCPI", name="wiz_generic")
    d["channels"] = [
        {"name": "nplc", "access": "rw", "dtype": "float", "config": True, "unit": "PLC",
         "get": ":SENS:CURR:NPLC?", "set": ":SENS:CURR:NPLC {value:.2f}"},
        {"name": "value", "access": "r", "dtype": "float", "get": "MEAS?"}]
    be.wizard_save(d)
    assert be.add_instrument("g", "wiz_generic", "SIM::wgen")["state"] == "connected"
    e = emu("SIM::wgen")
    be.write_channel("g.nplc", 2.345)
    assert ":SENS:CURR:NPLC 2.35" in e.log and be.read_channel("g.nplc") == pytest.approx(2.35)
    with pytest.raises(ChannelError, match="(?i)data out of range|-222"):
        be.write_channel("g.nplc", 50)                                  # 2400: max 10 PLC
    with pytest.raises(ChannelError, match="refused MEAS\\?: .*-113"):
        be.read_channel("g.value")                  # MEAS? is not a 2400 command: no reply
    assert be.instruments.get("g").state == "connected"                # and no reconnect
    e.out.nplc = 7.0
    be.write_channel("g.reset")
    assert e.out.nplc == 1.0 and "*RST" in e.log
    be.write_channel("g.clear_status")


def test_wizard_commands_are_admin_only():
    from easymeasure.remote import policy
    for cmd in ("wizard_save", "wizard_try", "wizard_check", "wizard_load", "wizard_delete"):
        policy.check("admin", cmd, {}, [])
        with pytest.raises(policy.Denied):
            policy.check("operator", cmd, {}, [])
