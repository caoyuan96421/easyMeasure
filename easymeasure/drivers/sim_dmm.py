"""Simulated digital multimeter (SCPI over a ``SIM::`` bus).

It "measures" the current through a simulated quantum dot whose gate, bias and
magnetic field are taken from the simulated world, by default from
``src.V1`` (gate), ``src.V2`` (bias) and ``magnet.field``.  Change them with the
``gate``, ``bias`` and ``field`` options.
"""

from __future__ import annotations

from ..common.errors import ChannelError
from ..backend.instrument import ActionSpec, ChannelSpec
from ._simbase import FAULT_OPTION_DOC, SimBusInstrument, get_or_create_device
from .simbus import FaultInjector, SCPIDevice
from .simworld import WORLD


class _DMMDevice(SCPIDevice):
    def __init__(self, name, faults, gate, bias, field):
        super().__init__(faults, idn="easyMeasure,SimDMM,SN%s,1.0" % name)
        self.gate, self.bias, self.field = gate, bias, field
        self.nplc = 1.0
        self.mode = "CURR"
        self.getters["READ"] = self._read
        self.getters["SENS:NPLC"] = lambda: "%g" % self.nplc
        self.setters["SENS:NPLC"] = self._set_nplc
        self.getters["FUNC"] = lambda: self.mode
        self.setters["FUNC"] = self._set_mode

    def _set_nplc(self, arg):
        self.nplc = min(max(float(arg), 0.01), 100.0)

    def _set_mode(self, arg):
        a = arg.strip().strip('"').upper()
        if a in ("CURR", "VOLT"):
            self.mode = a

    def _read(self):
        vg = float(WORLD.get(self.gate, 0.0))
        vb = float(WORLD.get(self.bias, 0.0))
        b = float(WORLD.get(self.field, 0.0))
        noise = 0.02 / max(self.nplc, 0.01) ** 0.5
        if self.mode == "VOLT":
            return "%.9E" % (vb + 1e-4 * noise * WORLD.rng.standard_normal())
        return "%.9E" % WORLD.current(vg, vb, b, noise)


class SimDMM(SimBusInstrument):
    DESCRIPTION = "Simulated DMM measuring a simulated quantum dot (SCPI over SIM bus)"
    CHANNELS = [
        ChannelSpec("reading", "r", "float", (), "nA", "measured value"),
        ChannelSpec("nplc", "rw", "float", (), "", "integration time (power line cycles)",
                    config=True),
        ChannelSpec("function", "rw", "str", (), "", "CURR or VOLT", config=True),
        ChannelSpec("idn", "r", "str", (), "", "identification string", config=True),
    ]
    ACTIONS = [ActionSpec("reset", "*RST: default settings (NPLC 1, current mode)"),
               ActionSpec("trigger", "*TRG: start a measurement")]
    DEFAULT_OPTIONS = dict(gpib_addr=2, gate="src.V1", bias="src.V2", field="magnet.field",
                           **FAULT_OPTION_DOC)

    def connect(self):
        o = self.options
        self.device = get_or_create_device(
            self.bus_name(), self.gpib_addr,
            lambda: _DMMDevice(self.name, FaultInjector(o), o["gate"], o["bias"], o["field"]))
        super().connect()

    def _action_reset(self):
        self.resource.write("SENS:NPLC 1")
        self.resource.write('FUNC "CURR"')

    def _action_trigger(self):
        self.resource.write("*TRG")

    def _read_reading(self):
        self.maybe_raise()
        return self.maybe_nan(float(self.resource.query("READ?")))

    def _read_nplc(self):
        return float(self.resource.query("SENS:NPLC?"))

    def _write_nplc(self, v):
        self.resource.write("SENS:NPLC %g" % v)

    def _read_function(self):
        return self.resource.query("FUNC?")

    def _write_function(self, v):
        if v.upper() not in ("CURR", "VOLT"):
            raise ChannelError("function must be CURR or VOLT")
        self.resource.write('FUNC "%s"' % v.upper())

    def _read_idn(self):
        return self.resource.query("*IDN?")
