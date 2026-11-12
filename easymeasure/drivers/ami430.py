"""American Magnetics Model 430 (and 420) superconducting magnet power supply programmer.

The magnet's configuration (coil constant, current limit, ramp rates, the
persistent switch's heater current and times, quench detection) lives in the
programmer, set once on its front panel; this driver only reads it.  What it does
is drive the magnet **safely**:

Field
    Writing ``field`` sets the target and starts the ramp (``RAMP``) and returns at
    once, like the front panel: ``status`` reads ``RAMPING``, then ``HOLDING``.  A
    sweep waits with a point action, e.g.
    ``wait_until(lambda: get('magnet.status') == 'HOLDING')``.  Reading ``field``
    gives the magnet's field (in persistent mode the field it was left at).

Persistent switch (``IsPersistentSwitchInstalled`` = Yes)
    The field can only be changed in **driven** mode (switch heater on).  In
    persistent mode a field write is refused: the supply would only change the
    current in its leads.  The actions do the two transitions in the order that
    cannot quench the magnet, step by step, checking the programmer's own state
    before every step:

    ``enter_persistent``
        1. the supply must hold the field (``HOLDING`` / ``PAUSED``); wait
           ``settle_time`` s, and until the magnet voltage has decayed below
           ``max_magnet_voltage``;
        2. switch heater off (``PSwitch 0``); wait while the switch cools
           (``COOLING SWITCH``);
        3. ramp the leads to zero (``ZERO``, option ``zero_leads``) and wait for
           ``AT ZERO``.  Status: ``PERSISTENT``.

    ``exit_persistent``
        1. ramp the supply to the magnet's persistent current (target = the
           persistent field, ``RAMP``) and wait for ``HOLDING``;
        2. heat the switch (``PSwitch 1``) **only** if the supply current matches
           the magnet current within ``match_tolerance`` A - heating at the wrong
           current forces the difference into the magnet and can quench it;
        3. wait while the switch heats (``HEATING SWITCH``), then hold the field
           (``RAMP``).  Status: ``HOLDING``, driven.

    Each action checks its preconditions, starts the procedure and returns; the
    procedure then advances whenever the driver talks to the programmer - every
    read, and the backend's watchdog every ``poll_interval`` s - so the field can be
    watched meanwhile.  ``status`` reads ``ENTERING PERSISTENT`` /
    ``EXITING PERSISTENT`` until it is done, ``operation`` says which step it is on
    (or why it stopped).  A step that does not complete in time, or a programmer
    that does something unexpected (paused from the front panel, ...), stops the
    procedure where it is safe, without heating the switch.  The ``pause`` action
    stops the supply and the procedure.

Quench
    The programmer detects quenches itself (and then takes the supply to zero).
    The driver checks its state at every call and at every watchdog poll; a quench
    raises :class:`~easymeasure.common.errors.InstrumentFault`, which ends a
    running sweep or script, stops the run queue and the user actions, and is
    reported to every front end.  Until the quench is cleared (``clear_quench``,
    or RESET QUENCH on the front panel) field writes and the persistent actions are
    refused; reading still works.

Model 420: the same command set, over RS-232.  Model 430: Ethernet
(``TCP::<ip>::7180``) or RS-232.  Both send ``\\r\\n`` line ends.
"""

from __future__ import annotations

import math
import time
from typing import Optional

from ..backend.instrument import ActionSpec, ChannelSpec
from ..backend.logsys import get_logger
from ..common.errors import ChannelError, InstrumentFault, ResourceError
from ._scpi import SCPIInstrument, fmt, to_float

log = get_logger("ami430")

STATES = {1: "RAMPING", 2: "HOLDING", 3: "PAUSED", 4: "MANUAL UP", 5: "MANUAL DOWN",
          6: "ZEROING", 7: "QUENCH", 8: "AT ZERO", 9: "HEATING SWITCH", 10: "COOLING SWITCH",
          11: "EXTERNAL RAMPDOWN"}
HOLDING, PAUSED, ZEROING, QUENCH, AT_ZERO, HEATING, COOLING = 2, 3, 6, 7, 8, 9, 10
STEADY = (HOLDING, PAUSED, AT_ZERO)          # the supply is not moving


def _yes(v) -> Optional[bool]:
    s = str(v).strip().lower()
    if s in ("yes", "true", "1", "on"):
        return True
    if s in ("no", "false", "0", "off"):
        return False
    return None                              # "auto"


class AMI430(SCPIInstrument):
    DESCRIPTION = "American Magnetics 430 / 420 magnet power supply programmer"
    INTERFACES = ("tcp", "serial", "gpib", "tcpip", "other", "sim")
    IDN_MATCH = ("AMERICAN MAGNETICS",)
    POLL_INTERVAL = 1.0
    CHANNELS = [
        ChannelSpec("field", "rw", "float", (), "T",
                    "set: ramp to this field (returns at once; status RAMPING, then HOLDING); "
                    "read: the magnet's field (persistent: the field it was left at)"),
        ChannelSpec("target", "r", "float", (), "T", "the target field"),
        ChannelSpec("supply_field", "r", "float", (), "T",
                    "the field the supply current corresponds to (in persistent mode the "
                    "current in the leads, not in the magnet)"),
        ChannelSpec("current", "r", "float", (), "A", "the magnet current"),
        ChannelSpec("supply_current", "r", "float", (), "A", "the supply current"),
        ChannelSpec("voltage", "r", "float", (), "V", "the magnet voltage (voltage taps)"),
        ChannelSpec("ramp_rate", "r", "float", (), "T/min",
                    "ramp rate (segment 1; set on the programmer)"),
        ChannelSpec("status", "r", "str", (), "",
                    "RAMPING, HOLDING, PAUSED, ZEROING, AT ZERO, PERSISTENT, HEATING SWITCH, "
                    "COOLING SWITCH, ENTERING PERSISTENT, EXITING PERSISTENT, QUENCH ..."),
        ChannelSpec("switch", "r", "str", (), "",
                    "persistent switch heater: ON (driven), OFF (persistent), HEATING, "
                    "COOLING, NONE"),
        ChannelSpec("quench", "r", "str", (), "", "Yes while a quench is in effect"),
        ChannelSpec("operation", "r", "str", (), "",
                    "the persistent-switch procedure: its step, idle, or why it stopped"),
        ChannelSpec("IsPersistentSwitchInstalled", "rw", "str", (), "",
                    "the magnet has a persistent switch: field changes need the heater on, "
                    "and enter/exit_persistent are available", config=True,
                    allowed=["Yes", "No"]),
    ]
    ACTIONS = [
        ActionSpec("enter_persistent", "Enter Persistent: wait for a steady field, switch "
                   "heater off, wait for it to cool, ramp the leads to zero"),
        ActionSpec("exit_persistent", "Exit Persistent: ramp the supply to the magnet "
                   "current, heat the switch only when they match, hold the field"),
        ActionSpec("pause", "PAUSE the supply (and stop a persistent-switch procedure)"),
        ActionSpec("clear_quench", "clear the quench condition (like RESET QUENCH)"),
    ]
    DEFAULT_OPTIONS = dict(SCPIInstrument.DEFAULT_OPTIONS, read_termination="\r\n",
                           write_termination="\r\n", timeout=5.0,
                           IsPersistentSwitchInstalled="auto", settle_time=10.0,
                           max_magnet_voltage=0.05, match_tolerance=0.05, zero_leads=True,
                           status_interval=0.25)

    @staticmethod
    def EMULATOR(options):                     # noqa: N802
        from .emulators.ami430 import AMI430Emulator
        return AMI430Emulator(options)

    fault: Optional[str] = None

    # -- connection -----------------------------------------------------------------------
    def identify(self) -> str:
        """``*IDN?``; over TCP the 430 first greets every new connection with a
        couple of lines, which are skipped."""
        self.attach_emulator()
        self.select(self.resource)
        self.resource.write("*IDN?")
        for _ in range(6):
            line = self.resource.read().strip()
            if "AMERICAN MAGNETICS" in line.upper() and "," in line:   # not the greeting
                self.idn = line
                return line
        raise IOError("%s: no identification from the programmer" % self.name)

    def connect(self):
        self.identify()
        self.clear_errors()
        self.units_to_T = 0.1 if self.qf("FIELD:UNITS?") < 0.5 else 1.0     # kG or T
        self.coil = self.qf("COILconst?") * self.units_to_T                  # T/A
        if not self.coil > 0:
            raise IOError("%s: the programmer has no coil constant; set it on the front panel"
                          % self.name)
        self.per_min = 60.0 if self.qf("RAMP:RATE:UNITS?") < 0.5 else 1.0
        self.limit = self._optional_float("CURRent:LIMit?")
        self.ctrl_switch = self._optional_bool("PSwitch:INSTalled?")
        opt = _yes(self.options.get("IsPersistentSwitchInstalled", "auto"))
        self.switch_installed = opt if opt is not None else bool(self.ctrl_switch)
        self.heat_time = self._optional_float("PSwitch:HeatTIME?") or 60.0
        self.cool_time = self._optional_float("PSwitch:CoolTIME?") or 60.0
        self.psw_rate = self._optional_float("PSwitch:PowerSupplyRampRate?") or 1.0
        self.read_errors()
        self.seq: Optional[dict] = None
        self.operation = "idle"
        self._state = None
        self._state_t = 0.0
        self._vtaps = True
        # a quench in effect now is reported by the first call after connecting (the
        # watchdog's first poll at the latest), so that it reaches the backend
        self.fault = None
        self._state = self._query_state()
        self._state_t = time.monotonic()

    def _optional_float(self, q: str) -> Optional[float]:
        """A query the 420 (or an older 430) may not know: None if refused."""
        try:
            v = self.qf(q)
        except (IOError, OSError, ValueError, ResourceError):
            self.resource.clear()
            v = None
        if self.read_errors():
            return None
        return v

    def _optional_bool(self, q: str) -> Optional[bool]:
        v = self._optional_float(q)
        return None if v is None else v > 0.5

    def read_errors(self):
        out = []
        for _ in range(20):
            r = self.q("SYST:ERR?")
            code = r.split(",", 1)[0].strip()
            try:
                if int(float(code)) == 0:
                    break
            except ValueError:
                raise IOError("bad error-queue reply %r" % r[:60]) from None
            out.append(r)
        return out

    # -- the programmer's state --------------------------------------------------------------
    def _query_state(self) -> int:
        r = self.q("STATE?")
        try:
            return int(float(r))
        except ValueError:
            raise IOError("unexpected STATE? reply %r" % r[:40]) from None

    def _tick(self, force: bool = False) -> int:
        """Read STATE? (at most every ``status_interval`` s unless *force*), detect a
        quench, advance a running persistent-switch procedure.  Returns the state."""
        now = time.monotonic()
        iv = float(self.options.get("status_interval", 0.25) or 0)
        if force or self._state is None or now - self._state_t >= iv or self.seq is not None:
            self._state = self._query_state()
            self._state_t = now
        s = self._state
        if s == QUENCH:
            if self.fault is None:
                b = self._field_or_nan()
                what = (" during %s" % self._seq_name()) if self.seq else ""
                self.fault = "QUENCH detected by the programmer%s (magnet at %.4g T when " \
                             "detected); clear it with clear_quench once the magnet is safe" \
                             % (what, b)
                if self.seq is not None:
                    self.operation = "stopped: quench during %s" % self._seq_name()
                    self.seq = None
                log.error("%s: %s", self.name, self.fault)
                raise InstrumentFault(self.fault)
        elif self.fault is not None:
            # cleared on the front panel (RESET QUENCH) or by clear_quench
            if not self.qb("QUench?"):
                log.warning("%s: quench cleared", self.name)
                self.fault = None
                if self.operation.startswith("stopped: quench"):
                    self.operation = "idle (quench cleared)"
        if self.seq is not None:
            self._advance(s)
        return s

    def _field_or_nan(self) -> float:
        try:
            return self.qf("FIELD:MAGnet?") * self.units_to_T
        except Exception:
            return math.nan

    def _heater(self) -> Optional[bool]:
        if not (self.switch_installed or self.ctrl_switch):
            return None
        try:
            return self.qb("PSwitch?")
        except IOError:
            return None

    def _refuse_when_faulted(self, what: str) -> None:
        if self.fault is not None:
            raise InstrumentFault("%s: cannot %s: %s" % (self.name, what, self.fault))

    def _seq_name(self) -> str:
        if self.seq is None:
            return ""
        return "entering persistent mode" if self.seq["op"] == "enter" else \
            "exiting persistent mode"

    def poll(self) -> None:
        self._tick(force=True)

    # -- reading -------------------------------------------------------------------------------
    def read(self, channel: str):
        s = self._tick()
        if channel == "field":
            return self.qf("FIELD:MAGnet?") * self.units_to_T
        if channel == "target":
            return self.qf("FIELD:TARGet?") * self.units_to_T
        if channel == "supply_field":
            return self.qf("CURRent:SUPPly?") * self.coil
        if channel == "current":
            return self.qf("CURRent:MAGnet?")
        if channel == "supply_current":
            return self.qf("CURRent:SUPPly?")
        if channel == "voltage":
            v = self._magnet_voltage()
            return math.nan if v is None else v
        if channel == "ramp_rate":
            r = self.q("RAMP:RATE:FIELD:1?").split(",")[0]
            return to_float(r) * self.units_to_T * self.per_min
        if channel == "status":
            return self._status(s)
        if channel == "switch":
            return self._switch_text(s)
        if channel == "quench":
            return "Yes" if s == QUENCH or self.fault is not None else "No"
        if channel == "operation":
            return self.operation
        if channel == "IsPersistentSwitchInstalled":
            return "Yes" if self.switch_installed else "No"
        raise ChannelError("%s has no channel %s" % (self.name, channel))

    def _status(self, s: int) -> str:
        if s == QUENCH or self.fault is not None:
            return "QUENCH"
        if self.seq is not None:
            return "ENTERING PERSISTENT" if self.seq["op"] == "enter" else "EXITING PERSISTENT"
        if self.switch_installed and s in STEADY and self._heater() is False:
            return "PERSISTENT"
        return STATES.get(s, "STATE %d" % s)

    def _switch_text(self, s: int) -> str:
        if not self.switch_installed:
            return "NONE"
        if s == HEATING:
            return "HEATING"
        if s == COOLING:
            return "COOLING"
        h = self._heater()
        return "?" if h is None else "ON" if h else "OFF"

    def _magnet_voltage(self) -> Optional[float]:
        if not self._vtaps:
            return None
        try:
            v = self.qf("VOLTage:MAGnet?")
        except (IOError, ResourceError):
            self.resource.clear()
            v = None
        if self.read_errors() or v is None:
            self._vtaps = False                   # no voltage taps: not asked again
            return None
        return v

    # -- writing --------------------------------------------------------------------------------
    def write(self, channel: str, value):
        if channel == "field":
            self._set_field(float(value))
        elif channel == "IsPersistentSwitchInstalled":
            self._set_switch_installed(_yes(value))
        else:
            raise ChannelError("%s: %s cannot be set" % (self.name, channel))

    def _set_field(self, b: float) -> None:
        s = self._tick(force=True)
        self._refuse_when_faulted("set the field")
        if self.seq is not None:
            raise ChannelError("%s is busy %s (%s): the field cannot be changed now"
                               % (self.name, self._seq_name(), self.operation))
        if s in (HEATING, COOLING):
            raise ChannelError("%s: the persistent switch is %s: wait until it is done"
                               % (self.name, "heating" if s == HEATING else "cooling"))
        if self.switch_installed or self.ctrl_switch:
            heater = self._heater()
            if heater is False:
                raise ChannelError(
                    "%s: the magnet is in persistent mode (switch heater off): run "
                    "exit_persistent first - ramping now would only change the current in the "
                    "leads%s" % (self.name, "" if self.switch_installed else
                                 " (the programmer reports a persistent switch although "
                                 "IsPersistentSwitchInstalled is No)"))
        if self.limit and abs(b) > self.limit * self.coil * (1 + 1e-9):
            raise ChannelError("%s: %g T is beyond the programmer's current limit (%g T)"
                               % (self.name, b, self.limit * self.coil))
        self.checked("CONF:FIELD:TARG %s" % fmt(b / self.units_to_T))
        self.checked("RAMP")
        self._state = None

    def _set_switch_installed(self, on: Optional[bool]) -> None:
        if on is None:
            raise ChannelError("IsPersistentSwitchInstalled is Yes or No")
        s = self._tick(force=True)
        if self.seq is not None:
            raise ChannelError("%s is busy %s" % (self.name, self._seq_name()))
        if on and self.ctrl_switch is False:
            raise ChannelError("%s: the programmer is configured without a persistent switch "
                               "(Load menu); set it there first" % self.name)
        if not on and self.switch_installed and (s in (HEATING, COOLING) or
                                                 self._heater() is False):
            raise ChannelError("%s: the switch is %s: exit persistent mode first" % (
                self.name, "busy" if s in (HEATING, COOLING) else "off (persistent)"))
        self.switch_installed = on

    # -- actions ---------------------------------------------------------------------------------
    def _common_checks(self, what: str) -> int:
        s = self._tick(force=True)
        self._refuse_when_faulted(what)
        if not self.switch_installed:
            raise ChannelError("%s: no persistent switch (IsPersistentSwitchInstalled = No)"
                               % self.name)
        if self.ctrl_switch is False:
            raise ChannelError("%s: the programmer is configured without a persistent switch"
                               % self.name)
        if self.seq is not None:
            raise ChannelError("%s is busy %s" % (self.name, self._seq_name()))
        if s in (HEATING, COOLING):
            raise ChannelError("%s: the switch is %s: wait until it is done" % (
                self.name, "heating" if s == HEATING else "cooling"))
        return s

    def _action_enter_persistent(self) -> None:
        s = self._common_checks("enter persistent mode")
        if self._heater() is False:
            raise ChannelError("%s is in persistent mode already (switch heater off)" % self.name)
        if s not in STEADY:
            raise ChannelError("%s is %s: wait until it holds the field (HOLDING) before "
                               "entering persistent mode" % (self.name, STATES.get(s, s)))
        now = time.monotonic()
        self.seq = {"op": "enter", "step": "settle", "t0": now, "since": now}
        self.operation = "entering persistent mode: waiting %.0f s for a steady field" % \
            float(self.options.get("settle_time", 10.0))
        log.info("%s: entering persistent mode at %.5g T", self.name, self._field_or_nan())
        self._advance(s)

    def _action_exit_persistent(self) -> None:
        s = self._common_checks("exit persistent mode")
        if self._heater() is not False:
            raise ChannelError("%s is not in persistent mode (switch heater on)" % self.name)
        if s not in STEADY:
            raise ChannelError("%s is %s: wait until the supply is steady" % (
                self.name, STATES.get(s, s)))
        b = self.qf("FIELD:MAGnet?")                  # persistent: the field it was left at
        self.checked("CONF:FIELD:TARG %s" % fmt(b))
        self.checked("RAMP")
        now = time.monotonic()
        diff = abs(self.qf("CURRent:SUPPly?") - self.qf("CURRent:MAGnet?"))
        self.seq = {"op": "exit", "step": "match", "t0": now,
                    "deadline": now + 60 + 3 * diff / max(self.psw_rate, 1e-6)}
        self.operation = "exiting persistent mode: ramping the supply to the magnet's %.5g T" \
            % (b * self.units_to_T)
        log.info("%s: exiting persistent mode at %.5g T", self.name, b * self.units_to_T)
        self._state = None
        self._advance(self._tick(force=True))

    def _action_pause(self) -> None:
        self.w("PAUSE")
        if self.seq is not None:
            self.operation = "stopped by pause while %s (step %s)" % (self._seq_name(),
                                                                      self.seq["step"])
            log.warning("%s: %s", self.name, self.operation)
            self.seq = None
        self._state = None

    def _action_clear_quench(self) -> None:
        self.checked("QUench 0")
        self._state = None
        s = self._tick(force=True)
        if s == QUENCH:
            raise ChannelError("%s: the quench condition is still in effect (an external "
                               "quench input?)" % self.name)
        self.fault = None
        self.operation = "idle (quench cleared; the supply is PAUSED)"

    # -- the persistent-switch procedures ---------------------------------------------------------
    def _done(self, msg: str) -> None:
        self.seq = None
        self.operation = "idle (%s)" % msg
        log.info("%s: %s", self.name, msg)

    def _fail(self, msg: str, pause: bool = False) -> None:
        name = self._seq_name()
        self.seq = None
        if pause:
            try:
                self.w("PAUSE")
            except Exception:
                pass
        self.operation = "stopped while %s: %s" % (name, msg)
        log.error("%s: %s", self.name, self.operation)

    def _advance(self, s: int) -> None:
        """One look at a running procedure: do the next step when its condition is
        met.  Every step reads the programmer's state again; nothing is assumed."""
        seq = self.seq
        now = time.monotonic()
        step = seq["step"]
        if seq["op"] == "enter":
            if step == "settle":
                if s not in STEADY:
                    return self._fail("the supply started %s while waiting for a steady "
                                      "field; the switch heater was left on" % STATES.get(s, s))
                if self._heater() is not True:
                    return self._fail("the switch heater went off (front panel?)")
                v = self._magnet_voltage()
                vmax = float(self.options.get("max_magnet_voltage", 0.05))
                if v is not None and abs(v) > vmax:
                    seq["since"] = now
                    self.operation = "entering persistent mode: waiting for the magnet " \
                                     "voltage (%.3g V) to decay" % v
                    return None
                if now - seq["since"] < float(self.options.get("settle_time", 10.0)):
                    return None
                self.checked("PSwitch 0")
                seq.update(step="cool", t0=now, seen=False,
                           deadline=now + 60 + 3 * self.cool_time)
                self.operation = "entering persistent mode: cooling the switch (%.0f s)" % \
                    self.cool_time
                return None
            if step == "cool":
                if s == COOLING:
                    seq["seen"] = True
                if s == COOLING or (not seq["seen"] and now - seq["t0"] < 3.0):
                    if now > seq["deadline"]:
                        return self._fail("the switch did not finish cooling in time")
                    return None
                if self._heater() is not False:
                    return self._fail("the switch heater is on again")
                if not self.options.get("zero_leads", True):
                    return self._done("persistent at %.5g T" % self._field_or_nan())
                if s not in (ZEROING, AT_ZERO):
                    self.checked("ZERO")
                is_ = abs(self.qf("CURRent:SUPPly?"))
                seq.update(step="zero", t0=now,
                           deadline=now + 120 + 3 * is_ / max(self.psw_rate, 1e-6))
                self.operation = "entering persistent mode: ramping the leads to zero"
                return None
            if step == "zero":
                if s == AT_ZERO:
                    return self._done("persistent at %.5g T, leads at zero"
                                      % self._field_or_nan())
                if s == ZEROING or now - seq["t0"] < 2.0:
                    if now > seq["deadline"]:
                        return self._fail("the leads did not reach zero in time", pause=True)
                    return None
                return self._fail("the ramp of the leads to zero stopped (%s); the magnet is "
                                  "persistent, check the programmer" % STATES.get(s, s))
        else:
            if step == "match":
                if s == HOLDING:
                    im = self.qf("CURRent:MAGnet?")
                    is_ = self.qf("CURRent:SUPPly?")
                    tol = float(self.options.get("match_tolerance", 0.05))
                    if abs(im - is_) <= tol:
                        # nothing may have changed since: heater still off, still holding
                        if self._heater() is not False or self._query_state() != HOLDING:
                            return self._fail("the programmer changed state just before "
                                              "heating; the switch was NOT heated", pause=True)
                        self.checked("PSwitch 1")
                        seq.update(step="heat", t0=now, seen=False,
                                   deadline=now + 60 + 3 * self.heat_time)
                        self.operation = "exiting persistent mode: heating the switch (%.0f s)" \
                            % self.heat_time
                        return None
                    seq.setdefault("held_since", now)
                    if now - seq["held_since"] > 30:
                        return self._fail("the supply holds %.4f A but the magnet carries %.4f A "
                                          "(more than match_tolerance = %g A apart); the switch "
                                          "was NOT heated" % (is_, im, tol), pause=True)
                    return None
                if s in (1, 4, 5) or (s in STEADY and now - seq["t0"] < 2.0):
                    if now > seq["deadline"]:
                        return self._fail("the supply did not reach the magnet current in time; "
                                          "the switch was NOT heated", pause=True)
                    return None
                return self._fail("the ramp to the magnet current stopped (%s); the switch was "
                                  "NOT heated" % STATES.get(s, s), pause=True)
            if step == "heat":
                if s == HEATING:
                    seq["seen"] = True
                if s == HEATING or (not seq["seen"] and now - seq["t0"] < 3.0):
                    if now > seq["deadline"]:
                        return self._fail("the switch did not finish heating in time")
                    return None
                if self._heater() is not True:
                    return self._fail("the switch heater is off again")
                self.checked("RAMP")            # hold the field, now driven
                self._state = None
                return self._done("driven at %.5g T" % self._field_or_nan())
        return None
