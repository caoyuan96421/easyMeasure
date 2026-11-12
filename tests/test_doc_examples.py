"""The examples the documentation shows run as described: the graphene and VNA
setups load, their sweeps validate, the scripts of the scripting chapter
(examples/scripts/*.py, included verbatim in the pages) finish, and the simulated
physics shows what the tutorials say it shows."""

import json
from pathlib import Path

import numpy as np
import pytest

from easymeasure.backend.storage import DataSet

EX = Path(__file__).resolve().parents[1] / "examples"
SCRIPTS = sorted((EX / "scripts").glob("*.py"))


@pytest.fixture
def graphene(backend):
    assert backend.load_setup(path=str(EX / "graphene_setup.json")) == []
    backend.monitor.clear()
    backend.write_channel(name="magnet.rate", value=60.0)       # 1 T/s: quicker tests
    return backend


def _run(be, item, timeout=300):
    be.queue_add(item, start=True)
    assert be.queue.wait_idle(timeout)
    return be.queue_state()["history"][-1]


def test_setups_and_sweeps_validate(graphene, backend):
    for f in ("graphene_dirac", "graphene_fan", "graphene_nD", "graphene_leakage",
              "graphene_dense_dirac", "graphene_circle", "graphene_hysteresis",
              "graphene_time_trace"):
        r = graphene.validate_sweep(definition=json.loads((EX / (f + ".json")).read_text()))
        assert r["ok"], f
    assert graphene.validate_sweep(definition=json.loads(
        (EX / "graphene_fan.json").read_text()))["shape"] == [101, 61]
    backend.channels.clear()
    assert backend.load_setup(path=str(EX / "vna_setup.json")) == []
    for f in ("vna_power", "vna_power_field"):
        assert backend.validate_sweep(definition=json.loads((EX / (f + ".json")).read_text()))["ok"]


def test_quick_start_physics(graphene):
    """The Dirac peak sits at about +0.3 V and the gate does not leak below 8 V."""
    d = json.loads((EX / "graphene_dirac.json").read_text())
    h = _run(graphene, {"kind": "sweep", "definition": d})
    ds = DataSet.load(h["path"])
    assert ds.status == "finished"
    v = ds.setpoints[0]["Vbg"]
    assert abs(v[np.nanargmax(ds.data["Rxx"])] - 0.3) < 0.25
    assert 3e3 < np.nanmax(ds.data["Rxx"]) < 6e3
    assert np.nanmax(np.abs(ds.data["Ileak"])) < 1e-10
    # load_sweep() reads a definition back from the editor's JSON and from the data file
    from easymeasure.backend.scripting import load_sweep
    assert load_sweep(EX / "graphene_dirac.json") == d
    assert load_sweep(h["path"])["dimensions"][0]["points"] == 161


def test_custom_sweep_paths(graphene):
    """The custom-sweeps page: the dense mapping is dense at the peak, the circle
    goes round, and the triangle comes back."""
    from easymeasure.backend.sweep import compute_setpoints, normalize_definition
    sp = lambda f: compute_setpoints(normalize_definition(json.loads((EX / (f + ".json")).read_text())))[0]
    v = sp("graphene_dense_dirac")["Vbg"]
    assert np.diff(v)[80] < np.diff(v)[0] / 8 and abs(v[80] - 0.3) < 1e-9
    c = sp("graphene_circle")
    np.testing.assert_allclose(np.hypot(c["nD.n"], c["nD.D"] / 0.2), 1.0)
    h = sp("graphene_hysteresis")["Vbg"]
    assert h[0] == pytest.approx(0) and h.max() == pytest.approx(5) and h.min() == pytest.approx(-5)
    assert h[-1] == pytest.approx(0, abs=1e-9)


def test_threshold_sweep_stops_itself(graphene):
    d = json.loads((EX / "graphene_leakage.json").read_text())
    h = _run(graphene, {"kind": "sweep", "definition": d})
    ds = DataSet.load(h["path"])
    assert ds.status == "terminated"
    last = np.flatnonzero(np.isfinite(ds.data["Ileak"]))[-1]
    assert 9.0 < ds.setpoints[0]["Vbg"][last] < 10.0
    assert abs(ds.data["Ileak"][last]) > 1e-9
    assert graphene.read_channel(name="Vbg") == pytest.approx(0.0)   # post phase ran


def test_gap_opens_with_displacement_field(graphene):
    graphene.set_many(values={"nD.n": 0.0, "nD.D": 0.0})
    r0 = graphene.read_channel(name="Rxx")
    graphene.write_channel(name="nD.D", value=0.3)
    r3 = graphene.read_channel(name="Rxx")
    assert graphene.read_channel(name="Vbg") == pytest.approx((0.15 + 0.3 / 0.0452) / 1.2)
    assert r3 > 10 * r0
    graphene.write_channel(name="nD.n", value=2.0)
    assert graphene.read_channel(name="Rxx") < r3 / 10


def test_rxx_from_two_lockins(graphene):
    """Rxx = Vxx / I is right where the sample is insulating; Vxx / 10 nA is not,
    because the current through the 1 MΩ series resistor drops."""
    from easymeasure.drivers.simworld import WORLD
    graphene.set_many(values={"nD.n": 0.0, "nD.D": 0.0})
    i0 = graphene.read_channel(name="I")
    assert i0 == pytest.approx(0.01 / (1e6 + 3 * 4.6e3 + 2e3), rel=0.03)
    graphene.set_many(values={"nD.n": 0.0, "nD.D": 0.3})
    vbg, vtg = graphene.read_channel(name="Vbg"), graphene.read_channel(name="Vtg")
    true = np.mean([WORLD.graphene(vbg, vtg, 0.0)["Rxx"] for _ in range(20)])
    rxx = np.mean([graphene.read_channel(name="Rxx") for _ in range(5)])
    naive = np.mean([graphene.read_channel(name="Vxx") for _ in range(5)]) * 1e8
    assert rxx == pytest.approx(true, rel=0.03)
    assert naive < 0.85 * true
    assert graphene.read_channel(name="I") < 0.8 * i0


def test_nd_map_skips_the_corners(graphene):
    """The tutorial's n-D map: combinations needing a gate beyond ±10 V are skipped."""
    d = json.loads((EX / "graphene_nD.json").read_text())
    r = graphene.validate_sweep(definition=d)
    n = np.linspace(-4, 4, 101)[:, None]
    dd = np.linspace(-0.45, 0.45, 31)[None, :]
    vbg = (n + 0.15 + dd / 0.0452) / 1.2
    vtg = (n + 0.15 - dd / 0.0452) / 1.8
    expected = int(((np.abs(vbg) > 10) | (np.abs(vtg) > 10)).sum())
    assert expected > 0
    assert "composite nD: %d of 3131 points out of range, will be skipped" % expected in r["warnings"]


def test_landau_minima_and_unpolled_magnet(graphene):
    """Rxx dips at filling factor 4 in a field, and the lock-in sees the field even
    when nobody reads the magnet (it ramps by itself)."""
    import time
    graphene.write_channel(name="B", value=4.0)
    t0 = time.time()
    while graphene.read_channel(name="magnet.status") != "HOLDING" and time.time() - t0 < 10:
        time.sleep(0.05)
    n4 = 4 * 4.0 / 41.36                       # ν = 4
    vbg = lambda n: (n + 0.15) / 0.6           # top gate at 0
    graphene.write_channel(name="Vbg", value=vbg(n4), ramp=False)
    r_min = graphene.read_channel(name="Rxx")
    graphene.write_channel(name="Vbg", value=vbg(n4 * 1.5), ramp=False)
    assert graphene.read_channel(name="Rxx") > 2 * r_min
    graphene.write_channel(name="B", value=0.0)
    time.sleep(1.0)                            # nobody reads the magnet meanwhile
    from easymeasure.drivers.simworld import WORLD
    assert WORLD.get("magnet.field") == pytest.approx(3.0, abs=0.3)   # 1 T/s from 4 T


@pytest.mark.parametrize("script", SCRIPTS, ids=[p.stem for p in SCRIPTS])
def test_scripting_examples_run(graphene, script):
    h = _run(graphene, {"kind": "script", "name": script.stem, "code": script.read_text()},
             timeout=600)
    assert h["status"] == "finished", h
    if script.stem == "find_dirac_then_zoom":
        assert abs(graphene.scripts.state["dirac_point"] - 0.3) < 0.3


def test_vna_traces_have_a_frequency_axis(backend):
    assert backend.load_setup(path=str(EX / "vna_setup.json")) == []
    d = json.loads((EX / "vna_power.json").read_text())
    d["dimensions"][0]["points"] = 8
    h = _run(backend, {"kind": "sweep", "definition": d})
    ds = DataSet.load(h["path"])
    assert ds.data["S11"].shape == (8, 401)
    f = ds.value_axes["S11"][0]["coords"]
    assert f.shape == (401,) and f[0] == pytest.approx(5.995e9) and f[-1] == pytest.approx(6.005e9)
    assert ds.value_axes["S11"][0]["unit"] == "Hz"
    # the resonance dip lies at 6 GHz at moderate power
    k = int(np.argmin(ds.data["S11"][4]))
    assert abs(f[k] - 6.0e9) < 0.5e6
    # the axis survives the HDF5 copy and is used by the plots
    import time
    from easymeasure.common import ndview
    h5 = Path(h["path"]).with_suffix(".h5")
    t0 = time.time()
    while not h5.exists() and time.time() - t0 < 10:
        time.sleep(0.05)
    for d2 in (ds, DataSet.load(h5)):
        axes = ndview.axes_for(d2, "S11")
        assert [a["label"] for a in axes] == ["P (dBm)", "frequencies (Hz)"]
        np.testing.assert_allclose(axes[1]["coords"], f)
