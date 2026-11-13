"""Composite channels: multivariate writes, composite reads, one-to-one mappings,
nesting, dependency loops, ramps, sweeps and setups (backend/composites.py,
backend/channels.py)."""

import json
import math
import threading
import time

import numpy as np
import pytest

from easymeasure.backend.composites import (MapComposite, MultiComposite,
                                            Variable, composite_from_dict, find_cycle)
from easymeasure.backend.storage import DataSet
from easymeasure.common.errors import ChannelError, ExpressionError, LimitError, OutOfRange
from easymeasure.common.exprs import Expression


def gates(be, **kw):
    """eps / U onto Vx / Vy (demo aliases: ±1 V, 2 V/s)."""
    d = dict(type="multi", name="gates",
             variables=[{"name": "eps", "min": -0.4, "max": 0.4, "unit": "V"},
                        {"name": "U", "min": 0, "max": 0.5, "unit": "V"}],
             targets={"Vx": "U + eps", "Vy": "U - eps"})
    d.update(kw)
    return be.define_composite(**d)


def writes_of(be, inst="src"):
    """Record (channel, value) of every write to an instrument."""
    h = be.instruments.get(inst)
    seen = []
    orig = h.write

    def w(c, v):
        seen.append((c, v))
        return orig(c, v)
    h.write = w
    return seen, (lambda: setattr(h, "write", orig))


# -- formulas --------------------------------------------------------------------------
def test_piecewise_formulas():
    e = Expression("piecewise(x < 0, -x, x < 1, x**2, 1)")
    assert [e.evaluate({"x": v}) for v in (-2, 0.5, 3)] == [2, 0.25, 1]
    np.testing.assert_allclose(e.evaluate({"x": np.array([-2, 0.5, 3])}), [2, 0.25, 1])
    no_default = Expression("piecewise(x > 0, sqrt(x))")
    assert math.isnan(no_default.evaluate({"x": -1.0}))        # no branch: NaN, no warning
    assert Expression("-x if x < 0 else 2*x").evaluate({"x": -3}) == 3
    np.testing.assert_allclose(Expression("interp(x, [0, 1, 2], [0, 10, 0])").evaluate(
        {"x": np.array([0.5, 1.5])}), [5, 5])
    assert Expression("piecewise(x < 0, -1, x > 0, 1, 0)").free_names == ["x"]
    with pytest.raises(ExpressionError):
        Expression("piecewise(x)").evaluate({"x": 1})
    with pytest.raises(ExpressionError):
        Expression("__import__('os')")


# -- definitions -----------------------------------------------------------------------
def test_definitions_are_validated(demo):
    with pytest.raises(ChannelError, match="function / constant"):
        Variable("sin")
    with pytest.raises(ChannelError, match="min > max"):
        Variable("a", min=1, max=0)
    with pytest.raises(ChannelError, match="ramp_rate"):
        Variable("a", ramp_rate=0)
    with pytest.raises(ChannelError, match="repeat"):
        MultiComposite("m", ["a", "a"], {"Vx": "a"})
    with pytest.raises(ChannelError, match="own variables"):
        MultiComposite("m", ["a", "b"], {"b": "a"})
    # names that are not variables must be parameters: checked when it is defined
    with pytest.raises(ExpressionError, match="unknown name.*q"):
        demo.define_composite(type="multi", name="m", variables=["a"], targets={"Vx": "a + q"})
    with pytest.raises(ChannelError, match="no variables"):
        MultiComposite("m", [], {"Vx": "1"})
    be = demo
    with pytest.raises(ChannelError, match="does not exist"):
        be.define_composite(type="multi", name="m", variables=["a"], targets={"nope": "a"})
    with pytest.raises(ChannelError, match="is read-only"):
        be.define_composite(type="multi", name="m", variables=["a"], targets={"Vdiff": "a"})
    be.define_alias(name="Iro", target="dmm.reading", read_only=True)
    with pytest.raises(ChannelError, match="read-only"):
        be.define_composite(type="multi", name="m", variables=["a"], targets={"Iro": "a"})
    # a variable may share its short name with an alias: it is m.Vg
    be.define_composite(type="multi", name="m", variables=["Vg"], targets={"Vx": "Vg"})
    assert be.channels.kind("m.Vg") == "variable" and be.channels.kind("Vg") == "alias"
    be.remove_composite("m")
    with pytest.raises(ChannelError, match="already an alias"):
        be.define_composite(type="multi", name="Vg", variables=["a"], targets={"Vx": "a"})
    gates(be)
    # variables are named after their composite: another eps is no conflict
    be.define_composite(type="multi", name="other", variables=["eps"], targets={"Vb": "eps"})
    assert be.channels.kind("other.eps") == "variable"
    be.remove_composite("other")
    with pytest.raises(ChannelError, match="already an instrument"):
        be.define_composite(type="multi", name="src", variables=["eps"], targets={"Vb": "eps"})
    with pytest.raises(ChannelError, match="already a composite"):
        be.add_instrument("gates", "sim_dmm", "SIM::g")
    with pytest.raises(ChannelError, match="already exists"):
        gates(be)
    with pytest.raises(ChannelError, match="type must be"):
        composite_from_dict({"type": "nope", "name": "x"})
    r = be.define_composite(type="multi", name="lazy", variables=["p", "q"], targets={"Vb": "p"})
    assert r["warnings"] == ["composite lazy: variable lazy.q drives no target"]
    assert r["channels"] == ["lazy.p", "lazy.q"]
    # check_composite: the same checks, nothing defined
    r = be.check_composite(type="map", name="mV", target="Vb", forward="x/1000",
                           backward="1000*y", min=-500, max=500)
    assert r["channels"] == ["mV"] and "mV" not in be.channels._owner
    with pytest.raises(ChannelError, match="inverse"):
        be.check_composite(type="map", name="mV", target="Vb", forward="x/1000",
                           backward="100*y", min=-500, max=500)


def test_mapping_inverse_check():
    ok = MapComposite("T", "Tset", "x + 273.15", "y - 273.15", -273.15, 26.85)
    assert ok.image == pytest.approx((0.0, 300.0))
    # piecewise linear (two slopes) and its inverse
    pw = MapComposite("p", "Vx", "piecewise(x < 0, 2*x, 0.5*x)", "y/2 if y < 0 else 2*y", -1, 1)
    assert pw.fwd(-0.5) == -1 and pw.fwd(1) == 0.5 and pw.bwd(0.5) == 1
    MapComposite("ex", "Vx", "exp(x)", "log(y)", -3, 3)                 # non-linear
    MapComposite("g", "Vx", "1e-3*x", "1e3*y", 1e-3, 1e3)               # wide, log-sampled too
    with pytest.raises(ChannelError, match=r"at x = -1 forward gives y = 1 and backward gives "
                                           r"1 back"):
        MapComposite("sq", "Vx", "x**2", "sqrt(y)", -1, 1)              # not one-to-one
    with pytest.raises(ChannelError, match="not the inverse"):
        MapComposite("off", "Vx", "2*x", "y/2.001", 0, 1)               # slightly off
    MapComposite("loose", "Vx", "2*x", "y/2.001", 0, 1, tolerance=1e-3)
    with pytest.raises(ChannelError, match="not the inverse"):
        # a kink in the backward: the check catches errors only in part of the range
        MapComposite("kink", "Vx", "x", "y if y < 0.7 else y + 0.01", 0, 1)
    with pytest.raises(ChannelError, match="not finite at x = 0"):
        MapComposite("l", "Vx", "log(x)", "exp(y)", 0, 1)
    with pytest.raises(ChannelError, match="single number"):
        MapComposite("v", "Vx", "[x, x]", "y", 0, 1)
    with pytest.raises(ChannelError, match="finite min and max"):
        MapComposite("r", "Vx", "x", "y", None, 1)
    with pytest.raises(ChannelError, match="below max"):
        MapComposite("r", "Vx", "x", "y", 1, 1)
    with pytest.raises(ChannelError, match="onto itself"):
        MapComposite("r", "r", "x", "y", 0, 1)
    with pytest.raises(ExpressionError, match="forward"):
        MapComposite("r", "Vx", "x + y", "y", 0, 1)                     # forward is of x only


def test_mapping_write_and_read(demo):
    be, ch = demo, demo.channels
    assert ch.get("Tset_C") == pytest.approx(ch.get("Tset") - 273.15)
    ch.set("Tset_C", -73.15, ramp=False)
    assert ch.get("Tset") == pytest.approx(200.0)
    assert ch.get("Tset_C") == pytest.approx(-73.15)                    # read back through y
    with pytest.raises(LimitError, match="outside the range of the mapping"):
        ch.set("Tset_C", 30)
    # the read is a real read-back: a raw write to the hardware is seen
    ch.set("tc.setpoint", 250.0)
    assert ch.get("Tset_C") == pytest.approx(-23.15)
    # beyond the target's limits: refused before anything is written
    r = be.define_composite(type="map", name="Vg_mV", target="Vg", forward="x/1000",
                            backward="1000*y", min=-3000, max=3000, unit="mV")
    assert "beyond the limits of Vg" in r["warnings"][0]
    before = ch.get("Vg")
    with pytest.raises(LimitError, match="Vg"):
        ch.set("Vg_mV", 2500)
    assert ch.get("Vg") == before
    ch.set("Vg_mV", 500)
    assert ch.get("Vg") == pytest.approx(0.5) and ch.get("Vg_mV") == pytest.approx(500)
    inf = be.channel_info("Vg_mV")
    assert inf["kind"] == "map" and inf["unit"] == "mV" and inf["target"] == "Vg"


def test_read_composites(demo):
    be, ch = demo, demo.channels
    ch.set("dot.detuning", 0.2, ramp=False)
    assert ch.get("Vdiff") == pytest.approx(0.2)
    be.define_composite(type="read", name="R", expression="piecewise(abs(I) > 1e-12, Vg / I, "
                        "nan)", unit="Ω")
    ch.set("Vg", 0.5, ramp=False)
    v = ch.get("R")
    assert np.isfinite(v) or np.isnan(v)
    be.define_composite(type="read", name="spec2", expression="2*spectrum")
    spec2 = np.asarray(ch.get("spec2"))                     # a vector input stays a vector
    assert spec2.shape == np.asarray(ch.get("spectrum")).shape and spec2.ndim == 1
    # nesting: a read of a read, a mapping and a variable
    gates(be)
    be.define_composite(type="read", name="mix", expression="Vdiff + Tset_C/1000 + gates.eps")
    assert math.isnan(ch.get("mix"))                                    # eps never set
    ch.set_many({"gates.eps": 0.1, "gates.U": 0.2})
    assert ch.get("mix") == pytest.approx(ch.get("Vdiff") + ch.get("Tset_C") / 1000 + 0.1)
    # read-only, also as a target
    with pytest.raises(ChannelError, match="read-only"):
        ch.set("Vdiff", 1)
    with pytest.raises(ChannelError, match="unknown channel"):
        be.define_composite(type="read", name="bad", expression="Vx + nonexistent")
    # a channel named like a constant is used as the channel
    be.define_alias(name="e", target="src.V2")
    be.define_composite(type="read", name="e2", expression="2*e")
    ch.set("e", 0.25, ramp=False)
    assert ch.get("e2") == pytest.approx(0.5)
    inf = be.channel_info("mix")
    assert inf["kind"] == "read" and not inf["writable"] and inf["shape"] is None


def test_multivariate_variables(demo):
    be, ch = demo, demo.channels
    gates(be)
    assert math.isnan(ch.get("gates.eps")) and math.isnan(ch.get("gates.U"))       # never set
    with pytest.raises(ChannelError, match="U has never been set"):
        ch.set("gates.eps", 0.1)
    with pytest.raises(LimitError, match="eps.*above maximum 0.4"):
        ch.set_many({"gates.eps": 0.5, "gates.U": 0.1})
    plan = ch.set_many({"gates.eps": 0.1, "gates.U": 0.3})
    assert plan["Vx"] == pytest.approx(0.4) and plan["Vy"] == pytest.approx(0.2)
    assert ch.get("Vx") == pytest.approx(0.4) and ch.get("Vy") == pytest.approx(0.2)
    assert ch.get("gates.eps") == 0.1 and ch.get("gates.U") == 0.3                  # last set values
    ch.set("gates.U", 0.1)                                                    # eps stays 0.1
    assert ch.get("Vx") == pytest.approx(0.2) and ch.get("Vy") == pytest.approx(0.0)
    # a target beyond its alias limits: refused before anything is written
    be.define_composite(type="multi", name="amp", variables=["g"], targets={
        "Vb": "g", "Vg": "10*g"})
    ch.set("amp.g", 0.1, ramp=False)
    with pytest.raises(LimitError, match="Vg"):
        ch.set("amp.g", 0.3)                                                # Vg 3 V > 2 V
    assert ch.get("Vb") == pytest.approx(0.1) and ch.get("amp.g") == 0.1
    # only the targets that depend on the variable are written
    be.define_composite(type="multi", name="split", variables=["a", "b"], targets={
        "Vg": "a", "Vb": "b + 0*a"})
    ch.set_many({"split.a": 0.0, "split.b": 0.0}, ramp=False)
    seen, undo = writes_of(be)
    ch.set("split.b", 0.2, ramp=False)
    undo()
    assert [c for c, _ in seen] == ["V2"]                               # Vb only
    # piecewise target and a vector target from scalar variables
    be.define_composite(type="multi", name="wav", variables=[{"name": "amp_", "min": 0,
                                                             "max": 2}],
                        targets={"wave": "amp_*sin(linspace(0, 2*pi, 64))",
                                 "Vb": "piecewise(amp_ < 1, 0.1, 0.3)"})
    ch.set("wav.amp_", 1.5, ramp=False)
    np.testing.assert_allclose(ch.get("wave"), 1.5 * np.sin(np.linspace(0, 2 * np.pi, 64)))
    assert ch.get("Vb") == pytest.approx(0.3)
    inf = be.channel_info("gates.eps")
    assert (inf["kind"], inf["composite"], inf["unit"], inf["min"]) == ("variable", "gates",
                                                                        "V", -0.4)


def test_ramps_follow_all_rates(demo):
    be, ch = demo, demo.channels
    gates(be, variables=[{"name": "eps", "min": -0.4, "max": 0.4, "ramp_rate": 0.5},
                         {"name": "U", "min": 0, "max": 0.5}])
    ch.set_many({"gates.eps": 0.0, "gates.U": 0.2}, ramp=False)
    t0 = time.monotonic()
    ch.set("gates.eps", 0.3)                       # 0.3 V at 0.5 V/s; Vx/Vy 2 V/s would allow 0.15 s
    assert time.monotonic() - t0 > 0.5
    assert ch.get("Vx") == pytest.approx(0.5) and ch.get("Vy") == pytest.approx(-0.1)
    # a non-linear relation: every alias stays within its own rate along the path
    be.define_alias(name="VL", target="src.V3", min=-1, max=1, ramp_rate=2.0, replace=True)
    be.define_composite(type="multi", name="sq", variables=[{"name": "q", "min": -1, "max": 1}],
                        targets={"VL": "q**2"})
    ch.set("sq.q", -1.0, ramp=False)
    seen, undo = writes_of(be)
    stamps = []
    h = be.instruments.get("src")
    w = h.write
    h.write = lambda c, v: (stamps.append((time.monotonic(), v)), w(c, v))
    t = time.monotonic()
    ch.set("sq.q", 1.0)                         # VL 1 -> 0 -> 1: 2 V of travel at 2 V/s
    h.write = w
    undo()
    assert time.monotonic() - t > 0.8
    rates = [abs(b[1] - a[1]) / max(b[0] - a[0], 1e-9) for a, b in zip(stamps, stamps[1:])]
    assert max(rates) < 2.0 * 1.6
    # a mapping's own ramp rate (in its units) limits too
    be.define_composite(type="map", name="slow", target="Vb", forward="x/10", backward="10*y",
                        min=-5, max=5, ramp_rate=2.0)
    ch.set("slow", 0.0, ramp=False)
    t = time.monotonic()
    ch.set("slow", 1.0)                      # 1 unit at 2 /s: 0.5 s (Vb alone: 0.1 s)
    assert time.monotonic() - t > 0.4 and ch.get("Vb") == pytest.approx(0.1)
    # moved behind its back: a straight line from where the hardware really is
    ch.set("src.V2", -0.3)
    seen, undo = writes_of(be)
    ch.set("slow", 2.0)
    undo()
    assert seen[0][1] < -0.2                 # starts near the real -0.3, not the remembered 0.1
    assert ch.get("Vb") == pytest.approx(0.2)


def test_nesting_diamond_and_conflicts(demo):
    be, ch = demo, demo.channels
    gates(be)
    # a mapping onto a variable, a multivariate composite onto variables of another
    be.define_composite(type="map", name="eps_mV", target="gates.eps", forward="x/1000",
                        backward="1000*y", min=-400, max=400, unit="mV")
    be.define_composite(type="multi", name="outer", variables=["s"],
                        targets={"gates.U": "0.2 + s", "eps_mV": "100*s"})
    ch.set("outer.s", 0.1, ramp=False)          # U = 0.3, eps = 0.01 (both new: computed once)
    assert ch.get("gates.U") == pytest.approx(0.3) and ch.get("gates.eps") == pytest.approx(0.01)
    assert ch.get("Vx") == pytest.approx(0.31) and ch.get("Vy") == pytest.approx(0.29)
    assert ch.get("eps_mV") == pytest.approx(10.0)
    seen, undo = writes_of(be)
    ch.set("outer.s", 0.2, ramp=False)
    undo()
    assert sorted(c for c, _ in seen) == ["V3", "V4"]   # each alias written once, not per path
    assert ch.get("Vx") == pytest.approx(0.42)          # 0.4 + 0.02
    # limits of a nested variable apply: U max 0.5
    with pytest.raises(LimitError, match="U.*above maximum 0.5"):
        ch.set("outer.s", 0.35)
    assert ch.get("outer.s") == 0.2
    # two requests for the same alias that disagree
    with pytest.raises(ChannelError, match="two values at once"):
        ch.set_many({"outer.s": 0.1, "gates.U": 0.4})
    ch.set_many({"outer.s": 0.1, "gates.U": 0.3}, ramp=False)       # agreeing: fine
    # what a sweep needs to know: the instruments behind
    assert ch.instruments_behind("outer.s") == {"src"} and ch.aliases_behind("outer.s") == {"Vx", "Vy"}
    assert ch.aliases_behind("Vdiff") == {"Vx", "Vy"}


def test_dependency_loops_are_refused(demo):
    be, ch = demo, demo.channels
    be.define_composite(type="map", name="m1", target="Vx", forward="x", backward="y",
                        min=-1, max=1)
    be.define_composite(type="map", name="m2", target="m1", forward="x", backward="y",
                        min=-1, max=1)
    with pytest.raises(ChannelError, match=r"circular dependency: (m1 → m2 → m1|m2 → m1 → m2)"):
        be.define_composite(type="map", name="m1", target="m2", forward="x", backward="y",
                            min=-1, max=1, replace=True)
    assert be.channels._owner["m1"].target == "Vx"          # the old definition is kept
    # through two multivariate composites
    be.define_composite(type="multi", name="GA", variables=["a"], targets={"Vy": "a"})
    be.define_composite(type="multi", name="GB", variables=["b"], targets={"GA.a": "b"})
    with pytest.raises(ChannelError, match="circular dependency"):
        be.define_composite(type="multi", name="GA", variables=["a"],
                            targets={"Vy": "a", "GB.b": "a"}, replace=True)
    # reads
    be.define_composite(type="read", name="r1", expression="Vx + 1")
    be.define_composite(type="read", name="r2", expression="r1 * 2")
    with pytest.raises(ChannelError, match="circular dependency: r1 → r2 → r1"):
        be.define_composite(type="read", name="r1", expression="r2 + 1", replace=True)
    with pytest.raises(ChannelError, match="r3 → r3"):
        be.define_composite(type="read", name="r3", expression="r3")
    # removal / redefinition of something in use
    with pytest.raises(ChannelError, match="used by"):
        be.remove_composite("m1")
    with pytest.raises(ChannelError, match="still used by GB"):
        be.define_composite(type="multi", name="GA", variables=["a2"], targets={"Vy": "a2"},
                            replace=True)
    with pytest.raises(ChannelError, match="used by composite"):
        be.remove_alias("Vx")
    # a loop that slipped in anyway (here: forced past the checks) fails fast, no hang
    c = be.channels._owner["r1"]
    c.inputs = ["r2"]
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("e", _catch(ch.get, "r1")), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive() and "circular dependency" in str(out["e"])
    m = be.channels._owner["m1"]
    m.target = "m2"
    with pytest.raises(ChannelError, match="circular dependency"):
        ch.set("m2", 0.1)
    assert find_cycle({"a": ["b"], "b": ["c"], "c": ["a"]}) == ["a", "b", "c", "a"]
    assert find_cycle({"a": ["b"], "b": []}) is None


def _catch(fn, *a):
    try:
        return fn(*a)
    except Exception as exc:
        return exc


def test_sweeps_over_composites(demo):
    be = demo
    gates(be)
    be.define_composite(type="read", name="sumxy", expression="Vx + Vy")
    # both variables in one dimension: set together, so even the very first point works
    d = {"name": "both", "dimensions": [
        {"points": 5, "sweep": [{"channel": "gates.eps", "start": -0.2, "stop": 0.2},
                                {"channel": "gates.U", "start": 0.1, "stop": 0.3}],
         "record": ["Vx", "Vy", "sumxy"]}]}
    be.validate_sweep(d)
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert be.queue.wait_idle(60)
    item = be.queue_state()["history"][-1]
    assert item["status"] == "finished", item
    ds = DataSet.load(item["path"])
    eps, U = np.linspace(-0.2, 0.2, 5), np.linspace(0.1, 0.3, 5)
    np.testing.assert_allclose(ds.data["Vx"], U + eps, atol=1e-9)
    np.testing.assert_allclose(ds.data["sumxy"], 2 * U, atol=1e-9)
    # U outer, eps inner: U is set first (validation does not need eps / U known)
    d = {"name": "nested", "dimensions": [
        {"points": 3, "sweep": [{"channel": "gates.eps", "start": -0.1, "stop": 0.1}], "record": ["Vx"]},
        {"points": 2, "sweep": [{"channel": "gates.U", "start": 0.2, "stop": 0.4}], "record": []}]}
    be.validate_sweep(d)
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert be.queue.wait_idle(60)
    item = be.queue_state()["history"][-1]
    assert item["status"] == "finished", item
    ds = DataSet.load(item["path"])
    np.testing.assert_allclose(ds.data["Vx"], np.array([[0.1, 0.3], [0.2, 0.4], [0.3, 0.5]]),
                               atol=1e-9)
    # limits of a combination are checked before the sweep starts
    bad = {"name": "bad", "dimensions": [
        {"points": 3, "sweep": [{"channel": "gates.eps", "start": 0, "stop": 0.6}], "record": []}]}
    with pytest.raises(Exception, match="eps.*above maximum 0.4"):
        be.validate_sweep(bad)
    # a mapping as the swept channel
    d = {"name": "map", "dimensions": [
        {"points": 3, "sweep": [{"channel": "Tset_C", "start": 20, "stop": 26}],
         "record": ["Tset"]}]}
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert be.queue.wait_idle(120)
    item = be.queue_state()["history"][-1]
    assert item["status"] == "finished", item
    np.testing.assert_allclose(DataSet.load(item["path"]).data["Tset"],
                               [293.15, 296.15, 299.15], atol=1e-6)


def test_out_of_range_actions(demo):
    """skip (default): sweeps skip combinations that put a target out of range, a direct
    write is refused; error: the sweep does not start; clamp: the target stops at
    its limit.  Variables swept in different dimensions are set together, so an outer
    variable never moves the hardware with a stale inner one."""
    be = demo
    gates(be, targets={"Vx": "3*U + eps", "Vy": "U - eps"})      # Vx: ±1 V
    assert next(c for c in be.list_channels()["composites"]
                if c["name"] == "gates")["out_of_range"] == "skip"
    eps, U = np.linspace(-0.4, 0.4, 5), np.linspace(0.0, 0.5, 6)
    out = (3 * U[None, :] + eps[:, None]) > 1 + 1e-9                # (eps, U)
    d = {"name": "oor", "dimensions": [
        {"points": 5, "sweep": [{"channel": "gates.eps", "start": -0.4, "stop": 0.4}], "record": ["Vx"]},
        {"points": 6, "sweep": [{"channel": "gates.U", "start": 0.0, "stop": 0.5}], "record": []}]}
    r = be.validate_sweep(d)
    assert "composite gates: %d of 30 points out of range, will be skipped" % out.sum() in r["warnings"]
    be.set_many({"gates.eps": 0.0, "gates.U": 0.0})
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert be.queue.wait_idle(60)
    item = be.queue_state()["history"][-1]
    assert item["status"] == "finished", item
    ds = DataSet.load(item["path"])
    np.testing.assert_array_equal(np.isnan(ds.data["Vx"]), out)
    np.testing.assert_allclose(ds.data["Vx"][~out], (3 * U[None, :] + eps[:, None])[~out],
                               atol=1e-9)
    # both in one dimension: skipped the same way
    d1 = {"name": "diag", "dimensions": [
        {"points": 6, "sweep": [{"channel": "gates.U", "start": 0, "stop": 0.5},
                                {"channel": "gates.eps", "start": 0, "stop": 0}], "record": ["Vx"]}]}
    be.queue_add({"kind": "sweep", "definition": d1}, start=True)
    assert be.queue.wait_idle(60)
    np.testing.assert_array_equal(np.isnan(DataSet.load(be.queue_state()["history"][-1]["path"])
                                           .data["Vx"]), 3 * U > 1 + 1e-9)
    # a direct write is refused in skip mode, and says why
    with pytest.raises(OutOfRange, match="Vx.*above maximum 1.*composite gates, which skips"):
        be.set_many({"gates.eps": 0.4, "gates.U": 0.5})
    # error: the sweep is refused before it starts
    gates(be, targets={"Vx": "3*U + eps", "Vy": "U - eps"}, out_of_range="error", replace=True)
    with pytest.raises(Exception, match="Vx.*above maximum 1"):
        be.validate_sweep(d)
    # clamp: the target stops at its limit, the other follows its formula
    gates(be, targets={"Vx": "3*U + eps", "Vy": "U - eps"}, out_of_range="clamp", replace=True)
    assert be.validate_sweep(d)["warnings"] == []
    be.set_many({"gates.eps": 0.1, "gates.U": 0.5}, ramp=False)
    assert be.channels.get("Vx") == pytest.approx(1.0) and be.channels.get("Vy") == pytest.approx(0.4)
    with pytest.raises(ChannelError, match="out_of_range must be one of"):
        gates(be, out_of_range="wrap", replace=True)
    # kept in setups
    assert composite_from_dict(next(c for c in be.list_channels()["composites"]
                                    if c["name"] == "gates"))[0].out_of_range == "clamp"


def test_setups_keep_nested_composites(demo, tmp_path):
    be = demo
    gates(be)
    be.define_composite(type="map", name="eps_mV", target="gates.eps", forward="x/1000",
                        backward="1000*y", min=-400, max=400, unit="mV")
    be.define_composite(type="multi", name="outer", variables=[{"name": "s", "ramp_rate": 0.5}],
                        targets={"gates.U": "0.2 + s", "eps_mV": "100*s"})
    be.define_composite(type="read", name="r", expression="eps_mV + Vdiff")
    p = tmp_path / "setup.json"
    be.save_setup(str(p))
    setup = json.loads(p.read_text())
    setup["composites"].reverse()                    # users before what they use
    p.write_text(json.dumps(setup))
    be.channels.clear()
    assert be.load_setup(path=str(p)) == []
    kinds = {c["name"]: c["type"] for c in be.list_channels()["composites"]}
    assert kinds == {"dot": "multi", "Vdiff": "read", "Tset_C": "map", "gates": "multi",
                     "eps_mV": "map", "outer": "multi", "r": "read"}
    assert be.channel_info("outer.s")["ramp_rate"] == 0.5
    be.channels.set("outer.s", 0.1, ramp=False)
    assert be.channels.get("Vx") == pytest.approx(0.31)
    virt = {v["name"]: v for v in be.list_channels()["channels"]}
    assert virt["outer.s"]["kind"] == "variable" and virt["outer.s"]["last"] == 0.1
    assert virt["r"]["kind"] == "read" and virt["eps_mV"]["kind"] == "map"
    # a composite without a type is reported, the rest loads
    old = json.loads(p.read_text())
    old["composites"].append({"name": "det", "targets": {"Vx": "x", "Vy": "-x"}})
    p.write_text(json.dumps(old))
    probs = be.load_setup(path=str(p))
    assert any("det" in x and "type must be" in x for x in probs), probs


def test_monitor_and_scripts_use_composites(demo):
    be = demo
    gates(be)
    be.run_script_now("set_many({'gates.eps': 0.05, 'gates.U': 0.1})\n"
                      "state['eps'] = get('gates.eps')")
    assert be.queue.wait_idle(30)
    assert be.queue_state()["history"][-1]["status"] == "finished"
    assert be.channels.get("Vx") == pytest.approx(0.15)
    be.set_many({"gates.eps": 0.0, "gates.U": 0.2}, ramp=False)
    assert be.channels.get("Vy") == pytest.approx(0.2)
    be.monitor_add("Vdiff", 0.05)
    be.monitor_add("gates.eps", 0.05)
    t0 = time.time()
    while time.time() - t0 < 5:
        st = {e["channel"]: e for e in be.monitor.status()}
        if all(st[c]["last_value"] is not None for c in ("Vdiff", "gates.eps")):
            break
        time.sleep(0.05)
    assert st["gates.eps"]["last_value"] == 0.0 and st["Vdiff"]["last_error"] in (None, "")
    # removing the composite takes its channels off the monitor
    be.remove_composite("gates")
    assert "gates.eps" not in {e["channel"] for e in be.monitor.status()}
