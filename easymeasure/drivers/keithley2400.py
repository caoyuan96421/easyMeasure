"""Keithley 2400-series SourceMeter (2400, 2401, 2410, 2420, 2425, 2430, 2440).

Address e.g. ``GPIB0::24::INSTR``, or ``ASRL3::INSTR`` / ``COM3`` for RS-232 (set the
``baudrate`` option and the instrument's terminator to LF).  ``SIM::<name>``
talks to a built-in emulator (no hardware needed).

Channels (see :mod:`._smu` for the mode logic)

=================  ======  ==================================================
``mode``           config  ``Voltage`` (force V, measure I) / ``Current``
``voltage``        V       source (Voltage mode) or measured (Current mode)
``current``        A       measured (Voltage mode) or source (Current mode)
``Icompliance``    config  current limit in Voltage mode (A) …
``Vcompliance``    config  … voltage limit in Current mode (V)
``output``         config  ``ON`` / ``OFF``
``nplc``           config  integration time, 0.01 … 10 PLC
``sense``          config  ``2-wire`` / ``4-wire`` (``:SYST:RSEN``)
``terminals``      config  ``FRONT`` / ``REAR``
=================  ======  ==================================================

Options: ``reset`` (send ``*RST`` on connect), ``autorange`` (source and measure
auto-ranging, default on), ``check_errors`` (read the error queue after every
setting so a refused value raises an error, default on).
"""

from __future__ import annotations

from ..backend.instrument import ActionSpec, ChannelSpec
from ..backend.resources import GPIB_LIKE
from ._scpi import fmt
from ._smu import SMUBase

_Q = {"V": "VOLT", "I": "CURR"}


class Keithley2400(SMUBase):
    DESCRIPTION = "Keithley 2400 SourceMeter (voltage or current source, measure the other)"
    INTERFACES = GPIB_LIKE    # GPIB; COM / TCP: RS-232 where it has one, or a Prologix GPIB adapter
    IDN_MATCH = ("KEITHLEY",)
    OUTPUTS = ["1"]
    NPLC_RANGE = (0.01, 10.0)
    ACTIONS = [ActionSpec("clear_status", "*CLS: clear the error queue and status"),
               ActionSpec("beep", "short beep (find the instrument in the rack)")]
    DEFAULT_OPTIONS = dict(SMUBase.DEFAULT_OPTIONS, autorange=True)

    @staticmethod
    def EMULATOR(options):                     # noqa: N802 - class constant style
        from .emulators.smu import Keithley2400Emulator
        return Keithley2400Emulator(options)

    def extra_channels(self, o):
        return [ChannelSpec("terminals", "rw", "str", (), "", "front or rear terminals",
                            config=True, allowed=["FRONT", "REAR"])]

    # -- setup --------------------------------------------------------------------
    def setup_output(self, o):
        self.w(":SOUR:VOLT:MODE FIX")
        self.w(":SOUR:CURR:MODE FIX")
        self.w(":SENS:FUNC:CONC OFF")           # measure one function: fastest
        self._sense_function(self.hw_mode(o))

    def _sense_function(self, mode):
        meas = "CURR" if mode == "Voltage" else "VOLT"
        src = "VOLT" if mode == "Voltage" else "CURR"
        self.w(":SENS:FUNC '%s'" % meas)
        self.w(":FORM:ELEM %s" % meas)          # :READ? returns just that reading
        if self.options.get("autorange", True):
            self.w(":SENS:%s:RANG:AUTO ON" % meas)
            self.w(":SOUR:%s:RANG:AUTO ON" % src)

    # -- hardware commands ----------------------------------------------------------
    def hw_mode(self, o):
        r = self.q(":SOUR:FUNC?").upper()
        if r.startswith("VOLT"):
            return "Voltage"
        if r.startswith("CURR"):
            return "Current"
        raise IOError("unexpected source function %r" % r)

    def hw_set_mode(self, o, mode):
        self.checked(":SOUR:FUNC %s" % ("VOLT" if mode == "Voltage" else "CURR"))
        self._sense_function(mode)

    def hw_level(self, o, q):
        return self.qf(":SOUR:%s:LEV?" % _Q[q])

    def hw_set_level(self, o, q, v):
        self.checked(":SOUR:%s:LEV %s" % (_Q[q], fmt(v)))

    def hw_measure(self, o, q):
        return self.qf(":READ?")

    def hw_compliance(self, o, q):
        return self.qf(":SENS:%s:PROT?" % _Q[q])

    def hw_set_compliance(self, o, q, v):
        self.checked(":SENS:%s:PROT %s" % (_Q[q], fmt(v)))

    def hw_output(self, o):
        return self.qb(":OUTP?")

    def hw_set_output(self, o, on):
        self.checked(":OUTP %s" % ("ON" if on else "OFF"))

    def hw_nplc(self, o, q):
        return self.qf(":SENS:%s:NPLC?" % _Q[q])

    def hw_set_nplc(self, o, q, v):
        self.checked(":SENS:%s:NPLC %s" % (_Q[q], fmt(v)))

    def hw_sense(self, o):
        return self.qb(":SYST:RSEN?")

    def hw_set_sense(self, o, on):
        self.checked(":SYST:RSEN %s" % ("ON" if on else "OFF"))

    def read_extra(self, base, o):
        if base == "terminals":
            return "REAR" if self.q(":ROUT:TERM?").upper().startswith("REAR") else "FRONT"
        return super().read_extra(base, o)

    def write_extra(self, base, o, value):
        if base == "terminals":
            self.checked(":ROUT:TERM %s" % ("REAR" if value == "REAR" else "FRON"))
            return
        super().write_extra(base, o, value)

    # -- actions ----------------------------------------------------------------------
    def _action_clear_status(self):
        self.w("*CLS")

    def _action_beep(self):
        self.w(":SYST:BEEP 1000, 0.2")


Keithley2400.CHANNELS = Keithley2400.static_channels()
DRIVER = Keithley2400
