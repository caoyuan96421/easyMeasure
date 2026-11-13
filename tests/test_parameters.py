"""Parameters: named constants of a setup (values, or formulas of other parameters).

Covers the rules (no circles, no live channels, names unique, not writable or
sweepable), their use in composites (multivariate targets, reads, mappings - which
are checked to be inverse again when a parameter changes), actions and point
actions, setups, data files, permissions, and a randomised stress of formula
graphs and of parameters changing while sweeps run."""

import json
import math
import threading
import time

import numpy as np
import pytest

from easymeasure.backend.parameters import Parameter, compute, ordered
from easymeasure.backend.storage import DataSet
from easymeasure.common.errors import ChannelError, ExpressionError
from easymeasure.remote import policy


def _run(be, d, timeout=60):
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert be.queue.wait_idle(timeout)
    item = be.queue_state()["history"][-1]
    return item, (DataSet.load(item["path"]) if item.get("path") else None)


def test_definition_rules(demo):
    be = demo
    with pytest.raises(ChannelError, match="value or a formula"):
        Parameter("p")
    with pytest.raises(ChannelError, match="not both"):
        Parameter("p", value=1, formula="2")
    with pytest.raises(ChannelError, match="finite"):
        Parameter("p", value=float("inf"))
    with pytest.raises(ChannelError, match="function / constant"):
        Parameter("pi", value=3)
    with pytest.raises(ExpressionError):
        Parameter("p", formula="2 +")
    be.define_parameter(name="a", value=2.0, unit="V")
    be.define_parameter(name="b", formula="3*a")
    be.define_parameter(name="c", formula="a + b**2")
    assert be.read_channel(name="c") == pytest.approx(2 + 36)
    # circles, also by redefinition and to itself
    with pytest.raises(ChannelError, match="circle: (a → c → a|c → a → c)"):
        be.define_parameter(name="a", formula="c - 1", replace=True)
    with pytest.raises(ChannelError, match="circle"):
        be.define_parameter(name="d", formula="d + 1")
    assert be.read_channel(name="a") == 2.0                          # unchanged
    # only parameters, never live channels
    with pytest.raises(ChannelError, match="Vg is a channel"):
        be.define_parameter(name="d", formula="2*Vg")
    with pytest.raises(ChannelError, match="unknown name nope"):
        be.define_parameter(name="d", formula="2*nope")
    # names are unique among channels, composites and parameters
    with pytest.raises(ChannelError, match="already an alias"):
        be.define_parameter(name="Vg", value=1)
    with pytest.raises(ChannelError, match="already a parameter"):
        be.define_alias(name="a", target="src.V1")
    be.define_composite(type="multi", name="G", variables=["u"], targets={"Vx": "u"})
    be.define_parameter(name="u", value=1)          # the variable is G.u: no conflict
    be.remove_parameter(name="u")
    with pytest.raises(ChannelError, match="already a composite"):
        be.define_parameter(name="G", value=1)
    # a variable may share a parameter's name: in its composite's formulas it is the variable
    be.define_composite(type="multi", name="H", variables=["a"], targets={"Vy": "a"})
    be.set_many(values={"H.a": 0.3}, ramp=False)
    assert be.read_channel(name="Vy") == pytest.approx(0.3)
    be.remove_composite(name="H")
    with pytest.raises(ChannelError, match="already a parameter"):
        be.define_composite(type="multi", name="a", variables=["x"], targets={"Vy": "x"})
    with pytest.raises(ChannelError, match="already exists"):
        be.define_parameter(name="a", value=5)
    # a value parameter is a channel like the others: written, set together, swept
    assert be.write_channel(name="a", value=2) == 2.0
    assert be.set_many(values={"a": 3})["a"] == 3.0
    assert be.validate_sweep(definition={"name": "s", "dimensions": [
        {"points": 2, "sweep": [{"channel": "a", "start": 0, "stop": 1}], "record": ["c"]}]})["ok"]
    assert be.channel_info(name="a")["access"] == "rw"
    with pytest.raises(ChannelError, match="read-only"):
        be.write_channel(name="b", value=1)
    assert be.write_channel(name="a", value=1.0) == 1.0
    assert be.read_channel(name="c") == pytest.approx(1 + 9)        # formulas follow
    info = be.channel_info(name="b")
    assert info["kind"] == "parameter" and not info["writable"] and info["formula"] == "3*a"
    # removing: refused while used
    with pytest.raises(ChannelError, match="used by b, c"):
        be.remove_parameter(name="a")
    be.remove_parameter(name="c")
    be.remove_parameter(name="b")
    be.remove_parameter(name="a")
    assert be.list_parameters() == []


def test_composites_use_parameters(demo):
    be = demo
    be.define_parameter(name="k", value=2.0)
    be.define_parameter(name="half_k", formula="k/2")
    be.define_composite(type="multi", name="G", variables=[{"name": "a", "min": -1, "max": 1}],
                        targets={"Vx": "a*half_k", "Vy": "-a/k"})
    be.set_many(values={"G.a": 0.5}, ramp=False)
    assert be.read_channel(name="Vx") == pytest.approx(0.5) and be.read_channel(name="Vy") == \
        pytest.approx(-0.25)
    # a parameter change moves nothing; the next write uses it
    be.write_channel(name="k", value=4.0)
    assert be.read_channel(name="Vx") == pytest.approx(0.5)
    be.set_many(values={"G.a": 0.5}, ramp=False)
    assert be.read_channel(name="Vx") == pytest.approx(1.0) and be.read_channel(name="Vy") == \
        pytest.approx(-0.125)
    # sweep validation (vectorised) uses the parameters too
    d = {"name": "s", "dimensions": [
        {"points": 5, "sweep": [{"channel": "G.a", "start": 0, "stop": 1}], "record": ["Vx", "k"]}]}
    r = be.validate_sweep(definition=d)
    assert r["ok"] and "composite G: 0 of" not in str(r["warnings"])
    be.write_channel(name="k", value=8.0)                    # Vx = 4a: above 1 V for a > 0.25
    r = be.validate_sweep(definition=d)
    assert "composite G: 3 of 5 points out of range, will be skipped" in r["warnings"]
    item, ds = _run(be, d)
    np.testing.assert_array_equal(np.isnan(ds.data["Vx"]), [False, False, True, True, True])
    k = ds.data["k"]                                          # parameters can be recorded
    np.testing.assert_allclose(k[np.isfinite(ds.data["Vx"])], 8.0)
    assert ds.header["parameters"]["k"] == {"value": 8.0, "unit": "", "formula": None}
    assert ds.header["parameters"]["half_k"]["formula"] == "k/2"
    # a read composite
    be.define_composite(type="read", name="Vk", expression="Vx * k")
    assert be.read_channel(name="Vk") == pytest.approx(be.read_channel(name="Vx") * 8)
    # unknown names in formulas are refused, naming them
    with pytest.raises(ExpressionError, match="unknown name.*q"):
        be.define_composite(type="multi", name="H", variables=["b"], targets={"Vb": "b*q"})
    with pytest.raises(ChannelError, match="used by half_k, G, Vk"):
        be.remove_parameter(name="k")


def test_mappings_are_checked_again_when_a_parameter_changes(demo):
    be = demo
    be.define_parameter(name="gain", value=1000.0)
    be.define_composite(type="map", name="Vmv", target="Vg", forward="x/gain",
                        backward="y*gain", min=-500, max=500, unit="mV")
    be.write_channel(name="Vmv", value=200, ramp=False)
    assert be.read_channel(name="Vg") == pytest.approx(0.2)
    assert be.read_channel(name="Vmv") == pytest.approx(200)
    be.write_channel(name="gain", value=500.0)
    assert be.read_channel(name="Vmv") == pytest.approx(100)         # read follows
    # a value that breaks the mapping is refused, and the old one stays in force
    with pytest.raises(ChannelError, match="mapping Vmv"):
        be.write_channel(name="gain", value=0.0)
    assert be.channels.parameter_values()["gain"] == 500.0
    assert be.read_channel(name="Vmv") == pytest.approx(100)
    be.write_channel(name="Vmv", value=250, ramp=False)
    assert be.read_channel(name="Vg") == pytest.approx(0.5)
    # a mapping whose range now exceeds the target's limits: warned, not refused
    w = be.define_parameter(name="gain", value=100.0, replace=True)["warnings"]
    assert any("beyond the limits of Vg" in x for x in w)


def test_actions_scripts_and_point_actions_see_parameters(demo):
    be = demo
    be.define_parameter(name="Cb", value=0.6)
    be.define_parameter(name="Ct", formula="1.5*Cb")
    be.define_user_action(name="use", code="set('Vg', Ct - Cb)\nstate['seen'] = (Cb, params.Ct, "
                                           "params['Cb'], 'Ct' in params, dict(params))")
    assert be.write_channel(name="use")["status"] == "finished"
    assert be.read_channel(name="Vg") == pytest.approx(0.3)
    cb, ct, cb2, has, allp = be.scripts.state["seen"]
    assert (cb, ct, cb2, has) == (0.6, pytest.approx(0.9), 0.6, True)
    assert allp == {"Cb": 0.6, "Ct": pytest.approx(0.9)}
    # a value parameter is written like a channel, a formula parameter is read-only
    for code in ("params.Cb = 0.7", "params['Cb'] = 0.7", "set('Cb', 0.7)"):
        be.define_user_action(name="setp", code=code, replace=True)
        be.write_channel(name="setp")
        assert be.read_channel("Ct") == pytest.approx(1.05)
        be.write_channel("Cb", 0.6)
    be.define_user_action(name="bad", code="params.Ct = 1", replace=True)
    with pytest.raises(ChannelError, match="read-only"):
        be.write_channel(name="bad")
    # a name taken by the script API is not shadowed by a parameter
    be.define_parameter(name="state_", value=1.0)
    be.define_parameter(name="np_", value=2.0)
    be.define_user_action(name="ns", code="state['ok'] = (np.pi > 3, state_ + np_)", replace=True)
    assert be.write_channel(name="ns")["status"] == "finished"
    assert be.scripts.state["ok"] == (True, 3.0)
    # point actions
    d = {"name": "pa", "dimensions": [{"points": 3, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.2}],
                                       "record": ["Vg"], "hooks": {"post_record": {
                                           "enabled": True, "code": "state.setdefault('pa', []).append(Cb)"}}}]}
    be.scripts.state.pop("pa", None)
    item, _ = _run(be, d)
    assert item["status"] == "finished" and be.scripts.state["pa"] == [0.6] * 3


def test_setups_keep_parameters(demo, tmp_path):
    be = demo
    be.define_parameter(name="a", value=1.0, unit="V", description="first")
    be.define_parameter(name="b", formula="2*a")
    be.define_parameter(name="c", formula="b + a")
    be.define_composite(type="multi", name="G", variables=["u"], targets={"Vx": "u*c/10"})
    p = tmp_path / "s.json"
    be.save_setup(path=str(p))
    d = json.loads(p.read_text())
    assert [x["name"] for x in d["parameters"]] == ["a", "b", "c"]
    assert d["parameters"][0] == {"name": "a", "unit": "V", "description": "first", "value": 1.0}
    # any order in the file works; composites using them load
    d["parameters"].reverse()
    p.write_text(json.dumps(d))
    assert be.load_setup(path=str(p)) == []
    assert be.channels.parameter_values() == {"c": 3.0, "b": 2.0, "a": 1.0}
    assert any(c.name == "G" for c in be.channels.composites())
    # a bad parameter is reported and left out; the rest loads
    d["parameters"].append({"name": "loop", "formula": "loop + 1"})
    d["parameters"].append({"name": "wrong", "formula": "a +"})
    p.write_text(json.dumps(d))
    problems = be.load_setup(path=str(p))
    assert any(x.startswith("parameter loop:") and "circle" in x for x in problems)
    assert any(x.startswith("parameter wrong:") for x in problems)
    assert set(be.channels.parameter_values()) == {"a", "b", "c"}


def test_permissions():
    assert policy.required_level("list_parameters") == 0
    assert policy.required_level("write_channel") == 1
    assert policy.required_level("define_parameter") == 2
    assert policy.required_level("remove_parameter") == 2


def test_ordered_and_compute_directly():
    ps = {p.name: p for p in (Parameter("z", formula="y*2"), Parameter("y", formula="x + 1"),
                              Parameter("x", value=1))}
    assert [p.name for p in ordered(ps.values())] == ["x", "y", "z"]
    assert compute(ps) == {"z": 4.0, "y": 2.0, "x": 1.0}
    with pytest.raises(ChannelError, match="parameter y = 1/x") as ei:
        compute({"x": Parameter("x", value=0), "y": Parameter("y", formula="1/x")})
    assert ei.value.parameter == "y"
    with pytest.raises(ChannelError, match="not a finite number"):
        compute({"x": Parameter("x", value=1e300), "y": Parameter("y", formula="x*x")})


def test_random_parameter_graphs():
    """Random formula graphs: the values agree with an independent evaluation, and a
    circle is reported exactly when there is one."""
    rng = np.random.default_rng(3)
    for case in range(300):
        n = int(rng.integers(2, 9))
        names = ["p%d" % i for i in range(n)]
        defs, refs = {}, {}
        for i, nm in enumerate(names):
            if rng.random() < 0.35:
                defs[nm] = Parameter(nm, value=float(np.round(rng.uniform(-3, 3), 3)))
                refs[nm] = []
            else:
                k = int(rng.integers(1, 3))
                pool = names if rng.random() < 0.15 else names[:i] or names
                used = sorted(set(rng.choice(pool, size=k)))
                expr = " + ".join("%g*%s" % (rng.uniform(-2, 2), u) for u in used) + " + 1"
                defs[nm] = Parameter(nm, formula=expr)
                refs[nm] = used
        # independent: DFS for a cycle, then evaluate recursively
        state, cyc = {}, False

        def dfs(u):
            nonlocal cyc
            state[u] = 1
            for v in refs[u]:
                if state.get(v) == 1:
                    cyc = True
                elif state.get(v) is None:
                    dfs(v)
            state[u] = 2
        for nm in names:
            if state.get(nm) is None:
                dfs(nm)
        if cyc:
            with pytest.raises(ChannelError, match="circle"):
                compute(defs)
            continue
        memo = {}

        def val(u):
            if u not in memo:
                p = defs[u]
                memo[u] = p.value if not p.formula else float(eval(
                    p.formula, {"__builtins__": {}}, {r: val(r) for r in refs[u]}))
            return memo[u]
        got = compute(defs)
        for nm in names:
            assert math.isclose(got[nm], val(nm), rel_tol=1e-12, abs_tol=1e-12), (case, nm)


def test_parameters_change_while_sweeps_run(demo):
    """Changing a parameter while a sweep uses it: every point is set with one
    consistent value (no torn reads), nothing crashes, and the hardware never
    exceeds a limit."""
    be = demo
    for al, tgt in (("Vx", "src.V3"), ("Vy", "src.V4")):
        be.define_alias(name=al, target=tgt, min=-1, max=1, ramp_rate=None, unit="V", replace=True)
    be.define_parameter(name="g", value=0.5)
    be.define_parameter(name="g2", formula="-g")
    be.define_composite(type="multi", name="G", variables=[{"name": "a", "min": -1, "max": 1}],
                        targets={"Vx": "a*g", "Vy": "a*g2"})
    be.set_many(values={"G.a": 0}, ramp=False)
    h = be.instruments.get("src")
    seen, orig = [], h.write

    def spy(c, v):
        seen.append((c, v))
        return orig(c, v)
    h.write = spy
    stop = threading.Event()
    errors = []

    def wiggle():
        rng = np.random.default_rng(1)
        while not stop.is_set():
            try:
                be.write_channel(name="g", value=float(rng.uniform(0.2, 2.0)))
            except Exception as exc:                  # pragma: no cover - reported below
                errors.append(exc)
            time.sleep(0.002)
    th = threading.Thread(target=wiggle)
    th.start()
    try:
        d = {"name": "wig", "dimensions": [
            {"points": 200, "sweep": [{"channel": "G.a", "start": -1, "stop": 1}],
             "record": ["Vx", "Vy", "g"]}]}
        item, ds = _run(be, d, 120)
    finally:
        stop.set()
        th.join(5)
        h.write = orig
    assert not errors, errors
    assert item["status"] == "finished", item
    vx, vy = ds.data["Vx"], ds.data["Vy"]
    ok = np.isfinite(vx)
    # one parameter value per point: Vy = -Vx exactly where measured
    np.testing.assert_allclose(vy[ok], -vx[ok], atol=1e-12)
    assert max(abs(v) for c, v in seen if c in ("V3", "V4")) <= 1.0 + 1e-12
