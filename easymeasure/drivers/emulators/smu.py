"""Emulated source-measure units: Keithley 2400 and Keysight B2900.

Both drive a resistive load (option ``load_ohm``, default 10 kΩ) and clamp at
the compliance like the real instruments do.
"""

from __future__ import annotations

from typing import Dict, List

from . import SCPIEmulator, SCPIError, fmt_num, parse_bool, parse_num, unquote
from ..simbus import FaultInjector


class _Output:
    def __init__(self):
        self.reset()

    def reset(self):
        self.func = "VOLT"
        self.level = {"VOLT": 0.0, "CURR": 0.0}
        self.prot = {"VOLT": 21.0, "CURR": 1.05e-4}
        self.nplc = 1.0
        self.on = False
        self.rsen = False
        self.low = "GRO"

    def measure(self, what: str, load: float) -> float:
        """Reading of quantity *what* (VOLT/CURR) at the load, compliance-clamped."""
        if not self.on:
            return 0.0
        if self.func == "VOLT":
            v = self.level["VOLT"]
            i = v / load
            lim = abs(self.prot["CURR"])
            if abs(i) > lim:                                 # in compliance
                i = lim if i > 0 else -lim
                v = i * load
            return v if what == "VOLT" else i
        i = self.level["CURR"]
        v = i * load
        lim = abs(self.prot["VOLT"])
        if abs(v) > lim:
            v = lim if v > 0 else -lim
            i = v / load
        return v if what == "VOLT" else i


def _func_arg(a: List[str]) -> str:
    if len(a) != 1:
        raise SCPIError(-109, "Missing parameter")
    v = unquote(a[0]).upper()
    if v in ("VOLT", "VOLTAGE", "VOLT:DC", "VOLTAGE:DC"):
        return "VOLT"
    if v in ("CURR", "CURRENT", "CURR:DC", "CURRENT:DC"):
        return "CURR"
    raise SCPIError(-224, "Illegal parameter value")


class Keithley2400Emulator(SCPIEmulator):
    """Command subset of the 2400 Series SourceMeter manual (chapter 18, SCPI)."""

    def __init__(self, options=None):
        o = options or {}
        super().__init__("KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C32   Oct  4 2010 14:20:11/A02  /K/J",
                         FaultInjector(o))
        self.load = float(o.get("load_ohm", 1e4))
        self.out = _Output()
        self.sense_func = "CURR"
        self.elements = ["VOLT", "CURR", "RES", "TIME", "STAT"]
        self.concurrent = True
        self.term = "FRON"
        self.ranges_auto: Dict[str, bool] = {}
        a = self.add
        S = "[:SOURce[1]]"
        a(S + ":FUNCtion[:MODE]", lambda x, n: self._set_func(_func_arg(x)))
        a(S + ":FUNCtion[:MODE]?", lambda x, n: self.out.func)
        for q in ("VOLTage", "CURRent"):
            k = q[:4].upper()
            a(S + ":%s[:LEVel][:IMMediate][:AMPLitude]" % q,
              lambda x, n, k=k: self._set_level(k, x))
            a(S + ":%s[:LEVel][:IMMediate][:AMPLitude]?" % q,
              lambda x, n, k=k: fmt_num(self.out.level[k]))
            a(S + ":%s:MODE" % q, lambda x, n: self._fixed(x))
            a(S + ":%s:RANGe:AUTO" % q,
              lambda x, n, k=k: self.ranges_auto.__setitem__("SOUR" + k, parse_bool(x[0])))
            a("[:SENSe[1]]:%s[:DC]:PROTection[:LEVel]" % q, lambda x, n, k=k: self._set_prot(k, x))
            a("[:SENSe[1]]:%s[:DC]:PROTection[:LEVel]?" % q, lambda x, n, k=k: fmt_num(self.out.prot[k]))
            a("[:SENSe[1]]:%s[:DC]:NPLCycles" % q, lambda x, n: self._set_nplc(x))
            a("[:SENSe[1]]:%s[:DC]:NPLCycles?" % q, lambda x, n: fmt_num(self.out.nplc))
            a("[:SENSe[1]]:%s[:DC]:RANGe:AUTO" % q,
              lambda x, n, k=k: self.ranges_auto.__setitem__("SENS" + k, parse_bool(x[0])))
        a("[:SENSe[1]]:FUNCtion:CONCurrent", lambda x, n: setattr(self, "concurrent", parse_bool(x[0])))
        a("[:SENSe[1]]:FUNCtion[:ON]", lambda x, n: setattr(self, "sense_func", _func_arg(x)))
        a(":FORMat:ELEMents[:SENSe[1]]", lambda x, n: self._elements(x))
        a(":OUTPut[1][:STATe]", lambda x, n: setattr(self.out, "on", parse_bool(x[0])))
        a(":OUTPut[1][:STATe]?", lambda x, n: "1" if self.out.on else "0")
        a(":READ?", lambda x, n: self._read())
        a(":SYSTem:RSENse", lambda x, n: setattr(self.out, "rsen", parse_bool(x[0])))
        a(":SYSTem:RSENse?", lambda x, n: "1" if self.out.rsen else "0")
        a(":ROUTe:TERMinals", lambda x, n: self._term(x))
        a(":ROUTe:TERMinals?", lambda x, n: self.term)
        a(":SYSTem:BEEPer[:IMMediate]", lambda x, n: None)

    def reset(self):
        self.out.reset()
        self.sense_func = "CURR"
        self.term = "FRON"

    def _next_error(self) -> str:                   # 2400 style: 0,"No error"
        r = super()._next_error()
        return r.replace("+0,", "0,", 1) if r.startswith("+0,") else r

    def _set_func(self, f):
        self.out.func = f

    def _fixed(self, x):
        if unquote(x[0]).upper() not in ("FIX", "FIXED"):
            raise SCPIError(-224, "Illegal parameter value")

    def _set_level(self, k, x):
        lim = 210.0 if k == "VOLT" else 1.05
        self.out.level[k] = parse_num(x[0], -lim, lim)

    def _set_prot(self, k, x):
        lo, hi = (-210.0, 210.0) if k == "VOLT" else (-1.05, 1.05)
        self.out.prot[k] = parse_num(x[0], lo, hi)

    def _set_nplc(self, x):
        self.out.nplc = parse_num(x[0], 0.01, 10.0)

    def _elements(self, x):
        els = [unquote(e).upper()[:4] for e in x]
        if not els or any(e not in ("VOLT", "CURR", "RES", "TIME", "STAT") for e in els):
            raise SCPIError(-224, "Illegal parameter value")
        self.elements = els

    def _term(self, x):
        v = x[0].upper()
        if v not in ("FRON", "FRONT", "REAR"):
            raise SCPIError(-224, "Illegal parameter value")
        self.term = "REAR" if v == "REAR" else "FRON"

    def _read(self):
        if not self.out.on:
            raise SCPIError(803, "Output disabled")          # and no reading at all
        vals = []
        for e in self.elements:
            if e in ("VOLT", "CURR"):
                vals.append(fmt_num(self.out.measure(e, self.load)))
            elif e == "RES":
                vals.append(fmt_num(9.91e37))
            elif e == "TIME":
                vals.append(fmt_num(0.0))
            else:
                vals.append(fmt_num(0.0))
        return ",".join(vals)


class B2900Emulator(SCPIEmulator):
    """Command subset of the Keysight B2900 SCPI command reference."""

    def __init__(self, options=None, outputs: int = 1):
        o = options or {}
        model = "B2902A" if outputs == 2 else "B2901A"
        super().__init__("Keysight Technologies,%s,MY51140123,3.4.2011.5100" % model, FaultInjector(o))
        self.load = float(o.get("load_ohm", 1e4))
        self.outs = {str(i + 1): _Output() for i in range(outputs)}
        self.auto_on = True             # :OUTP:ON:AUTO - measuring switches the output on!
        a = self.add
        a(":SOURce{n}:FUNCtion:MODE", lambda x, n: setattr(self._o(n), "func", _func_arg(x)))
        a(":SOURce{n}:FUNCtion:MODE?", lambda x, n: self._o(n).func)
        for q in ("VOLTage", "CURRent"):
            k = q[:4].upper()
            a("[:SOURce{n}]:%s[:LEVel][:IMMediate][:AMPLitude]" % q,
              lambda x, n, k=k: self._set_level(n, k, x))
            a("[:SOURce{n}]:%s[:LEVel][:IMMediate][:AMPLitude]?" % q,
              lambda x, n, k=k: fmt_num(self._o(n).level[k]))
            a(":SENSe{n}:%s[:DC]:PROTection[:LEVel][:BOTH]" % q,
              lambda x, n, k=k: self._set_prot(n, k, x))
            a(":SENSe{n}:%s[:DC]:PROTection[:LEVel][:BOTH]?" % q,
              lambda x, n, k=k: fmt_num(self._o(n).prot[k]))
            a(":SENSe{n}:%s[:DC]:NPLCycles" % q,
              lambda x, n: setattr(self._o(n), "nplc", parse_num(x[0], 4e-4, 100.0)))
            a(":SENSe{n}:%s[:DC]:NPLCycles?" % q, lambda x, n: fmt_num(self._o(n).nplc))
            a(":MEASure:%s[:DC]?" % q, lambda x, n, k=k: self._measure(k, x))
        a(":SENSe{n}:REMote", lambda x, n: setattr(self._o(n), "rsen", parse_bool(x[0])))
        a(":SENSe{n}:REMote?", lambda x, n: "1" if self._o(n).rsen else "0")
        a(":OUTPut{n}[:STATe]", lambda x, n: setattr(self._o(n), "on", parse_bool(x[0])))
        a(":OUTPut{n}[:STATe]?", lambda x, n: "1" if self._o(n).on else "0")
        a(":OUTPut{n}:LOW", lambda x, n: self._low(n, x))
        a(":OUTPut{n}:LOW?", lambda x, n: self._o(n).low)
        a(":OUTPut{n}:ON:AUTO", lambda x, n: setattr(self, "auto_on", parse_bool(x[0])))

    def reset(self):
        for o in self.outs.values():
            o.reset()

    def _o(self, n: str) -> _Output:
        o = self.outs.get(n or "1")
        if o is None:
            raise SCPIError(-114, "Header suffix out of range")
        return o

    def _set_level(self, n, k, x):
        lim = 210.0 if k == "VOLT" else 3.03
        self._o(n).level[k] = parse_num(x[0], -lim, lim)

    def _set_prot(self, n, k, x):
        lo, hi = (2e-2, 210.0) if k == "VOLT" else (1e-9, 3.03)
        self._o(n).prot[k] = parse_num(x[0], lo, hi)

    def _low(self, n, x):
        o = self._o(n)
        v = x[0].upper()
        if v not in ("GRO", "GROUND", "FLO", "FLOAT"):
            raise SCPIError(-224, "Illegal parameter value")
        if o.on:
            raise SCPIError(-221, "Settings conflict")      # change with the output off
        o.low = v[:3]

    def _measure(self, k, x):
        chans = ["1"]
        if x:
            t = x[0].strip()
            if not (t.startswith("(@") and t.endswith(")")):
                raise SCPIError(-104, "Data type error")
            chans = [c.strip() for c in t[2:-1].split(",")]
        out = []
        for c in chans:
            o = self._o(c)
            if not o.on and self.auto_on:
                o.on = True                                   # what the real one does
            out.append(fmt_num(o.measure(k, self.load)))
        return ",".join(out)
