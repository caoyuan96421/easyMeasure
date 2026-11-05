"""Keysight (Agilent) B2900 series precision source/measure units (not a driver itself;
see ``keysight_b2901`` and ``keysight_b2902``).

Commands follow the B2900 SCPI reference: ``:SOUR<c>:FUNC:MODE``,
``:SOUR<c>:VOLT``/``CURR``, ``:SENS<c>:CURR:PROT`` (compliance),
``:SENS<c>:<func>:NPLC``, ``:SENS<c>:REM`` (4-wire), ``:OUTP<c>`` and spot
measurements with ``:MEAS:<func>? (@<c>)``.
"""

from __future__ import annotations

from ..backend.instrument import ActionSpec, ChannelSpec
from ._scpi import fmt
from ._smu import SMUBase

_Q = {"V": "VOLT", "I": "CURR"}


class B2900Base(SMUBase):
    IDN_MATCH = ("B29",)
    NPLC_RANGE = (4e-4, 100.0)
    ACTIONS = [ActionSpec("clear_status", "*CLS: clear the error queue and status")]
    DEFAULT_OPTIONS = dict(SMUBase.DEFAULT_OPTIONS)

    @staticmethod
    def EMULATOR(options):                     # noqa: N802
        from .emulators.smu import B2900Emulator
        return B2900Emulator(options, outputs=2)

    def extra_channels(self, o):
        s = self.suffix(o)
        return [ChannelSpec("low" + s, "rw", "str", (), "", "low terminal: GROUND or FLOAT "
                            "(change with the output off)", config=True, allowed=["GROUND", "FLOAT"])]

    def hw_mode(self, o):
        r = self.q(":SOUR%s:FUNC:MODE?" % o).upper()
        if r.startswith("VOLT"):
            return "Voltage"
        if r.startswith("CURR"):
            return "Current"
        raise IOError("unexpected source mode %r" % r)

    def hw_set_mode(self, o, mode):
        self.checked(":SOUR%s:FUNC:MODE %s" % (o, "VOLT" if mode == "Voltage" else "CURR"))

    def hw_level(self, o, q):
        return self.qf(":SOUR%s:%s?" % (o, _Q[q]))

    def hw_set_level(self, o, q, v):
        self.checked(":SOUR%s:%s %s" % (o, _Q[q], fmt(v)))

    def hw_measure(self, o, q):
        return self.qf(":MEAS:%s? (@%s)" % (_Q[q], o))

    def hw_compliance(self, o, q):
        return self.qf(":SENS%s:%s:PROT?" % (o, _Q[q]))

    def hw_set_compliance(self, o, q, v):
        self.checked(":SENS%s:%s:PROT %s" % (o, _Q[q], fmt(v)))

    def hw_output(self, o):
        return self.qb(":OUTP%s?" % o)

    def hw_set_output(self, o, on):
        self.checked(":OUTP%s %s" % (o, "ON" if on else "OFF"))

    def hw_nplc(self, o, q):
        return self.qf(":SENS%s:%s:NPLC?" % (o, _Q[q]))

    def hw_set_nplc(self, o, q, v):
        self.checked(":SENS%s:%s:NPLC %s" % (o, _Q[q], fmt(v)))

    def hw_sense(self, o):
        return self.qb(":SENS%s:REM?" % o)

    def hw_set_sense(self, o, on):
        self.checked(":SENS%s:REM %s" % (o, "ON" if on else "OFF"))

    def read_extra(self, base, o):
        if base == "low":
            return "FLOAT" if self.q(":OUTP%s:LOW?" % o).upper().startswith("FLO") else "GROUND"
        return super().read_extra(base, o)

    def write_extra(self, base, o, value):
        if base == "low":
            self.checked(":OUTP%s:LOW %s" % (o, "FLO" if value == "FLOAT" else "GRO"))
            return
        super().write_extra(base, o, value)

    def _action_clear_status(self):
        self.w("*CLS")
