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
        ch.set("output", "MAYBE")              # allowed values
    ch.set("output", "OFF")
    assert ch.get("output") == "OFF"
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
