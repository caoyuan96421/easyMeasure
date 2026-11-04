"""Simulated temperature controller with first-order thermal lag."""

from __future__ import annotations

import math
import threading
import time

from ...common.errors import ChannelError
from ..instrument import ChannelSpec
from ._simbase import FAULT_OPTION_DOC, SimDirectInstrument
from .simworld import WORLD

_STATE = {}
_LOCK = threading.Lock()


class SimTemperatureController(SimDirectInstrument):
    DESCRIPTION = "Simulated temperature controller (setpoint + lagging temperature)"
    CHANNELS = [
        ChannelSpec("setpoint", "rw", "float", (), "K"),
        ChannelSpec("temperature", "r", "float", (), "K"),
        ChannelSpec("heater", "r", "float", (), "%"),
        ChannelSpec("time_constant", "rw", "float", (), "s"),
    ]
    DEFAULT_OPTIONS = dict(base_temperature=1.5, **FAULT_OPTION_DOC)

    def connect(self):
        self.gate()
        base = float(self.options["base_temperature"])
        with _LOCK:
            self.st = _STATE.setdefault(self.name, {"T": base, "sp": base, "tau": 5.0,
                                                    "t": time.monotonic()})

    def _update(self):
        with _LOCK:
            st = self.st
            now = time.monotonic()
            dt, st["t"] = now - st["t"], now
            st["T"] = st["sp"] + (st["T"] - st["sp"]) * math.exp(-dt / max(st["tau"], 1e-3))
            WORLD.set(self.name + ".temperature", st["T"])
            return dict(st)

    def read(self, channel):
        self.gate()
        st = self._update()
        if channel == "temperature":
            return self.finish(st["T"] + 0.002 * self.rng.standard_normal())
        if channel == "setpoint":
            return st["sp"]
        if channel == "heater":
            return max(0.0, min(100.0, 10 * (st["sp"] - st["T"]) + 2 * st["sp"]))
        if channel == "time_constant":
            return st["tau"]
        raise ChannelError("unknown channel %s" % channel)

    def write(self, channel, value):
        self.gate()
        self._update()
        with _LOCK:
            if channel == "setpoint":
                if not 0 <= value <= 400:
                    raise ChannelError("setpoint out of range")
                self.st["sp"] = float(value)
            elif channel == "time_constant":
                self.st["tau"] = max(0.01, float(value))
            else:
                raise ChannelError("channel %s not writable" % channel)
