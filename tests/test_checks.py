"""The editors' checks (check_parameter, check_code, check_user_action) and the
naming of multivariate variables (composite.variable)."""

import pytest

from easymeasure.common.errors import ChannelError, ExpressionError
from easymeasure.common.exprs import Expression, rename


def gates(be):
    return be.define_composite(type="multi", name="gates",
                               variables=[{"name": "eps", "min": -0.4, "max": 0.4},
                                          {"name": "U", "min": 0, "max": 0.5}],
                               targets={"Vx": "U + eps", "Vy": "U - eps"})


def test_variables_are_named_after_their_composite(demo):
    r = gates(demo)
    assert r["channels"] == ["gates.eps", "gates.U"]
    ch = demo.channels
    assert ch.kind("gates.eps") == "variable" and ch.group_of("gates.U") == "gates"
    assert not ch.exists("eps")
    demo.set_many({"gates.eps": 0.1, "gates.U": 0.2}, ramp=False)
    assert ch.get("Vx") == pytest.approx(0.3)
    assert ch.instruments_behind("gates.eps") == {"src"}
    # other formulas use the full name
    demo.define_composite(type="read", name="twice", expression="2 * gates.eps")
    assert ch.get("twice") == pytest.approx(0.2)
    demo.define_composite(type="map", name="eps_mV", target="gates.eps", forward="x/1000",
                         backward="1000*y", min=-400, max=400)
    demo.write_channel("eps_mV", 50, ramp=False)
    assert ch.get("gates.eps") == pytest.approx(0.05)
    # names that would read like something else
    with pytest.raises(ChannelError, match="taken"):
        demo.define_composite(type="multi", name="h", variables=["T"], targets={"Vb": "T"})
    with pytest.raises(ChannelError, match="already an instrument"):
        demo.define_composite(type="multi", name="dmm", variables=["a"], targets={"Vb": "a"})
    # a sweep and a script by the full names
    r = demo.validate_sweep(definition={"name": "s", "dimensions": [
        {"points": 3, "sweep": [{"channel": "gates.eps", "start": 0, "stop": 0.1}],
         "record": ["Vx", "gates.eps"]}]})
    assert r["ok"]


def test_expressions_with_dotted_names():
    e = Expression("MEMS.Vd * 2 + np.sin(theta) + z.real")
    assert e.free_names == ["MEMS.Vd", "theta", "z"]
    assert e.evaluate({"MEMS.Vd": 1.5, "theta": 0.0, "z": 2 + 3j}) == pytest.approx(5.0)
    with pytest.raises(ExpressionError):
        Expression("MEMS._secret")
    with pytest.raises(ExpressionError):
        Expression("np.nope(1)")
    assert rename("a.b + c", {"a.b": "x.b", "c": "k.c"}) == "x.b + k.c"


def test_check_parameter(demo):
    r = demo.check_parameter(name="k", definition="2.5")
    assert r["value"] == 2.5 and r["current"] == 2.5 and "k" not in demo.channels.parameter_values()
    demo.define_parameter(name="k", definition="2.5")
    assert demo.list_parameters()[0]["value"] == 2.5
    r = demo.check_parameter(name="k2", definition="k * 2")
    assert r["formula"] == "k * 2" and r["current"] == 5.0
    demo.define_parameter(name="k2", definition="k * 2")
    assert demo.channels.parameter_values()["k2"] == 5.0
    with pytest.raises(ChannelError, match="circle"):
        demo.check_parameter(name="k", definition="k2 + 1", replace=True)
    with pytest.raises(ChannelError):
        demo.check_parameter(name="k3", definition="Vg * 2")         # a live channel
    with pytest.raises(ChannelError, match="unknown"):
        demo.check_parameter(name="k3", definition="nope + 1")
    # a mapping that uses it is checked too, and keeps working afterwards
    demo.define_composite(type="map", name="Vk", target="Vb", forward="x / k",
                          backward="y * k", min=-1, max=1)
    with pytest.raises(Exception):
        demo.check_parameter(name="k", definition="0", replace=True)
    demo.write_channel("Vk", 0.5, ramp=False)
    assert demo.channels.get("Vb") == pytest.approx(0.2)


def test_check_code_and_user_actions(demo):
    r = demo.check_code("x = get('Vg')\nset('nope', 1)\nset_many({'gates.eps': 0})\n")
    assert r["ok"] and len(r["warnings"]) == 2
    assert "nope" in r["warnings"][0] and "gates.eps" in r["warnings"][1]
    r = demo.check_code("for i in range(3)\n    pass")
    assert not r["ok"] and r["errors"][0].startswith("line 1:")
    r = demo.check_user_action(name="ok_one", code="set('Vg', 0)")
    assert r["ok"] and not r["warnings"]
    r = demo.check_user_action(name="", code="set('Vg', 0)")
    assert not r["ok"]
    demo.define_user_action(name="zero", code="set('Vg', 0)")
    assert not demo.check_user_action(name="zero", code="pass")["ok"]
    assert demo.check_user_action(name="zero", code="pass", replace=True)["ok"]
