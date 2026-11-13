"""Emulated Agilent PNA series network analyzer (E8356A / E8357A / E8358A), for the
``e8358a`` driver.

The command set is the PNA programming guide's (firmware A.07): measurements are
created by name on a channel (``CALCulate{ch}:PARameter[:DEFine] 'name',S21``),
selected (``CALC:PAR:SEL``) and read as complex data (``CALC:DATA? SDATA``); the
sweep is configured under ``SENSe{ch}`` and the source under ``SOURce{ch}``.  A sweep
is started with ``SENSe:SWEep:MODE SINGle`` (or ``GROups`` with a group count, for
averaging) and takes about ``points / IF bandwidth`` seconds per direction; ``*OPC``
sets the operation-complete bit of ``*ESR?`` when it has finished.

The device under test is a hanger resonator on a transmission line: a dip in S21
at ``f0`` (option ``emulate_f0``, default 5 GHz), a cable delay, losses, and noise
that grows with the IF bandwidth and falls with power and averaging.  S12 = S21 and
S22 = S11 (a reciprocal, symmetric device).

Option ``time_scale`` divides the time a sweep takes (tests use a large one).
"""

from __future__ import annotations

import time

import numpy as np

from . import SCPIEmulator, SCPIError, compile_pattern, fmt_num, parse_bool, parse_num, unquote
from ..simbus import FaultInjector

MODELS = {"E8356A": 3e9, "E8357A": 6e9, "E8358A": 9e9}
F_MIN = 300e3
MAX_POINTS = 16001
IFBW = [1, 2, 3, 5, 7, 10, 15, 20, 30, 50, 70, 100, 150, 200, 300, 500, 700, 1e3, 1.5e3,
        2e3, 3e3, 5e3, 7e3, 10e3, 15e3, 20e3, 30e3, 35e3, 40e3]
PARAMS = ("S11", "S21", "S12", "S22")


class PNAEmulator(SCPIEmulator):
    def __init__(self, options=None):
        o = options or {}
        self.model = str(o.get("emulate_model", "E8358A")).upper()
        if self.model not in MODELS:
            raise ValueError("emulate_model must be one of %s" % ", ".join(MODELS))
        self.f_max = MODELS[self.model]
        super().__init__("Agilent Technologies,%s,US41140123,A.07.50.67" % self.model,
                         FaultInjector(o))
        self.time_scale = float(o.get("time_scale", 1.0))
        self.f0 = float(o.get("emulate_f0", 5e9))
        self.rng = np.random.default_rng(int(o.get("seed", 1)))
        self.fmt = "ASC"
        self.trig_source = "IMM"
        self.init_cont = True
        self.output = True
        self.done_at = 0.0               # when the running sweep ends
        self.opc_pending = False
        self.triggers = 0                # sweeps started (tests)
        self._fpreset()
        self.meas = {"CH1_S11_1": "S11"}  # power-on state: one S11 measurement
        self.feeds = {1: "CH1_S11_1"}
        self.selected = "CH1_S11_1"
        a = self.add
        # the sweep's operation-complete replaces the plain IEEE-488.2 handlers
        for pat, fn in (("*OPC", lambda x, n: self._opc()),
                        ("*OPC?", lambda x, n: self._opc_query()),
                        ("*ESR?", lambda x, n: self._esr())):
            self.handlers.insert(0, (compile_pattern(pat), fn))
        a(":SYSTem:FPReset", lambda x, n: self._fpreset())
        a(":SYSTem:PRESet", lambda x, n: self.reset())
        a(":FORMat[:DATA]", lambda x, n: self._format(x))
        a(":FORMat[:DATA]?", lambda x, n: "ASC,+0" if self.fmt == "ASC" else self.fmt)
        a(":TRIGger[:SEQuence]:SOURce", lambda x, n: self._trig(x))
        a(":TRIGger[:SEQuence]:SOURce?", lambda x, n: self.trig_source)
        a(":INITiate:CONTinuous", lambda x, n: setattr(self, "init_cont", parse_bool(self._one(x))))
        a(":INITiate:CONTinuous?", lambda x, n: "1" if self.init_cont else "0")
        a(":INITiate{n}[:IMMediate]", lambda x, n: self._initiate(n))
        a(":OUTPut[:STATe]", lambda x, n: setattr(self, "output", parse_bool(self._one(x))))
        a(":OUTPut[:STATe]?", lambda x, n: "1" if self.output else "0")
        # measurements
        a(":CALCulate{n}:PARameter[:DEFine]", lambda x, n: self._define(x, n))
        a(":CALCulate{n}:PARameter[:DEFine]:EXTended", lambda x, n: self._define(x, n))
        a(":CALCulate{n}:PARameter:CATalog?", lambda x, n: self._catalog(n))
        a(":CALCulate{n}:PARameter:SELect", lambda x, n: self._select(x, n))
        a(":CALCulate{n}:PARameter:DELete", lambda x, n: self._delete(x, n))
        a(":CALCulate{n}:PARameter:DELete:ALL", lambda x, n: self._delete_all(n))
        a(":CALCulate{n}:DATA?", lambda x, n: self._data(x, n))
        a(":DISPlay:WINDow{n}[:STATe]", lambda x, n: self._window(x, n))
        a(":DISPlay:WINDow{n}[:STATe]?", lambda x, n: "1" if self.window_on else "0")
        a(":DISPlay:WINDow{n}:TRACe{n}:FEED", lambda x, n: self._feed(x))
        # the sweep
        for key, attr, lo_hi in (("STARt", "start", None), ("STOP", "stop", None),
                                 ("CENTer", "center", None), ("SPAN", "span", None)):
            a(":SENSe{n}:FREQuency:" + key, lambda x, n, k=attr: self._freq(n, k, x))
            a(":SENSe{n}:FREQuency:" + key + "?", lambda x, n, k=attr: fmt_num(self._fget(k)))
        a(":SENSe{n}:SWEep:POINts", lambda x, n: self._points(x, n))
        a(":SENSe{n}:SWEep:POINts?", lambda x, n: "+%d" % self._ch(n, "points"))
        a(":SENSe{n}:SWEep:TIME?", lambda x, n: self._ch(n, "points") and fmt_num(self._sweep_time()))
        a(":SENSe{n}:SWEep:TYPE", lambda x, n: self._sweep_type(x, n))
        a(":SENSe{n}:SWEep:TYPE?", lambda x, n: self._ch(n, "sweep_type"))
        a(":SENSe{n}:SWEep:MODE", lambda x, n: self._sweep_mode(x, n))
        a(":SENSe{n}:SWEep:MODE?", lambda x, n: self._ch(n, "mode"))
        a(":SENSe{n}:SWEep:GROups:COUNt", lambda x, n: self._groups(x, n))
        a(":SENSe{n}:SWEep:GROups:COUNt?", lambda x, n: "+%d" % self._ch(n, "groups"))
        a(":SENSe{n}:BANDwidth[:RESolution]", lambda x, n: self._ifbw(x, n))
        a(":SENSe{n}:BANDwidth[:RESolution]?", lambda x, n: fmt_num(self._ch(n, "ifbw")))
        a(":SENSe{n}:BWIDth[:RESolution]", lambda x, n: self._ifbw(x, n))
        a(":SENSe{n}:BWIDth[:RESolution]?", lambda x, n: fmt_num(self._ch(n, "ifbw")))
        a(":SENSe{n}:AVERage[:STATe]", lambda x, n: self._set(n, "avg", parse_bool(self._one(x))))
        a(":SENSe{n}:AVERage[:STATe]?", lambda x, n: "1" if self._ch(n, "avg") else "0")
        a(":SENSe{n}:AVERage:COUNt", lambda x, n: self._set(
            n, "avg_count", int(parse_num(self._one(x), 1, 65536))))
        a(":SENSe{n}:AVERage:COUNt?", lambda x, n: "+%d" % self._ch(n, "avg_count"))
        a(":SENSe{n}:AVERage:CLEar", lambda x, n: self._set(n, "avg_done", 0))
        a(":SENSe{n}:CORRection[:STATe]", lambda x, n: self._correction(x, n))
        a(":SENSe{n}:CORRection[:STATe]?", lambda x, n: "1" if self._ch(n, "corr") else "0")
        a(":SOURce{n}:POWer{n}[:LEVel][:IMMediate][:AMPLitude]", lambda x, n: self._power(x, n))
        a(":SOURce{n}:POWer{n}[:LEVel][:IMMediate][:AMPLitude]?",
          lambda x, n: fmt_num(self._ch(n, "power")))

    # -- state ----------------------------------------------------------------------------
    def _fpreset(self):
        """SYSTem:FPReset: factory settings, no measurements, no windows."""
        self.start, self.stop, self.points, self.ifbw = 10e6, self.f_max, 201, 35e3
        self.power, self.sweep_type, self.mode, self.groups = 0.0, "LIN", "CONT", 1
        self.avg, self.avg_count, self.avg_done, self.corr = False, 1, 0, False
        self.output = True
        self.meas, self.feeds, self.selected = {}, {}, None
        self.window_on = False
        self.data = {}
        self.done_at = 0.0

    def reset(self):
        """*RST / SYSTem:PRESet: factory settings with one S11 measurement in window 1."""
        self._fpreset()
        self.meas = {"CH1_S11_1": "S11"}
        self.feeds = {1: "CH1_S11_1"}
        self.selected = "CH1_S11_1"
        self.window_on = True

    @staticmethod
    def _one(x):
        if len(x) != 1:
            raise SCPIError(-109, "Missing parameter")
        return x[0]

    def _ch(self, n, attr):
        if n not in ("", "1"):
            raise SCPIError(-114, "Header suffix out of range")   # one channel here
        return getattr(self, attr)

    def _set(self, n, attr, value):
        self._ch(n, attr)
        setattr(self, attr, value)

    def _busy(self):
        if time.monotonic() < self.done_at:
            # the PNA queues settings during a sweep; changing them aborts it here
            self.done_at = time.monotonic()

    # -- formats, triggers -----------------------------------------------------------------
    def _format(self, x):
        f = x[0].strip().upper()
        if f.startswith("ASC"):
            self.fmt = "ASC"
        elif f in ("REAL", "REAL32", "REAL64"):
            self.fmt = "REAL"                     # accepted; the emulator answers in ASCII
        else:
            raise SCPIError(-224, "Illegal parameter value")

    def _trig(self, x):
        v = self._one(x).strip().upper()
        v = {"IMMEDIATE": "IMM", "EXTERNAL": "EXT", "MANUAL": "MAN"}.get(v, v)
        if v not in ("IMM", "EXT", "MAN"):
            raise SCPIError(-224, "Illegal parameter value")
        self.trig_source = v

    def _opc(self):
        self.opc_pending = True

    def _opc_query(self):
        wait = self.done_at - time.monotonic()
        if wait > 0:
            time.sleep(min(wait, 60.0))
        return "+1"

    def _esr(self):
        v = self.esr
        if self.opc_pending and time.monotonic() >= self.done_at:
            v |= 1
            self.opc_pending = False
        self.esr = 0
        return "+%d" % v

    # -- measurements ----------------------------------------------------------------------
    def _define(self, x, n):
        self._ch(n, "meas")
        if len(x) < 2:
            raise SCPIError(-109, "Missing parameter")
        name, par = unquote(x[0]), unquote(x[1]).upper()
        if par not in PARAMS:
            raise SCPIError(-224, "Illegal parameter value")
        if name in self.meas:
            raise SCPIError(-221, "Settings conflict; measurement name already exists")
        self.meas[name] = par

    def _catalog(self, n):
        self._ch(n, "meas")
        if not self.meas:
            return '"NO CATALOG"'
        return '"%s"' % ",".join("%s,%s" % kv for kv in self.meas.items())

    def _select(self, x, n):
        self._ch(n, "meas")
        name = unquote(self._one(x))
        if name not in self.meas:
            raise SCPIError(-224, "Illegal parameter value; measurement not found")
        self.selected = name

    def _delete(self, x, n):
        self._ch(n, "meas")
        name = unquote(self._one(x))
        if self.meas.pop(name, None) is None:
            raise SCPIError(-224, "Illegal parameter value; measurement not found")
        self.feeds = {t: m for t, m in self.feeds.items() if m != name}
        if self.selected == name:
            self.selected = None

    def _delete_all(self, n):
        self._ch(n, "meas")
        self.meas, self.feeds, self.selected = {}, {}, None

    def _window(self, x, n):
        if n not in ("", "1"):
            raise SCPIError(-114, "Header suffix out of range")
        self.window_on = parse_bool(self._one(x))

    def _feed(self, x):
        if not self.window_on:
            raise SCPIError(-221, "Settings conflict; window not on")
        name = unquote(self._one(x))
        if name not in self.meas:
            raise SCPIError(-224, "Illegal parameter value; measurement not found")
        self.feeds[len(self.feeds) + 1] = name

    def _data(self, x, n):
        self._ch(n, "meas")
        kind = self._one(x).strip().upper()
        if kind not in ("SDATA", "FDATA"):
            raise SCPIError(-224, "Illegal parameter value")
        if self.selected is None:
            raise SCPIError(-221, "Settings conflict; no measurement selected")
        par = self.meas[self.selected]
        if time.monotonic() < self.done_at:
            # a query during a sweep waits for it (the PNA holds the bus)
            time.sleep(min(self.done_at - time.monotonic(), 60.0))
        s = self.data.get(par)
        if s is None or len(s) != self.points:
            s = self._measure()[par]
        if kind == "FDATA":                       # formatted: log magnitude (MLOG)
            return ",".join(fmt_num(v) for v in 20 * np.log10(np.abs(s) + 1e-30))
        out = np.empty(2 * len(s))
        out[0::2], out[1::2] = s.real, s.imag
        return ",".join(fmt_num(v) for v in out)

    # -- the sweep -------------------------------------------------------------------------
    def _fget(self, k):
        if k == "center":
            return (self.start + self.stop) / 2
        if k == "span":
            return self.stop - self.start
        return getattr(self, k)

    def _freq(self, n, k, x):
        self._ch(n, "start")
        v = parse_num(self._one(x))
        start, stop = self.start, self.stop
        if k == "start":
            start = v
            stop = max(stop, start)
        elif k == "stop":
            stop = v
            start = min(start, stop)
        elif k == "center":
            half = (stop - start) / 2
            start, stop = v - half, v + half
        else:
            c = (start + stop) / 2
            start, stop = c - v / 2, c + v / 2
        if start < F_MIN or stop > self.f_max:
            raise SCPIError(-222, "Data out of range")
        self._busy()
        self.start, self.stop = start, stop

    def _points(self, x, n):
        self._set(n, "points", int(round(parse_num(self._one(x), 1, MAX_POINTS))))
        self._busy()

    def _sweep_type(self, x, n):
        v = self._one(x).strip().upper()
        v = {"LINEAR": "LIN", "LOGARITHMIC": "LOG", "SEGMENT": "SEGM"}.get(v, v)
        if v not in ("LIN", "LOG", "POW", "CW", "SEGM"):
            raise SCPIError(-224, "Illegal parameter value")
        self._set(n, "sweep_type", v)

    def _ifbw(self, x, n):
        v = parse_num(self._one(x), 1, 40e3)
        best = min(IFBW, key=lambda b: abs(b - v))          # the PNA rounds to its list
        self._set(n, "ifbw", float(best))
        self._busy()

    def _power(self, x, n):
        self._set(n, "power", parse_num(self._one(x), -90.0, 10.0))

    def _correction(self, x, n):
        on = parse_bool(self._one(x))
        if on:
            raise SCPIError(-221, "Settings conflict; no calibration for this channel")
        self._set(n, "corr", False)

    def _groups(self, x, n):
        self._set(n, "groups", int(parse_num(self._one(x), 1, 2000000)))

    def _sweep_time(self) -> float:
        """Seconds per direction (a real PNA adds band crossings, retrace ...)."""
        return self.points / self.ifbw * 1.2 + 0.002 * self.points + 0.01

    def _directions(self) -> int:
        fwd = any(p in ("S11", "S21") for p in self.meas.values())
        rev = any(p in ("S12", "S22") for p in self.meas.values())
        return 2 if (fwd and rev) else 1

    def _sweep_mode(self, x, n):
        v = self._one(x).strip().upper()
        v = {"SINGLE": "SING", "GROUPS": "GRO", "CONTINUOUS": "CONT"}.get(v, v)
        if v not in ("HOLD", "CONT", "GRO", "SING"):
            raise SCPIError(-224, "Illegal parameter value")
        self._set(n, "mode", v)
        if v in ("SING", "GRO"):
            if self.trig_source != "IMM" or not self.init_cont:
                return                            # waits for a trigger (not emulated)
            self._start(self.groups if v == "GRO" else 1)

    def _initiate(self, n):
        if n not in ("", "1"):
            raise SCPIError(-114, "Header suffix out of range")
        if self.init_cont:
            raise SCPIError(-213, "Init ignored")
        self._start(1)

    def _start(self, sweeps: int):
        self.triggers += 1
        t = self._sweep_time() * self._directions() * sweeps / self.time_scale
        self.done_at = time.monotonic() + t
        n_avg = self.avg_count if self.avg else 1
        self.data = self._measure(averages=max(1, min(sweeps, n_avg)))

    def freqs(self) -> np.ndarray:
        if self.sweep_type == "LOG":
            return np.geomspace(self.start, self.stop, self.points)
        return np.linspace(self.start, self.stop, self.points)

    def _measure(self, averages: int = 1) -> dict:
        f = self.freqs()
        q, qc, loss_db, tau = 2e4, 4e4, -25.0, 45e-9
        x = 2j * q * (f - self.f0) / self.f0
        line = 10 ** (loss_db / 20) * np.exp(-2j * np.pi * f * tau)
        s21 = line * (1 - (q / qc) / (1 + x))
        s11 = 0.08 * line * (q / qc) / (1 + x) + 0.02 * np.exp(-2j * np.pi * f * 3e-9)
        if not self.output:
            s21, s11 = 0 * s21, 0 * s11
        sigma = 1e-4 * np.sqrt(self.ifbw / 1e3) * 10 ** (-self.power / 20) / np.sqrt(averages)
        out = {}
        for p, s in (("S21", s21), ("S12", s21), ("S11", s11), ("S22", s11)):
            noise = self.rng.normal(0, sigma, len(f)) + 1j * self.rng.normal(0, sigma, len(f))
            out[p] = s + noise
        return out
