"""Emulated HP 34401A multimeter (SCPI subset of the 34401A user's guide, ch. 4).

The "inputs" carry fixed signals (options ``vdc``, ``vac``, ``idc``, ``iac``,
``ohm``); a reading above 120 % of a fixed range gives the overload value
9.90000000E+37 like the real meter.
"""

from __future__ import annotations

import random

from . import SCPIEmulator, SCPIError, fmt_num, parse_bool, parse_num, unquote
from ..simbus import FaultInjector

_FUNCS = {"VOLT": "VOLT:DC", "VOLT:DC": "VOLT:DC", "VOLTAGE": "VOLT:DC", "VOLTAGE:DC": "VOLT:DC",
          "VOLT:AC": "VOLT:AC", "VOLTAGE:AC": "VOLT:AC",
          "CURR": "CURR:DC", "CURR:DC": "CURR:DC", "CURRENT": "CURR:DC", "CURRENT:DC": "CURR:DC",
          "CURR:AC": "CURR:AC", "CURRENT:AC": "CURR:AC",
          "RES": "RES", "RESISTANCE": "RES", "FRES": "FRES", "FRESISTANCE": "FRES"}
_RANGES = {"VOLT:DC": [0.1, 1, 10, 100, 1000], "VOLT:AC": [0.1, 1, 10, 100, 750],
           "CURR:DC": [0.01, 0.1, 1, 3], "CURR:AC": [1, 3],
           "RES": [1e2, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8], "FRES": [1e2, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8]}
#: what the 34401A answers to FUNC? (DC functions without the :DC suffix)
_FUNC_REPLY = {"VOLT:DC": '"VOLT"', "VOLT:AC": '"VOLT:AC"', "CURR:DC": '"CURR"',
               "CURR:AC": '"CURR:AC"', "RES": '"RES"', "FRES": '"FRES"'}


class HP34401Emulator(SCPIEmulator):
    def __init__(self, options=None):
        o = options or {}
        super().__init__("HEWLETT-PACKARD,34401A,0,11-5-2", FaultInjector(o))
        self.signal = {"VOLT:DC": float(o.get("vdc", 1.234567)), "VOLT:AC": float(o.get("vac", 0.5)),
                       "CURR:DC": float(o.get("idc", 1.2e-3)), "CURR:AC": float(o.get("iac", 3e-4)),
                       "RES": float(o.get("ohm", 1000.0)), "FRES": float(o.get("ohm", 1000.0)) - 0.3}
        self.reset()
        self.remote = False
        self.readings = 0
        a = self.add
        a("[:SENSe]:FUNCtion[:ON]", lambda x, n: self._func(x))
        a("[:SENSe]:FUNCtion[:ON]?", lambda x, n: _FUNC_REPLY[self.func])
        for key, hdr in (("VOLT:DC", "[:SENSe]:VOLTage[:DC]"), ("VOLT:AC", "[:SENSe]:VOLTage:AC"),
                         ("CURR:DC", "[:SENSe]:CURRent[:DC]"), ("CURR:AC", "[:SENSe]:CURRent:AC"),
                         ("RES", "[:SENSe]:RESistance"), ("FRES", "[:SENSe]:FRESistance")):
            a(hdr + ":RANGe", lambda x, n, k=key: self._range(k, x))
            a(hdr + ":RANGe?", lambda x, n, k=key: fmt_num(self.range[k]))
            a(hdr + ":RANGe:AUTO", lambda x, n, k=key: self.auto.__setitem__(k, parse_bool(x[0])))
            a(hdr + ":RANGe:AUTO?", lambda x, n, k=key: "1" if self.auto[k] else "0")
            if key not in ("VOLT:AC", "CURR:AC"):
                a(hdr + ":NPLCycles", lambda x, n, k=key: self._nplc(k, x))
                a(hdr + ":NPLCycles?", lambda x, n, k=key: fmt_num(self.nplc[k]))
        a("[:SENSe]:DETector:BANDwidth", lambda x, n: self._band(x))
        a("[:SENSe]:DETector:BANDwidth?", lambda x, n: fmt_num(self.band))
        a("[:SENSe]:ZERO:AUTO", lambda x, n: self._zero(x))
        a("[:SENSe]:ZERO:AUTO?", lambda x, n: "1" if self.zero else "0")
        a(":TRIGger:SOURce", lambda x, n: self._trig(x))
        a(":TRIGger:COUNt", lambda x, n: self._count(x))
        a(":SAMPle:COUNt", lambda x, n: self._count(x))
        a(":READ?", lambda x, n: self._read())
        a(":SYSTem:REMote", lambda x, n: setattr(self, "remote", True))
        a(":SYSTem:BEEPer[:IMMediate]", lambda x, n: None)

    def reset(self):
        self.func = "VOLT:DC"
        self.range = {k: v[-1] for k, v in _RANGES.items()}
        self.auto = {k: True for k in _RANGES}
        self.nplc = {k: 10.0 for k in _RANGES}
        self.band = 20.0
        self.zero = True

    def _func(self, x):
        if len(x) != 1:
            raise SCPIError(-109, "Missing parameter")
        f = _FUNCS.get(unquote(x[0]).upper())
        if f is None:
            raise SCPIError(-224, "Illegal parameter value")
        self.func = f

    def _range(self, k, x):
        r = parse_num(x[0], specials={"MIN": _RANGES[k][0], "MAX": _RANGES[k][-1]})
        fit = [v for v in _RANGES[k] if v >= r * 0.999]
        if not fit:
            raise SCPIError(-222, "Data out of range")
        self.range[k] = fit[0]
        self.auto[k] = False

    def _nplc(self, k, x):
        v = parse_num(x[0], specials={"MIN": 0.02, "MAX": 100.0})
        fit = [p for p in (0.02, 0.2, 1, 10, 100) if p >= v * 0.999]
        if not fit:
            raise SCPIError(-222, "Data out of range")
        self.nplc[k] = float(fit[0])

    def _band(self, x):
        v = parse_num(x[0], specials={"MIN": 3.0, "MAX": 200.0})
        fit = [b for b in (3.0, 20.0, 200.0) if b >= v * 0.999]
        if not fit:
            raise SCPIError(-222, "Data out of range")
        self.band = fit[0]

    def _zero(self, x):
        v = x[0].upper()
        if v not in ("ON", "OFF", "ONCE", "1", "0"):
            raise SCPIError(-224, "Illegal parameter value")
        self.zero = v in ("ON", "1")

    @staticmethod
    def _count(x):
        parse_num(x[0], 1, 50000)

    def _trig(self, x):
        if x[0].upper() not in ("IMM", "IMMEDIATE", "BUS", "EXT", "EXTERNAL"):
            raise SCPIError(-224, "Illegal parameter value")

    def _read(self):
        self.readings += 1
        v = self.signal[self.func]
        v = v * (1 + 2e-6 * random.gauss(0, 1))
        if not self.auto[self.func] and abs(v) > 1.2 * self.range[self.func]:
            return fmt_num(9.9e37)
        return fmt_num(v)
