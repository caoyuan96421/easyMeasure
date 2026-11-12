import threading
import time

import numpy as np
import pytest

from easymeasure.backend.storage import DataSet
from easymeasure.backend.sweep import compute_setpoints, normalize_definition
from easymeasure.common.errors import ChannelError, SweepDefinitionError


def run(be, definition, timeout=60):
    be.queue_add({"kind": "sweep", "definition": definition}, start=True)
    assert be.queue.wait_idle(timeout)
    item = be.queue_state()["history"][-1]
    return item, (DataSet.load(item["path"]) if item.get("path") else None)


def test_definition_errors():
    for bad in [{}, {"dimensions": []}, {"dimensions": [{"sweep": []}]},
                {"dimensions": [{"points": 0}]},
                {"dimensions": [{"points": 3, "sweep": [{"channel": "Vg"}]}]},
                {"dimensions": [{"points": 3, "record": ["I"]}, {"points": 2, "record": ["I"]}]},
                {"dimensions": [{"points": 3}], "on_read_error": "whatever"}]:
        with pytest.raises(SweepDefinitionError):
            normalize_definition(bad)


def test_setpoints_mapping_and_values():
    d = normalize_definition({"dimensions": [
        {"points": 5, "sweep": [{"channel": "a", "start": 0, "stop": 1},
                                {"channel": "b", "start": 0, "stop": 4, "mapping": "start+(stop-start)*t**2"},
                                {"channel": "c", "values": [5, 4, 3, 2, 1]},
                                {"channel": "w", "mapping": "np.ones(8)*i"}]}]})
    sp = compute_setpoints(d)[0]
    np.testing.assert_allclose(sp["a"], np.linspace(0, 1, 5))
    np.testing.assert_allclose(sp["b"], 4 * np.linspace(0, 1, 5) ** 2)
    np.testing.assert_allclose(sp["c"], [5, 4, 3, 2, 1])
    assert sp["w"].shape == (5, 8)
    with pytest.raises(SweepDefinitionError):
        compute_setpoints(normalize_definition(
            {"dimensions": [{"points": 3, "sweep": [{"channel": "a", "mapping": "np.ones(i+1)"}]}]}))
    with pytest.raises(SweepDefinitionError):
        compute_setpoints(normalize_definition(
            {"dimensions": [{"points": 2, "sweep": [{"channel": "a", "values": [1, 2, 3]}]}]}))


def test_validation_against_channels(demo):
    base = {"dimensions": [{"points": 5, "sweep": [{"channel": "Vg", "start": 0, "stop": 3}],
                            "record": ["I"]}]}
    with pytest.raises(SweepDefinitionError, match="above maximum"):
        demo.validate_sweep(base)
    for sweep, msg in [({"channel": "I", "start": 0, "stop": 1}, "not writable"),
                       ({"channel": "src.output", "start": 0, "stop": 1}, "alias"),
                       ({"channel": "src.V1", "start": 0, "stop": 1}, "alias"),
                       ({"channel": "wave", "start": 0, "stop": 1}, "mapping is required"),
                       ({"channel": "nope", "start": 0, "stop": 1}, "nope")]:
        with pytest.raises(SweepDefinitionError, match=msg):
            demo.validate_sweep({"dimensions": [{"points": 3, "sweep": [sweep]}]})
    with pytest.raises(SweepDefinitionError, match="syntax"):
        demo.validate_sweep({"dimensions": [{"points": 2}], "pre": {"hook": "if:"}})
    # queue_add refuses invalid sweeps up front
    with pytest.raises(SweepDefinitionError):
        demo.queue_add({"kind": "sweep", "definition": base})


def test_3d_sweep_with_vectors_images_and_phases(demo):
    d = {"name": "three-d", "dimensions": [
        {"points": 6, "sweep": [{"channel": "Vg", "start": -0.2, "stop": 0.2},
                                {"channel": "wave", "mapping": "np.linspace(0,1,16)*t"}],
         "record": ["I", "XY", "image", "awg_rms"]},
        {"points": 3, "sweep": [{"channel": "Vb", "values": [-0.1, 0.0, 0.1]}], "record": ["spectrum"],
         "snake": True},
        {"points": 2, "sweep": [{"channel": "dot.detuning", "start": 0, "stop": 0.2}], "record": ["T", "@time"],
         "record_after_inner": True}],
        "pre": {"constants": [{"channel": "src.output", "value": "ON"}],
                "record": ["src.output", "lockin.sensitivity", "B"],
                "hook": "state['pre'] = get('Vg')\nset_raw('awg.run', 'RUN')"},
        "post": {"constants": [{"channel": "Vg", "value": 0.0}], "record": ["Vg"],
                 "hook": "state['post'] = True"}}
    item, ds = run(demo, d)
    assert item["status"] == "finished", item
    assert ds.status == "finished"
    assert ds.shape == [6, 3, 2]
    assert ds.data["I"].shape == (6, 3, 2)
    assert ds.data["XY"].shape == (6, 3, 2, 2)
    assert ds.data["image"].shape == (6, 3, 2, 32, 48)
    assert ds.data["spectrum"].shape == (3, 2, 201)
    assert ds.data["T"].shape == (2,)
    assert not np.isnan(ds.data["I"]).any()
    assert ds.points == 36
    assert ds.pre["src.output"] == "ON" and "B" in ds.pre
    assert ds.pre["lockin.sensitivity"] == "1e-3"
    # post phase records *before* applying post constants (documented order)
    assert ds.post["Vg"] == pytest.approx(0.2)
    assert demo.scripts.state["post"] is True
    # awg rms follows the waveform mapping (zero at t=0, growing afterwards)
    rms = ds.data["awg_rms"][:, 0, 0]
    assert rms[0] < rms[-1]
    # record_after_inner: T recorded after the inner loops -> later timestamps
    assert ds.data["@time"][1] > ds.data["@time"][0]
    assert demo.channels.get("Vg") == pytest.approx(0.0)
    # sweep definition is embedded in the file
    assert ds.header["definition"]["name"] == "three-d"
    # hdf5 export happened
    import os
    time.sleep(0.5)
    h5 = item["path"].replace(".emj", ".h5")
    for _ in range(50):
        if os.path.exists(h5):
            break
        time.sleep(0.1)
    ds2 = DataSet.load(h5)
    np.testing.assert_allclose(ds2.data["I"], ds.data["I"])


def test_abort_runs_post_and_keeps_partial(demo):
    d = {"name": "long", "dimensions": [
        {"points": 200, "sweep": [{"channel": "Vg", "start": 0, "stop": 1}], "record": ["I"],
         "pre_delay": 0.01}],
         "post": {"constants": [{"channel": "Vg", "value": 0.0}]}}
    demo.queue_add({"kind": "sweep", "definition": d}, start=True)
    time.sleep(0.6)
    demo.abort()
    assert demo.queue.wait_idle(30)
    item = demo.queue_state()["history"][-1]
    assert item["status"] == "aborted"
    ds = DataSet.load(item["path"])
    assert 0 < ds.points < 200
    assert np.isnan(ds.data["I"]).sum() == 200 - ds.points
    assert demo.channels.get("Vg") == pytest.approx(0.0)   # post constants ran


def test_pause_resume(demo):
    d = {"dimensions": [{"points": 40, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}],
                         "record": ["I"], "pre_delay": 0.01}]}
    demo.queue_add({"kind": "sweep", "definition": d}, start=True)
    time.sleep(0.2)
    demo.pause()
    time.sleep(0.2)
    assert demo.queue.state == "paused"
    p1 = demo.current_run()["progress"]["done"]
    time.sleep(0.4)
    assert demo.current_run()["progress"]["done"] == p1
    demo.resume()
    assert demo.queue.wait_idle(30)
    assert demo.queue_state()["history"][-1]["status"] == "finished"


def test_read_error_policies(demo):
    from easymeasure.backend.channels import Alias
    demo.add_instrument("chaos", "sim_chaos", options={"retries": 0, "exception_rate": 0.0})
    demo.define_alias(name="bad", target="chaos.crash")
    d = {"dimensions": [{"points": 4, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}],
                         "record": ["I", "bad"]}]}
    item, ds = run(demo, d)
    assert item["status"] == "finished"
    assert ds.errors["bad"] == 4
    assert np.isnan(ds.data["bad"]).all() and not np.isnan(ds.data["I"]).any()   # all-NaN, present
    d["on_read_error"] = "abort"
    item, ds = run(demo, d)
    assert item["status"] == "error"
    assert "crash" in item["error"] or "SystemExit" in item["error"] or "raised" in item["error"]


def test_set_error_policy(demo):
    demo.add_instrument("chaos", "sim_chaos", options={"retries": 0, "flaky_rate": 1.0})
    demo.define_alias(name="fs", target="chaos.flaky_set", min=-1, max=1)
    d = {"dimensions": [{"points": 3, "sweep": [{"channel": "fs", "start": 0, "stop": 0.5}],
                         "record": ["I"]}]}
    item, _ = run(demo, d)
    assert item["status"] == "error"
    d["on_set_error"] = "skip"
    item, ds = run(demo, d)
    assert item["status"] == "finished" and ds.points == 3


def test_queue_scripts_and_abort_infinite_loop(demo):
    demo.queue_add({"kind": "script", "code": "set('Vg', 0.1)\nstate['x'] = get('Vg')", "name": "s1"})
    demo.queue_add({"kind": "script", "code": "while True:\n    pass", "name": "busy"})
    demo.queue_add({"kind": "script", "code": "raise ValueError('oops')", "name": "bad"})
    demo.queue_add({"kind": "script", "code": "state['after'] = 1", "name": "s4"})
    demo.queue_start()
    time.sleep(0.8)
    assert demo.queue_state()["current"]["name"] == "busy"
    demo.abort()
    assert demo.queue.wait_idle(20)
    hist = {h["name"]: h["status"] for h in demo.queue_state()["history"]}
    assert hist == {"s1": "finished", "busy": "aborted", "bad": "error", "s4": "finished"}
    assert demo.scripts.state["x"] == pytest.approx(0.1)
    with pytest.raises(ChannelError):
        demo.queue_add({"kind": "script", "code": "def ("})


def test_stop_on_error_and_queue_editing(demo):
    demo.queue_options(stop_on_error=True)
    a = demo.queue_add({"kind": "script", "code": "raise RuntimeError('x')", "name": "a"})
    b = demo.queue_add({"kind": "script", "code": "pass", "name": "b"})
    c = demo.queue_add({"kind": "script", "code": "pass", "name": "c"})
    demo.queue_move(c, 0)
    assert [i["name"] for i in demo.queue_state()["queued"]] == ["c", "a", "b"]
    demo.queue_remove(c)
    demo.queue_start()
    time.sleep(0.5)
    st = demo.queue_state()
    assert st["state"] == "idle" and [i["name"] for i in st["queued"]] == ["b"]
    demo.queue_clear()
    assert demo.queue_state()["queued"] == []


def test_script_api_and_nested_sweep(demo):
    code = """
log('hello from script')
set('Vg', 0.2)
assert abs(get('Vg') - 0.2) < 1e-9
ok = wait_until(lambda: get('T') > 0, timeout=2)
state['ok'] = ok
state['idn'] = instrument('src').read('idn')
ds = run_sweep({'name': 'nested', 'dimensions': [{'points': 3, 'sweep': [{'channel': 'Vb', 'start': 0, 'stop': 0.1}], 'record': ['I']}]})
state['nested'] = ds.status
state['nested_shape'] = ds.data['I'].shape
state['nested_path'] = ds.path
"""
    demo.queue_add({"kind": "script", "code": code}, start=True)
    assert demo.queue.wait_idle(30)
    assert demo.queue_state()["history"][-1]["status"] == "finished", demo.queue_state()["history"][-1]
    assert demo.scripts.state["ok"] is True
    assert "SimSource4" in demo.scripts.state["idn"]
    assert demo.scripts.state["nested"] == "finished"
    assert demo.scripts.state["nested_shape"] == (3,)       # the script sees the data
    assert demo.scripts.state["nested_path"].endswith("_nested.emj")


def test_point_hooks_order_and_context(demo):
    log = "state.setdefault('log', []).append((%r, k, i, round(get('Vg'), 6)))"
    hooks = {n: {"enabled": True, "code": log % n}
             for n in ("pre_sweep", "post_sweep", "pre_record", "post_record")}
    hooks["post_record"]["code"] += "\nstate.setdefault('rec', []).append(float(values['I']))"
    d = {"name": "hooks", "dimensions": [
        {"points": 3, "sweep": [{"channel": "Vg", "start": 0.1, "stop": 0.3}], "record": ["I"],
         "hooks": hooks},
        {"points": 2, "sweep": [{"channel": "Vb", "start": 0, "stop": 0.1}], "record": [],
         "hooks": {"pre_sweep": {"enabled": False, "code": "state['never'] = 1"},
                   "post_sweep": "state.setdefault('outer', []).append((index, setpoints['Vb']))"}}]}
    demo.scripts.state.clear()
    demo.write_channel("Vg", 0.0, ramp=False)
    item, ds = run(demo, d)
    assert item["status"] == "finished", item
    st = demo.scripts.state
    seq = st["log"]
    assert len(seq) == 4 * 6
    first = seq[:4]
    # pre_sweep still sees the old value; afterwards the new setpoint is in place
    assert [s[0] for s in first] == ["pre_sweep", "post_sweep", "pre_record", "post_record"]
    assert first[0][3] == pytest.approx(0.0) and first[1][3] == pytest.approx(0.1)
    assert [s[2] for s in seq[::4]] == [0, 1, 2, 0, 1, 2]
    # post_record sees exactly what was stored
    np.testing.assert_allclose(st["rec"], ds.data["I"].T.ravel())
    assert "never" not in st                                   # disabled hook kept, not run
    assert st["outer"] == [((0,), 0.0), ((1,), 0.1)]           # string form = enabled code
    nd = normalize_definition(d)
    assert nd["dimensions"][1]["hooks"]["pre_sweep"] == {"enabled": False,
                                                         "code": "state['never'] = 1"}
    assert nd["dimensions"][0]["hooks"]["pre_record"]["enabled"] is True


def test_point_hooks_with_record_after_inner_and_actions(demo):
    d = {"name": "hooks2", "dimensions": [
        {"points": 2, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}], "record": ["I"],
         "hooks": {"pre_record": {"enabled": True, "code": "state.setdefault('s', []).append('in%d' % i)"}}},
        {"points": 2, "sweep": [{"channel": "Vb", "start": 0, "stop": 0.1}], "record": ["T"],
         "record_after_inner": True,
         "hooks": {"post_sweep": {"enabled": True, "code": "state.setdefault('s', []).append('set%d' % i)"},
                   "pre_record": {"enabled": True, "code": "action('dmm.trigger')\nstate['s'].append('rec%d' % i)"}}}]}
    demo.scripts.state.clear()
    item, _ = run(demo, d)
    assert item["status"] == "finished", item
    assert demo.scripts.state["s"] == ["set0", "in0", "in1", "rec0", "set1", "in0", "in1", "rec1"]


def test_point_hook_errors(demo):
    base = {"name": "bad", "dimensions": [
        {"points": 3, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}], "record": ["I"]}]}
    bad = json_copy(base)
    bad["dimensions"][0]["hooks"] = {"pre_record": {"enabled": True, "code": "if:"}}
    with pytest.raises(SweepDefinitionError, match="pre_record hook: syntax error"):
        demo.validate_sweep(bad)
    # a disabled hook is not even compiled
    bad["dimensions"][0]["hooks"]["pre_record"]["enabled"] = False
    demo.validate_sweep(bad)
    for wrong in ({"pre_measure": "x"}, {"pre_record": 5}, "code", {"post_sweep": {"code": 1}}):
        b = json_copy(base)
        b["dimensions"][0]["hooks"] = wrong
        with pytest.raises(SweepDefinitionError):
            normalize_definition(b)
    # a failing hook stops the sweep with its error, post phase still runs
    fail = json_copy(base)
    fail["dimensions"][0]["hooks"] = {"post_sweep": {"enabled": True,
                                                     "code": "if i == 1: raise ValueError('boom')"}}
    fail["post"] = {"constants": [{"channel": "Vg", "value": 0.0}]}
    item, ds = run(demo, fail)
    assert item["status"] == "error" and "boom" in item.get("error", ""), item
    assert ds.points == 1
    assert demo.channels.get("Vg") == pytest.approx(0.0)


def json_copy(d):
    import json
    return json.loads(json.dumps(d))


def test_queue_start_when_empty_stays_idle(demo):
    q = demo.queue
    assert demo.queue_start() is False
    st = demo.queue_state()
    assert st["state"] == "idle" and not st["processing"]
    assert st["can"] == {"start": False, "stop": False, "pause": False, "resume": False,
                         "abort": False, "abort_all": False}
    # an item added afterwards waits for an explicit start
    d = {"name": "wait", "dimensions": [
        {"points": 40, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}], "record": ["I"],
         "pre_delay": 0.02}]}
    demo.queue_add({"kind": "sweep", "definition": d})
    time.sleep(0.3)
    st = demo.queue_state()
    assert st["state"] == "idle" and st["current"] is None and st["can"]["start"]
    assert demo.queue_start() is True
    t0 = time.time()
    while demo.queue_state()["current"] is None and time.time() - t0 < 5:
        time.sleep(0.02)
    st = demo.queue_state()
    assert st["state"] == "running"
    assert st["can"]["pause"] and st["can"]["abort"] and not st["can"]["start"]
    assert not st["can"]["resume"]
    demo.pause()
    t0 = time.time()
    while demo.queue_state()["state"] != "paused" and time.time() - t0 < 5:
        time.sleep(0.02)
    st = demo.queue_state()
    assert st["can"]["resume"] and not st["can"]["pause"] and st["can"]["abort"]
    demo.resume()
    assert q.wait_idle(30)
    st = demo.queue_state()
    # queue ran empty: back to idle by itself
    assert st["state"] == "idle" and not st["processing"] and not any(st["can"].values())
    # the next item does not start on its own
    demo.queue_add({"kind": "sweep", "definition": dict(d, name="later")})
    time.sleep(0.3)
    assert demo.queue_state()["current"] is None


def test_instrument_last_values(demo):
    inst = {i["name"]: i for i in demo.list_instruments()}
    ch = {c["name"]: c for c in inst["src"]["channels"]}
    assert all(c["last"] is None for c in ch.values())        # nothing read on start
    demo.write_channel("Vg", 0.123, ramp=False)               # alias Vg -> src.V1
    demo.read_channel("lockin.sensitivity")
    demo.read_channel("spectrum")
    inst = {i["name"]: i for i in demo.list_instruments()}
    v1 = {c["name"]: c for c in inst["src"]["channels"]}["V1"]
    assert v1["last"] == pytest.approx(0.123) and v1["last_t"] > 0
    lk = {c["name"]: c for c in inst["lockin"]["channels"]}
    assert lk["sensitivity"]["last"] == "1e-3"
    spec_inst = next(i for i in inst.values() if i["driver"] == "sim_spectrum")
    tr = next(c for c in spec_inst["channels"] if c["last"] is not None)
    assert isinstance(tr["last"], dict) and "shape" in tr["last"]   # arrays: shape only


@pytest.mark.parametrize("hook", ["pre_sweep", "post_sweep", "pre_record", "post_record"])
def test_skip_point_from_every_action(demo, hook):
    """skip_point() at i == 2 ends that point: nothing after the call happens, its data
    are NaN, and the sweep continues with the next point."""
    trace = "state.setdefault('t', []).append((%r, i))"
    hooks = {h: {"enabled": True, "code": trace % h} for h in
             ("pre_sweep", "post_sweep", "pre_record", "post_record")}
    hooks[hook]["code"] += "\nif i == 2:\n    try:\n        skip_point()\n" \
                           "    except Exception:\n        state['swallowed'] = True"
    d = {"name": "skip_" + hook, "dimensions": [
        {"points": 5, "sweep": [{"channel": "Vg", "start": 0.0, "stop": 0.4}],
         "record": ["I", "spectrum"], "post_delay": 0.001, "hooks": hooks}]}
    demo.scripts.state.clear()
    demo.write_channel("Vg", -0.5, ramp=False)
    item, ds = run(demo, d)
    assert item["status"] == "finished", item
    st = demo.scripts.state
    assert "swallowed" not in st               # `except Exception` cannot swallow the skip
    order = ["pre_sweep", "post_sweep", "pre_record", "post_record"]
    at2 = [h for h, i in st["t"] if i == 2]
    assert at2 == order[:order.index(hook) + 1]           # the rest of the point did not run
    assert [i for h, i in st["t"] if h == "pre_sweep"] == [0, 1, 2, 3, 4]   # sweep went on
    # this point's data are NaN, the others are real
    assert np.isnan(ds.data["I"][2]) and np.isnan(ds.data["spectrum"][2]).all()
    assert not np.isnan(np.delete(ds.data["I"], 2)).any()
    assert ds.points == 5 and ds.skipped == 1 and ds.footer["skipped_points"] == 1


def test_skip_outer_point_skips_inner_dims(demo):
    d = {"name": "skip_outer", "dimensions": [
        {"points": 3, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.2}], "record": ["I"],
         "hooks": {"pre_record": {"enabled": True,
                                  "code": "state['inner'] = state.get('inner', 0) + 1"}}},
        {"points": 4, "sweep": [{"channel": "Vb", "start": 0, "stop": 0.3}], "record": ["T"],
         "hooks": {"post_sweep": {"enabled": True, "code": "if i in (1, 3): skip_point()"}}}]}
    demo.scripts.state.clear()
    item, ds = run(demo, d)
    assert item["status"] == "finished", item
    assert demo.scripts.state["inner"] == 3 * 2          # inner loop ran for i = 0, 2 only
    assert np.isnan(ds.data["I"][:, [1, 3]]).all() and not np.isnan(ds.data["I"][:, [0, 2]]).any()
    assert np.isnan(ds.data["T"][[1, 3]]).all() and not np.isnan(ds.data["T"][[0, 2]]).any()
    assert ds.points == ds.total_points == 12             # progress complete
    assert ds.skipped == 2
    # the file reads back the same (journal and HDF5)
    from easymeasure.backend.storage import DataSet as DS
    again = DS.load(item["path"])
    np.testing.assert_array_equal(np.isnan(again.data["I"]), np.isnan(ds.data["I"]))
    assert again.points == 12 and again.skipped == 2
    # a live client applying the events gets the same picture
    live = DS(ds.header)
    for f in [(0, (0, 0), {"I": 1.0}), (0, (1, 0), {"I": 2.0})]:
        live.apply_point(f[0], f[1], f[2])
    live.apply_point(1, (1,), {}, {}, skip=True)
    assert live.filled[0][:, 1].all() and live.points == 2 + 3


def test_skip_point_outside_point_actions(demo):
    with pytest.raises(Exception, match="only works in a sweep's per-point actions"):
        demo.scripts.run("skip_point()", "plain")
    d = {"name": "badskip", "dimensions": [{"points": 2, "record": ["I"]}],
         "pre": {"hook": "skip_point()"}}
    item, _ = run(demo, d)
    assert item["status"] == "error" and "per-point actions" in item.get("error", "")


def test_skip_point_hdf5_export(demo):
    d = {"name": "skip_h5", "dimensions": [
        {"points": 4, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.3}], "record": ["I"],
         "hooks": {"pre_record": "if i == 1: skip_point()"}}]}
    item, ds = run(demo, d)
    import os
    h5 = item["path"].replace(".emj", ".h5")
    for _ in range(100):
        if os.path.exists(h5):
            break
        time.sleep(0.1)
    time.sleep(0.3)
    d5 = DataSet.load(h5)
    assert np.isnan(d5.data["I"][1]) and not np.isnan(d5.data["I"][[0, 2, 3]]).any()
    assert d5.points == 4 and d5.skipped == 1


def test_skip_line_is_break(demo):
    """skip_line() in the inner dimension: rest of that line skipped, outer goes on."""
    d = {"name": "skip_line", "dimensions": [
        {"points": 5, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.4}], "record": ["I"],
         "hooks": {"pre_record": {"enabled": True, "code":
                                  "state.setdefault('seen', []).append(index)\n"
                                  "if i == 2 and index[1] == 1: skip_line()"}}},
        {"points": 3, "sweep": [{"channel": "Vb", "start": 0, "stop": 0.2}], "record": ["T"],
         "record_after_inner": True,
         "hooks": {"pre_record": {"enabled": True, "code": "state.setdefault('outer', []).append(i)"}}}]}
    demo.scripts.state.clear()
    item, ds = run(demo, d)
    assert item["status"] == "finished", item
    seen = demo.scripts.state["seen"]
    assert [s for s in seen if s[1] == 1] == [(0, 1), (1, 1), (2, 1)]   # break at i = 2
    assert len([s for s in seen if s[1] == 2]) == 5                      # next line complete
    assert demo.scripts.state["outer"] == [0, 1, 2]       # outer point still recorded
    I = ds.data["I"]
    assert np.isnan(I[2:, 1]).all() and not np.isnan(I[:2, 1]).any()
    assert not np.isnan(I[:, [0, 2]]).any() and not np.isnan(ds.data["T"]).any()
    assert ds.points == 15 and ds.skipped == 3 and ds.footer["skipped_points"] == 3

    # in the outermost dimension the sweep simply ends there
    d2 = {"name": "skip_line_outer", "dimensions": [
        {"points": 2, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}], "record": ["I"]},
        {"points": 4, "sweep": [{"channel": "Vb", "start": 0, "stop": 0.3}], "record": [],
         "hooks": {"pre_sweep": "if i == 1: skip_line()"}}]}
    item, ds = run(demo, d2)
    assert item["status"] == "finished"
    assert not np.isnan(ds.data["I"][:, 0]).any() and np.isnan(ds.data["I"][:, 1:]).all()
    assert ds.points == ds.total_points == 8


def test_end_sweep_is_exit(demo):
    d = {"name": "end", "dimensions": [
        {"points": 4, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.3}], "record": ["I"],
         "hooks": {"post_record": {"enabled": True, "code":
                                   "if index == (1, 1):\n    try:\n        end_sweep()\n"
                                   "    except Exception:\n        state['swallowed'] = 1"}}},
        {"points": 3, "sweep": [{"channel": "Vb", "start": 0, "stop": 0.2}], "record": ["T"]}],
         "post": {"constants": [{"channel": "Vg", "value": 0.0}], "hook": "state['post'] = 1"}}
    demo.scripts.state.clear()
    demo.queue_add({"kind": "sweep", "definition": d}, start=True)
    demo.queue_add({"kind": "script", "code": "state['next'] = 1", "name": "after"})
    assert demo.queue.wait_idle(30)
    hist = demo.queue_state()["history"]
    item = next(h for h in hist if h.get("name") == "end")
    assert item["status"] == "terminated" and not item.get("error"), item
    st = demo.scripts.state
    assert "swallowed" not in st and st["post"] == 1      # post phase ran
    assert st["next"] == 1                                # the queue went on
    ds = DataSet.load(item["path"])
    assert ds.status == "terminated"
    assert ds.footer["terminated_at"] == {"dimension": 1, "action": "post_record", "index": [1, 1]}
    assert any(e.get("event") == "terminated" for e in ds.events)
    logs = [r["msg"] for r in demo.recent_logs(300)]
    assert any("terminated by end_sweep() in the post-record action of dimension 1 at point "
               "[1, 1]: 6 of 12 points measured" in m for m in logs), logs[-5:]
    # saved like any finished run: the HDF5 copy is written too
    import os
    h5 = item["path"].replace(".emj", ".h5")
    for _ in range(100):
        if os.path.exists(h5):
            break
        time.sleep(0.1)
    time.sleep(0.3)
    d5 = DataSet.load(h5)
    assert d5.status == "terminated" and d5.points == 6
    I = ds.data["I"]
    # exit after recording keeps the point just measured (index (1, 1)); nothing after it
    assert not np.isnan(I[:, 0]).any() and not np.isnan(I[:2, 1]).any()
    assert np.isnan(I[2:, 1]).all() and np.isnan(I[:, 2]).all()
    assert ds.points == 6
    assert demo.channels.get("Vg") == pytest.approx(0.0)
    # the flow calls are refused outside the per-point actions
    for fn in ("skip_line", "end_sweep"):
        with pytest.raises(Exception, match="per-point actions"):
            demo.scripts.run(fn + "()", "plain")
