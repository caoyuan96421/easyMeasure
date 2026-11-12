"""Stress tests of multivariate composites in sweeps: out-of-range skip / error /
clamp, variables spread over dimensions, snake scans, value lists, ramps, nested
composites, point actions, abort, and the vectorised validation.

The randomised test checks, for every case:

* the hardware is never written outside an alias' limits;
* skip: the NaN pattern of the inner records is exactly the predicted set of
  out-of-range points, and validation predicts their number;
* error: validation fails exactly when some point is out of range;
* clamp: the recorded targets are the clipped formulas;
* every recorded variable (in any dimension) equals its setpoint.
"""

import json
import time

import numpy as np
import pytest

from easymeasure.backend.storage import DataSet
from easymeasure.backend.sweep import compute_setpoints, normalize_definition

LIM = {"Vx": 1.0, "Vy": 1.0, "Vg": 2.0}
FORMS = [
    {"Vx": "a + b", "Vy": "a - b"},
    {"Vx": "2*a + 0.5*b - c", "Vy": "a - b + c", "Vg": "3*c + a"},
    {"Vx": "a*a + b", "Vy": "b - c"},
    {"Vx": "1.5*a", "Vy": "b + 0.3", "Vg": "a*b*4 + c"},
    {"Vx": "sin(3*a) + b", "Vy": "a - 0.7*c"},
]


def evalf(expr, env):
    return eval(expr, {"sin": np.sin, "cos": np.cos}, env)


def run_case(be, rng, case):
    """One random composite, sweep and out-of-range mode; asserts the invariants."""
    form = FORMS[rng.integers(len(FORMS))]
    used = sorted({v for e in form.values() for v in "abc" if v in e.replace("sin", "")})
    mode = ["skip", "error", "clamp"][rng.integers(3)]
    ramps = rng.random() < 0.3
    for al in ("Vx", "Vy", "Vg"):
        be.define_alias(name=al, target={"Vx": "src.V3", "Vy": "src.V4", "Vg": "src.V1"}[al],
                        min=-LIM[al], max=LIM[al], ramp_rate=(40.0 if ramps else None),
                        unit="V", replace=True)
    vlim = {v: float(rng.uniform(0.3, 1.2)) for v in used}
    vramp = {v: (float(rng.uniform(5, 30)) if ramps and rng.random() < 0.5 else None) for v in used}
    try:
        be.remove_composite(name="G")
    except Exception:
        pass
    be.define_composite(type="multi", name="G",
                        variables=[{"name": v, "min": -vlim[v], "max": vlim[v], "ramp_rate": vramp[v]}
                                   for v in used],
                        targets=form, out_of_range=mode)
    # start from a reachable point
    be.set_many(values={"G." + v: 0.0 for v in used})
    ndim = int(rng.integers(1, 4))
    dims = [{"points": int(rng.integers(2, 6)), "sweep": [], "record": [],
             "snake": bool(rng.random() < 0.5)} for _ in range(ndim)]
    swept = [v for v in used if rng.random() < 0.85] or used[:1]
    for v in swept:
        k = int(rng.integers(ndim))
        lo, hi = sorted(rng.uniform(-vlim[v], vlim[v], 2))
        if rng.random() < 0.3:
            vals = list(np.round(rng.uniform(-vlim[v], vlim[v], dims[k]["points"]), 4))
            dims[k]["sweep"].append({"channel": "G." + v, "values": vals})
        else:
            dims[k]["sweep"].append({"channel": "G." + v, "start": float(lo), "stop": float(hi)})
    if rng.random() < 0.3:                       # an unrelated channel too
        k = int(rng.integers(ndim))
        dims[k]["sweep"].append({"channel": "Vb", "start": -0.1, "stop": 0.1})
    fixed = {v: float(np.round(rng.uniform(-0.2, 0.2), 3)) for v in used if v not in swept}
    if fixed:
        be.set_many(values=dict({"G." + v: 0.0 for v in swept}, **{"G." + k: x for k, x in fixed.items()}))
    dims[0]["record"] = list(form) + [s["channel"] for s in dims[0]["sweep"]]
    for k in range(1, ndim):                     # outer dims record their own variables
        dims[k]["record"] = [s["channel"] for s in dims[k]["sweep"]]
        if rng.random() < 0.3:
            dims[k]["record_after_inner"] = True
    d = {"name": "stress%d" % case, "dimensions": dims}
    nd = normalize_definition(json.loads(json.dumps(d)))
    sp = compute_setpoints(nd)
    shape = [dm["points"] for dm in nd["dimensions"]]
    # prediction on the full grid
    grid = {}
    for v in used:
        arr = np.full(shape, fixed.get(v, np.nan))
        for k in range(ndim):
            if "G." + v in sp[k]:
                sh = [1] * ndim; sh[k] = shape[k]
                arr = np.broadcast_to(np.asarray(sp[k]["G." + v], float).reshape(sh), shape).copy()
        grid[v] = arr
    tv = {t: np.asarray(evalf(e, dict(grid)), float) * np.ones(shape) for t, e in form.items()}
    oor = np.zeros(shape, bool)
    for t, arr in tv.items():
        oor |= np.abs(arr) > LIM[t] + 1e-9
    seen, restore = [], None
    h = be.instruments.get("src")
    orig = h.write

    def spy(c, v):
        seen.append((c, v))
        return orig(c, v)
    h.write = spy
    info = dict(case=case, mode=mode, ramps=ramps, form=form, shape=shape, oor=int(oor.sum()),
                dims=[[s["channel"] for s in dm["sweep"]] for dm in dims])
    try:
        try:
            r = be.validate_sweep(definition=d)
            vok = r["ok"]
            warn = r.get("warnings", [])
            verr = r.get("errors")
        except Exception as exc:
            vok, warn, verr = False, [], str(exc)
        if mode == "error":
            assert vok == (not oor.any()), ("error-mode validation", vok, verr, info)
            if not vok:
                return info
        else:
            assert vok, ("validation failed", verr, info)
        if mode == "skip":
            n = int(oor.sum())
            msg = [w for w in warn if w.startswith("composite G:")]
            if n:
                assert msg and msg[0].startswith("composite G: %d of " % n), (msg, n, info)
            else:
                assert not msg, (msg, info)
        be.queue_add({"kind": "sweep", "definition": d}, start=True)
        assert be.queue.wait_idle(120), info
        item = be.queue_state()["history"][-1]
        assert item["status"] == "finished", (item.get("error"), item["status"], info)
        ds = DataSet.load(item["path"])
        if any(t not in ds.data for t in form):
            raise AssertionError(("missing data", sorted(ds.data), ds.footer if hasattr(ds, "footer") else None, info))
        for t in form:
            got = np.asarray(ds.data[t])
            if mode == "skip":
                assert np.array_equal(np.isnan(got), oor), ("nan pattern", t, np.isnan(got).astype(int), oor.astype(int), info)
                np.testing.assert_allclose(got[~oor], tv[t][~oor], atol=1e-6, err_msg=str(info))
            elif mode == "clamp":
                np.testing.assert_allclose(got, np.clip(tv[t], -LIM[t], LIM[t]), atol=1e-6, err_msg=str(info))
            else:
                np.testing.assert_allclose(got, tv[t], atol=1e-6, err_msg=str(info))
        for k in range(ndim):                     # recorded variables == their setpoints
            for s_ in dims[k]["sweep"]:
                ch = s_["channel"]
                got = np.asarray(ds.data[ch])
                want = np.broadcast_to(np.asarray(sp[k][ch], float).reshape(
                    [shape[k]] + [1] * (ndim - k - 1)), tuple(shape[k:]))
                assert got.shape == want.shape, (ch, got.shape, want.shape)
                fin = np.isfinite(got)
                if k == 0 and mode != "skip":
                    assert fin.all(), (ch, info)
                bad = ~np.isclose(got[fin], want[fin], atol=1e-9)
                assert not bad.any(), ("recorded %s in dim %d != setpoint" % (ch, k + 1),
                                       got.tolist(), want.tolist(), info)
        # hardware never beyond the limits
        chmap = {"V3": "Vx", "V4": "Vy", "V1": "Vg"}
        for c, v in seen:
            if c in chmap:
                assert abs(v) <= LIM[chmap[c]] + 1e-9, ("hardware beyond limit", c, v, info)
        info["writes"] = len(seen)
        return info
    finally:
        h.write = orig




def _run(be, d, timeout=120):
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    assert be.queue.wait_idle(timeout)
    item = be.queue_state()["history"][-1]
    return item, (DataSet.load(item["path"]) if item.get("path") else None)


def _spy(be, inst="src"):
    h = be.instruments.get(inst)
    seen, orig = [], h.write

    def w(c, v):
        seen.append((c, v))
        return orig(c, v)
    h.write = w
    return seen


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_composite_sweeps(demo, seed):
    rng = np.random.default_rng(seed)
    for case in range(25):
        run_case(demo, rng, case)


def test_vectorised_validation_agrees_with_one_by_one(demo, monkeypatch):
    """The fast (all combinations at once) and the slow (one by one) validation give
    the same counts and the same errors."""
    be = demo
    rng = np.random.default_rng(5)
    for case in range(30):
        form = FORMS[case % len(FORMS)]
        used = sorted({v for e in form.values() for v in "abc" if v in e.replace("sin", "")})
        mode = ["skip", "error", "clamp"][case % 3]
        try:
            be.remove_composite(name="G")
        except Exception:
            pass
        be.define_composite(type="multi", name="G", targets=form, out_of_range=mode,
                            variables=[{"name": v, "min": -1, "max": 1} for v in used])
        be.set_many(values={"G." + v: 0.0 for v in used})
        dims = [{"points": int(rng.integers(2, 9)), "sweep": [], "record": ["Vx"]},
                {"points": int(rng.integers(2, 9)), "sweep": [], "record": []}]
        for v in used:
            lo, hi = sorted(rng.uniform(-1, 1, 2))
            dims[int(rng.integers(2))]["sweep"].append({"channel": "G." + v, "start": lo, "stop": hi})
        d = {"name": "agree", "dimensions": dims}

        def outcome():
            try:
                return ("ok", sorted(be.validate_sweep(definition=d)["warnings"]))
            except Exception as exc:
                return ("error", str(exc))
        fast = outcome()
        with monkeypatch.context() as m:
            m.setattr(type(be.channels), "grid_out_of_range", lambda self, values: None)
            slow = outcome()
        assert fast == slow, (case, mode, form, d)


def test_ramp_across_a_skipped_non_convex_region(demo):
    """Between two allowed points the formula may leave the range (here |1.5 sin 3a|
    > 1 between them): the gates then ramp on a straight line, which stays within
    the limits, instead of failing the sweep."""
    be = demo
    for var_rate, alias_rate in ((None, 20.0), (5.0, None), (5.0, 20.0)):
        for al, tgt in (("Vx", "src.V3"), ("Vy", "src.V4")):
            be.define_alias(name=al, target=tgt, min=-1, max=1, ramp_rate=alias_rate,
                            unit="V", replace=True)
        try:
            be.remove_composite(name="G")
        except Exception:
            pass
        be.define_composite(type="multi", name="G", targets={"Vx": "1.5*sin(3*a) + b", "Vy": "b"},
                            variables=[{"name": "a", "min": -1, "max": 1, "ramp_rate": var_rate},
                                       {"name": "b", "min": -1, "max": 1}])
        be.set_many(values={"G.a": 0, "G.b": 0})
        seen = _spy(be)
        item, ds = _run(be, {"name": "nc", "dimensions": [
            {"points": 41, "sweep": [{"channel": "G.a", "start": -1, "stop": 1}], "record": ["Vx"]}]})
        assert item["status"] == "finished", item
        a = np.linspace(-1, 1, 41)
        np.testing.assert_array_equal(np.isnan(ds.data["Vx"]), np.abs(1.5 * np.sin(3 * a)) > 1)
        assert max(abs(v) for c, v in seen if c == "V3") <= 1.0


def test_nested_composites_keep_their_own_mode(demo):
    """CB drives the variables of CA, CA drives the gates.  A value out of range is
    handled by the mode of the composite that computed it."""
    be = demo
    for al, tgt in (("Vx", "src.V3"), ("Vy", "src.V4")):
        be.define_alias(name=al, target=tgt, min=-1, max=1, ramp_rate=None, unit="V", replace=True)
    P, Q = np.linspace(-1, 1, 7)[:, None], np.linspace(-1, 1, 5)[None, :]
    eps, U = P * Q * np.ones((7, 5)), (P + Q) * np.ones((7, 5))
    bad_b = np.abs(eps) > 0.6 + 1e-9                     # CB's target eps (CA: ±0.6)
    bad_a = (np.abs(U + eps) > 1 + 1e-9) | (np.abs(U - eps) > 1 + 1e-9)
    assert (bad_a & ~bad_b).any() and (bad_b & ~bad_a).any()
    d = {"name": "nest", "dimensions": [
        {"points": 7, "sweep": [{"channel": "CB.p", "start": -1, "stop": 1}], "record": ["Vx", "Vy"]},
        {"points": 5, "sweep": [{"channel": "CB.q", "start": -1, "stop": 1}], "record": []}]}
    for ma, mb in (("skip", "skip"), ("error", "skip"), ("skip", "error"), ("clamp", "skip"),
                   ("skip", "clamp")):
        for n in ("CB", "CA"):
            try:
                be.remove_composite(name=n)
            except Exception:
                pass
        be.define_composite(type="multi", name="CA", out_of_range=ma, targets={"Vx": "U + eps", "Vy": "U - eps"},
                            variables=[{"name": "eps", "min": -0.6, "max": 0.6},
                                       {"name": "U", "min": -5, "max": 5}])
        be.define_composite(type="multi", name="CB", out_of_range=mb, targets={"CA.eps": "p*q", "CA.U": "p + q"},
                            variables=[{"name": "p", "min": -1, "max": 1},
                                       {"name": "q", "min": -1, "max": 1}])
        be.set_many(values={"CB.p": 0, "CB.q": 0})
        if "error" in (ma, mb):
            with pytest.raises(Exception, match="above maximum|below minimum"):
                be.validate_sweep(definition=d)
            continue
        seen = _spy(be)
        item, ds = _run(be, d)
        assert item["status"] == "finished", (ma, mb, item)
        e_ = np.clip(eps, -0.6, 0.6) if mb == "clamp" else eps
        vx, vy = U + e_, U - e_
        skipped = np.zeros((7, 5), bool)
        if mb == "skip":
            skipped |= bad_b
        if ma == "skip":
            skipped |= (np.abs(vx) > 1 + 1e-9) | (np.abs(vy) > 1 + 1e-9)
        np.testing.assert_array_equal(np.isnan(ds.data["Vx"]), skipped, err_msg=str((ma, mb)))
        want = np.clip(vx, -1, 1) if ma == "clamp" else vx
        np.testing.assert_allclose(ds.data["Vx"][~skipped], want[~skipped], atol=1e-9)
        assert max(abs(v) for c, v in seen if c in ("V3", "V4")) <= 1.0


def test_point_actions_and_fully_skipped_sweeps(demo):
    """The pre-sweep action runs at every point (it comes before the check), the
    others only where something is measured; a sweep whose every point is skipped
    still has its (all-NaN) data."""
    be = demo
    for al, tgt in (("Vx", "src.V3"), ("Vy", "src.V4")):
        be.define_alias(name=al, target=tgt, min=-1, max=1, ramp_rate=None, unit="V", replace=True)
    be.define_composite(type="multi", name="G", targets={"Vx": "a + b", "Vy": "a - b"},
                        variables=[{"name": "a", "min": -3, "max": 3}, {"name": "b", "min": -1, "max": 1}])
    be.set_many(values={"G.a": 0, "G.b": 0})
    code = "state[%r] = state.get(%r, 0) + 1"
    hooks = {h: {"enabled": True, "code": code % (h, h)}
             for h in ("pre_sweep", "post_sweep", "pre_record", "post_record")}
    be.scripts.state.clear()
    d = {"name": "hooks", "dimensions": [
        {"points": 11, "sweep": [{"channel": "G.a", "start": -1, "stop": 1}], "record": ["Vx"], "hooks": hooks},
        {"points": 3, "sweep": [{"channel": "G.b", "start": -0.5, "stop": 0.5}], "record": ["G.b"]}]}
    item, ds = _run(be, d)
    A, B = np.linspace(-1, 1, 11)[:, None], np.linspace(-0.5, 0.5, 3)[None, :]
    bad = (np.abs(A + B) > 1 + 1e-9) | (np.abs(A - B) > 1 + 1e-9)
    st = dict(be.scripts.state)
    assert st["pre_sweep"] == 33
    assert st["post_sweep"] == st["pre_record"] == st["post_record"] == 33 - bad.sum()
    np.testing.assert_array_equal(np.isnan(ds.data["Vx"]), bad)
    np.testing.assert_allclose(ds.data["G.b"], [-0.5, 0, 0.5])       # outer record: its setpoint
    assert ds.footer["out_of_range_skipped"] == bad.sum()
    # every point out of range
    d2 = {"name": "none", "dimensions": [
        {"points": 4, "sweep": [{"channel": "G.a", "start": 2, "stop": 3}], "record": ["Vx", "Vy"]},
        {"points": 2, "sweep": [{"channel": "G.b", "start": 0, "stop": 0.1}], "record": ["G.b"]}]}
    assert "composite G: 8 of 8 points out of range, will be skipped" in \
        be.validate_sweep(definition=d2)["warnings"]
    item, ds = _run(be, d2)
    assert item["status"] == "finished"
    assert ds.data["Vx"].shape == (4, 2) and np.isnan(ds.data["Vx"]).all()
    assert np.isnan(DataSet.load(item["path"]).data["Vy"]).all()


def test_big_maps_validate_fast_and_abort_at_once(demo):
    """A 1000 x 1000 map validates in well under a second; a long stretch of skipped
    points and the slow (nested) validation can be aborted at once."""
    be = demo
    for al, tgt in (("Vx", "src.V3"), ("Vy", "src.V4")):
        be.define_alias(name=al, target=tgt, min=-1, max=1, ramp_rate=None, unit="V", replace=True)
    be.define_composite(type="multi", name="G", targets={"Vx": "a + b", "Vy": "a - b"},
                        variables=[{"name": "a", "min": -10, "max": 10}, {"name": "b", "min": -10, "max": 10}])
    be.set_many(values={"G.a": 0, "G.b": 0})
    d = {"name": "big", "dimensions": [
        {"points": 1000, "sweep": [{"channel": "G.a", "start": -10, "stop": 10}], "record": ["Vx"]},
        {"points": 1000, "sweep": [{"channel": "G.b", "start": -10, "stop": 10}], "record": []}]}
    t0 = time.time()
    r = be.validate_sweep(definition=d)
    assert time.time() - t0 < 2.0
    a = np.linspace(-10, 10, 1000)
    n_bad = int(((np.abs(a[:, None] + a[None, :]) > 1 + 1e-12) |
                 (np.abs(a[:, None] - a[None, :]) > 1 + 1e-12)).sum())
    assert "composite G: %d of 1000000 points out of range, will be skipped" % n_bad in r["warnings"]
    d["dimensions"][0]["points"] = d["dimensions"][1]["points"] = 300
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    time.sleep(1.5)
    t0 = time.time()
    be.abort()
    assert be.queue.wait_idle(10) and time.time() - t0 < 2.0
    assert be.queue_state()["history"][-1]["status"] == "aborted"
    # nested: checked one by one at the start of the run, still abortable
    be.define_composite(type="multi", name="H", targets={"G.a": "p", "G.b": "q"},
                        variables=[{"name": "p", "min": -10, "max": 10}, {"name": "q", "min": -10, "max": 10}])
    be.set_many(values={"H.p": 0, "H.q": 0})
    d["dimensions"][0]["sweep"][0]["channel"] = "H.p"
    d["dimensions"][1]["sweep"][0]["channel"] = "H.q"
    d["dimensions"][0]["points"] = d["dimensions"][1]["points"] = 480     # < one-by-one limit
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    time.sleep(0.5)
    t0 = time.time()
    be.abort()
    assert be.queue.wait_idle(10) and time.time() - t0 < 2.0
    assert be.queue_state()["history"][-1]["status"] == "aborted"
