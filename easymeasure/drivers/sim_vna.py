"""Simulated vector network analyzer (no port; address ``NONE``).

Measures the reflection S11 of a superconducting resonator (see
``SimWorld.resonator_s11``): the dip deepens and narrows with power as the
two-level-system losses saturate, moves down at high power (Kerr effect), and moves
down and broadens in a magnetic field (option ``field``: a ``sim_magnet``).  Low
power and a wide IF bandwidth give noisy traces, like the real thing.

Each reading is a whole frequency sweep: ``S11`` (magnitude, dB) and
``S11_phase`` (degrees), with their frequency axis in ``frequencies`` - the plots
use it as the x axis.  Used in the tutorials.
"""

from __future__ import annotations

import time

import numpy as np

from ..backend.instrument import ActionSpec, ChannelSpec
from ..common.errors import ChannelError
from ._simbase import SimDirectInstrument
from .simworld import WORLD

_POINTS = [101, 201, 401, 801, 1601]
_IFBW = [10.0, 100.0, 1000.0, 10000.0]


class SimVNA(SimDirectInstrument):
    DESCRIPTION = "Simulated vector network analyzer (S11 of a superconducting resonator)"
    CHANNELS = [
        ChannelSpec("power", "rw", "float", (), "dBm", "source power"),
        ChannelSpec("S11", "r", "float", (-1,), "dB", "reflection magnitude, one sweep",
                    timeout=60.0, axes=("frequencies",)),
        ChannelSpec("S11_phase", "r", "float", (-1,), "deg", "reflection phase, one sweep",
                    timeout=60.0, axes=("frequencies",)),
        ChannelSpec("frequencies", "r", "float", (-1,), "Hz", "frequency of each point"),
        ChannelSpec("f_start", "rw", "float", (), "Hz", "start frequency", config=True),
        ChannelSpec("f_stop", "rw", "float", (), "Hz", "stop frequency", config=True),
        ChannelSpec("points", "rw", "float", (), "", "points per sweep", config=True,
                    allowed=_POINTS),
        ChannelSpec("if_bandwidth", "rw", "float", (), "Hz", "IF bandwidth (less = slower, "
                    "less noise)", config=True, allowed=_IFBW),
        ChannelSpec("output", "rw", "str", (), "", "source on / off", config=True,
                    allowed=["ON", "OFF"]),
    ]
    ACTIONS = [ActionSpec("preset", "factory settings: 5.995–6.005 GHz, 401 points, "
                          "1 kHz, -30 dBm")]
    DEFAULT_OPTIONS = dict(field="magnet.field", sweep_time_scale=0.1)

    @classmethod
    def suggested_addresses(cls, driver_name):
        return [{"address": "NONE", "description": "simulated, no port"}]

    def connect(self):
        self.gate()
        if not hasattr(self, "f_start"):
            self._action_preset()

    def _action_preset(self):
        self.f_start, self.f_stop, self.points, self.ifbw = 5.995e9, 6.005e9, 401, 1000.0
        self.pwr, self.output = -30.0, "ON"

    def _freqs(self):
        return np.linspace(self.f_start, self.f_stop, int(self.points))

    def _sweep(self):
        # a real sweep takes points / IF bandwidth seconds (scaled down here)
        time.sleep(min(self.points / self.ifbw * float(self.options["sweep_time_scale"]), 2.0))
        if self.output != "ON":
            return np.full(int(self.points), 1e-6 + 0j)
        return WORLD.resonator_s11(self._freqs(), self.pwr,
                                   float(WORLD.get(self.options["field"])), self.ifbw)

    def read(self, channel):
        self.gate()
        if channel == "S11":
            return self.finish(20 * np.log10(np.abs(self._sweep())))
        if channel == "S11_phase":
            return self.finish(np.degrees(np.angle(self._sweep())))
        if channel == "frequencies":
            return self._freqs()
        return {"power": self.pwr, "f_start": self.f_start, "f_stop": self.f_stop,
                "points": float(self.points), "if_bandwidth": self.ifbw,
                "output": self.output}[channel]

    def write(self, channel, value):
        self.gate()
        if channel == "power":
            if not -90.0 <= value <= 10.0:
                raise ChannelError("power must be -90 … +10 dBm")
            self.pwr = float(value)
        elif channel in ("f_start", "f_stop"):
            if not 1e5 <= value <= 20e9:
                raise ChannelError("frequency must be 100 kHz … 20 GHz")
            setattr(self, channel, float(value))
            if self.f_start >= self.f_stop:
                raise ChannelError("f_start must be below f_stop")
        elif channel == "points":
            self.points = int(value)
        elif channel == "if_bandwidth":
            self.ifbw = float(value)
        elif channel == "output":
            self.output = value
        else:
            raise ChannelError("channel %s is not writable" % channel)
