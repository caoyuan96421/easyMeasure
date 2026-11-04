import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EXAMPLES = ROOT / "examples"

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "emhome"
    home.mkdir()
    monkeypatch.setenv("EASYMEASURE_HOME", str(home))
    from easymeasure.backend.drivers import _simbase, simbus, simworld
    simbus.reset_buses()
    _simbase.reset_devices()
    simworld.WORLD.reset()
    yield home


@pytest.fixture
def backend(tmp_path):
    from easymeasure.backend.service import Backend
    be = Backend(home=tmp_path / "emhome", log_to_console=False,
                 settings={"export_hdf5": True, "fsync_interval": 0.05, "autosave_setup": True})
    yield be
    be.close()


@pytest.fixture
def demo(backend):
    problems = backend.load_setup(path=str(EXAMPLES / "demo_setup.json"))
    assert problems == []
    backend.monitor.clear()
    return backend


def load_example(name):
    return json.loads((EXAMPLES / name).read_text())


def pytest_unconfigure(config):
    """The robustness tests deliberately abandon driver threads that hang forever.
    Interpreter teardown around such threads can crash inside native extensions
    (Qt/numpy/h5py) after all tests have finished, so exit directly with the
    session's status (the summary has already been printed)."""
    import sys
    status = getattr(config, "_em_exitstatus", None)
    if status is not None:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(int(status))


def pytest_sessionfinish(session, exitstatus):
    session.config._em_exitstatus = exitstatus
