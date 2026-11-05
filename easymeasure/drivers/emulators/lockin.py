"""Emulated SR830 / SR850 lock-in amplifier (command set of the manuals' chapter 5).

Option ``emulate_model`` = ``"SR830"`` (default) or ``"SR850"``.  The emulated
sample returns a signal proportional to the sine output with a fixed phase, and
the aux inputs read back half of the aux outputs (a "loop-back cable").
"""

from __future__ import annotations

import math

from . import SCPIEmulator, SCPIError, fmt_num, parse_num
from ..simbus import FaultInjector

SIGNAL_GAIN = 1e-3        # V of signal per V rms of sine output (voltage input)
SIGNAL_PHASE = 30.0       # deg


class SR830Emulator(SCPIEmulator):
    def __init__(self, options=None):
        o = options or {}
        self.model = str(o.get("emulate_model", "SR830")).upper()
        super().__init__("Stanford_Research_Systems,%s,s/n48261,ver1.07" % self.model,
                         FaultInjector(o))
        self.reset()
        self.ddef = {1: (0, 0), 2: (0, 0)}
        self.trcd = {i: (i, 0, 0, 0) for i in range(1, 5)}
        self.outx = None
        self.busy_polls = 0
        a = self.add
        a("OUTX", lambda x, n: setattr(self, "outx", self._int(x, 0, 1)))
        a("OUTX?", lambda x, n: str(self.outx))
        a("OUTP?", lambda x, n: fmt_num(self._outp(self._int(x, 1, 4))))
        a("OAUX?", lambda x, n: fmt_num(self.auxv[self._int(x, 1, 4)] * 0.5))
        a("AUXV", lambda x, n: self._auxv(x))
        a("AUXV?", lambda x, n: fmt_num(self.auxv[self._int(x, 1, 4)]))
        a("SLVL", lambda x, n: self._set("slvl", parse_num(self._one(x), 0.004, 5.0)))
        a("SLVL?", lambda x, n: fmt_num(self.slvl))
        a("FREQ", lambda x, n: self._freq(x))
        a("FREQ?", lambda x, n: fmt_num(self.freq))
        a("PHAS", lambda x, n: self._phas(x))
        a("PHAS?", lambda x, n: fmt_num(self.phas))
        hi_harm = 32767 if self.model == "SR850" else 19999
        a("HARM", lambda x, n: self._set("harm", self._int(x, 1, hi_harm)))
        a("HARM?", lambda x, n: str(self.harm))
        for cmd, attr, hi in (("FMOD", "fmod", 2 if self.model == "SR850" else 1),
                              ("SENS", "sens", 26), ("OFLT", "oflt", 19), ("OFSL", "ofsl", 3),
                              ("ICPL", "icpl", 1), ("IGND", "ignd", 1), ("ILIN", "ilin", 3),
                              ("ISRC", "isrc", 2 if self.model == "SR850" else 3),
                              ("RMOD", "rmod", 2), ("SYNC", "sync", 1)):
            a(cmd, lambda x, n, attr=attr, hi=hi: self._set(attr, self._int(x, 0, hi)))
            a(cmd + "?", lambda x, n, attr=attr: str(getattr(self, attr)))
        if self.model == "SR850":
            a("IGAN", lambda x, n: self._set("igan", self._int(x, 0, 1)))
            a("IGAN?", lambda x, n: str(self.igan))
            a("TRCD", lambda x, n: self._trcd(x))
            a("TRCD?", lambda x, n: ",".join(str(v) for v in self.trcd[self._int(x, 1, 4)]))
            a("OUTR?", lambda x, n: fmt_num(self._trace(self._int(x, 1, 4))))
        else:
            a("DDEF", lambda x, n: self._ddef(x))
            a("DDEF?", lambda x, n: "%d,%d" % self.ddef[self._int(x, 1, 2)])
            a("OUTR?", lambda x, n: fmt_num(self._display(self._int(x, 1, 2))))
        a("AGAN", lambda x, n: self._agan())
        a("APHS", lambda x, n: self._aphs())
        a("ARSV", lambda x, n: self._busy())
        a("*STB?", lambda x, n: self._stb(x))

    def reset(self):
        self.slvl, self.freq, self.phas, self.harm = 1.0, 1000.0, 0.0, 1
        self.fmod = 0 if self.model == "SR850" else 1      # internal reference
        self.sens, self.oflt, self.ofsl = 26, 8, 1
        self.icpl, self.ignd, self.ilin, self.isrc, self.rmod, self.sync = 0, 0, 0, 0, 1, 0
        self.igan = 0
        self.auxv = {i: 0.0 for i in range(1, 5)}

    # -- argument helpers --------------------------------------------------------------
    @staticmethod
    def _one(x):
        if len(x) != 1:
            raise SCPIError(-109, "Missing parameter")
        return x[0]

    def _int(self, x, lo, hi, k=0):
        if len(x) <= k:
            raise SCPIError(-109, "Missing parameter")
        try:
            v = float(x[k])
        except ValueError:
            raise SCPIError(-104, "Data type error") from None
        if v != int(v) or not lo <= v <= hi:
            raise SCPIError(-222, "Data out of range")
        return int(v)

    def _set(self, attr, v):
        setattr(self, attr, v)

    # -- physics -----------------------------------------------------------------------
    def _current_input(self):
        return self.isrc >= 2

    def _xy(self):
        amp = self.slvl * SIGNAL_GAIN * (1e-6 if self._current_input() else 1.0)
        th = math.radians(SIGNAL_PHASE - self.phas)
        return amp * math.cos(th), amp * math.sin(th)

    def _outp(self, i):
        x, y = self._xy()
        return {1: x, 2: y, 3: math.hypot(x, y), 4: math.degrees(math.atan2(y, x))}[i]

    def _noise(self, k):
        return (12e-9 if k == 0 else 11e-9) * (1e-6 if self._current_input() else 1.0)

    def _display(self, ch):
        j, _ = self.ddef[ch]
        if j == 0:
            return self._outp(1 if ch == 1 else 2)
        if j == 1:
            return self._outp(3 if ch == 1 else 4)
        if j == 2:
            return self._noise(ch - 1)
        return self.auxv[(1 if ch == 1 else 3) + (j - 3)] * 0.5

    def _trace(self, i):
        j, k, l, _ = self.trcd[i]

        def qty(c):
            if c == 0:
                return 1.0
            if 1 <= c <= 4:
                return self._outp(c)
            if c in (5, 6):
                return self._noise(c - 5)
            if c == 7:
                return math.hypot(self._noise(0), self._noise(1))
            if 8 <= c <= 11:
                return self.auxv[c - 7] * 0.5
            return self.freq
        return qty(j) * qty(k) / qty(l)

    # -- commands -----------------------------------------------------------------------
    def _auxv(self, x):
        i = self._int(x, 1, 4)
        if len(x) < 2:
            raise SCPIError(-109, "Missing parameter")
        self.auxv[i] = parse_num(x[1], -10.5, 10.5)

    def _freq(self, x):
        internal = self.fmod == (0 if self.model == "SR850" else 1)
        if not internal:
            raise SCPIError(-221, "Settings conflict")     # only with the internal reference
        self.freq = parse_num(self._one(x), 0.001, 102000.0)

    def _phas(self, x):
        v = parse_num(self._one(x), -360.0, 729.99)
        self.phas = (v + 180.0) % 360.0 - 180.0

    def _ddef(self, x):
        ch = self._int(x, 1, 2)
        self.ddef[ch] = (self._int(x, 0, 4, 1), self._int(x, 0, 2, 2) if len(x) > 2 else 0)

    def _trcd(self, x):
        i = self._int(x, 1, 4)
        self.trcd[i] = (self._int(x, 0, 12, 1), self._int(x, 0, 12, 2), self._int(x, 0, 24, 3),
                        self._int(x, 0, 1, 4) if len(x) > 4 else 0)

    def _busy(self):
        self.busy_polls = 3          # "no command in progress" clears for a few polls

    def _agan(self):
        r = abs(complex(*self._xy()))
        full = [m * 10.0 ** e for e in range(-9, 1) for m in (2, 5, 10)][:27]
        if self._current_input():
            full = [f * 1e-6 for f in full]
        self.sens = next((i for i, f in enumerate(full) if f >= 1.2 * r), 26)
        self._busy()

    def _aphs(self):
        self.phas = SIGNAL_PHASE
        self._busy()

    def _stb(self, x):
        if x and self._int(x, 0, 7) == 1:
            if self.busy_polls > 0:
                self.busy_polls -= 1
                return "0"
            return "1"
        return "2" if self.busy_polls == 0 else "0"
