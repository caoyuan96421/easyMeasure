"""Emulated Yokogawa 7651 DC source (its native GP-IB command set).

As the instrument (IM 7651-01E): a program message is commands written one after
the other with no separator (``F1R4S+1.5E+0E``), at most 50 characters.  ``F``,
``R``, ``S`` and ``O`` are stored until ``E`` (execute); ``LV`` / ``LA`` take
effect at once and are only accepted in current / voltage mode (error 11).
A message with anything else - a ``;``, ``*IDN?``, a value that does not fit the
range, a message too long - is rejected as a whole: nothing in it is executed and
status bit 4 is set until the next message that is not a query.

``OD`` answers the programmed value, e.g. ``NDCV+1.0000E+0`` (``H1``; no header
after ``H0``), with ``E`` instead of ``N`` while the output is held at the
limiter (a resistor ``load_ohm`` on the output; the current limiter does not work
on the 10 / 100 mV ranges).  ``OC`` answers ``STS1=<n>`` (16 output on, 4
previous message rejected, 2 program running, 1 program being edited), ``OS``
five lines ending with ``END``.  ``RU0`` stops a program.  A device clear
(``device_clear``, from the simulated GPIB card) restores the power-on panel:
1 V range, 0 V, output off, limits 30 V / 120 mA.

Options: ``load_ohm``, ``emulate_program`` (``running`` / ``editing``: the state
of the program memory at power-on).  Rejected messages are kept in ``errors``.
"""

from __future__ import annotations

import re
from typing import List

from ..simbus import FaultInjector, SimDevice

RANGES = {1: {2: 10e-3, 3: 100e-3, 4: 1.0, 5: 10.0, 6: 30.0},
          5: {4: 1e-3, 5: 10e-3, 6: 100e-3}}
MAX = {1: 32.0, 5: 0.12}
_NUM = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?"
_TOKEN = re.compile(r"(OD|OC|OS|RU|DL|LV|LA|SA|H|F|R|S|O|E)(%s)?" % _NUM)
_QUERIES = {"OD", "OC", "OS"}


class _Rejected(Exception):
    pass


class Yokogawa7651Emulator(SimDevice):
    def __init__(self, options=None):
        o = options or {}
        super().__init__(FaultInjector(o))
        self.load = float(o.get("load_ohm", 1e3))
        program = str(o.get("emulate_program", "")).lower()
        self.running, self.editing = program == "running", program == "editing"
        self.log: List[str] = []
        self.errors: List[str] = []
        self.clears = 0
        self.power_on()

    def power_on(self) -> None:
        self.func, self.range, self.value = 1, 4, 0.0
        self.lv, self.la = 30, 120
        self.on = False
        self.header = True
        self.error = False
        self.pending: dict = {}

    def device_clear(self) -> None:
        self.clears += 1
        self.power_on()

    @property
    def unknown(self) -> List[str]:     # what the driver sent that the 7651 rejected
        return self.errors

    def handle(self, command: str):
        self.log.append(command)
        text = command.strip().upper()
        try:
            tokens = self._parse(text)
        except _Rejected as exc:
            self.errors.append("%s: %s" % (exc, command))
            self.error = True
            return None
        if not all(t[0] in _QUERIES for t in tokens):
            self.error = False
        replies = []
        for cmd, arg in tokens:
            try:
                r = self._do(cmd, arg)
            except _Rejected as exc:
                self.errors.append("%s: %s" % (exc, command))
                self.error = True
                self.pending = {}
                return None
            if r is not None:
                replies.extend(r if isinstance(r, list) else [r])
        if not replies:
            return None
        return replies[0] if len(replies) == 1 else replies

    def _parse(self, text: str):
        if len(text) > 50:
            raise _Rejected("message longer than 50 characters")
        out, pos = [], 0
        while pos < len(text):
            m = _TOKEN.match(text, pos)
            if not m or m.end() == pos:
                raise _Rejected("syntax error at %r" % text[pos:pos + 8])
            out.append((m.group(1), m.group(2)))
            pos = m.end()
        return out

    def _do(self, cmd: str, arg):
        if cmd in _QUERIES:
            return {"OD": self._od, "OC": self._oc, "OS": self._os}[cmd]()
        if arg is None:
            if cmd != "E":
                raise _Rejected("%s needs a number" % cmd)
            self._execute()
            return None
        if cmd == "E":
            raise _Rejected("E takes no number")
        if cmd in ("S", "SA"):
            self.pending["S"] = float(arg)
            self.pending.pop("auto", None)
            if cmd == "SA":
                self.pending["auto"] = True
            return None
        if not re.fullmatch(r"\d+", arg):
            raise _Rejected("%s needs an integer" % cmd)
        n = int(arg)
        if cmd in ("F", "R", "O"):
            self.pending[cmd] = n
        elif cmd == "H":
            self.header = n == 1
        elif cmd == "DL":
            if n not in (0, 1, 2):
                raise _Rejected("DL%d" % n)
        elif cmd == "RU":
            if n == 0:
                self.running = False
        elif cmd == "LV":
            if self.func != 5:
                raise _Rejected("error 11: LV in voltage mode")
            if not 1 <= n <= 30:
                raise _Rejected("LV out of range")
            self.lv = n
        elif cmd == "LA":
            if self.func != 1:
                raise _Rejected("error 11: LA in current mode")
            if not 5 <= n <= 120:
                raise _Rejected("LA out of range")
            self.la = n
        return None

    def _execute(self) -> None:
        p, self.pending = self.pending, {}
        func = p.get("F", self.func)
        if func not in RANGES:
            raise _Rejected("F%d" % func)
        rng = p.get("R", self.range if func == self.func else 4)
        value = p.get("S", self.value if func == self.func else 0.0)
        if p.get("auto"):
            rng = next((r for r, full in sorted(RANGES[func].items())
                        if abs(value) <= min(full * 1.2, MAX[func])), None)
        if rng not in RANGES[func]:
            raise _Rejected("R%s" % rng)
        if abs(value) > min(RANGES[func][rng] * 1.2, MAX[func]) + 1e-12:
            raise _Rejected("value does not fit the range")
        if "O" in p and p["O"] not in (0, 1):
            raise _Rejected("O%d" % p["O"])
        self.func, self.range, self.value = func, rng, value
        if "O" in p:
            self.on = p["O"] == 1

    def _limited(self) -> bool:
        if not self.on:
            return False
        if self.func == 1:
            return self.range >= 4 and abs(self.value) / self.load > self.la * 1e-3 + 1e-12
        return abs(self.value) * self.load > self.lv + 1e-12

    def _number(self) -> str:
        mant, exp = ("%+.4E" % self.value).split("E")
        return "%sE%+d" % (mant, int(exp))

    def _od(self) -> str:
        body = self._number()
        if not self.header:
            return body
        return "%sDC%s%s" % ("E" if self._limited() else "N", "V" if self.func == 1 else "A",
                             body)

    def _oc(self) -> str:
        return "STS1=%d" % ((16 if self.on else 0) | (4 if self.error else 0)
                            | (2 if self.running else 0) | (1 if self.editing else 0))

    def _os(self):
        return ["MDL7651REV1.10", "F%dR%dS%s" % (self.func, self.range, self._number()),
                "PI1.0SW1.0", "LV%dLA%d" % (self.lv, self.la), "END"]

    # the "load": what a meter across the output would see
    def output(self) -> float:
        if not self.on:
            return 0.0
        if self._limited():
            if self.func == 1:
                return (self.la * 1e-3 if self.value > 0 else -self.la * 1e-3) * self.load
            return self.lv if self.value > 0 else -self.lv
        return self.value
