"""SDK-free SHG tests; run with --confcutdir=tests/shg for isolated teardown."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from importlib.util import find_spec

# Core-only installations do not need SHG's optional scientific dependencies.
collect_ignore_glob = ["test_*.py"] if any(find_spec(name) is None for name in ("scipy", "PIL")) else []

@pytest.fixture(autouse=True)
def isolated_shg_state(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYMEASURE_HOME", str(tmp_path/"emhome"))
    from easymeasure.drivers import _simbase, sim_magnet, simbus, simworld
    simbus.reset_buses()
    _simbase.reset_devices()
    simworld.WORLD.reset()
    sim_magnet._STATE.clear()
    import shg_addon.spot_tracking as tracking
    monkeypatch.setattr(tracking, "HOME", tmp_path/"tracking-state")
    monkeypatch.setattr(tracking, "DATA", tmp_path/"tracking-data")


@pytest.fixture(autouse=True)
def isolated_retardation_output(request, monkeypatch, tmp_path):
    if request.node.name == "test_retardation_workflow":
        import shg_addon.configuration as configuration
        monkeypatch.setattr(configuration, "ROOT", tmp_path)
