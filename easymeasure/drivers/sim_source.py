"""Simulated 4-channel DC voltage source speaking SCPI over a ``SIM::`` bus.

Address: ``SIM::<bus>`` (several instruments can share a bus; set ``gpib_addr``).
Outputs are published into the simulated world as ``<instrument>.V1`` etc. so
simulated detectors respond to them.
"""

from __future__ import annotations

from ..backend.instrument import ActionSpec, ChannelSpec
from ._simbase import FAULT_OPTION_DOC, SimBusInstrument, get_or_create_device
from .simbus import FaultInjector, SCPIDevice
from .simworld import WORLD


class _SourceDevice(SCPIDevice):
    def __init__(self, name: str, faults: FaultInjector):
        super().__init__(faults, idn="easyMeasure,SimSource4,SN%s,1.0" % name)
        self.name = name
        self.volts = [0.0] * 4
        self.output = "ON"
        for i in range(4):
            self.setters["SOUR%d:VOLT" % (i + 1)] = self._setter(i)
            self.getters["SOUR%d:VOLT" % (i + 1)] = self._getter(i)
        self.setters["OUTP"] = self._set_output
        self.getters["OUTP"] = lambda: self.output

    def _setter(self, i):
        def f(arg):
            v = float(arg)
            self.volts[i] = v
            WORLD.set("%s.V%d" % (self.name, i + 1), v if self.output == "ON" else 0.0)
        return f

    def _getter(self, i):
        return lambda: "%.9E" % self.volts[i]

    def _set_output(self, arg):
        self.output = "ON" if arg.strip().upper() in ("ON", "1") else "OFF"
        for i in range(4):
            WORLD.set("%s.V%d" % (self.name, i + 1), self.volts[i] if self.output == "ON" else 0.0)


class SimVoltageSource(SimBusInstrument):
    DESCRIPTION = "Simulated 4-channel DC voltage source (SCPI over SIM bus)"
    CHANNELS = [ChannelSpec("V%d" % i, "rw", "float", (), "V", "output voltage %d" % i)
                for i in range(1, 5)] + [
        ChannelSpec("output", "rw", "str", (), "", "ON/OFF", config=True),
        ChannelSpec("idn", "r", "str", (), "", "identification string", config=True),
    ]
    ACTIONS = [ActionSpec("clear_status", "*CLS: clear the error queue")]
    DEFAULT_OPTIONS = dict(gpib_addr=1, **FAULT_OPTION_DOC)

    def connect(self):
        self.device = get_or_create_device(
            self.bus_name(), self.gpib_addr,
            lambda: _SourceDevice(self.name, FaultInjector(self.options)))
        super().connect()

    def _action_clear_status(self):
        self.resource.write("*CLS")

    def read(self, channel):
        self.maybe_raise()
        if channel.startswith("V"):
            return self.maybe_nan(float(self.resource.query("SOUR%s:VOLT?" % channel[1:])))
        if channel == "output":
            return self.resource.query("OUTP?")
        if channel == "idn":
            return self.resource.query("*IDN?")
        return super().read(channel)

    def write(self, channel, value):
        self.maybe_raise()
        if channel.startswith("V"):
            self.resource.write("SOUR%s:VOLT %.9E" % (channel[1:], value))
            return
        if channel == "output":
            self.resource.write("OUTP %s" % value)
            return
        super().write(channel, value)
