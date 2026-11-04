"""Every action of every driver, through every path (API, script, sweep hook),
with and without hardware faults."""

import socket
import threading
import time

import pytest

from easymeasure.backend.drivers import DriverRegistry
from easymeasure.backend.instrument import Instrument
from easymeasure.backend.storage import DataSet
from easymeasure.common.errors import EasyMeasureError, InstrumentError, InstrumentTimeout


# ---------------------------------------------------------------- registry --
def test_every_declared_action_is_implemented():
    """A driver must implement each action it declares (or override action())."""
    reg = DriverRegistry()
    desc = reg.describe()
    assert desc["errors"] == {}
    checked = 0
    for name in desc["drivers"]:
        cls = reg.get(name)
        overrides = cls.action is not Instrument.action
        for a in getattr(cls, "ACTIONS", []):
            assert overrides or callable(getattr(cls, "_action_" + a.name, None)), \
                "%s declares action %r but has no _action_%s()" % (name, a.name, a.name)
            checked += 1
    assert checked >= 15


# ------------------------------------------------------- simulated drivers --
def test_all_simulated_actions_and_effects(demo):
    be = demo
    be.add_instrument("chaos", "sim_chaos", options={"call_timeout": 0.5, "exception_rate": 0.0})
    expected = {
        "src": {"clear_status"},
        "dmm": {"reset", "trigger"},
        "lockin": {"auto_phase", "auto_gain"},
        "magnet": {"hold"},
        "spec": {"preset"},
        "cam": {"snap", "cooler_on"},
        "awg": {"start", "stop", "trigger"},
        "tc": set(),
        "chaos": {"ok", "fail", "hang", "crash"},
    }
    got = {i["name"]: {a["name"] for a in i["actions"]} for i in be.list_instruments()}
    assert got == expected
    # run every non-faulty action
    for inst, acts in expected.items():
        for a in sorted(acts - {"fail", "hang", "crash"}):
            be.run_action("%s.%s" % (inst, a))
    # effects
    be.write_channel("dmm.nplc", 20)
    be.write_channel("dmm.function", "VOLT")
    be.run_action("dmm.reset")
    assert be.read_channel("dmm.nplc") == 1 and be.read_channel("dmm.function") == "CURR"
    be.write_channel("spec.points", 11)
    be.write_channel("spec.start", 1.0)
    be.run_action("spec.preset")
    assert be.read_channel("spec.points") == 201 and be.read_channel("spec.start") == 3.0
    be.run_action("awg.start")
    assert be.read_channel("awg.run") == "RUN"
    be.run_action("awg.stop")
    assert be.read_channel("awg.run") == "STOP" and be.read_channel("awg_rms") == 0.0
    be.write_channel("lockin.sensitivity", "1")
    be.run_action("lockin.auto_gain")
    assert be.read_channel("lockin.sensitivity") != "1"
    assert be.instruments.get("cam").driver.cooler is True
    # magnet: start a ramp to 2 T at 10 T/min, hold after a moment -> stops there
    be.channels.set("B", 2.0, ramp=False)
    time.sleep(0.4)
    assert be.read_channel("magnet.status") == "RAMPING"
    be.run_action("magnet.hold")
    f = be.read_channel("B")
    assert 0 < f < 2.0 and be.read_channel("magnet.status") == "HOLDING"
    assert be.read_channel("magnet.target") == pytest.approx(f, abs=1e-6)
    # chaos: failures are contained and reported, never retried
    for bad, exc in (("fail", InstrumentError), ("crash", InstrumentError),
                     ("hang", InstrumentTimeout)):
        t = time.monotonic()
        with pytest.raises(exc):
            be.run_action("chaos." + bad)
        assert time.monotonic() - t < 3
    be.run_action("chaos.ok")                        # instrument still usable
    # unknown action / bad references
    for ref in ("dmm.nope", "nope.reset", "dmm", ""):
        with pytest.raises(EasyMeasureError):
            be.run_action(ref)


def test_actions_in_sweep_hooks(demo):
    be = demo
    be.write_channel("dmm.nplc", 30)
    be.write_channel("spec.points", 5)
    d = {"name": "hooks", "dimensions": [
        {"points": 4, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}], "record": ["I"]}],
        "pre": {"hook": "action('dmm.reset')\naction('awg.start')", "record": ["dmm.nplc", "awg.run"]},
        "post": {"hook": "action('spec.preset')\naction('awg.stop')"}}
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert be.queue.wait_idle(30)
    item = be.queue_state()["history"][-1]
    assert item["status"] == "finished", item
    ds = DataSet.load(item["path"])
    assert ds.pre["dmm.nplc"] == 1 and ds.pre["awg.run"] == "RUN"   # pre hook ran first
    assert be.read_channel("spec.points") == 201 and be.read_channel("awg.run") == "STOP"
    # a failing action in the pre hook makes the sweep fail cleanly (post still runs)
    be.add_instrument("chaos", "sim_chaos", options={"exception_rate": 0.0})
    d["pre"]["hook"] = "action('chaos.fail')"
    d["post"]["hook"] = "action('awg.start')"
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert be.queue.wait_idle(30)
    item = be.queue_state()["history"][-1]
    assert item["status"] == "error" and "simulated action failure" in item["error"]
    assert be.read_channel("awg.run") == "RUN"


def test_actions_under_hardware_faults(backend):
    """Faulty bus instruments: actions either succeed or fail cleanly; the
    instrument stays usable and nothing ever runs twice because of a retry."""
    be = backend
    be.add_instrument("dmm", "sim_dmm", "SIM::FA", {"gpib_addr": 2, "error_rate": 0.2,
                                                    "garble_rate": 0.2, "disconnect_rate": 0.05,
                                                    "exception_rate": 0.1, "call_timeout": 0.5,
                                                    "seed": 3})
    be.add_instrument("lockin", "sim_lockin", options={"exception_rate": 0.3, "hang_rate": 0.05,
                                                       "hang_time": 3, "call_timeout": 0.4,
                                                       "seed": 5})
    for h in be.instruments.handles():
        for _ in range(10):
            if h.state == "connected":
                break
            try:
                h.connect()
            except Exception:
                pass
    ok = failed = 0
    for i in range(60):
        ref = ("dmm.reset", "dmm.trigger", "lockin.auto_phase", "lockin.auto_gain")[i % 4]
        try:
            be.run_action(ref)
            ok += 1
        except EasyMeasureError:
            failed += 1
    assert ok > 10 and failed > 0
    # still usable afterwards
    for _ in range(10):
        try:
            be.read_channel("dmm.reading")
            break
        except EasyMeasureError:
            pass
    else:
        pytest.fail("dmm unusable after faulty actions")


# ------------------------------------------------- real-hardware drivers --
class FakeSCPI:
    """Minimal line-based SCPI instrument on a local TCP port."""

    def __init__(self, idn, replies):
        self.idn = idn
        self.replies = dict(replies)
        self.received = []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.alive = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.alive:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        buf = b""
        with c:
            while self.alive:
                try:
                    data = c.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    cmd = line.decode().strip()
                    self.received.append(cmd)
                    if cmd.endswith("?"):
                        reply = self.idn if cmd == "*IDN?" else self.replies.get(cmd, "0")
                        c.sendall((reply + "\n").encode())

    def close(self):
        self.alive = False
        self.srv.close()

    def wait_for(self, predicate, timeout=3.0):
        """Writes have no reply: give the server thread time to record them."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate(list(self.received)):
                return list(self.received)
            time.sleep(0.02)
        return list(self.received)


def test_keithley_actions_and_config_over_tcp(backend):
    fake = FakeSCPI("KEITHLEY INSTRUMENTS INC.,MODEL 2400,1,C30", {
        ":SOUR:VOLT?": "0.5", ":READ?": "1.2E-6,0", ":SENS:CURR:PROT?": "1E-4",
        ":OUTP?": "1", ":SENS:CURR:NPLC?": "1"})
    try:
        be = backend
        h = be.add_instrument("smu", "keithley2400", "TCP::127.0.0.1::%d" % fake.port)
        assert h["state"] == "connected", h
        assert {a["name"] for a in h["actions"]} == {"clear_status", "beep"}
        assert {c["name"] for c in h["channels"] if c["config"]} == {"compliance", "output", "nplc"}
        be.run_action("smu.clear_status")
        be.run_action("smu.beep")
        be.write_channel("smu.output", "ON")
        be.write_channel("smu.nplc", 10)
        assert be.read_channel("smu.current") == pytest.approx(1.2e-6)
        assert be.read_channel("smu.output") == "ON"
        rx = fake.wait_for(lambda r: ":OUTP ON" in r and any(x.startswith(":SENS:CURR:NPLC") for x in r))
        assert "*CLS" in rx and any(r.startswith(":SYST:BEEP") for r in rx)
        assert ":OUTP ON" in rx and any(r.startswith(":SENS:CURR:NPLC 10") for r in rx)
        with pytest.raises(EasyMeasureError):
            be.write_channel("smu.output", "MAYBE")
    finally:
        fake.close()


def test_scpi_generic_actions_and_config_over_tcp(backend):
    fake = FakeSCPI("ACME,PSU,1,1", {"VOLT?": "1.5", "OUTP?": "OFF", "MEAS:CURR?": "0.1,0.2"})
    try:
        opts = {"idn_contains": "ACME", "init": ["SYST:REM"],
                "channels": [
                    {"name": "V", "get": "VOLT?", "set": "VOLT {value:.6g}", "unit": "V"},
                    {"name": "I", "get": "MEAS:CURR?", "index": 1},
                    {"name": "output", "dtype": "str", "get": "OUTP?", "set": "OUTP {value}",
                     "config": True}],
                "actions": [{"name": "trigger", "command": "*TRG"},
                            {"name": "reset", "command": ["*RST", "*CLS"]}]}
        be = backend
        h = be.add_instrument("psu", "scpi_generic", "TCP::127.0.0.1::%d" % fake.port, opts)
        assert h["state"] == "connected", h
        assert {a["name"] for a in h["actions"]} == {"trigger", "reset"}
        be.run_action("psu.trigger")
        be.run_action("psu.reset")
        be.write_channel("psu.output", "ON")
        assert be.read_channel("psu.I") == pytest.approx(0.2)
        be.define_alias(name="Vpsu", target="psu.V", min=0, max=5)
        be.write_channel("Vpsu", 2.0, ramp=False)
        want = ("SYST:REM", "*TRG", "*RST", "*CLS", "OUTP ON", "VOLT 2")
        rx = fake.wait_for(lambda r: all(c in r for c in want))
        for cmd in want:
            assert cmd in rx, (cmd, rx)
        assert rx.index("*RST") < rx.index("*CLS")
        with pytest.raises(EasyMeasureError):
            be.define_alias(name="out", target="psu.output")   # config channel
    finally:
        fake.close()
