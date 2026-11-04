"""Simulated arbitrary waveform generator: accepts vector (1-D array) writes."""

from __future__ import annotations

import numpy as np

from ...common.errors import ChannelError
from ..instrument import ActionSpec, ChannelSpec
from ._simbase import FAULT_OPTION_DOC, SimDirectInstrument
from .simworld import WORLD


class SimAWG(SimDirectInstrument):
    DESCRIPTION = "Simulated AWG (vector-valued waveform channel)"
    CHANNELS = [
        ChannelSpec("waveform", "rw", "float", (-1,), "V", "waveform samples"),
        ChannelSpec("rms", "r", "float", (), "V", "RMS of the loaded waveform"),
        ChannelSpec("offset", "rw", "float", (), "V", config=True),
        ChannelSpec("run", "rw", "str", (), "", "RUN or STOP", config=True),
    ]
    ACTIONS = [ActionSpec("start", "start the output (run = RUN)"),
               ActionSpec("stop", "stop the output (run = STOP)"),
               ActionSpec("trigger", "single-shot trigger")]
    DEFAULT_OPTIONS = dict(max_samples=65536, **FAULT_OPTION_DOC)

    def connect(self):
        self.gate()
        self.wave = getattr(self, "wave", np.zeros(16))
        self.off = getattr(self, "off", 0.0)
        self.state = getattr(self, "state", "STOP")

    def _action_start(self):
        self.gate()
        self.state = "RUN"
        self._publish()

    def _action_stop(self):
        self.gate()
        self.state = "STOP"
        self._publish()

    def _action_trigger(self):
        self.gate()

    def _publish(self):
        rms = float(np.sqrt(np.mean((self.wave + self.off) ** 2))) if self.state == "RUN" else 0.0
        WORLD.set(self.name + ".rms", rms)
        return rms

    def read(self, channel):
        self.gate()
        if channel == "waveform":
            return self.finish(self.wave.copy())
        if channel == "rms":
            return self.finish(self._publish())
        if channel == "offset":
            return self.off
        if channel == "run":
            return self.state
        raise ChannelError("unknown channel %s" % channel)

    def write(self, channel, value):
        self.gate()
        if channel == "waveform":
            arr = np.asarray(value, dtype=float).ravel()
            if not 1 <= arr.size <= int(self.options["max_samples"]):
                raise ChannelError("waveform length out of range")
            if np.any(np.abs(arr) > 10):
                raise ChannelError("waveform exceeds +-10 V")
            self.wave = arr
        elif channel == "offset":
            self.off = float(value)
        elif channel == "run":
            v = str(value).upper()
            if v not in ("RUN", "STOP"):
                raise ChannelError("run must be RUN or STOP")
            self.state = v
        else:
            raise ChannelError("channel %s not writable" % channel)
        self._publish()
