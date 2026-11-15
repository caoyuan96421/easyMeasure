"""Emulated American Magnetics Model 420 programmer with a superconducting magnet.

Only the commands and queries of the Model 420 manual (Rev. 7, chapter 4) exist.
Like the real one, it answers anything else with a beep (``beeps``), error -101
(command) or -201 (query) and **no reply**: a driver that sends a 430 query times
out, as on the hardware.

* ``STATE?``: 1 RAMPING, 2 HOLDING, 3 PAUSED, 4 MANUAL UP, 5 MANUAL DOWN,
  6 ZEROING, 7 QUENCH, 8 HEATING SWITCH, 9 AT ZERO;
* one current measurement, the shunt in the supply output: ``CURRent:MAGnet?`` and
  ``FIELD:MAGnet?`` read the supply current, also in persistent mode;
* ``PSwitch 1`` heats the switch for the heating time (STATE 8, ramping refused),
  then PAUSEs; ``PSwitch 0`` turns the heater off with no cooling state - the
  switch is still resistive for ``cool_time`` s, and a ramp started meanwhile
  changes the magnet current (the operator has to wait, the 420 does not);
* a switch heated while the supply and the magnet currents differ by more than
  ``mismatch_quench`` A quenches the magnet.

Options: ``time_scale``, ``coil`` (T/A), ``limit`` (A), ``rate`` (A/s), ``heat_time``,
``cool_time`` (s), ``switch`` (installed), ``heater_current`` (mA), ``units``
(0 = kG, 1 = T), ``rate_units`` (0 = per second, 1 = per minute), ``inductance`` (H),
``mismatch_quench`` (A), ``heater`` (the switch heater is on at the start; off, as
at power-on, by default).
"""

from __future__ import annotations

import math
import threading
import time
from typing import List, Optional

from . import SCPIEmulator, SCPIError, split_args, _split_messages
from ..simbus import FaultInjector


def _num(x: float) -> str:
    return "%.4f" % x


class AMI420Emulator(SCPIEmulator):
    def __init__(self, options=None):
        o = options or {}
        super().__init__("AMERICAN MAGNETICS INC.,MODEL 420,REV 3.07", FaultInjector(o))
        self.lock = threading.RLock()
        self.scale = float(o.get("time_scale", 1.0))
        self.coil = float(o.get("coil", 0.1))                # T/A
        self.limit = float(o.get("limit", 90.0))             # A
        self.rate = float(o.get("rate", 0.05))               # A/s
        self.heat_time = float(o.get("heat_time", 15.0))
        self.cool_time = float(o.get("cool_time", 10.0))
        self.has_switch = bool(o.get("switch", True))
        self.heater_ma = float(o.get("heater_current", 46.0 if self.has_switch else 0.0))
        self.units = int(o.get("units", 1))
        self.rate_units = int(o.get("rate_units", 1))
        self.L = float(o.get("inductance", 10.0))
        self.mismatch_quench = float(o.get("mismatch_quench", 0.5))
        self.beeps = 0
        # state
        self.Is = 0.0                    # supply (= shunt) current
        self.Im = 0.0                    # magnet current
        self.target = 0.0                # programmed current, A
        self.mode = "pause"              # ramp | hold | pause | up | down | zero | zeroed
        # off at power-on (PSwitch default 0); option heater: on, as left by an operator
        self.heater = self.has_switch and bool(o.get("heater", False))
        self.switch = ("warm" if self.heater else "cold") if self.has_switch else "none"
        self.t_switch = 0.0
        self.quenched = False
        self.quench_count = 0
        self.dIm = 0.0
        self.t = self._now()
        self.history = []
        a = self.add
        a("SYSTem:LOCal", lambda x, n: None)
        a("SYSTem:REMote", lambda x, n: None)
        a("*TST?", lambda x, n: "1")
        a("STATE?", lambda x, n: str(self._state()))
        a("QUench?", lambda x, n: "1" if self.quenched else "0")
        a("QUench", lambda x, n: self._quench_cmd(x))
        a("FIELD:UNITS?", lambda x, n: str(self.units))
        a("RAMP:RATE:UNITS?", lambda x, n: str(self.rate_units))
        a("COILconst?", lambda x, n: _num(self.coil * self._kg()))
        a("CURRent:LIMit?", lambda x, n: _num(self.limit))
        a("PSwitch:CURRent?", lambda x, n: _num(self.heater_ma))
        a("PSwitch:TIME?", lambda x, n: "%d" % self.heat_time)
        a("PSwitch", lambda x, n: self._psw(x))
        a("PSwitch?", lambda x, n: "1" if self.heater else "0")
        a("VOLTage:PSwitch?", lambda x, n: _num(3.2 if self.heater else 0.0))
        a("CURRent:MAGnet?", lambda x, n: _num(self.Is))
        a("FIELD:MAGnet?", lambda x, n: _num(self.Is * self.coil * self._kg()))
        a("VOLTage:MAGnet?", lambda x, n: _num(self.L * self.dIm))
        a("VOLTage:SUPPly?", lambda x, n: _num(0.01 * self.Is + self.L * self.dIm))
        a("CURRent:PROGram?", lambda x, n: _num(self.target))
        a("FIELD:PROGram?", lambda x, n: _num(self.target * self.coil * self._kg()))
        a("CONFigure:CURRent:PROGram", lambda x, n: self._program(x, 1.0))
        a("CONFigure:FIELD:PROGram", lambda x, n: self._program(x, 1.0 / (self.coil *
                                                                          self._kg())))
        a("RAMP:RATE:CURRent?", lambda x, n: _num(self.rate * self._per()))
        a("RAMP:RATE:FIELD?", lambda x, n: _num(self.rate * self._per() * self.coil * self._kg()))
        a("RAMP", lambda x, n: self._go("ramp"))
        a("PAUSE", lambda x, n: self._event("PAUSE") or setattr(self, "mode", "pause"))
        a("UP", lambda x, n: self._go("up"))
        a("DOWN", lambda x, n: self._go("down"))
        a("ZERO", lambda x, n: self._go("zero"))

    def _kg(self) -> float:
        return 10.0 if self.units == 0 else 1.0

    def _per(self) -> float:
        return 60.0 if self.rate_units == 1 else 1.0

    # -- the 420's command parser: unknown -> beep, error, no reply ------------------------
    def handle(self, command: str) -> Optional[str]:
        with self.lock:
            self.update()
            replies: List[str] = []
            for part in _split_messages(command):
                self.log.append(part)
                head, _, rest = part.strip().partition(" ")
                if head.startswith(":"):             # the 420 has no leading colons
                    self._refuse(part, head)
                    continue
                for rx, fn in self.handlers:
                    if rx.match(":" + head):
                        try:
                            r = fn(split_args(rest), "")
                        except SCPIError as exc:
                            self.beeps += 1
                            self.push_error(exc.code, exc.msg)
                            r = None
                        if r is not None:
                            replies.append(r)
                        break
                else:
                    self._refuse(part, head)
            return ";".join(replies) if replies else None

    def _refuse(self, part: str, head: str) -> None:
        self.unknown.append(part)
        self.beeps += 1
        if head.endswith("?"):
            self.push_error(-201, "Unrecognized query")
        else:
            self.push_error(-101, "Unrecognized command")

    def _next_error(self) -> str:
        if not self.errors:
            return '0,"No errors"'
        code, msg = self.errors.popleft()
        return '%d,"%s"' % (code, msg)

    # -- time -----------------------------------------------------------------------
    def _now(self) -> float:
        return time.monotonic() * self.scale

    def _event(self, what: str) -> None:
        self.history.append((self._now(), what))

    def update(self) -> None:
        with self.lock:
            now = self._now()
            dt = max(0.0, now - self.t)
            self.t = now
            while dt > 0:
                h = min(dt, 0.5)
                self._step(h)
                dt -= h

    def _step(self, h: float) -> None:
        im0 = self.Im
        if self.switch in ("heating", "cooling"):
            self.t_switch += h
            if self.switch == "heating" and self.t_switch >= self.heat_time:
                self.switch = "warm"
                self._event("switch warm")
                if abs(self.Is - self.Im) > self.mismatch_quench and not self.quenched:
                    self.quench("switch heated with %.3f A in the supply and %.3f A in the "
                                "magnet" % (self.Is, self.Im))
                else:
                    self.Im = self.Is
                if not self.quenched:
                    self.mode = "pause"          # manual 3.4.2: PAUSED after heating
            elif self.switch == "cooling" and self.t_switch >= self.cool_time:
                self.switch = "cold"
                self._event("switch cold")
        if self.quenched:
            self.Is = 0.0
            self.Im *= math.exp(-h / 2.0)
            if abs(self.Im) < 1e-4:
                self.Im = 0.0
        elif self.mode in ("ramp", "zero", "up", "down"):
            goal = {"ramp": self.target, "zero": 0.0, "up": self.limit,
                    "down": -self.limit}[self.mode]
            d = goal - self.Is
            if abs(d) <= self.rate * h:
                self.Is = goal
                self.mode = {"ramp": "hold", "zero": "zeroed"}.get(self.mode, "pause")
            else:
                self.Is += math.copysign(self.rate * h, d)
        if self.switch in ("warm", "none", "cooling") and not self.quenched:
            # a resistive switch (still while it cools), or none: the supply drives the magnet
            self.Im = self.Is
        self.dIm = (self.Im - im0) / h if h > 0 else 0.0

    def quench(self, why: str = "quench") -> None:
        with self.lock:
            self.quenched = True
            self.quench_count += 1
            self.mode = "pause"
            self._event("QUENCH: %s" % why)

    # -- commands ---------------------------------------------------------------------------
    def _state(self) -> int:
        if self.quenched:
            return 7
        if self.switch == "heating":
            return 8
        return {"ramp": 1, "hold": 2, "pause": 3, "up": 4, "down": 5, "zero": 6,
                "zeroed": 9}[self.mode]

    def _program(self, args, to_amps: float) -> None:
        if not args:
            raise SCPIError(-104, "Missing parameter")
        try:
            v = float(args[0]) * to_amps
        except ValueError:
            raise SCPIError(-102, "Invalid argument") from None
        if abs(v) > self.limit + 1e-9:
            raise SCPIError(-105, "Out of range")
        self.target = v

    def _go(self, mode: str) -> None:
        if self.quenched:
            raise SCPIError(-302, "Quench condition")
        if self.switch == "heating":
            raise SCPIError(-301, "Heating switch")
        self.mode = mode
        self._event("%s to %.4f A" % (mode.upper(), self.target if mode == "ramp" else 0.0))

    def _psw(self, args) -> None:
        if not args:
            raise SCPIError(-104, "Missing parameter")
        v = args[0].strip()
        if v not in ("0", "1"):
            raise SCPIError(-103, "Non-boolean argument")
        on = v == "1"
        if on and not self.has_switch:       # PSwitch 0 is accepted (as on the hardware)
            raise SCPIError(-107, "No switch installed")
        if on == self.heater:
            return
        self.heater = on
        self.switch = "heating" if on else "cooling"
        self.t_switch = 0.0
        self._event("PSwitch %d (supply %.4f A, magnet %.4f A)" % (on, self.Is, self.Im))

    def _quench_cmd(self, args) -> None:
        v = (args[0].strip() if args else "")
        if v == "0":
            if self.quenched:
                self.quenched = False
                self.mode = "pause"
                self._event("quench cleared")
        elif v == "1":
            self.quench("set by command")
        else:
            raise SCPIError(-103, "Non-boolean argument")
