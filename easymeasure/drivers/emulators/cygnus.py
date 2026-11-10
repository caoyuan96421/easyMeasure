"""Emulated Cygnus 16430 8-channel supply (Cao RQM Lab firmware, ``Core/Src/scpi.c``).

The command handling is a port of the firmware's ``dispatch_line``: the exact
verbs (``VOLT n,v``, ``OUTP? n``, ``MEAS:VOLT? n`` ... no SCPI long/short forms),
a one-slot error register read by ``SYST:ERR?`` (``0`` or ``-1,"<message>"``),
and - like the firmware - **no reply at all** to a query it rejects (bad channel,
unknown command), so a driver mistake shows up as a timeout plus an error.

Every output drives a resistive load (option ``load_ohm``, default 10 kΩ, or a
list of 8): with the relay on the channel regulates the voltage until the
current reaches its limit (then the voltage drops, constant current); with the
relay open the readback is the unloaded output node (setpoint, no current).
Readings follow the round-robin ADC: a channel's value is refreshed every
``8 · 24 · SF / 32768`` s (SF = ``RATE``), so a reading right after a change is
the old one (option ``emulate_latency``, default on).
"""

from __future__ import annotations

import math
import time
from typing import List, Optional

from ..simbus import FaultInjector, SimDevice

N = 8
VMAX = 150.0
IMAX = 0.040
IDN = "Cygnus-16430,8CH,0,1.0.0"


class Cygnus16430Emulator(SimDevice):
    def __init__(self, options=None):
        o = options or {}
        super().__init__(FaultInjector(o))
        load = o.get("load_ohm", 1e4)
        self.load: List[float] = [float(x) for x in load] if isinstance(load, (list, tuple)) \
            else [float(load)] * N
        self.latency = bool(o.get("emulate_latency", True))
        self.err = ""
        self.log: List[str] = []
        self.unknown: List[str] = []
        self.cal_runs = 0
        self.cal_seconds = float(o.get("cal_seconds", 0.2))
        self.reset()

    def reset(self) -> None:
        self.v_set = [0.0] * N
        self.i_set = [0.0] * N
        self.on = [False] * N
        self.sf = {"V": 13, "I": 13}
        self.alpha = {"VOLT": [1.0] * N, "CURR": [1.0] * N}
        self.changed = [0.0] * N
        self.prev = [(0.0, 0.0)] * N

    # -- physics ----------------------------------------------------------------------
    def _true(self, ch: int):
        if not self.on[ch]:
            return self.v_set[ch], 0.0               # relay open: the unloaded node
        r = self.load[ch]
        v = self.v_set[ch]
        i = v / r
        if i > self.i_set[ch]:                       # current limit: constant current
            i = self.i_set[ch]
            v = i * r
        return v, i

    def period(self, which: str) -> float:
        return N * 24.0 * self.sf[which] / 32768.0

    def _reading(self, ch: int):
        now = time.monotonic()
        if self.latency and now - self.changed[ch] < max(self.period("V"), self.period("I")):
            return self.prev[ch]                     # not converted since the change
        return self._true(ch)

    def _changing(self, ch: int) -> None:
        self.prev[ch] = self._reading(ch)
        self.changed[ch] = time.monotonic()

    # -- protocol ----------------------------------------------------------------------
    def _set_err(self, msg: str) -> None:
        self.err = msg

    @staticmethod
    def _args(s: str) -> List[str]:
        return [t.strip() for t in s.split(",")] if s.strip() else []

    def _chan(self, tok: Optional[str]) -> Optional[int]:
        try:
            v = int(tok)                                # strtol, 1..8
        except (TypeError, ValueError):
            return None
        return v - 1 if 1 <= v <= N else None

    def handle(self, command: str) -> Optional[str]:
        line = command.strip()
        if not line:
            return None
        self.log.append(line)
        parts = line.split(None, 1)
        verb = parts[0].upper()
        args = self._args(parts[1] if len(parts) > 1 else "")
        query = verb.endswith("?")
        if query:
            verb = verb[:-1]
        if verb == "*IDN":
            return IDN
        if verb == "*RST":
            self.reset()
            self.err = ""
            return None
        if verb == "OUTP":
            ch = self._chan(args[0] if args else None)
            if ch is None:
                return self._set_err("OUTP: bad channel")
            if query:
                return "1" if self.on[ch] else "0"
            val = args[1].upper() if len(args) > 1 else None
            if val not in ("ON", "1", "OFF", "0"):
                return self._set_err("OUTP: bad value")
            self._changing(ch)
            self.on[ch] = val in ("ON", "1")
            return None
        if verb in ("VOLTA", "CURRA"):
            name, mx = verb, (VMAX if verb == "VOLTA" else IMAX)
            if query:
                return self._set_err("%s: query not supported" % name)
            if len(args) != N:
                return self._set_err("%s: wrong argument count" % name)
            try:
                vals = [float(a) for a in args]
            except ValueError:
                return self._set_err("%s: bad value" % name)
            if any(v < 0 for v in vals):
                return self._set_err("%s: negative not allowed (one-quadrant supply)" % name)
            if any(v > mx for v in vals):
                return self._set_err("VOLTA: > 150.0 V max" if verb == "VOLTA"
                                     else "CURRA: > 40.0 mA max")
            for ch, v in enumerate(vals):
                self._changing(ch)
                (self.v_set if verb == "VOLTA" else self.i_set)[ch] = v
            return None
        if verb in ("VOLT", "CURR"):
            ch = self._chan(args[0] if args else None)
            if ch is None:
                return self._set_err("%s: bad channel" % verb)
            target = self.v_set if verb == "VOLT" else self.i_set
            if query:
                return "%.6f" % target[ch]
            try:
                value = float(args[1])
            except (IndexError, ValueError):
                return self._set_err("%s: bad value" % verb)
            if value < 0:
                return self._set_err("%s: negative not allowed (one-quadrant supply)" % verb)
            if value > (VMAX if verb == "VOLT" else IMAX):
                return self._set_err("VOLT: > 150.0 V max" if verb == "VOLT"
                                     else "CURR: > 40.0 mA max")
            self._changing(ch)
            target[ch] = value
            return None
        if verb == "RATE":
            which = args[0] if args else None
            if which not in ("V", "I"):
                return self._set_err("RATE: need V or I")
            if query:
                return "%u" % self.sf[which]
            try:
                sf = int(args[1])
            except (IndexError, ValueError):
                return self._set_err("RATE: bad SF value")
            if not 13 <= sf <= 255:
                return self._set_err("RATE: SF must be 13..255 (chop enabled)")
            self.sf[which] = sf
            return None
        if ":" in verb:
            root, sub = verb.split(":", 1)
            if root == "MEAS":
                if sub not in ("VOLT", "CURR"):
                    return self._set_err("MEAS: unknown sub-command")
                ch = self._chan(args[0] if args else None)
                if ch is None:
                    return self._set_err("MEAS:%s: bad channel" % sub)
                v, i = self._reading(ch)
                return "%.9f" % (v if sub == "VOLT" else i)
            if root == "FILT":
                ch = self._chan(args[0] if args else None)
                try:
                    alpha = float(args[1])
                except (IndexError, ValueError):
                    alpha = None
                if ch is None or alpha is None:
                    return self._set_err("FILT: bad args")
                if sub not in ("VOLT", "CURR"):
                    return self._set_err("FILT: bad axis")
                if not (0.0 < alpha <= 1.0) or math.isnan(alpha):
                    return self._set_err("FILT: set failed")
                self.alpha[sub][ch] = alpha
                return None
            if root == "CAL" and sub == "AUTO" and not query:
                if any(self.on):
                    return self._set_err("CAL:AUTO: failed (ITRIM full-range step)")
                time.sleep(self.cal_seconds)          # the firmware blocks while it sweeps
                self.cal_runs += 1
                return None
            if root == "SYST" and sub == "ERR":
                if not query:
                    return self._set_err("SYST:ERR? is query only")
                if not self.err:
                    return "0"
                r, self.err = '-1,"%s"' % self.err, ""
                return r
        self.unknown.append(line)
        return self._set_err("Unknown command")
