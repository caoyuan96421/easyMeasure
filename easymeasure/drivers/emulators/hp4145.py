"""Emulated HP 4145B (or 4155C in 4145 syntax mode) in HP-IB user mode.

Commands: ``ID``, ``US``, ``IT1``-``IT3``, ``BC``, ``DR<n>``, ``DV ch,range,value,comp``,
``DI ch,range,value,comp``, ``DS [ch]``, ``TV ch``, ``TI ch`` (and ``CMD?`` on the
4155).  SMUs 1-4 drive a resistor each (option ``load_ohm``) and clamp at their
compliance; VM1 / VM2 (channels 5, 6 for TV) read VS1 / VS2 (channels 5, 6 for
DV).  A measurement answers ``<status><channel><V|I><value>``, e.g.
``NAI+1.0000E-03`` (status ``N`` normal, ``C`` in compliance).  Commands that are
not allowed or out of range set ``errors`` and change nothing; commands the
emulator does not know go to ``unknown``.

Option ``emulate_model``: ``4145B`` (default) or ``4155C``; ``emulate_cmd_mode``:
the 4155's command mode (2 = 4145 syntax; anything else and it ignores 4145
commands, like a 4155 left in SCPI mode).
"""

from __future__ import annotations

import re
from typing import List

from ..simbus import FaultInjector, SimDevice

V_RANGE = {0: 100.0, 1: 20.0, 2: 40.0, 3: 100.0}
I_RANGE = {0: 0.1, **{k: 10.0 ** (k - 10) for k in range(1, 10)}}
ENVELOPE = [(20.0, 0.1), (40.0, 0.05), (100.0, 0.02)]


class _SMU:
    def __init__(self):
        self.mode = None            # None (disabled) | "V" | "I"
        self.value = 0.0
        self.comp = 0.0


class HP4145Emulator(SimDevice):
    def __init__(self, options=None):
        o = options or {}
        super().__init__(FaultInjector(o))
        self.model = str(o.get("emulate_model", "4145B")).upper()
        self.cmd_mode = int(o.get("emulate_cmd_mode", 2))
        self.load = float(o.get("load_ohm", 1e3))
        self.smu = {n: _SMU() for n in range(1, 5)}
        self.vs = {5: None, 6: None}
        self.user_mode = False
        self.it = 1
        self.log: List[str] = []
        self.unknown: List[str] = []
        self.errors: List[str] = []

    def handle(self, command: str):
        self.log.append(command)
        c = command.strip()
        if c.upper() == "ID":
            return "HP%s" % self.model
        if self.model.startswith("415") and c.upper() == "CMD?":
            return str(self.cmd_mode)
        if self.model.startswith("415") and self.cmd_mode != 2:
            self.unknown.append(command)            # SCPI mode: 4145 commands mean nothing
            return None
        m = re.fullmatch(r"([A-Z]{2})\s*(.*)", c.upper())
        if not m:
            self.unknown.append(command)
            return None
        head, args = m.group(1), [a.strip() for a in m.group(2).split(",") if a.strip()]
        try:
            return self._dispatch(head, args, command)
        except (ValueError, IndexError):
            self.errors.append("syntax: " + command)
            return None

    def _dispatch(self, head, args, command):
        if head == "US":
            self.user_mode = True
            return None
        if head == "IT":
            n = int(args[0]) if args else 0
            if n not in (1, 2, 3):
                raise ValueError
            self.it = n
            return None
        if head in ("BC", "DR"):
            return None
        if head in ("DV", "DI", "DS", "TV", "TI") and not self.user_mode:
            self.errors.append("not in user mode: " + command)
            return None
        if head in ("DV", "DI"):
            ch, rng, val, comp = int(args[0]), int(args[1]), float(args[2]), float(args[3])
            if head == "DV" and ch in self.vs:
                if abs(val) > 20.0:
                    raise ValueError
                self.vs[ch] = val
                return None
            if ch not in self.smu:
                raise ValueError
            if head == "DV":
                if rng not in V_RANGE or abs(val) > V_RANGE[rng] or not self._ok(val, comp):
                    self.errors.append("out of range: " + command)
                    return None
            else:
                if rng not in I_RANGE or abs(val) > I_RANGE[rng] or not self._ok(comp, val):
                    self.errors.append("out of range: " + command)
                    return None
            s = self.smu[ch]
            s.mode, s.value, s.comp = head[1], val, comp
            return None
        if head == "DS":
            chans = [int(a) for a in args] or list(self.smu) + list(self.vs)
            for ch in chans:
                if ch in self.smu:
                    self.smu[ch].mode = None
                elif ch in self.vs:
                    self.vs[ch] = None
                else:
                    raise ValueError
            return None
        if head in ("TV", "TI"):
            ch = int(args[0])
            if head == "TV" and ch in (5, 6):
                v = self.vs[ch] or 0.0
                return "N%sV%+.4E" % ("EF"[ch - 5], v)
            if ch not in self.smu:
                raise ValueError
            v, i, comp = self._operate(self.smu[ch])
            x = v if head == "TV" else i
            return "%s%s%s%+.4E" % ("C" if comp else "N", "ABCD"[ch - 1], head[1], x)
        self.unknown.append(command)
        return None

    @staticmethod
    def _ok(v, i) -> bool:
        return any(abs(v) <= vm + 1e-12 and abs(i) <= im + 1e-15 for vm, im in ENVELOPE)

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
