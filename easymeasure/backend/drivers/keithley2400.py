"""Keithley 2400-series SourceMeter (voltage source / current measure).

Example of a hand-written driver for real hardware.  Address e.g.
``GPIB0::24::INSTR`` or ``ASRL3::INSTR`` (baudrate option for RS-232).
"""

from __future__ import annotations

from ...common.errors import ChannelError
from ..instrument import ActionSpec, ChannelSpec, Instrument


class Keithley2400(Instrument):
    DESCRIPTION = "Keithley 2400 SourceMeter (source V, measure I)"
    CHANNELS = [
        ChannelSpec("voltage", "rw", "float", (), "V", "source voltage"),
        ChannelSpec("current", "r", "float", (), "A", "measured current"),
        ChannelSpec("compliance", "rw", "float", (), "A", "current compliance", config=True),
        ChannelSpec("output", "rw", "str", (), "", "ON / OFF", config=True),
        ChannelSpec("nplc", "rw", "float", (), config=True),
    ]
    ACTIONS = [ActionSpec("clear_status", "*CLS"),
               ActionSpec("beep", "short beep (find the instrument in the rack)")]
    DEFAULT_OPTIONS = {"timeout": 5.0, "reset": False}

    def connect(self):
        r = self.resource
        idn = r.query("*IDN?")
        if "KEITHLEY" not in idn.upper():
            raise IOError("not a Keithley: %r" % idn)
        if self.options.get("reset"):
            r.write("*RST")
        r.write(":SOUR:FUNC VOLT")
        r.write(":SENS:FUNC 'CURR'")
        r.write(":FORM:ELEM CURR")

    def _action_clear_status(self):
        self.resource.write("*CLS")

    def _action_beep(self):
        self.resource.write(":SYST:BEEP 1000, 0.2")

    def _read_voltage(self):
        return self.resource.query(":SOUR:VOLT?")

    def _write_voltage(self, v):
        self.resource.write(":SOUR:VOLT %.6g" % v)

    def _read_current(self):
        return self.resource.query(":READ?").split(",")[0]

    def _read_compliance(self):
        return self.resource.query(":SENS:CURR:PROT?")

    def _write_compliance(self, v):
        self.resource.write(":SENS:CURR:PROT %.6g" % v)

    def _read_output(self):
        return "ON" if self.resource.query(":OUTP?").strip() == "1" else "OFF"

    def _write_output(self, v):
        v = v.strip().upper()
        if v not in ("ON", "OFF"):
            raise ChannelError("output must be ON or OFF")
        self.resource.write(":OUTP %s" % v)

    def _read_nplc(self):
        return self.resource.query(":SENS:CURR:NPLC?")

    def _write_nplc(self, v):
        self.resource.write(":SENS:CURR:NPLC %.4g" % v)
