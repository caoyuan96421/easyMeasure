import time

import numpy as np
import pytest

from easymeasure.backend.channels import Alias, Composite
from easymeasure.common.errors import ChannelError, ExpressionError, LimitError


def test_limits_and_protection(demo):
    ch = demo.channels
    with pytest.raises(LimitError):
        ch.set("Vg", 5.0)
    with pytest.raises(LimitError):
        ch.set("Vg", float("nan"))
    with pytest.raises(ChannelError):
        ch.set("I", 1.0)                       # read-only target
    with pytest.raises(ChannelError):
        ch.set("src.V1", 1.0)                  # raw channels are not settable
    with pytest.raises(LimitError):
        ch.set("Vg", 1e9)
    # configuration channels are set directly and cannot be aliased
    ch.set("src.output", "OFF")
    assert ch.get("src.output") == "OFF"
    with pytest.raises(ChannelError, match="configuration"):
        ch.add_alias(Alias("out", "src.output"))
    with pytest.raises(ChannelError):
        ch.set("Vg", "abc")
    # value unchanged after refusals
    assert ch.get("Vg") == 0.0


def test_ramp_rate_respected(demo):
    ch = demo.channels
    seen = []
    h = demo.instruments.get("src")
    orig = h.write

    def spy(channel, value):
        seen.append((time.monotonic(), value))
        orig(channel, value)
    h.write = spy
    t0 = time.monotonic()
    ch.set("Vb", 0.4)                          # ramp_rate 1 V/s -> ~0.4 s
    dt = time.monotonic() - t0
    assert 0.35 < dt < 1.5
    assert seen[-1][1] == pytest.approx(0.4)
    # no step faster than the rate (allow one tick of slack)
    for (ta, va), (tb, vb) in zip(seen, seen[1:]):
        assert abs(vb - va) <= 1.0 * (tb - ta) + 0.06
    assert ch.get("Vb") == pytest.approx(0.4)
    seen.clear()
    ch.set("Vb", 0.0, ramp=False)
    assert len(seen) == 1


def test_ramp_step_and_scale(demo):
    ch = demo.channels
    ch.add_alias(Alias("Vamp", "src.V3", min=-10, max=10, ramp_step=0.5, scale=10.0, offset=1.0))
    ch.set("Vamp", 3.0)          # raw = (3-1)/10 = 0.2
    assert demo.instruments.get("src").read("V3") == pytest.approx(0.2)
    assert ch.get("Vamp") == pytest.approx(3.0)


def test_abortable_ramp(demo):
    ch = demo.channels
    from easymeasure.common.errors import AbortRequested
    calls = {"n": 0}

    def abort_check():
        calls["n"] += 1
        if calls["n"] > 3:
            raise AbortRequested("stop")
    ch.set("Tset", 1.5, ramp=False)
    with pytest.raises(AbortRequested):
        ch.set("Tset", 100.0, abort_check=abort_check)   # 5 K/s -> would take 20 s
    assert ch.get("Tset") < 10


def test_composite(demo):
    ch = demo.channels
    ch.set("detuning", 0.3)
    assert ch.get("Vx") == pytest.approx(0.3)
    assert ch.get("Vy") == pytest.approx(-0.3)
    assert ch.get("detuning") == pytest.approx(0.3)
    with pytest.raises(LimitError):
        ch.set("detuning", 0.95)                 # composite limit
    ch.add_composite(Composite("big", {"Vx": "2*x", "Vy": "x"}))
    with pytest.raises(LimitError):
        ch.set("big", 0.8)                       # Vx target 1.6 > 1: refused before writing
    assert ch.get("Vy") == pytest.approx(-0.3)    # nothing was written
    with pytest.raises(ExpressionError):
        Composite("bad", {"Vx": "y*2"})
    with pytest.raises(ChannelError):
        ch.add_composite(Composite("c2", {"nope": "x"}))
    with pytest.raises(ChannelError):
        ch.remove_alias("Vx")                    # used by a composite


def test_composite_ramp_respects_target_rates(demo):
    ch = demo.channels
    ch.add_alias(Alias("A1", "src.V3", min=-1, max=1, ramp_rate=1.0), replace=True)
    ch.add_alias(Alias("A2", "src.V4", min=-1, max=1, ramp_rate=0.5), replace=True)
    ch.add_composite(Composite("pair", {"A1": "x", "A2": "x"}))
    ch.set("pair", 0.0, ramp=False)
    t0 = time.monotonic()
    ch.set("pair", 0.25)     # A2 limits: 0.25 / 0.5 = 0.5 s
    assert time.monotonic() - t0 > 0.4
    assert ch.get("A2") == pytest.approx(0.25)


def test_vector_channel(demo):
    ch = demo.channels
    w = np.sin(np.linspace(0, 2 * np.pi, 64))
    ch.set("wave", w)
    np.testing.assert_allclose(ch.get("wave"), w)
    with pytest.raises(LimitError):
        ch.set("wave", w * 10)


def test_alias_validation():
    with pytest.raises(ChannelError):
        Alias("1bad", "a.b")
    with pytest.raises(ChannelError):
        Alias("ok", "nodot")
    with pytest.raises(ChannelError):
        Alias("ok", "a.b", min=1, max=0)
    with pytest.raises(ChannelError):
        Alias("ok", "a.b", ramp_rate=-1)


def test_review_ramp_bypasses_closed(demo):
    """Regression tests for ways around limits / ramp rates found in review."""
    ch = demo.channels
    src = demo.instruments.get("src")
    ch.set("Vg", -0.5)
    # 1-element list must not skip the ramp (2 V/s -> ~0.5 s for 1 V)
    t = time.monotonic()
    ch.set("Vg", [0.5])
    assert time.monotonic() - t > 0.35
    with pytest.raises(ChannelError):
        ch.set("Vg", [0.1, 0.2])
    # infinity is refused even if only one side is limited
    ch.add_alias(Alias("Vr", "src.V3", max=1.0, ramp_rate=0.1), replace=True)
    with pytest.raises(LimitError):
        ch.set("Vr", float("-inf"))
    # raw query / raw write respect alias limits and invalidate the cache
    with pytest.raises(LimitError):
        demo.query_channel("src.V1", 50.0)
    with pytest.raises(LimitError):
        ch.set_raw("src.V1", -7.0)
    ch.set_raw("src.V1", -1.0)
    seen = []
    orig = src.write
    src.write = lambda c, v: (seen.append(v), orig(c, v))
    ch.set("Vg", 0.0)                     # must ramp from the real -1.0, not a cached value
    assert seen[0] < -0.8
    src.write = orig


def test_review_unknown_start_refuses_to_jump(demo):
    ch = demo.channels
    ch.add_alias(Alias("Vn", "src.V4", min=-2, max=2, ramp_rate=0.1), replace=True)
    src = demo.instruments.get("src")
    orig = src.read
    src.read = lambda c: float("nan") if c == "V4" else orig(c)
    try:
        with pytest.raises(LimitError, match="present value"):
            ch.set("Vn", 1.5)
        ch.set("Vn", 1.5, ramp=False)    # explicit override
    finally:
        src.read = orig


def test_review_composite_nonlinear_path_respects_rates(demo):
    ch = demo.channels
    ch.add_alias(Alias("VL", "src.V3", min=-1, max=1, ramp_rate=2.0), replace=True)
    ch.add_composite(Composite("q", {"VL": "x**2"}, min=-1, max=1))
    ch.set("VL", 1.0, ramp=False)
    ch.set("q", -1.0, ramp=False)
    src = demo.instruments.get("src")
    seen = []
    orig = src.write
    src.write = lambda c, v: (seen.append((time.monotonic(), v)), orig(c, v))
    t = time.monotonic()
    ch.set("q", 1.0)          # VL goes 1 -> 0 -> 1: 2 V of travel at 2 V/s
    dt = time.monotonic() - t
    src.write = orig
    assert dt > 0.8
    rates = [abs(b[1] - a[1]) / max(b[0] - a[0], 1e-9) for a, b in zip(seen, seen[1:])]
    assert max(rates) < 2.0 * 1.6       # one tick of slack
    # ramp_step honoured through a composite
    ch.add_alias(Alias("VS", "src.V4", min=-1, max=1, ramp_step=0.1), replace=True)
    ch.add_composite(Composite("s", {"VS": "x"}))
    ch.set("VS", 0.0, ramp=False)
    seen.clear()
    src.write = lambda c, v: (seen.append(v), orig(c, v))
    ch.set("s", 0.9)
    src.write = orig
    assert len(seen) >= 9 and max(abs(b - a) for a, b in zip([0.0] + seen, seen)) <= 0.1 + 1e-9


def test_actions_and_config_channels(demo):
    be = demo
    be.add_instrument("chaos", "sim_chaos", options={"call_timeout": 0.5, "retries": 2,
                                                     "exception_rate": 0.0})
    info = {i["name"]: i for i in be.list_instruments()}
    assert {a["name"] for a in info["dmm"]["actions"]} == {"reset", "trigger"}
    cfg = {c["name"] for c in info["dmm"]["channels"] if c["config"]}
    assert cfg == {"nplc", "function", "idn"}
    # actions run once (never retried), failures/hangs/crashes are contained
    be.run_action("chaos.ok")
    assert be.instruments.get("chaos").driver.actions_done == 1
    for bad in ("chaos.fail", "chaos.crash", "chaos.hang", "chaos.nope", "nodot"):
        with pytest.raises(Exception):
            be.run_action(bad)
    be.run_action("chaos.ok")
    # an action makes cached alias values stale -> next ramp reads back
    be.write_channel("Vg", 0.2)
    assert be.channels.last_value("Vg") == pytest.approx(0.2)
    be.run_action("src.clear_status")
    assert be.channels.last_value("Vg") is None
    # config channel round trip, including through the dmm reset action
    be.write_channel("dmm.nplc", 10)
    assert be.read_channel("dmm.nplc") == 10
    be.run_action("dmm.reset")
    assert be.read_channel("dmm.nplc") == 1
    # non-config raw channels still cannot be set directly
    with pytest.raises(ChannelError):
        be.write_channel("src.V1", 0.1)
    # actions from scripts / hooks (the hang above replaced the driver object, so
    # count relative to the current one)
    n = be.instruments.get("chaos").driver.actions_done
    be.queue_add({"kind": "script", "code": "action('dmm.trigger')\ninstrument('chaos').action('ok')"},
                 start=True)
    assert be.queue.wait_idle(20)
    assert be.queue_state()["history"][-1]["status"] == "finished"
    assert be.instruments.get("chaos").driver.actions_done == n + 1
