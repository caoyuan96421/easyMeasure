"""Simulated source-measure unit for gate voltages (no port; address ``NONE``).

Sources a voltage and measures the current that flows: for a gate, the leakage
through the gate dielectric, which rises steeply above the dielectric's breakdown
voltage (option ``breakdown_v``).  The output voltage is what the simulated
graphene sample sees (``<name>.V`` in the simulated world).  Used in the tutorials.
"""

from __future__ import annotations

from ..backend.instrument import ActionSpec, ChannelSpec
from ..common.errors import ChannelError
from ._simbase import SimDirectInstrument
from .simworld import WORLD


class SimSMU(SimDirectInstrument):
    DESCRIPTION = "Simulated source-measure unit for a gate (voltage out, leakage current in)"
    CHANNELS = [
        ChannelSpec("voltage", "rw", "float", (), "V", "output voltage"),
        ChannelSpec("current", "r", "float", (), "A", "measured current (gate leakage)"),
        ChannelSpec("output", "rw", "str", (), "", "output relay", config=True,
                    allowed=["ON", "OFF"]),
        ChannelSpec("compliance", "rw", "float", (), "A", "current limit", config=True),
    ]
    ACTIONS = [ActionSpec("zero", "set the output to 0 V at once")]
    DEFAULT_OPTIONS = dict(max_voltage=40.0, leak_ohm=2e12, breakdown_v=8.0)

    @classmethod
    def suggested_addresses(cls, driver_name):
        return [{"address": "NONE", "description": "simulated, no port"}]

    def connect(self):
        self.gate()
        self.v = getattr(self, "v", 0.0)
        self.output = getattr(self, "output", "ON")
        self.compliance = getattr(self, "compliance", 1e-6)
        self._publish()

    def _publish(self):
        WORLD.set(self.name + ".V", self.v if self.output == "ON" else 0.0)

    def _current(self):
        if self.output != "ON":
            return 0.0
        o = self.options
        i = WORLD.gate_leakage(self.v, float(o["leak_ohm"]), float(o["breakdown_v"]))
        return max(-self.compliance, min(self.compliance, i))

    def _action_zero(self):
        self.gate()
        self.v = 0.0
        self._publish()

    def read(self, channel):
        self.gate()
        if channel == "voltage":
            return self.finish(self.v)
        if channel == "current":
            return self.finish(self._current())
        if channel == "output":
            return self.output
        if channel == "compliance":
            return self.compliance
        raise ChannelError("unknown channel %s" % channel)

    def write(self, channel, value):
        self.gate()
        if channel == "voltage":
            if abs(value) > float(self.options["max_voltage"]):
                raise ChannelError("%g V is beyond the %g V of this source"
                                   % (value, float(self.options["max_voltage"])))
            self.v = float(value)
        elif channel == "output":
            self.output = value
        elif channel == "compliance":
            if not 1e-12 <= value <= 0.1:
                raise ChannelError("compliance must be 1 pA … 100 mA")
            self.compliance = float(value)
        else:
            raise ChannelError("channel %s is not writable" % channel)
        self._publish()
