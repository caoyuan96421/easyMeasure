"""Emulated Yokogawa 7651 DC source (its GP-IB command set).

Commands are letters followed by an optional number and may be chained or
separated by ``;``; function, range, value, limits and output are staged and
take effect with ``E`` (execute), like on the instrument.  ``OD`` answers e.g.
``NDCV+1.0000E+0`` (``E`` instead of ``N`` when the value does not fit the range
or a limit), ``OC`` the status ``STS1=<n>`` (bit 4: output on), ``OS`` the
settings over several lines ending with ``END``.  Unknown commands are
recorded in ``unknown`` and answered with nothing.
"""

from __future__ import annotations

import re
from typing import List

from ..simbus import FaultInjector, SimDevice

RANGES = {1: {2: 10e-3, 3: 100e-3, 4: 1.0, 5: 10.0, 6: 30.0},
          5: {4: 1e-3, 5: 10e-3, 6: 100e-3}}
MAX = {1: 32.0, 5: 0.12}
_TOKEN = re.compile(r"(OD|OC|OS|LV|LA|H|F|R|S|E|O)([+-]?\d*\.?\d*(?:E[+-]?\d+)?)", re.I)


class Yokogawa7651Emulator(SimDevice):
    def __init__(self, options=None):
        o = options or {}
        super().__init__(FaultInjector(o))
        self.load = float(o.get("load_ohm", 1e3))
        self.func, self.range, self.value = 1, 4, 0.0          # power-on: 1 V range, 0 V
        self.lv, self.la = 30, 120
        self.on = False
        self.error = False
        self.pending: dict = {}
        self.log: List[str] = []
        self.unknown: List[str] = []
        self.executed = 0

    def handle(self, command: str):
        self.log.append(command)
        text = command.replace(";", "").replace(" ", "").upper()
        replies = []
        pos = 0
        while pos < len(text):
            m = _TOKEN.match(text, pos)
            if not m or m.end() == pos:
                self.unknown.append(command)
                return None
            cmd, arg = m.group(1), m.group(2)
            pos = m.end()
            if cmd in ("F", "R", "O", "LV", "LA", "H"):
                if not re.fullmatch(r"\d+", arg or ""):
                    self.unknown.append(command)
                    return None
                self.pending[cmd] = int(arg)
            elif cmd == "S":
                try:
                    self.pending["S"] = float(arg)
                except ValueError:
                    self.unknown.append(command)
                    return None
            elif cmd == "E":
                self._execute()
            elif cmd == "OD":
                replies.append(self._od())
            elif cmd == "OC":
                replies.append("STS1=%d" % ((16 if self.on else 0) | (2 if self.error else 0)))
            elif cmd == "OS":
                return ["MDL7651REV1.10", "F%dR%dS%+.4E" % (self.func, self.range, self.value),
                        "PRS0PRE0", "LV%dLA%d" % (self.lv, self.la), "END"]
        return replies[-1] if replies else None

    def _execute(self) -> None:
        p, self.pending = self.pending, {}
        self.executed += 1
        if "F" in p:
            if p["F"] not in RANGES:
                self.error = True
                return
            if p["F"] != self.func:
                self.func = p["F"]
                self.range = max(RANGES[self.func])  # a new function starts on its top range
                self.value = 0.0
        if "R" in p:
            if p["R"] not in RANGES[self.func]:
                self.error = True
                return
            self.range = p["R"]
        if "LV" in p:
            if not 1 <= p["LV"] <= 30:
                self.error = True
                return
            self.lv = p["LV"]
        if "LA" in p:
            if not 5 <= p["LA"] <= 120:
                self.error = True
                return
            self.la = p["LA"]
        if "S" in p:
            full = RANGES[self.func][self.range]
            if abs(p["S"]) > min(full * 1.2, MAX[self.func]) + 1e-12:
                self.error = True                         # value does not fit the range
                return
            self.value = p["S"]
        if "O" in p:
            self.on = p["O"] == 1
        self.error = False

    def _od(self) -> str:
        return "%sDC%s%+.4E" % ("E" if self.error else "N", "V" if self.func == 1 else "A",
                                self.value)

    # the "load": what a meter across the output would see
    def output(self) -> float:
        return self.value if self.on else 0.0
