"""Emulated HP 4145B, or HP 4155/4156 with its command sets, in HP-IB user mode.

Follows the protocol of a 4155 driver verified on the hardware (see
``drivers/hp4145.py``).

HP 4145 command set: ``BC``, ``US``, ``ID``, ``DP1``, ``EI1``, ``IT1`` … ``IT3``,
``DV<n>,<range>,<V>,<I comp>`` / ``DI<n>,<range>,<I>,<V comp>`` (SMU n = 1 … 4;
``DV<n>`` alone disables it), ``DS<k>,<V>`` (voltage source VS1 / VS2; ``DS<k>``
alone disables it), ``TV<n>`` / ``TI<n>`` (5, 6: the voltmeters, wired to VS1 /
VS2).  Several commands go in one message, separated by ``;``.  A reading is
``<status><channel letter><V|I><value>``, e.g. ``NAI+1.0000E-03`` (``N`` normal,
``C`` at its compliance).  SCPI (``*IDN?``, ``CMD?``, ``:…``) is not understood in
this command set: ``errors`` gets it and nothing is answered (as the hardware: a
beep, and the query times out).

A 4155/4156 in its own command set (option ``emulate_command_set`` = ``SCPI`` or
``FLEX``) understands ``*IDN?``, ``CMD?`` (0 SCPI, 1 FLEX), ``:PAGE`` (FLEX -> SCPI)
and ``:SYST:LANG COMP`` (-> HP 4145 command set, ``emulate_switch_time`` s during
which everything is lost); 4145 commands are not understood there (``unknown``).

SMUs 1 … 4 drive a resistor each (option ``load_ohm``) and clamp at their
compliance.  Commands out of range go to ``errors`` and change nothing; commands
the emulator does not know go to ``unknown``.  Options: ``emulate_model``
(``4145B``, ``4155A``/``B``/``C``, ``4156C``), ``emulate_host`` (the 4155's HOSTC
firmware revision, for ``ID``).
"""

from __future__ import annotations

import re
import time
from typing import List, Optional

from ..simbus import FaultInjector, SimDevice

V_RANGE = {0: 100.0, -1: 2.0, 1: 20.0, 2: 40.0, 3: 100.0}
V_COMP = {-1: 0.1, 1: 0.1, 2: 0.05, 3: 0.02}
I_RANGE = {0: 0.1, **{k: 10.0 ** (k - 10) for k in range(1, 10)}}
_LANG = re.compile(r":SYST(EM)?:LANG(UAGE)?\s+COMP(ATIBILITY)?\s*$", re.I)


class _SMU:
    def __init__(self):
        self.mode: Optional[str] = None     # None (disabled) | "V" | "I"
        self.value = 0.0
        self.comp = 0.0


class HP4145Emulator(SimDevice):
    def __init__(self, options=None):
        o = options or {}
        super().__init__(FaultInjector(o))
        self.model = str(o.get("emulate_model", "4145B")).upper()
        self.host = str(o.get("emulate_host", "01.04"))
        cs = str(o.get("emulate_command_set", "HP4145")).upper()
        self.command_set = cs if self.model.startswith("415") else "HP4145"
        self.switch_time = float(o.get("emulate_switch_time", 1.0))
        self._switch_until = 0.0
        self.load = float(o.get("load_ohm", 1e3))
        self.smu = {n: _SMU() for n in range(1, 5)}
        self.vs = {1: None, 2: None}
        self.user_mode = False
        self.it = 1
        self.dp = self.ei = False
        self.log: List[str] = []
        self.unknown: List[str] = []
        self.errors: List[str] = []

    def idn(self) -> str:
        return "HEWLETT-PACKARD,%s,0,%s:01.00" % (self.model, self.host)

    def handle(self, command: str):
        self.log.append(command)
        if time.monotonic() < self._switch_until:
            return None                              # resetting into the 4145 set
        replies = []
        for part in command.split(";"):
            c = part.strip()
            if not c:
                continue
            r = self._one(c)
            if r is not None:
                replies.append(r)
        return ";".join(replies) if replies else None

    def _one(self, c: str):
        if self.command_set != "HP4145":
            if c == "*IDN?":
                return self.idn()
            if c.upper() == "CMD?":
                return "0" if self.command_set == "SCPI" else "1"
            if self.command_set == "FLEX" and c.upper() == ":PAGE":
                self.command_set = "SCPI"
                return None
            if self.command_set == "SCPI" and _LANG.match(c):
                self.command_set = "HP4145"
                self.user_mode = False
                self._switch_until = time.monotonic() + self.switch_time
                return None
            self.unknown.append(c)
            return None
        if c.startswith((":", "*")) or c.upper() == "CMD?":
            self.errors.append("invalid character (SCPI in the 4145 command set): " + c)
            return None
        m = re.fullmatch(r"([A-Z]{2})\s*(.*)", c.upper())
        if not m:
            self.unknown.append(c)
            return None
        head, rest = m.group(1), m.group(2)
        args = [a.strip() for a in rest.split(",")] if rest.strip() else []
        try:
            return self._dispatch(head, args, c)
        except (ValueError, IndexError):
            self.errors.append("syntax: " + c)
            return None

    def _dispatch(self, head, args, command):
        if head == "ID":
            return "HP%s" % self.model if self.model.startswith("4145") else self.idn()
        if head == "US":
            self.user_mode = True
            return None
        if head == "BC":
            return None
        if head in ("DP", "EI") and not self.model.startswith("4145"):
            setattr(self, head.lower(), args == ["1"])
            return None
        if head == "IT":
            n = int(args[0])
            if n not in (1, 2, 3):
                raise ValueError
            self.it = n
            return None
        if head not in ("DV", "DI", "DS", "TV", "TI"):
            self.unknown.append(command)
            return None
        if not self.user_mode:
            self.errors.append("not in user mode: " + command)
            return None
        if head in ("DV", "DI"):
            ch = int(args[0])
            if ch not in self.smu:
                raise ValueError
            if len(args) == 1:
                if head != "DV":
                    raise ValueError
                self.smu[ch].mode = None          # DV<n>: disabled
                return None
            rng, val, comp = int(args[1]), float(args[2]), float(args[3])
            if head == "DV":
                if rng == -1 and self.model.startswith("4145"):
                    raise ValueError
                full = V_RANGE[rng] if rng else next(
                    V_RANGE[r] for r in (-1, 1, 2, 3) if abs(val) <= V_RANGE[r])
                code = rng or next(r for r in (-1, 1, 2, 3) if abs(val) <= V_RANGE[r])
                if abs(val) > full or comp > V_COMP[code]:
                    self.errors.append("out of range: " + command)
                    return None
            else:
                if rng not in I_RANGE or abs(val) > I_RANGE[rng]:
                    self.errors.append("out of range: " + command)
                    return None
                vmax = 100.0 if abs(val) <= 0.02 else 40.0 if abs(val) <= 0.05 else 20.0
                if comp > vmax:
                    self.errors.append("out of range: " + command)
                    return None
            s = self.smu[ch]
            s.mode, s.value, s.comp = head[1], val, comp
            return None
        if head == "DS":
            k = int(args[0])
            if k not in self.vs:
                raise ValueError
            if len(args) == 1:
                self.vs[k] = None
                return None
            v = float(args[1])
            if abs(v) > 20.0:
                self.errors.append("out of range: " + command)
                return None
            self.vs[k] = v
            return None
        ch = int(args[0])
        if head == "TV" and ch in (5, 6):
            return "N%sV%+.5E" % ("EF"[ch - 5], self.vs[ch - 4] or 0.0)
        if ch not in self.smu:
            raise ValueError
        v, i, comp = self._operate(self.smu[ch])
        x = v if head == "TV" else i
        return "%s%s%s%+.5E" % ("C" if comp else "N", "ABCD"[ch - 1], head[1], x)

    def _operate(self, s: _SMU):
        """(V, I, in_compliance) at the load."""
        if s.mode is None:
            return 0.0, 0.0, False
        if s.mode == "V":
            v, i = s.value, s.value / self.load
            if abs(i) > abs(s.comp):
                i = abs(s.comp) if i > 0 else -abs(s.comp)
                return i * self.load, i, True
            return v, i, False
        i, v = s.value, s.value * self.load
        if abs(v) > abs(s.comp):
            v = abs(s.comp) if v > 0 else -abs(s.comp)
            return v, v / self.load, True
        return v, i, False
