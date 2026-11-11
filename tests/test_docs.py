"""The documentation's generated pages cover every driver, command and setting, and
(when mkdocs is installed) the site builds without warnings."""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("gen_reference", ROOT / "docs" / "gen_reference.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.PAGES.clear()
    drivers = mod.generate()
    return mod, drivers


def test_every_driver_has_a_page(gen):
    mod, drivers = gen
    from easymeasure.backend.registry import DriverRegistry
    reg = DriverRegistry()
    reg.scan()
    names = set(reg.describe()["drivers"])
    assert names and names == set(drivers)
    inst = mod.PAGES["instruments.md"]
    for n in names:
        page = mod.PAGES["reference/drivers/%s.md" % n]
        assert "## Channels" in page and "## Options" in page
        assert "(reference/drivers/%s.md)" % n in inst, n
    # live channel lists from the emulators (mode-dependent drivers included)
    assert drivers["keithley2400"]["live"] == "SIM::keithley2400"
    assert "`voltAll`" in mod.PAGES["reference/drivers/cygnus16430.md"]
    assert "`Icompliance`" in mod.PAGES["reference/drivers/keithley2400.md"]
    # every hardware driver has hand-written notes
    for n in mod.HARDWARE_ORDER:
        assert (ROOT / "docs" / "_driver_notes" / ("%s.md" % n)).exists(), n


def test_every_command_is_documented(gen):
    mod, _ = gen
    from easymeasure.backend.service import Backend
    page = mod.PAGES["reference/commands.md"]
    grouped = {c for _, g in mod.COMMAND_GROUPS for c in g}
    for c in Backend.COMMANDS:
        assert "### `%s` {#%s}" % (c, c) in page, c
        assert c in grouped, "add %s to a group in docs/gen_reference.py" % c
        f = getattr(Backend, c)
        assert (f.__doc__ or "").strip(), "Backend.%s has no docstring" % c
    assert "_(no description)_" not in page


def test_settings_and_expressions(gen):
    mod, _ = gen
    from easymeasure.backend.service import DEFAULT_SETTINGS
    from easymeasure.common import exprs
    from easymeasure.remote.config import DEFAULTS
    page = mod.PAGES["reference/settings.md"]
    for k in list(DEFAULT_SETTINGS) + list(DEFAULTS):
        assert "| `%s` |" % k in page, k
    for line in page.splitlines():           # every setting explained in the source
        if line.startswith("| `") and line.count("|") == 4:
            assert line.rstrip("| ").split("|")[3].strip(), line
    ex = mod.PAGES["reference/expressions.md"]
    for f in ("piecewise", "interp", "where", "sqrt"):
        assert "`%s`" % f in ex and f in exprs._NP_FUNCS


def test_api_pages_cover_every_module(gen):
    mod, _ = gen
    pages = {k for k in mod.PAGES if k.startswith("reference/api/")}
    for path in (ROOT / "easymeasure").rglob("*.py"):
        if "__pycache__" in path.parts or path.name == "__main__.py":
            continue
        rel = path.relative_to(ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        assert "reference/api/%s.md" % "/".join(parts) in pages, path


@pytest.mark.skipif(shutil.which("mkdocs") is None, reason="mkdocs not installed")
def test_site_builds_strictly(tmp_path):
    r = subprocess.run([sys.executable, "-m", "mkdocs", "build", "--strict", "-q",
                        "-d", str(tmp_path / "site")], cwd=str(ROOT), capture_output=True,
                       text=True, timeout=600,
                       env=dict(__import__("os").environ, DISABLE_MKDOCS_2_WARNING="true",
                                QT_QPA_PLATFORM="offscreen"))
    assert r.returncode == 0, r.stderr[-3000:]
    assert (tmp_path / "site" / "instruments" / "index.html").exists()
    assert (tmp_path / "site" / "reference" / "drivers" / "ni63xx" / "index.html").exists()
