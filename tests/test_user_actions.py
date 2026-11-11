"""User-defined actions: named snippets that control channels (backend/user_actions.py)."""

import json
import threading
import time

import pytest

from easymeasure.common.errors import ChannelError


def test_define_run_and_results(demo):
    be = demo
    r = be.define_user_action(name="gates to zero", code="set_many({'Vx': 0, 'Vy': 0})\n"
                              "set('Vg', 0, ramp=False)\nprint('done')",
                              description="all gates to 0 V", confirm=True)
    assert r["confirm"] and r["description"] == "all gates to 0 V"
    be.channels.set("Vg", 0.4, ramp=False)
    be.channels.set("Vx", 0.3, ramp=False)
    res = be.run_user_action("gates to zero")
    assert res["status"] == "finished" and res["error"] is None and res["elapsed"] >= 0
    assert be.channels.get("Vg") == 0 and be.channels.get("Vx") == 0
    acts = be.get_state()["actions"]
    assert [a["name"] for a in acts] == ["gates to zero"]
    assert acts[0]["running"] is False and acts[0]["last"]["status"] == "finished"
    # limits still apply: the error is reported, not raised
    be.define_user_action(name="too far", code="set('Vg', 5)")
    res = be.run_user_action("too far")
    assert res["status"] == "error" and "Vg" in res["error"]
    assert be.channels.get("Vg") == 0
    # a Python error, with the line
    be.define_user_action(name="broken", code="x = 1\ny = undefined_name")
    res = be.run_user_action("broken")
    assert res["status"] == "error" and "undefined_name" in res["error"]
    # definitions are checked
    with pytest.raises(ChannelError, match="syntax error line 1"):
        be.define_user_action(name="bad", code="set('Vg',")
    with pytest.raises(ChannelError, match="no code"):
        be.define_user_action(name="empty", code="  ")
    with pytest.raises(ChannelError, match="action name"):
        be.define_user_action(name="1st", code="pass")
    with pytest.raises(ChannelError, match="already exists"):
        be.define_user_action(name="broken", code="pass")
    be.define_user_action(name="broken", code="pass", replace=True)
    assert be.run_user_action("broken")["status"] == "finished"
    with pytest.raises(ChannelError, match="no action"):
        be.run_user_action("nope")


def test_order_rename_and_remove(demo):
    be = demo
    for n in ("a", "b", "c"):
        be.define_user_action(name=n, code="pass")
    be.move_user_action("c", 0)
    assert [a["name"] for a in be.list_user_actions()] == ["c", "a", "b"]
    be.define_user_action(name="a2", code="sleep(0)", rename_from="a")
    assert [a["name"] for a in be.list_user_actions()] == ["c", "a2", "b"]
    with pytest.raises(ChannelError, match="already exists"):
        be.define_user_action(name="b", code="pass", rename_from="c")
    be.remove_user_action("b")
    assert [a["name"] for a in be.list_user_actions()] == ["c", "a2"]
    with pytest.raises(ChannelError, match="no action"):
        be.remove_user_action("b")


def test_abort_nesting_and_state(demo):
    be = demo
    be.define_user_action(name="slow", code="state['n'] = 0\nwhile True:\n"
                          "    state['n'] += 1\n    sleep(0.02)")
    out = {}
    t = threading.Thread(target=lambda: out.update(be.run_user_action("slow")))
    t.start()
    t0 = time.time()
    while not be.list_user_actions()[0]["running"] and time.time() - t0 < 5:
        time.sleep(0.01)
    assert be.list_user_actions()[0]["running"]
    with pytest.raises(ChannelError, match="already running"):
        be.run_user_action("slow")
    with pytest.raises(ChannelError, match="is running"):
        be.remove_user_action("slow")
    time.sleep(0.1)
    assert be.abort_user_action("slow") == ["slow"]
    t.join(5)
    assert out["status"] == "aborted" and be.scripts.state["n"] > 1   # shared ``state``
    with pytest.raises(ChannelError, match="not running"):
        be.abort_user_action("slow")
    # a paused sweep does not hold an action back
    be.control.pause()
    try:
        be.define_user_action(name="quick", code="set('Vb', 0.1, ramp=False)")
        assert be.run_user_action("quick")["status"] == "finished"
    finally:
        be.control.resume()
    # actions calling actions; loops are refused, errors stop the caller
    be.define_user_action(name="outer", code="user_action('quick')\nset('Vb', get('Vb') * 2, "
                          "ramp=False)")
    assert be.run_user_action("outer")["status"] == "finished"
    assert be.channels.get("Vb") == pytest.approx(0.2)
    be.define_user_action(name="loop1", code="user_action('loop2')")
    be.define_user_action(name="loop2", code="user_action('loop1')")
    res = be.run_user_action("loop1")
    assert res["status"] == "error" and "calls itself (loop1 → loop2 → loop1)" in res["error"]
    be.define_user_action(name="fails", code="raise RuntimeError('boom')")
    be.define_user_action(name="caller", code="user_action('fails')\nset('Vb', 0.4)")
    res = be.run_user_action("caller")
    assert res["status"] == "error" and "boom" in res["error"]
    assert be.channels.get("Vb") == pytest.approx(0.2)
    # abort all reaches running actions too; an abort while nested stops the outer one
    be.define_user_action(name="wait", code="sleep(30)")
    be.define_user_action(name="wraps", code="user_action('wait')\nset('Vb', 0.4)")
    out.clear()
    t = threading.Thread(target=lambda: out.update(be.run_user_action("wraps")))
    t.start()
    time.sleep(0.2)
    be.abort_all()
    t.join(5)
    assert out["status"] == "aborted" and be.channels.get("Vb") == pytest.approx(0.2)
    # no sweeps from an action (they belong in the queue)
    be.define_user_action(name="sweeps", code="run_sweep({'name': 'x', 'dimensions': []})")
    assert "not available" in be.run_user_action("sweeps")["error"]


def test_actions_travel_with_the_setup(demo, tmp_path):
    be = demo
    be.define_user_action(name="zero", code="set('Vg', 0)", description="d", confirm=True)
    be.define_user_action(name="other", code="pass")
    p = tmp_path / "s.json"
    be.save_setup(str(p))
    saved = json.loads(p.read_text())
    assert saved["actions"][0] == {"name": "zero", "code": "set('Vg', 0)", "description": "d",
                                   "confirm": True}
    be.remove_user_action("zero")
    saved["actions"].append({"name": "broken", "code": "def ("})
    p.write_text(json.dumps(saved))
    problems = be.load_setup(path=str(p))
    assert [a["name"] for a in be.list_user_actions()] == ["zero", "other"]
    assert any(x.startswith("action broken: ") for x in problems)
    # the autosave keeps them too
    auto = json.loads((be.home / "autosave_setup.json").read_text())
    assert [a["name"] for a in auto["actions"]] == ["zero", "other"]


def test_events(demo):
    be = demo
    seen = []
    be.add_listener(lambda k, d: k == "user_action" and seen.append(dict(d)))
    be.define_user_action(name="x", code="pass")
    be.run_user_action("x")
    assert [s["status"] for s in seen] == ["running", "finished"] and seen[0]["name"] == "x"
