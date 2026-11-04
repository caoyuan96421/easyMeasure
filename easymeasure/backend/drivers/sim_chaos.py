"""A deliberately pathological instrument for robustness testing.

Every channel misbehaves in a different way that real (buggy) hardware and
drivers do:

=============  ===============================================================
``value``      sensible reading, but with the configured fault rates
``garbage``    returns wrong types: None, dicts, ragged lists, bytes, huge text
``vector``     1-D reading whose length changes randomly
``hang``       blocks forever (never returns)
``slow``       takes ``slow_time`` seconds (default 1.5 s)
``crash``      raises SystemExit / KeyboardInterrupt / MemoryError
``flaky_set``  write-only; fails ``flaky_rate`` of the time (default 0.5)
``text``       string channel with occasional binary garbage
=============  ===============================================================

The backend must survive all of them.
"""

from __future__ import annotations

import time

import numpy as np

from ...common.errors import ChannelError
from ..instrument import ActionSpec, ChannelSpec
from ._simbase import FAULT_OPTION_DOC, SimDirectInstrument


class SimChaos(SimDirectInstrument):
    DESCRIPTION = "Pathological instrument for robustness testing"
    CHANNELS = [
        ChannelSpec("value", "rw", "float", (), "a.u."),
        ChannelSpec("garbage", "r", "float", (), "a.u."),
        ChannelSpec("vector", "r", "float", (-1,), "a.u."),
        ChannelSpec("hang", "r", "float", (), "", "never returns"),
        ChannelSpec("slow", "r", "float", (), ""),
        ChannelSpec("crash", "r", "float", (), "", "raises SystemExit etc."),
        ChannelSpec("flaky_set", "w", "float", (), ""),
        ChannelSpec("text", "rw", "str", (), "", config=True),
    ]
    ACTIONS = [ActionSpec("ok", "an action that works"),
               ActionSpec("fail", "an action that always raises"),
               ActionSpec("hang", "an action that never returns"),
               ActionSpec("crash", "an action that raises SystemExit")]
    DEFAULT_OPTIONS = dict(slow_time=1.5, flaky_rate=0.5, call_timeout=3.0,
                           **FAULT_OPTION_DOC)

    def connect(self):
        self.gate()
        self.val = getattr(self, "val", 0.0)
        self.txt = getattr(self, "txt", "hello")

    def _action_ok(self):
        self.gate()
        self.actions_done = getattr(self, "actions_done", 0) + 1

    def _action_fail(self):
        raise IOError("simulated action failure")

    def _action_hang(self):
        while True:
            time.sleep(1.0)

    def _action_crash(self):
        raise SystemExit(2)

    def read(self, channel):
        rng = self.fi.rng
        if channel == "hang":
            while True:          # a truly stuck driver
                time.sleep(1.0)
        self.gate()
        if channel == "value":
            return self.finish(self.val + 0.01 * rng.random())
        if channel == "garbage":
            return rng.choice([None, {"x": 1}, [1, [2, 3]], b"\x00\xff", "x" * 100000,
                               object(), float("inf"), 1.0, "3.14", [[1.0]]])
        if channel == "vector":
            return np.random.default_rng().standard_normal(rng.randint(0, 50))
        if channel == "slow":
            time.sleep(float(self.options["slow_time"]))
            return 1.0
        if channel == "crash":
            raise rng.choice([SystemExit(1), KeyboardInterrupt(), MemoryError("boom"),
                              RecursionError("deep"), GeneratorExit()])
        if channel == "text":
            return self.txt if rng.random() > 0.2 else b"\xff\xfe\x00bin"
        raise ChannelError("unknown channel %s" % channel)

    def write(self, channel, value):
        self.gate()
        if channel == "value":
            self.val = value
        elif channel == "flaky_set":
            if self.fi.rng.random() < float(self.options["flaky_rate"]):
                raise IOError("simulated write failure")
        elif channel == "text":
            self.txt = value
        else:
            raise ChannelError("channel %s not writable" % channel)
