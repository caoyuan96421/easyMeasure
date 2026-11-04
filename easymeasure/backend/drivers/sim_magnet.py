"""Simulated superconducting magnet power supply.

Writing ``field`` sets a target and the simulated field then ramps there at
``rate`` T/min in real time - like the real thing.  ``field`` reads the actual
field; ``status`` is RAMPING or HOLDING.  Combine with an alias ramp rate or a
script that waits for HOLDING.
"""

from __future__ import annotations

import threading
import time

from ...common.errors import ChannelError
from ..instrument import ActionSpec, ChannelSpec
from ._simbase import FAULT_OPTION_DOC, SimDirectInstrument
from .simworld import WORLD

_STATE = {}
_STATE_LOCK = threading.Lock()


class SimMagnet(SimDirectInstrument):
    DESCRIPTION = "Simulated magnet supply (field ramps in real time)"
    CHANNELS = [
        ChannelSpec("field", "rw", "float", (), "T", "set target / read actual field"),
        ChannelSpec("target", "r", "float", (), "T"),
        ChannelSpec("rate", "rw", "float", (), "T/min", config=True),
        ChannelSpec("status", "r", "str", (), "", "RAMPING / HOLDING"),
    ]
    ACTIONS = [ActionSpec("hold", "stop ramping at the present field")]
    DEFAULT_OPTIONS = dict(max_field=9.0, **FAULT_OPTION_DOC)

    def connect(self):
        self.gate()
        with _STATE_LOCK:
            st = _STATE.setdefault(self.name, {"field": 0.0, "target": 0.0, "rate": 10.0,
                                               "t": time.monotonic()})
        self.st = st

    def _action_hold(self):
        self.gate()
        st = self._update()
        with _STATE_LOCK:
            self.st["target"] = st["field"]

    def _update(self):
        with _STATE_LOCK:
            st = self.st
            now = time.monotonic()
            dt = now - st["t"]
            st["t"] = now
            step = st["rate"] / 60.0 * dt
            diff = st["target"] - st["field"]
            if abs(diff) <= step:
                st["field"] = st["target"]
            else:
                st["field"] += step if diff > 0 else -step
            WORLD.set(self.name + ".field", st["field"])
            return dict(st)

    def read(self, channel):
        self.gate()
        st = self._update()
        if channel == "field":
            return self.finish(st["field"])
        if channel == "target":
            return st["target"]
        if channel == "rate":
            return st["rate"]
        if channel == "status":
            return "HOLDING" if st["field"] == st["target"] else "RAMPING"
        raise ChannelError("unknown channel %s" % channel)

    def write(self, channel, value):
        self.gate()
        self._update()
        if channel == "field":
            if abs(value) > float(self.options["max_field"]):
                raise ChannelError("field exceeds supply limit")
            with _STATE_LOCK:
                self.st["target"] = float(value)
        elif channel == "rate":
            if not 0 < value <= 60:
                raise ChannelError("rate out of range")
            with _STATE_LOCK:
                self.st["rate"] = float(value)
        else:
            raise ChannelError("channel %s not writable" % channel)
