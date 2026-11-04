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
        {"points": 2, "sweep": [{"channel": "detuning", "start": 0, "stop": 0.2}], "record": ["T", "@time"],
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
    assert "bad" not in ds.data and not np.isnan(ds.data["I"]).any()
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
status = run_sweep({'name': 'nested', 'dimensions': [{'points': 3, 'sweep': [{'channel': 'Vb', 'start': 0, 'stop': 0.1}], 'record': ['I']}]})
state['nested'] = status
"""
    demo.queue_add({"kind": "script", "code": code}, start=True)
    assert demo.queue.wait_idle(30)
    assert demo.queue_state()["history"][-1]["status"] == "finished", demo.queue_state()["history"][-1]
    assert demo.scripts.state["ok"] is True
    assert "SimSource4" in demo.scripts.state["idn"]
    assert demo.scripts.state["nested"] == "finished"
