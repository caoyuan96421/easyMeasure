"""Emulated American Magnetics Model 430 programmer with a superconducting magnet.

Commands and queries are those of the Model 430 manual (remote interface
reference): ``STATE?``, ``FIELD:MAGnet?``, ``CONFigure:FIELD:TARGet``, ``RAMP``,
``PAUSE``, ``ZERO``, ``PSwitch``, ``QUench`` ... and the IEEE-488.2 ``*IDN?``,
``*CLS``, ``SYSTem:ERRor?``.

The magnet behaves like one, in (scaled) real time:

* the supply ramps its current at the segment-1 rate while the magnet is driven
  (switch heater on), at the faster "PSw P/S ramp rate" while the switch is cold
  (then only the current in the leads changes, the magnet keeps its persistent
  current);
* ``PSwitch 1`` heats the switch for the heated time (STATE 9), ``PSwitch 0``
  cools it for the cooled time (STATE 10); ramping is refused meanwhile;
* when the switch turns resistive the supply current is forced into the magnet:
  a difference of more than ``mismatch_quench`` A **quenches the magnet**, as
  heating a switch at the wrong current can do;
* a quench (``quench()`` from a test, or a mismatch) sets STATE 7 and
  ``QUench? 1``, zeroes the supply and lets the magnet current decay; ``QUench 0``
  clears it (the supply then PAUSEs).

Options: ``time_scale`` (simulated seconds per real second), ``coil`` (T/A),
``limit`` (A), ``rate`` (segment-1 rate, A/s), ``psw_rate`` (A/s), ``heat_time``,
``cool_time`` (s), ``switch`` (a persistent switch is installed), ``units``
(0 = kG, 1 = T), ``rate_units`` (0 = per second, 1 = per minute), ``inductance``
(H), ``mismatch_quench`` (A), ``auto_zero`` (ramp the leads to zero after
cooling, as the front panel does), ``greeting`` (the TCP welcome lines),
``model`` (430 or 420).
"""

from __future__ import annotations

import math
import threading
import time
from typing import Optional

from . import SCPIEmulator, SCPIError, fmt_num, parse_num
from ..simbus import FaultInjector


class AMI430Emulator(SCPIEmulator):
    def __init__(self, options=None):
        o = options or {}
        model = int(o.get("model", 430))
        super().__init__("AMERICAN MAGNETICS INC.,MODEL %d,2.65" % model, FaultInjector(o))
        self.lock = threading.RLock()
        self.scale = float(o.get("time_scale", 1.0))
        self.coil = float(o.get("coil", 0.1))                # T/A
        self.limit = float(o.get("limit", 90.0))             # A
        self.rate = float(o.get("rate", 0.05))               # A/s, driven
        self.psw_rate = float(o.get("psw_rate", 2.0))        # A/s, switch cold
        self.heat_time = float(o.get("heat_time", 20.0))
        self.cool_time = float(o.get("cool_time", 30.0))
        self.has_switch = bool(o.get("switch", True))
        self.units = int(o.get("units", 1))
        self.rate_units = int(o.get("rate_units", 1))
        self.L = float(o.get("inductance", 10.0))
        self.mismatch_quench = float(o.get("mismatch_quench", 0.5))
        self.auto_zero = bool(o.get("auto_zero", False))
        self.greet = bool(o.get("greeting", False))
        self.voltage_taps = bool(o.get("voltage_taps", True))
        # state
        self.Is = 0.0                    # supply current
        self.Im = 0.0                    # magnet current
        self.target = 0.0                # A
        self.mode = "pause"              # ramp | hold | pause | zero | zeroed
        self.heater = self.has_switch    # heater on = switch resistive = driven
        self.switch = "warm" if self.has_switch else "none"   # warm|cold|heating|cooling
        self.t_switch = 0.0
        self.persistent = False
        self.quenched = False
        self.quench_count = 0
        self.dIm = 0.0                   # dIm/dt for the magnet voltage
        self.t = self._now()
        self.history = []                # (time, event) for the tests
        a = self.add
        a("STATE?", lambda x, n: str(self._state()))
        a("QUench?", lambda x, n: "1" if self.quenched else "0")
        a("QUench", lambda x, n: self._quench_cmd(x))
        a("FIELD:MAGnet?", lambda x, n: self._f(self.Im * self.coil))
        a("FIELD:TARGet?", lambda x, n: self._f(self.target * self.coil))
        a("FIELD:UNITS?", lambda x, n: str(self.units))
        a("CONFigure:FIELD:TARGet", lambda x, n: self._set_target(x))
        a("CURRent:MAGnet?", lambda x, n: fmt_num(self.Im))
        a("CURRent:SUPPly?", lambda x, n: fmt_num(self.Is))
        a("CURRent:LIMit?", lambda x, n: fmt_num(self.limit))
        a("VOLTage:MAGnet?", lambda x, n: self._vmag())
        a("VOLTage:SUPPly?", lambda x, n: fmt_num(0.01 * self.Is + self.L * self.dIm))
        a("COILconst?", lambda x, n: fmt_num(self.coil * (10.0 if self.units == 0 else 1.0)))
        a("RAMP", lambda x, n: self._ramp())
        a("PAUSE", lambda x, n: self._event("PAUSE") or setattr(self, "mode", "pause"))
        a("ZERO", lambda x, n: self._zero())
        a("RAMP:RATE:UNITS?", lambda x, n: str(self.rate_units))
        a("RAMP:RATE:SEGments?", lambda x, n: "1")
        a("RAMP:RATE:FIELD:{n}?", lambda x, n: self._rate_q(n))
        a("PSwitch", lambda x, n: self._psw(x))
        a("PSwitch?", lambda x, n: "1" if self.heater else "0")
        a("PSwitch:INSTalled?", lambda x, n: "1" if self.has_switch else "0")
        a("PSwitch:HeatTIME?", lambda x, n: "%d" % self.heat_time)
        a("PSwitch:CoolTIME?", lambda x, n: "%d" % self.cool_time)
        a("PSwitch:PowerSupplyRampRate?", lambda x, n: fmt_num(self.psw_rate))
        a("PERSistent?", lambda x, n: "1" if self.persistent else "0")
        if model == 420:
            # queries the 420 does not have (the driver must do without them)
            gone = ("PSwitch:INSTalled?", "CURRent:LIMit?", "PSwitch:PowerSupplyRampRate?",
                    "VOLTage:MAGnet?")
            from . import compile_pattern
            pats = {compile_pattern(g).pattern for g in gone}
            self.handlers = [(rx, fn) for rx, fn in self.handlers if rx.pattern not in pats]

    # -- time -----------------------------------------------------------------------
    def _now(self) -> float:
        return time.monotonic() * self.scale

    def _event(self, what: str) -> None:
        self.history.append((self._now(), what))

    def handle(self, command: str) -> Optional[str]:
        with self.lock:
            self.update()
            reply = super().handle(command)
            if self.greet:
                self.greet = False
                lines = ["American Magnetics Model 430 IP Interface", "Hello."]
                return lines + ([reply] if reply is not None else [])
            return reply

    def update(self) -> None:
        """Advance the simulation to now."""
        with self.lock:
            now = self._now()
            dt = max(0.0, now - self.t)
            self.t = now
            # integrate in small steps (the switch may change state within dt)
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
                self.persistent = False
                self._event("switch warm")
                if abs(self.Is - self.Im) > self.mismatch_quench and not self.quenched:
                    self.quench("switch heated with %.3f A in the supply and %.3f A in the "
                                "magnet" % (self.Is, self.Im))
                else:
                    self.Im = self.Is
                if not self.quenched:
                    self.mode = "pause"
            elif self.switch == "cooling" and self.t_switch >= self.cool_time:
                self.switch = "cold"
                self.persistent = True
                self._event("switch cold")
                self.mode = "pause"
                if self.auto_zero:
                    self.target = 0.0
                    self.mode = "zero"
        if self.quenched:
            self.Is = 0.0
            self.Im *= math.exp(-h / 2.0)
            if abs(self.Im) < 1e-4:
                self.Im = 0.0
        elif self.mode in ("ramp", "zero"):
            goal = 0.0 if self.mode == "zero" else self.target
            r = self.psw_rate if self.switch in ("cold",) else self.rate
            d = goal - self.Is
            if abs(d) <= r * h:
                self.Is = goal
                self.mode = "zeroed" if self.mode == "zero" else "hold"
            else:
                self.Is += math.copysign(r * h, d)
        if self.switch in ("warm", "none", "cooling") and not self.quenched:
            # resistive switch (or none): the supply current flows through the magnet
            self.Im = self.Is
        self.dIm = (self.Im - im0) / h if h > 0 else 0.0

    # -- the things a test does to it ---------------------------------------------------
    def quench(self, why: str = "quench") -> None:
        with self.lock:
            self.quenched = True
            self.quench_count += 1
            self.mode = "pause"
            self._event("QUENCH: %s" % why)

    # -- commands ---------------------------------------------------------------------------
    def _f(self, tesla: float) -> str:
        return fmt_num(tesla * (10.0 if self.units == 0 else 1.0))

    def _vmag(self) -> str:
        if not self.voltage_taps:
            raise SCPIError(-241, "Hardware missing")
        return fmt_num(self.L * self.dIm)

    def _state(self) -> int:
        if self.quenched:
            return 7
        if self.switch == "heating":
            return 9
        if self.switch == "cooling":
            return 10
        return {"ramp": 1, "hold": 2, "pause": 3, "zero": 6, "zeroed": 8}[self.mode]

    def _set_target(self, args) -> None:
        if not args:
            raise SCPIError(-109, "Missing parameter")
        v = parse_num(args[0]) * (0.1 if self.units == 0 else 1.0) / self.coil
        if abs(v) > self.limit + 1e-9:
            raise SCPIError(-222, "Data out of range")
        self.target = v

    def _no_ramp_now(self) -> None:
        if self.quenched:
            raise SCPIError(-310, "Quench condition: clear the quench first")
        if self.switch == "heating":
            raise SCPIError(-301, "Ramping disallowed during heating")
        if self.switch == "cooling":
            raise SCPIError(-302, "Ramping disallowed during cooling")

    def _ramp(self) -> None:
        self._no_ramp_now()
        self.mode = "ramp"
        self._event("RAMP to %.4f A" % self.target)

    def _zero(self) -> None:
        self._no_ramp_now()
        self.mode = "zero"
        self._event("ZERO")

    def _rate_q(self, n) -> str:
        if (n or "1") != "1":
            raise SCPIError(-222, "Data out of range")
        per = 60.0 if self.rate_units == 1 else 1.0
        return "%s,%s" % (self._f(self.rate * self.coil * per).strip(),
                          self._f(self.limit * self.coil).strip())

    def _psw(self, args) -> None:
        if not args:
            raise SCPIError(-109, "Missing parameter")
        on = args[0].strip() in ("1", "ON")
        if not self.has_switch:
            raise SCPIError(-311, "No persistent switch installed")
        if self.quenched:
            raise SCPIError(-310, "Quench condition")
        if self.mode in ("ramp", "zero") or self.switch in ("heating", "cooling"):
            raise SCPIError(-303, "Persistent switch change not allowed while ramping")
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
                self.target = self.Is
                self._event("quench cleared")
        elif v == "1":
            self.quench("set by command")
        else:
            raise SCPIError(-224, "Illegal parameter value")
