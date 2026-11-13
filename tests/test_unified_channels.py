"""Channels as one model: every kind is read and written the same way, its access
(r, w, rw) is respected, and all names share one namespace, checked when something
is defined."""

import threading
import time

import pytest

from easymeasure.backend.instrument import ActionSpec, ChannelSpec, Instrument
from easymeasure.common.errors import ChannelError


def test_every_kind_has_a_kind_and_an_access(demo):
    be = demo
    be.define_parameter(name="Cb", value=0.6)
    be.define_parameter(name="Ct", formula="2*Cb")
    be.define_user_action(name="zero", code="set('Vg', 0)")
    be.define_composite(type="multi", name="gates", variables=["eps"],
                        targets={"Vx": "eps", "Vy": "-eps"})
    want = {
        "src.V1": ("hardware", "rw"), "dmm.reading": ("hardware", "r"),
        "dmm.nplc": ("config", "rw"), "dmm.idn": ("config", "r"),
        "dmm.reset": ("action", "w"), "Vg": ("alias", "rw"), "I": ("alias", "r"),
        "gates.eps": ("variable", "rw"), "Vdiff": ("read", "r"), "Tset_C": ("map", "rw"),
        "Cb": ("parameter", "rw"), "Ct": ("parameter", "r"),
        "zero": ("user_action", "w"), "@time": ("special", "r"),
    }
    for name, (kind, access) in want.items():
        info = be.channel_info(name)
        assert (info["kind"], info["access"]) == (kind, access), name
        assert info["readable"] == ("r" in access) and info["writable"] == ("w" in access)
    # the flat list has them all, once each
    listing = be.list_channels()["channels"]
    names = [c["name"] for c in listing]
    assert len(names) == len(set(names))
    assert set(want) <= set(names)
    assert next(c for c in listing if c["name"] == "Cb")["last"] == 0.6
    with pytest.raises(ChannelError, match="unknown channel"):
        be.channel_info("nope")
    with pytest.raises(ChannelError, match="has no channel"):
        be.channel_info("dmm.nope")


def test_access_is_respected_everywhere(demo):
    be = demo
    be.define_parameter(name="Ct", formula="2")
    for name in ("dmm.reading", "Vdiff", "@time", "Ct", "dmm.idn", "I"):
        with pytest.raises(ChannelError, match="read-only"):
            be.write_channel(name, 1.0)
        with pytest.raises(ChannelError, match="read-only"):
            be.set_many({name: 1.0})
    with pytest.raises(ChannelError, match="action"):
        be.read_channel("dmm.reset")
    # nothing was written when one channel of a set_many is refused
    be.write_channel("Vg", 0.1, ramp=False)
    with pytest.raises(ChannelError):
        be.set_many({"Vg": 0.3, "Vdiff": 1.0}, ramp=False)
    assert be.read_channel("Vg") == pytest.approx(0.1)


def test_instrument_channels_are_written_as_they_are(demo):
    be = demo
    # Vg (src.V1) is limited to ±2 V; the instrument's own channel is not
    be.write_channel("src.V1", 3.0)
    assert be.read_channel("src.V1") == 3.0
    assert be.read_channel("Vg") == pytest.approx(3.0)     # the alias reads it back
    be.write_channel("src.V1", 0.0)
    # composites may drive and read instrument channels directly
    be.define_composite(type="multi", name="raw", variables=["v"],
                        targets={"src.V3": "v", "src.V4": "-v"})
    be.write_channel("raw.v", 0.25, ramp=False)
    assert be.read_channel("src.V3") == pytest.approx(0.25)
    assert be.read_channel("src.V4") == pytest.approx(-0.25)
    be.define_composite(type="read", name="sumv", expression="src.V3 + src.V4 + 1")
    assert be.read_channel("sumv") == pytest.approx(1.0)
    be.define_composite(type="map", name="v3mV", target="src.V3", forward="x/1000",
                        backward="1000*y", min=-500, max=500)
    be.write_channel("v3mV", 100, ramp=False)
    assert be.read_channel("src.V3") == pytest.approx(0.1)
    with pytest.raises(ChannelError, match="action"):
        be.define_composite(type="multi", name="bad", variables=["a"],
                            targets={"dmm.reset": "a"})
    with pytest.raises(ChannelError, match="cannot be set to numbers"):
        be.define_composite(type="multi", name="bad", variables=["a"],
                            targets={"dmm.reading": "a"})


def test_parameters_are_written_first(demo):
    be = demo
    be.define_parameter(name="k", value=1.0)
    be.define_composite(type="multi", name="g", variables=["u"], targets={"Vx": "k * u"})
    out = be.set_many({"g.u": 0.2, "k": 2.0}, ramp=False)
    assert out["k"] == 2.0 and out["Vx"] == pytest.approx(0.4)
    assert be.read_channel("Vx") == pytest.approx(0.4)
    # refused as a whole: the parameter keeps its value
    with pytest.raises(ChannelError):
        be.set_many({"g.u": 0.9, "k": 5.0}, ramp=False)       # Vx = 4.5 > 1
    assert be.read_channel("k") == 2.0
    # a parameter can be swept
    item = be.queue_add({"kind": "sweep", "definition": {"name": "k", "dimensions": [
        {"points": 3, "sweep": [{"channel": "k", "start": 1, "stop": 2}], "record": ["k"]}]}},
        start=True)
    assert be.queue.wait_idle(20)
    assert be.queue_state()["history"][-1]["status"] == "finished"
    assert be.read_channel("k") == 2.0


def test_actions_run_when_written(demo):
    be = demo
    be.define_user_action(name="note", code="state['n'] = state.get('n', 0) + 1")
    be.scripts.state.clear()
    r = be.write_channel("note")
    assert r["status"] == "finished" and be.scripts.state["n"] == 1
    # with other channels: they are written first, the actions run last, in order
    be.define_user_action(name="seen", code="state['vg'] = get('Vg')")
    out = be.set_many({"seen": None, "Vg": 0.3, "note": None}, ramp=False)
    assert be.scripts.state == {"n": 2, "vg": pytest.approx(0.3)}
    assert out["Vg"] == pytest.approx(0.3) and out["seen"]["status"] == "finished"
    # instrument actions too, from a script and as a sweep constant
    be.write_channel("dmm.nplc", 10)
    be.queue_add({"kind": "script", "code": "set('dmm.reset')\nset('note')"}, start=True)
    assert be.queue.wait_idle(20)
    assert be.queue_state()["history"][-1]["status"] == "finished"
    assert be.read_channel("dmm.nplc") == 1 and be.scripts.state["n"] == 3
    be.write_channel("dmm.nplc", 10)
    be.queue_add({"kind": "sweep", "definition": {"name": "c", "dimensions": [{"points": 2}],
                  "pre": {"constants": [{"channel": "dmm.reset"}, {"channel": "note"}]}}},
                 start=True)
    assert be.queue.wait_idle(20)
    assert be.read_channel("dmm.nplc") == 1 and be.scripts.state["n"] == 4


def test_a_user_action_run_by_a_script_stops_with_it(demo):
    be = demo
    be.define_user_action(name="long", code="sleep(30)\nstate['done'] = True")
    be.scripts.state.clear()
    be.queue_add({"kind": "script", "code": "set('long')"}, start=True)
    t0 = time.monotonic()
    while "long" not in be.user_actions.running() and time.monotonic() - t0 < 5:
        time.sleep(0.05)
    assert "long" in be.user_actions.running()
    be.abort()
    assert be.queue.wait_idle(10)
    assert time.monotonic() - t0 < 8
    assert "done" not in be.scripts.state and not be.user_actions.running()


def test_one_namespace(demo):
    be = demo
    be.define_parameter(name="Cb", value=0.6)
    be.define_user_action(name="zero", code="pass")
    be.define_composite(type="read", name="R", expression="Vg / I")
    taken = {"Vg": "an alias", "Cb": "a parameter", "zero": "a user action",
             "R": "a composite", "dmm": "an instrument"}
    for name, what in taken.items():
        for define in (lambda n: be.define_alias(name=n, target="src.V3"),
                       lambda n: be.define_parameter(name=n, value=1),
                       lambda n: be.define_user_action(name=n, code="pass"),
                       lambda n: be.define_composite(type="read", name=n, expression="Vg"),
                       lambda n: be.define_composite(type="multi", name=n, variables=["a"],
                                                     targets={"Vx": "a"}),
                       lambda n: be.add_instrument(name=n, driver="sim_dmm",
                                                   address="SIM::GPIB0::9::INSTR")):
            try:
                define(name)
            except ChannelError as exc:
                assert what in str(exc) or "already exists" in str(exc), (name, str(exc))
            else:
                raise AssertionError("%s was taken (%s) but accepted" % (name, what))
    # replacing a thing by one of the same kind is fine
    be.define_alias(name="Vg", target="src.V1", min=-2, max=2, replace=True)
    be.define_user_action(name="zero", code="pass", replace=True)
    # names are checked by the checks too
    assert not be.check_user_action(name="Cb", code="pass")["ok"]
    with pytest.raises(ChannelError, match="already an alias"):
        be.check_parameter(name="Vg", definition="1")
    with pytest.raises(ChannelError, match="already a user action"):
        be.check_composite(type="read", name="zero", expression="Vg")


def test_check_code_knows_the_access(demo):
    r = demo.check_code("get('dmm.reset')\nset('I', 1)\nset('dmm.reset')\nset_many({'Vdiff': 1})\n")
    assert r["ok"]
    w = " ".join(r["warnings"])
    assert "dmm.reset is write-only" in w and "I is read-only" in w and "Vdiff is read-only" in w
    assert len(r["warnings"]) == 3


class _Clash(Instrument):
    DESCRIPTION = "a channel and an action of the same name"
    CHANNELS = [ChannelSpec("zero", "rw", "float")]
    ACTIONS = [ActionSpec("zero", "")]

    def _action_zero(self):
        pass


def test_a_driver_may_not_name_a_channel_and_an_action_alike(demo):
    demo.drivers._classes["clash"] = _Clash
    h = demo.instruments.add("cl", "clash", "NONE", {})
    assert h.state == "error" and "both a channel and an action" in (h.last_error or "")
