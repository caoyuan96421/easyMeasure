"""Simulated spectrum analyser: reads a 1-D trace per measurement.

The resonance position depends on the simulated magnetic field and on a voltage
(``voltage`` option, default ``src.V3``).
"""

from __future__ import annotations

import numpy as np

from ...common.errors import ChannelError
from ..instrument import ChannelSpec
from ._simbase import FAULT_OPTION_DOC, SimDirectInstrument
from .simworld import WORLD


class SimSpectrumAnalyzer(SimDirectInstrument):
    DESCRIPTION = "Simulated spectrum analyser (1-D trace readings)"
    CHANNELS = [
        ChannelSpec("trace", "r", "float", (-1,), "a.u.", "spectrum trace"),
        ChannelSpec("frequencies", "r", "float", (-1,), "GHz", "frequency axis"),
        ChannelSpec("start", "rw", "float", (), "GHz"),
        ChannelSpec("stop", "rw", "float", (), "GHz"),
        ChannelSpec("points", "rw", "float", (), "", "number of points (2..10001)"),
        ChannelSpec("peak", "r", "float", (), "GHz", "frequency of the strongest peak"),
    ]
    DEFAULT_OPTIONS = dict(voltage="src.V3", field="magnet.field", **FAULT_OPTION_DOC)

    def connect(self):
        self.gate()
        self.f_start = getattr(self, "f_start", 3.0)
        self.f_stop = getattr(self, "f_stop", 9.0)
        self.npts = getattr(self, "npts", 201)

    def _freqs(self):
        return np.linspace(self.f_start, self.f_stop, int(self.npts))

    def _trace(self):
        return WORLD.spectrum(self._freqs(), float(WORLD.get(self.options["field"])),
                              float(WORLD.get(self.options["voltage"])))

    def read(self, channel):
        self.gate()
        if channel == "trace":
            return self.finish(self._trace())
        if channel == "frequencies":
            return self._freqs()
        if channel == "peak":
            tr = self._trace()
            return self.finish(float(self._freqs()[int(np.argmax(tr))]))
        if channel == "start":
            return self.f_start
        if channel == "stop":
            return self.f_stop
        if channel == "points":
            return float(self.npts)
        raise ChannelError("unknown channel %s" % channel)

    def write(self, channel, value):
        self.gate()
        if channel == "start":
            self.f_start = value
        elif channel == "stop":
            self.f_stop = value
        elif channel == "points":
            n = int(round(value))
            if not 2 <= n <= 10001:
                raise ChannelError("points must be between 2 and 10001")
            self.npts = n
        else:
            raise ChannelError("channel %s not writable" % channel)
