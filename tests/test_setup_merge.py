"""Merging part of a setup file into the current setup (frontend/setup_merge.py).

The planner checks everything offline with the backend's own rules; these tests
compare what it says with what the backend then really does."""

import copy
import json
import random

import pytest

from easymeasure.frontend.setup_merge import (ERROR, NEW, SAME, UNSELECTED, WARNING,
                                               MergeFileError, SetupMerge, rename_code,
                                               rename_formula)

from conftest import EXAMPLES


def graphene():
    return json.loads((EXAMPLES / "graphene_setup.json").read_text())


def plan(be, src):
    return SetupMerge.from_state(src, be.get_state(), be.list_drivers()["drivers"])


def apply(be, m):
    """Send the merge's commands to the backend; every one must be accepted."""
    for cmd, args in m.commands():
        getattr(be, cmd)(**copy.deepcopy(args))


def statuses(m, kind):
    return {it.name: it.status for it in m.items[kind]}


def msgs(m, kind, name):
    return " | ".join(m.item(kind, name).messages)


# --------------------------------------------------------------------------- basics
def test_items_and_not_a_setup(demo):
    m = plan(demo, graphene())
    assert [it.name for it in m.items["instruments"]] == ["bg", "ilockin", "lockin", "magnet", "tg"]
    assert [it.name for it in m.items["parameters"]] == ["Cb", "Ct", "n0", "kD"]
    assert [it.name for it in m.items["composites"]] == ["nD", "Rxx"]
    assert m.validate() and not m.can_merge                 # nothing selected yet
    assert all(it.status == UNSELECTED for its in m.items.values() for it in its)
    with pytest.raises(MergeFileError):
        SetupMerge({"instruments": []}, {})
    with pytest.raises(MergeFileError):
        SetupMerge.from_file(EXAMPLES / "graphene_nD.json", demo.get_state())   # a sweep
    bad = graphene()
    bad["aliases"].append(dict(bad["aliases"][0]))         # twice
    bad["aliases"].append({"target": "x.y"})               # no name
    m = plan(demo, bad)
    assert len(m.file_problems) == 2 and len(m.items["aliases"]) == 6


def test_identical_items_are_skipped_and_conflicts_reported(demo):
    m = plan(demo, graphene())
    for it in m.items["instruments"]:
        it.selected = True
    assert not m.validate()
    st = statuses(m, "instruments")
    assert st["magnet"] == SAME                       # sim_magnet at NONE in both
    assert st["lockin"] == ERROR and "already the instrument lockin" in msgs(m, "instruments",
                                                                             "lockin")
    assert st["bg"] == st["tg"] == st["ilockin"] == NEW
    # a new name resolves it
    m.rename("instruments", "lockin", "lockin2")
    assert m.validate() and m.can_merge
    assert [c for c, _ in m.commands()] == ["add_instrument"] * 4    # magnet is not added
    apply(demo, m)
    assert {"bg", "tg", "ilockin", "lockin2", "magnet"} <= set(demo.instruments.names())


def test_renamed_instrument_rewrites_alias_targets(demo):
    m = plan(demo, graphene())
    m.select("instruments", "lockin")
    m.rename("instruments", "lockin", "li2")
    m.select("aliases", "Vxx")
    assert m.validate(), m.errors
    assert m.item("aliases", "Vxx").merged["target"] == "li2.X"
    apply(demo, m)
    assert demo.channels.info("Vxx")["target"] == "li2.X"
    # without the rename the alias would use the existing lockin: fine too
    m = plan(demo, graphene())
    m.select("aliases", "Vxx")
    m.rename("aliases", "Vxx", "Vxx_old")
    assert m.validate()
    assert m.item("aliases", "Vxx").merged["target"] == "lockin.X"


def test_missing_dependencies_and_select_with_needs(demo):
    m = plan(demo, graphene())
    m.select("aliases", "Vbg")                       # bg is not in the demo setup
    assert not m.validate()
    assert "needs the instrument bg" in msgs(m, "aliases", "Vbg")
    assert m.needed_by("instruments", "bg") == ["alias Vbg"]
    m.select("aliases", "Vbg", False)
    added = m.select_with_needs("composites", "nD")
    assert set(added) == {("aliases", "Vbg"), ("aliases", "Vtg"), ("instruments", "bg"),
                          ("instruments", "tg"), ("parameters", "Cb"), ("parameters", "Ct"),
                          ("parameters", "n0"), ("parameters", "kD")}
    assert m.validate(), m.errors
    cmds = [(c, a["name"]) for c, a in m.commands()]
    # instruments, parameters, aliases, composites: each before what uses it
    assert cmds.index(("add_instrument", "bg")) < cmds.index(("define_alias", "Vbg")) < \
        cmds.index(("define_composite", "nD"))
    assert cmds.index(("define_parameter", "Cb")) < cmds.index(("define_composite", "nD"))
    apply(demo, m)
    demo.set_many({"nD.n": 0.5, "nD.D": 0.1})
    assert demo.channels.get("Vbg") == pytest.approx((0.5 + 0.15 + 0.1 / 0.0452) / 1.2)
    # merged again: everything is identical now
    m2 = plan(demo, graphene())
    m2.select_with_needs("composites", "nD")
    assert m2.validate() and not m2.can_merge
    assert m2.item("composites", "nD").status == SAME


def test_rename_alias_rewrites_formulas_and_actions(demo):
    src = graphene()
    src["actions"].append({"name": "show", "code": "x = get('Vxx') / get(\"I\")\n"
                                                   "log('%g' % x)  # 'I' in a comment\n"
                                                   "set('Vbg', Cb * 0)\n"
                                                   "print(params.Cb, 'I am text')\n",
                           "description": "", "confirm": False})
    m = plan(demo, src)
    m.select_with_needs("composites", "Rxx")         # Vxx, I and their lock-ins
    m.select("actions", "show")
    assert not m.validate()
    # I is already an alias of the demo setup; ilockin is new, lockin exists (other driver
    # options would be a conflict; here it is identical sim_lockin at NONE? no: demo's has
    # options) - rename both conflicting things
    assert "already the alias I" in msgs(m, "aliases", "I")
    m.rename("aliases", "I", "Ilock")
    if m.item("instruments", "lockin").selected:
        m.rename("instruments", "lockin", "lockin2")
    m.select_with_needs("aliases", "Vbg")
    m.select_with_needs("parameters", "Cb")
    m.rename("parameters", "Cb", "Cback")
    assert m.validate(), m.errors
    assert m.item("composites", "Rxx").merged["expression"] == "Vxx / Ilock"
    code = m.item("actions", "show").merged["code"]
    assert "get('Vxx') / get(\"Ilock\")" in code
    assert "'I' in a comment" in code                  # comments are left alone
    assert "set('Vbg', Cback * 0)" in code and "params.Cback" in code
    assert "'I am text'" in code
    apply(demo, m)
    assert demo.channels.info("Rxx")["kind"] == "read"


def test_composite_variable_conflicts_and_duplicate_new_names(demo):
    demo.define_alias(name="n", target="src.V1")      # no conflict: the variable is nD.n
    m = plan(demo, graphene())
    m.select_with_needs("composites", "nD")
    for it in m.selected("instruments"):
        it.new_name = it.name + "2"
    assert m.validate(), m.errors
    # the composite's name is taken: renaming it renames its variables too
    demo.define_alias(name="nD", target="src.V2")
    m = plan(demo, graphene())
    m.select_with_needs("composites", "nD")
    for it in m.selected("instruments"):
        it.new_name = it.name + "2"
    assert not m.validate() and "already the alias nD" in msgs(m, "composites", "nD")
    src = graphene()
    src["composites"].append({"type": "read", "name": "nn", "expression": "nD.n * 2"})
    m = plan(demo, src)
    m.select_with_needs("composites", "nn")
    for it in m.selected("instruments"):
        it.new_name = it.name + "2"
    m.rename("composites", "nD", "nD2")
    assert m.validate(), m.errors
    assert m.item("composites", "nn").merged["expression"] == "nD2.n * 2"
    apply(demo, m)
    demo.set_many({"nD2.n": 0.5, "nD2.D": 0.0})
    assert demo.read_channel("nn") == pytest.approx(1.0)
    # two merged items under one name
    m = plan(demo, graphene())
    m.select_with_needs("aliases", "Vbg")
    m.select_with_needs("aliases", "Vtg")
    m.rename("aliases", "Vtg", "Vbg")
    assert not m.validate()
    assert "would also be the name of the merged alias Vbg" in msgs(m, "aliases", "Vtg")
    # a parameter renamed to an existing channel name
    m = plan(demo, graphene())
    m.select("parameters", "Cb")
    m.rename("parameters", "Cb", "Vg")
    assert not m.validate() and "already the alias Vg" in msgs(m, "parameters", "Cb")


def test_invalid_names_drivers_and_targets(demo):
    m = plan(demo, graphene())
    m.select("instruments", "bg")
    m.rename("instruments", "bg", "back gate")
    assert not m.validate() and "invalid name" in msgs(m, "instruments", "bg")
    m.rename("instruments", "bg", "bg")
    m.select("parameters", "Cb")
    m.rename("parameters", "Cb", "1Cb")
    assert not m.validate() and m.item("parameters", "Cb").status == ERROR
    m.rename("parameters", "Cb", "sin")                # a function name
    assert not m.validate()
    m.rename("parameters", "Cb", "")
    m.select("actions", "gates to zero")
    m.rename("actions", "gates to zero", "1 bad")
    assert not m.validate() and m.item("actions", "gates to zero").status == ERROR
    # a driver the backend does not have
    src = graphene()
    src["instruments"].append({"name": "x", "driver": "no_such_driver", "address": "NONE"})
    m = plan(demo, src)
    m.select("instruments", "x")
    assert not m.validate() and "not available" in msgs(m, "instruments", "x")
    # an alias of a channel the instrument does not have / of a setting
    src = graphene()
    src["aliases"] += [{"name": "Q", "target": "magnet.nonsense"},
                       {"name": "R", "target": "magnet.rate"}]
    m = plan(demo, src)
    m.select("aliases", "Q")
    m.select("aliases", "R")
    assert not m.validate()
    assert "has no channel nonsense" in msgs(m, "aliases", "Q")
    assert "configuration channel" in msgs(m, "aliases", "R")


def test_circular_references_and_live_channels(demo):
    src = graphene()
    src["parameters"] += [{"name": "a", "formula": "b + 1"}, {"name": "b", "formula": "2*a"},
                          {"name": "c", "formula": "Vg * 2"},           # a live channel
                          {"name": "d", "formula": "a + 1"}]            # uses the circle
    src["composites"] += [
        {"type": "read", "name": "r1", "expression": "r2 + 1"},
        {"type": "read", "name": "r2", "expression": "r1 * 2"},
        {"type": "map", "name": "m1", "target": "m2", "forward": "x", "backward": "y"},
        {"type": "map", "name": "m2", "target": "m1", "forward": "x", "backward": "y"},
        {"type": "map", "name": "sq", "target": "Vg", "forward": "x**2", "backward": "sqrt(y)",
         "min": -1, "max": 1}]
    m = plan(demo, src)
    for n in ("a", "b", "c", "d"):
        m.select("parameters", n)
    for n in ("r1", "r2", "m1", "m2", "sq"):
        m.select("composites", n)
    assert not m.validate()
    st = statuses(m, "parameters")
    assert st["a"] == st["b"] == st["c"] == st["d"] == ERROR
    assert "circle" in msgs(m, "parameters", "a") + msgs(m, "parameters", "b")
    for n in ("r1", "r2", "m1", "m2"):
        assert m.item("composites", n).status == ERROR, n
    assert "inverse" in msgs(m, "composites", "sq")
    # leave the bad ones out: the rest merges
    for n in ("a", "b", "c", "d"):
        m.select("parameters", n, False)
    for n in ("r1", "r2", "m1", "m2", "sq"):
        m.select("composites", n, False)
    m.select("parameters", "Cb")
    assert m.validate()


def test_writable_targets_and_actions_warnings(demo):
    src = graphene()
    src["composites"] += [
        {"type": "multi", "name": "g", "variables": [{"name": "u"}],
         "targets": {"Vdiff": "u"}},                          # a read composite of the demo
        {"type": "multi", "name": "h", "variables": [{"name": "w"}],
         "targets": {"Vg": "w + Cb"}}]
    src["actions"].append({"name": "uses", "code": "get('Vxx')\nset('Vg', 0)\n"})
    m = plan(demo, src)
    m.select("composites", "g")
    m.select("composites", "h")
    m.select("actions", "uses")
    assert not m.validate()
    assert "cannot be set" in msgs(m, "composites", "g")
    assert "needs the parameter Cb" in msgs(m, "composites", "h")
    a = m.item("actions", "uses")
    assert a.status == WARNING and "Vxx" in " ".join(a.messages)     # a warning only
    m.select("composites", "g", False)
    m.select("parameters", "Cb")
    assert m.validate() and m.can_merge
    apply(demo, m)
    demo.write_channel("h.w", 0.1, ramp=False)
    assert demo.channels.get("Vg") == pytest.approx(0.7)


def test_rename_code_and_formula_helpers():
    code = ("v = get('Vxx')  # 'Vxx'\nset(\"Vbg\", k*2)\nrun_action('li', 'auto')\n"
            "x = get('li.X'); params['k']; params.k\ns = '''Vxx'''\nkk = 1\n")
    out = rename_code(code, {"Vxx": "V2", "Vbg": "Vb2", "k": "kk2"}, {"li": "li9"},
                      {"k": "kk2"}, {})
    assert "get('V2')  # 'Vxx'" in out
    assert "set(\"Vb2\", kk2*2)" in out
    assert "run_action('li9', 'auto')" in out and "get('li9.X')" in out
    assert "params['kk2']; params.kk2" in out and "s = '''V2'''" in out
    assert "kk = 1" in out                                     # other names untouched
    assert rename_code("x = (", {"a": "b"}, {}, {}, {}) == "x = ("     # broken code: as is
    assert rename_formula("(n - n0 + D/kD) / (2*Cb)", {"Cb": "C"}) == "(n - n0 + D / kD) / (2 * C)"
    assert rename_formula("(n - n0 + D/kD) / (2*Cb)", {"Ct": "C"}) == "(n - n0 + D/kD) / (2*Cb)"
    assert rename_formula("x*a", {"x": "z", "a": "b"}, keep=["x"]) == "x * b"


# --------------------------------------------------------------------------- random
def _rich_source():
    src = graphene()
    src["parameters"] += [{"name": "g1", "value": 2.0}, {"name": "g2", "formula": "g1 * 3"},
                          {"name": "g3", "formula": "g2 + Cb"}]
    src["aliases"] += [{"name": "Vg", "target": "bg.voltage", "min": -5, "max": 5},
                       {"name": "Vh", "target": "tg.voltage", "min": -5, "max": 5}]
    src["composites"] += [
        {"type": "read", "name": "G", "expression": "1 / Rxx"},
        {"type": "read", "name": "Gs", "expression": "G * g2"},
        {"type": "map", "name": "Vgm", "target": "Vg", "forward": "x / g1",
         "backward": "y * g1", "min": -2, "max": 2},
        {"type": "multi", "name": "pair", "variables": [{"name": "s", "min": -1, "max": 1},
                                                         {"name": "t", "min": -1, "max": 1}],
         "targets": {"Vh": "s + t * g3"}}]
    src["actions"] += [{"name": "probe", "code": "get('G'); set('Vg', g1 * 0); get('bg.voltage')"}]
    return src


INTERNAL = ("NoneType", "has no attribute", "not iterable", "KeyError", "Traceback",
            "unhashable", "takes ", "positional argument")


def backend_refuses_some(be, m) -> bool:
    """Send the commands of every selected item (problems or not) as merging them
    would; True if the backend refuses at least one."""
    invalid = any(it.merged is None for it in m.selected() if it.status != SAME)
    saved = {}
    for it in m.selected():
        saved[(it.kind, it.name)] = it.status
        if it.status == ERROR and it.merged is not None:
            it.status = NEW
    errors, m.errors = m.errors, []
    try:
        cmds = m.commands()
    except Exception:
        return True
    finally:
        m.errors = errors
        for it in m.selected():
            it.status = saved[(it.kind, it.name)]
    refused = invalid
    for cmd, args in cmds:
        try:
            r = getattr(be, cmd)(**copy.deepcopy(args))
            if cmd == "add_instrument" and r.get("state") != "connected":
                refused = True
        except Exception:
            refused = True
    return refused


def test_random_selections_agree_with_the_backend(demo):
    """For random selections and renames: when the merge checks out, the backend accepts
    every command; merging again finds everything identical."""
    rng = random.Random(7)
    base = demo.get_setup()
    src = _rich_source()
    taken = ["Vg", "I", "lockin", "magnet", "B", "detuning", "Vdiff", "T"]
    accepted = refused = 0
    for trial in range(60):
        demo.load_setup(setup=copy.deepcopy(base))
        m = plan(demo, src)
        kinds = [k for k in m.items if m.items[k]]
        for _ in range(rng.randint(1, 5)):
            if rng.random() < 0.7:
                k = rng.choice(kinds)
                m.select_with_needs(k, rng.choice(m.items[k]).name)
            else:                                   # alone, its needs maybe missing
                k = rng.choice(kinds)
                m.select(k, rng.choice(m.items[k]).name)
        for it in m.selected():
            r = rng.random()
            if r < 0.1:
                it.new_name = rng.choice(taken)                     # often a conflict
            elif r < 0.3:
                it.new_name = it.name + "_m"
        if rng.random() < 0.6:
            # resolve the name conflicts the way a user would: another name
            m.validate()
            for it in m.selected():
                if it.status == ERROR and any("already" in x or "also be the name" in x
                                              for x in it.messages):
                    it.new_name = it.target + "_x%d" % trial
        ok = m.validate()
        if not ok:
            refused += 1
            assert m.errors and not m.can_merge
            with pytest.raises(MergeFileError):
                m.commands()
            for e in m.errors:                    # a reason, never an internal failure
                assert not any(x in e for x in INTERNAL), e
            # and the backend agrees: sent anyway, at least one command is refused
            assert backend_refuses_some(demo, m), (trial, m.errors)
            continue
        accepted += 1
        apply(demo, m)                        # raises if the backend refuses anything
        again = plan(demo, src)
        for k, its in m.items.items():
            for it in its:
                j = again.item(k, it.name)
                j.selected, j.new_name = it.selected, it.new_name
        again.validate()
        for k, its in again.items.items():
            for it in its:
                if it.selected:
                    assert it.status == SAME, (trial, k, it.name, it.status, it.messages)
    assert accepted >= 5 and refused >= 5, (accepted, refused)


def test_random_selections_with_needs_always_merge(demo):
    """Selecting items together with what they need, without conflicting names, always
    works (on a setup that has none of the file's names)."""
    rng = random.Random(11)
    src = _rich_source()
    for trial in range(15):
        demo.load_setup(setup={"format": "easyMeasure-setup-1"})
        m = plan(demo, src)
        kinds = list(m.items)
        for _ in range(rng.randint(1, 4)):
            k = rng.choice(kinds)
            if m.items[k]:
                m.select_with_needs(k, rng.choice(m.items[k]).name)
        assert m.validate(), (trial, m.errors)
        apply(demo, m)


def test_swapped_names(demo):
    """Two merged items exchanging their names: the formulas follow each one."""
    m = plan(demo, graphene())
    m.select_with_needs("composites", "nD")
    m.rename("aliases", "Vbg", "Vtg")
    m.rename("aliases", "Vtg", "Vbg")
    assert m.validate(), m.errors
    t = m.item("composites", "nD").merged["targets"]
    assert t["Vtg"] == "(n - n0 + D/kD) / (2*Cb)" and t["Vbg"] == "(n - n0 - D/kD) / (2*Ct)"
    apply(demo, m)
    demo.set_many({"nD.n": 1.0, "nD.D": 0.0})
    assert demo.instruments.get("bg").read("voltage") == pytest.approx(1.15 / 1.2)
    assert demo.instruments.get("tg").read("voltage") == pytest.approx(1.15 / 1.8)


def test_more_edge_cases(demo):
    src = graphene()
    # a one-variable composite, an invalid alias, a mapping of a renamed alias,
    # actions that quote an instrument channel and another action
    src["composites"] += [
        {"type": "multi", "name": "Vgx", "variables": [{"name": "v", "min": -1, "max": 1}],
         "targets": {"Vbg": "v / 2"}},
        {"type": "map", "name": "Vbm", "target": "Vbg", "forward": "x * 2",
         "backward": "y / 2", "min": -1, "max": 1}]
    src["aliases"].append({"name": "bad", "target": "bg.voltage", "min": 2, "max": 1})
    src["actions"] += [{"name": "a1", "code": "x = get('bg.voltage')\nrun_user_action('a2')"},
                       {"name": "a2", "code": "log('two')"}]
    m = plan(demo, src)
    m.select("aliases", "bad")
    assert not m.validate() and "invalid" in msgs(m, "aliases", "bad")
    m.select("aliases", "bad", False)
    m.select_with_needs("composites", "Vgx")
    m.select_with_needs("composites", "Vbm")
    m.rename("aliases", "Vbg", "VB")
    m.rename("instruments", "bg", "backgate")
    m.select("actions", "a1")
    m.select("actions", "a2")
    m.rename("actions", "a2", "second")
    assert m.validate(), m.errors
    assert m.item("composites", "Vgx").merged["targets"] == {"VB": "v / 2"}
    assert m.item("composites", "Vbm").merged["target"] == "VB"
    code = m.item("actions", "a1").merged["code"]
    assert "get('backgate.voltage')" in code and "run_user_action('second')" in code
    apply(demo, m)
    demo.write_channel("Vgx.v", 0.5, ramp=False)
    assert demo.instruments.get("backgate").read("voltage") == pytest.approx(0.25)
    # renames that would merge two targets of one composite into one name
    m = plan(demo, graphene())
    m.select_with_needs("composites", "nD")
    for it in m.selected():
        if it.kind == "instruments":
            it.new_name = it.name + "2"
    m.rename("aliases", "Vbg", "Vq")
    m.rename("aliases", "Vtg", "Vq")
    assert not m.validate()
    assert "two of its targets" in msgs(m, "composites", "nD")


def test_offline_instrument_and_unknown_channel_lists(demo):
    """An alias of an instrument whose channels are not known (never connected) is
    accepted with a warning; one of a known instrument is checked."""
    st = demo.get_state()
    for i in st["instruments"]:
        if i["name"] == "lockin":
            i["channels"] = []            # as if its channel list were unknown
    src = graphene()
    m = SetupMerge.from_state(src, st, demo.list_drivers()["drivers"])
    m.select("aliases", "Vxx")
    assert m.validate()
    assert m.item("aliases", "Vxx").status == WARNING
    assert "once it is connected" in msgs(m, "aliases", "Vxx")
    # without the driver list nothing is said about drivers
    m = SetupMerge.from_state(src, demo.get_state(), None)
    m.select("instruments", "bg")
    assert m.validate()


def test_dependencies_on_different_items_of_the_same_name_warn(demo):
    """Rxx needs I; leaving the file's I out uses this setup's I (dmm.reading), which is
    not the same channel: allowed, but with a warning."""
    m = plan(demo, graphene())
    m.select("composites", "Rxx")
    m.select_with_needs("aliases", "Vxx")
    m.validate()
    rxx = m.item("composites", "Rxx")
    assert rxx.status == WARNING and "dmm.reading" in " ".join(rxx.messages)
