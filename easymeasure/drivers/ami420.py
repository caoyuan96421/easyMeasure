"""American Magnetics Model 420 power supply programmer, for superconducting magnets.

Written from the Model 420 manual (Rev. 7, chapter 4, "Remote Interface
Reference").  The 420 is not a slower 430: much of its command set is different,
and a query it does not know is answered with a beep, error -201 and **no reply**.
That is why the ``ami430`` driver times out on a 420.  What matters here:

* the target is the *programmed* field (``CONFigure:FIELD:PROGram``,
  ``FIELD:PROGram?``), and there is a single ramp rate (``RAMP:RATE:FIELD?``);
* there is one current measurement, the shunt in the supply output
  (``CURRent:MAGnet?``, ``FIELD:MAGnet?``).  In persistent mode it measures the
  current in the leads, not the magnet's;
* ``STATE?`` codes: 8 is HEATING SWITCH and 9 AT ZERO (on the 430, 8 is AT ZERO);
  there is no "cooling" state and no cooling time;
* no ``PSwitch:INSTalled?``: whether the magnet has a switch is not queryable.

Field
    Writing ``field`` sets the programmed field and starts the ramp (``RAMP``) and
    returns at once: ``status`` reads ``RAMPING``, then ``HOLDING``.

Persistent switch
    The 420 does not remember the magnet's current once the switch is cold (its
    manual tells the operator to write it down).  The driver does that: entering
    persistent mode records the current in a file of the easyMeasure home folder,
    *before* it turns the heater off, so it survives a restart of the backend.  The
    channel ``persistent_field`` shows it.  If it is unknown (a magnet left
    persistent by hand), ``exit_persistent`` is refused until you set
    ``persistent_field`` to the field the magnet was left at.

    ``enter_persistent``
        1. the supply holds the field (``HOLDING`` / ``PAUSED``); wait ``settle_time``
           s, and until the magnet voltage is below ``max_magnet_voltage``;
        2. record the current; switch heater off (``PSwitch 0``); wait ``cool_time``
           s while the switch cools (the 420 does not time this itself);
        3. ramp the leads to zero (``ZERO``, option ``zero_leads``) and wait for
           ``AT ZERO``.  Status: ``PERSISTENT``.

    ``exit_persistent``
        1. ramp the supply to the recorded current (``CONFigure:CURRent:PROGram``,
           ``RAMP``) and wait for ``HOLDING``;
        2. heat the switch (``PSwitch 1``) **only** if the supply current matches the
           recorded current within ``match_tolerance`` A;
        3. wait while the switch heats (``HEATING SWITCH``, the 420's heating time),
           then hold the field (``RAMP``).  Status: ``HOLDING``, driven.

Quench
    As with the 430: the programmer detects it, the driver raises
    :class:`~easymeasure.common.errors.InstrumentFault` (sweeps, scripts, the queue
    and user actions stop), and field writes and the persistent actions are refused
    until it is cleared (``clear_quench`` or RESET QUENCH).

Interfaces: RS-232 (9600 baud, 8 data bits, no parity, 1 stop bit by default; the
420 sends ``\\r\\n``) or IEEE-488 (primary address 22 by default).
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Optional

from ..backend.instrument import ActionSpec, ChannelSpec
from ..backend.logsys import get_logger
from ..common import paths
from ..common.errors import ChannelError, InstrumentFault
from ._scpi import SCPIInstrument, fmt, to_float

log = get_logger("ami420")

# STATE? of the Model 420 (manual table 4-5)
RAMPING, HOLDING, PAUSED, MANUAL_UP, MANUAL_DOWN, ZEROING, QUENCH, HEATING, AT_ZERO = range(1, 10)
STATES = {RAMPING: "RAMPING", HOLDING: "HOLDING", PAUSED: "PAUSED", MANUAL_UP: "MANUAL UP",
          MANUAL_DOWN: "MANUAL DOWN", ZEROING: "ZEROING", QUENCH: "QUENCH",
          HEATING: "HEATING SWITCH", AT_ZERO: "AT ZERO"}
STEADY = (HOLDING, PAUSED, AT_ZERO)          # the supply is not moving
MOVING = (RAMPING, MANUAL_UP, MANUAL_DOWN, ZEROING)


def _yes(v) -> Optional[bool]:
    s = str(v).strip().lower()
    if s in ("yes", "true", "1", "on"):
        return True
    if s in ("no", "false", "0", "off"):
        return False
    return None                              # "auto"


def memory_file(address: str) -> Path:
    """Where the persistent current of the magnet at *address* is kept."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", address).strip("_") or "magnet"
    return paths.home_dir() / "ami420" / ("%s.json" % safe)


class AMI420(SCPIInstrument):
    DESCRIPTION = "American Magnetics 420 magnet power supply programmer"
    INTERFACES = ("serial", "gpib", "tcp", "other", "sim")
    IDN_MATCH = ("AMERICAN MAGNETICS",)
    POLL_INTERVAL = 1.0
    CHANNELS = [
        ChannelSpec("field", "rw", "float", (), "T",
                    "set: ramp to this field (returns at once; status RAMPING, then HOLDING); "
                    "read: the magnet's field (persistent: the recorded field)"),
        ChannelSpec("target", "r", "float", (), "T", "the programmed field"),
        ChannelSpec("supply_field", "r", "float", (), "T",
                    "the field of the supply current (in persistent mode: of the current in "
                    "the leads, not in the magnet)"),
        ChannelSpec("current", "r", "float", (), "A",
                    "the magnet current (persistent: the recorded current)"),
        ChannelSpec("supply_current", "r", "float", (), "A", "the supply (shunt) current"),
        ChannelSpec("voltage", "r", "float", (), "V", "the magnet voltage (voltage taps)"),
        ChannelSpec("supply_voltage", "r", "float", (), "V",
                    "the supply voltage the programmer commands"),
        ChannelSpec("ramp_rate", "r", "float", (), "T/min", "ramp rate (set on the programmer)"),
        ChannelSpec("status", "r", "str", (), "",
                    "RAMPING, HOLDING, PAUSED, ZEROING, AT ZERO, PERSISTENT, HEATING SWITCH, "
                    "COOLING SWITCH, ENTERING PERSISTENT, EXITING PERSISTENT, QUENCH ..."),
        ChannelSpec("switch", "r", "str", (), "",
                    "persistent switch heater: ON (driven), OFF (persistent), HEATING, "
                    "COOLING, NONE"),
        ChannelSpec("quench", "r", "str", (), "", "Yes while a quench is in effect"),
        ChannelSpec("operation", "r", "str", (), "",
                    "the persistent-switch procedure: its step, idle, or why it stopped"),
        ChannelSpec("persistent_field", "rw", "float", (), "T",
                    "the field the magnet was left at in persistent mode (the 420 does not "
                    "know it): recorded by enter_persistent; set it by hand only for a magnet "
                    "made persistent without this driver.  NaN: unknown or driven"),
        ChannelSpec("IsPersistentSwitchInstalled", "rw", "str", (), "",
                    "the magnet has a persistent switch: field changes need the heater on, "
                    "and enter/exit_persistent are available", config=True,
                    allowed=["Yes", "No"]),
    ]
    ACTIONS = [
        ActionSpec("enter_persistent", "Enter Persistent: wait for a steady field, record the "
                   "current, switch heater off, wait for it to cool, ramp the leads to zero"),
        ActionSpec("exit_persistent", "Exit Persistent: ramp the supply to the recorded "
                   "current, heat the switch only when they match, hold the field"),
        ActionSpec("pause", "PAUSE the supply (and stop a persistent-switch procedure)"),
        ActionSpec("clear_quench", "clear the quench condition (like RESET QUENCH)"),
    ]
    # the 420 ends its replies with \r\n (serial) or \n + EOI (IEEE-488) and takes \n
    DEFAULT_OPTIONS = dict(SCPIInstrument.DEFAULT_OPTIONS, read_termination="\n",
                           write_termination="\n", timeout=5.0, baudrate=9600,
                           IsPersistentSwitchInstalled="auto", settle_time=10.0,
                           max_magnet_voltage=0.05, match_tolerance=0.05, zero_leads=True,
                           cool_time=60.0, status_interval=0.25)

    @staticmethod
    def EMULATOR(options):                     # noqa: N802
        from .emulators.ami420 import AMI420Emulator
        return AMI420Emulator(options)

    fault: Optional[str] = None

    # -- connection -----------------------------------------------------------------------
    def identify(self) -> str:
        idn = super().identify()
        u = idn.upper()
        if "430" in u and "420" not in u:
            raise IOError("%s: %r is a Model 430: use the ami430 driver" % (self.name, idn))
        return idn

    def connect(self):
        self.identify()
        self.clear_errors()
        self.read_errors()                    # an earlier session's errors
        self.units_to_T = 0.1 if self.qf("FIELD:UNITS?") < 0.5 else 1.0     # kG or T
        self.coil = self.qf("COILconst?") * self.units_to_T                  # T/A
        if not self.coil > 0:
            raise IOError("%s: the programmer has no coil constant; set it in its Load setup "
                          "menu" % self.name)
        self.per_min = 60.0 if self.qf("RAMP:RATE:UNITS?") < 0.5 else 1.0
        self.limit = self.qf("CURRent:LIMit?")
        self.heat_time = self.qf("PSwitch:TIME?")
        heater_ma = self.qf("PSwitch:CURRent?")
        heater = self.qb("PSwitch?")
        opt = _yes(self.options.get("IsPersistentSwitchInstalled", "auto"))
        if opt is not None:
            self.switch_installed = opt
        else:
            # not queryable on the 420: a heater that is on, or a heater current set in
            # the Load menu, says there is one (when unsure: Yes, the safe side)
            self.switch_installed = heater or heater_ma > 0
        self.seq: Optional[dict] = None
        self.operation = "idle"
        self.cool_until = 0.0
        self.memory_path = memory_file(self.address)
        self.memory = self._load_memory()
        if heater and self.memory is not None:
            log.warning("%s: the switch heater is on: the magnet is driven, the recorded "
                        "persistent current (%.5g A) is dropped" % (self.name,
                                                                     self.memory["current"]))
            self._forget()
        if self.switch_installed and not heater:
            self.operation = ("idle (persistent at %.5g T, recorded)" % self.memory["field"]
                              if self.memory else
                              "idle (persistent; the field it was left at is unknown: set "
                              "persistent_field before exit_persistent)")
        self.fault = None
        self._state = self._query_state()
        self._state_t = time.monotonic()

    def read_errors(self):
        out = []
        for _ in range(12):
            r = self.q("SYSTem:ERRor?")
            code = r.split(",", 1)[0].strip()
            try:
                if int(float(code)) == 0:
                    break
            except ValueError:
                raise IOError("bad error-queue reply %r" % r[:60]) from None
            out.append(r)
        return out

    # -- the recorded persistent current ---------------------------------------------------
    def _load_memory(self) -> Optional[dict]:
        try:
            m = json.loads(self.memory_path.read_text())
            float(m["current"])
            return m
        except FileNotFoundError:
            return None
        except Exception as exc:
            log.warning("%s: %s cannot be read (%s): the persistent field is unknown",
                        self.name, self.memory_path, exc)
            return None

    def _remember(self, current: float, how: str) -> None:
        m = {"current": float(current), "field": float(current) * self.coil,
             "coil_T_per_A": self.coil, "how": how, "time": time.time(),
             "when": time.strftime("%Y-%m-%d %H:%M:%S"), "instrument": self.name,
             "address": self.address}
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.memory_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, indent=1))
        tmp.replace(self.memory_path)          # written in full or not at all
        self.memory = m
        log.info("%s: persistent current %.6g A (%.6g T) recorded in %s", self.name, current,
                 m["field"], self.memory_path)

    def _forget(self) -> None:
        self.memory = None
        try:
            self.memory_path.unlink()
        except FileNotFoundError:
            pass

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
                self.fault = "QUENCH detected by the programmer%s (supply at %.4g T when " \
                             "detected); clear it with clear_quench once the magnet is safe" \
                             % (what, b)
                if self.seq is not None:
                    self.operation = "stopped: quench during %s" % self._seq_name()
                    self.seq = None
                log.error("%s: %s", self.name, self.fault)
                raise InstrumentFault(self.fault)
        elif self.fault is not None:
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
        if not self.switch_installed:
            return None
        return self.qb("PSwitch?")

    def _persistent(self) -> bool:
        return bool(self.switch_installed) and self._heater() is False

    def _cooling(self) -> bool:
        return time.monotonic() < self.cool_until

    def _refuse_when_faulted(self, what: str) -> None:
        if self.fault is not None:
            raise InstrumentFault("%s: cannot %s: %s" % (self.name, what, self.fault))

    def _seq_name(self) -> str:
        if self.seq is None:
            return ""
        return "entering persistent mode" if self.seq["op"] == "enter" else \
            "exiting persistent mode"

    def _rate_A_s(self) -> float:
        """The ramp rate in A/s (for the time a ramp may take)."""
        try:
            return max(abs(self.qf("RAMP:RATE:CURRent?")) * self.per_min / 60.0, 1e-6)
        except Exception:
            return 1e-3

    def poll(self) -> None:
        self._tick(force=True)

    # -- reading -------------------------------------------------------------------------------
    def read(self, channel: str):
        s = self._tick()
        if channel in ("field", "current"):
            if self._persistent():
                if self.memory is None:
                    return math.nan
                return self.memory["field"] if channel == "field" else self.memory["current"]
            if channel == "field":
                return self.qf("FIELD:MAGnet?") * self.units_to_T
            return self.qf("CURRent:MAGnet?")
        if channel == "target":
            return self.qf("FIELD:PROGram?") * self.units_to_T
        if channel == "supply_field":
            return self.qf("CURRent:MAGnet?") * self.coil
        if channel == "supply_current":
            return self.qf("CURRent:MAGnet?")
        if channel == "voltage":
            return self.qf("VOLTage:MAGnet?")
        if channel == "supply_voltage":
            return self.qf("VOLTage:SUPPly?")
        if channel == "ramp_rate":
            return to_float(self.q("RAMP:RATE:FIELD?")) * self.units_to_T * self.per_min
        if channel == "status":
            return self._status(s)
        if channel == "switch":
            return self._switch_text(s)
        if channel == "quench":
            return "Yes" if s == QUENCH or self.fault is not None else "No"
        if channel == "operation":
            return self.operation
        if channel == "persistent_field":
            return self.memory["field"] if self.memory is not None else math.nan
        if channel == "IsPersistentSwitchInstalled":
            return "Yes" if self.switch_installed else "No"
        raise ChannelError("%s has no channel %s" % (self.name, channel))

    def _status(self, s: int) -> str:
        if s == QUENCH or self.fault is not None:
            return "QUENCH"
        if self.seq is not None:
            return "ENTERING PERSISTENT" if self.seq["op"] == "enter" else "EXITING PERSISTENT"
        if s == HEATING:
            return STATES[HEATING]
        if self._cooling():
            return "COOLING SWITCH"
        if s in STEADY and self._persistent():
            return "PERSISTENT"
        return STATES.get(s, "STATE %d" % s)

    def _switch_text(self, s: int) -> str:
        if not self.switch_installed:
            return "NONE"
        if s == HEATING:
            return "HEATING"
        if self._cooling():
            return "COOLING"
        return "ON" if self._heater() else "OFF"

    # -- writing --------------------------------------------------------------------------------
    def write(self, channel: str, value):
        if channel == "field":
            self._set_field(float(value))
        elif channel == "persistent_field":
            self._set_persistent_field(float(value))
        elif channel == "IsPersistentSwitchInstalled":
            self._set_switch_installed(_yes(value))
        else:
            raise ChannelError("%s: %s cannot be set" % (self.name, channel))

    def _busy_checks(self, s: int) -> None:
        if self.seq is not None:
            raise ChannelError("%s is busy %s (%s)" % (self.name, self._seq_name(), self.operation))
        if s == HEATING:
            raise ChannelError("%s: the persistent switch is heating: wait until it is done"
                               % self.name)
        if self._cooling():
            raise ChannelError("%s: the persistent switch is cooling (%.0f s more): wait until "
                               "it is done" % (self.name, self.cool_until - time.monotonic()))

    def _set_field(self, b: float) -> None:
        s = self._tick(force=True)
        self._refuse_when_faulted("set the field")
        self._busy_checks(s)
        if self._persistent():
            raise ChannelError(
                "%s: the magnet is in persistent mode (switch heater off): run exit_persistent "
                "first - ramping now would only change the current in the leads" % self.name)
        if self.limit and abs(b) > self.limit * self.coil * (1 + 1e-9):
            raise ChannelError("%s: %g T is beyond the programmer's current limit (%g T)"
                               % (self.name, b, self.limit * self.coil))
        self.checked("CONFigure:FIELD:PROGram %s" % fmt(b / self.units_to_T))
        self.checked("RAMP")
        self._state = None

    def _set_persistent_field(self, b: float) -> None:
        s = self._tick(force=True)
        self._busy_checks(s)
        if not self._persistent():
            raise ChannelError("%s: the magnet is driven (switch heater on): its field is the "
                               "supply's, there is no persistent field to set" % self.name)
        if not math.isfinite(b) or (self.limit and abs(b) > self.limit * self.coil * (1 + 1e-9)):
            raise ChannelError("%s: %g T is not a field this magnet can carry" % (self.name, b))
        self._remember(b / self.coil, "set by hand")
        self.operation = "idle (persistent at %.5g T, set by hand)" % b

    def _set_switch_installed(self, on: Optional[bool]) -> None:
        if on is None:
            raise ChannelError("IsPersistentSwitchInstalled is Yes or No")
        s = self._tick(force=True)
        self._busy_checks(s)
        if not on and self.switch_installed and self._heater() is False:
            raise ChannelError("%s: the switch is off (persistent): exit persistent mode first"
                               % self.name)
        self.switch_installed = on

    # -- actions ---------------------------------------------------------------------------------
    def _common_checks(self, what: str) -> int:
        s = self._tick(force=True)
        self._refuse_when_faulted(what)
        if not self.switch_installed:
            raise ChannelError("%s: no persistent switch (IsPersistentSwitchInstalled = No)"
                               % self.name)
        self._busy_checks(s)
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
        if self.memory is None:
            raise ChannelError("%s: the field the magnet was left at is unknown (the Model 420 "
                               "does not record it): set persistent_field to it first" % self.name)
        if s not in STEADY:
            raise ChannelError("%s is %s: wait until the supply is steady" % (
                self.name, STATES.get(s, s)))
        im = float(self.memory["current"])
        if self.limit and abs(im) > self.limit * (1 + 1e-9):
            raise ChannelError("%s: the recorded current %.5g A is beyond the current limit "
                               "(%.5g A)" % (self.name, im, self.limit))
        self.checked("CONFigure:CURRent:PROGram %s" % fmt(im))
        self.checked("RAMP")
        now = time.monotonic()
        diff = abs(self.qf("CURRent:MAGnet?") - im)
        self.seq = {"op": "exit", "step": "match", "t0": now, "current": im,
                    "deadline": now + 60 + 3 * diff / self._rate_A_s()}
        self.operation = "exiting persistent mode: ramping the supply to the recorded " \
                         "%.5g A (%.5g T)" % (im, im * self.coil)
        log.info("%s: exiting persistent mode at %.5g A", self.name, im)
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
        self.operation = "idle (quench cleared)"

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
                v = self.qf("VOLTage:MAGnet?")
                vmax = float(self.options.get("max_magnet_voltage", 0.05))
                if abs(v) > vmax:
                    seq["since"] = now
                    self.operation = "entering persistent mode: waiting for the magnet " \
                                     "voltage (%.3g V) to decay" % v
                    return None
                if now - seq["since"] < float(self.options.get("settle_time", 10.0)):
                    return None
                # the current goes on record before the heater goes off
                im = self.qf("CURRent:MAGnet?")
                try:
                    self._remember(im, "enter_persistent")
                except OSError as exc:
                    return self._fail("the persistent current could not be recorded (%s); the "
                                      "switch heater was left on" % exc)
                self.checked("PSwitch 0")
                cool = float(self.options.get("cool_time", 60.0))
                self.cool_until = now + cool
                seq.update(step="cool", t0=now)
                self.operation = "entering persistent mode: cooling the switch (%.0f s)" % cool
                return None
            if step == "cool":
                if self._heater() is not False:
                    self._forget()
                    return self._fail("the switch heater is on again")
                if s not in STEADY:
                    return self._fail("the supply started %s while the switch cooled"
                                      % STATES.get(s, s), pause=True)
                if self._cooling():
                    return None
                if not self.options.get("zero_leads", True):
                    return self._done("persistent at %.5g T" % self.memory["field"])
                if s != AT_ZERO:
                    self.checked("ZERO")
                is_ = abs(self.qf("CURRent:MAGnet?"))
                seq.update(step="zero", t0=now, deadline=now + 120 + 3 * is_ / self._rate_A_s())
                self.operation = "entering persistent mode: ramping the leads to zero"
                return None
            if step == "zero":
                if s == AT_ZERO:
                    return self._done("persistent at %.5g T, leads at zero" % self.memory["field"])
                if s == ZEROING or now - seq["t0"] < 2.0:
                    if now > seq["deadline"]:
                        return self._fail("the leads did not reach zero in time", pause=True)
                    return None
                return self._fail("the ramp of the leads to zero stopped (%s); the magnet is "
                                  "persistent, check the programmer" % STATES.get(s, s))
        else:
            if step == "match":
                im = seq["current"]
                if s == HOLDING:
                    is_ = self.qf("CURRent:MAGnet?")
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
                if s in MOVING or (s in STEADY and now - seq["t0"] < 2.0):
                    if now > seq["deadline"]:
                        return self._fail("the supply did not reach the recorded current in "
                                          "time; the switch was NOT heated", pause=True)
                    return None
                return self._fail("the ramp to the recorded current stopped (%s); the switch "
                                  "was NOT heated" % STATES.get(s, s), pause=True)
            if step == "heat":
                if s == HEATING:
                    seq["seen"] = True
                if s == HEATING or (not seq["seen"] and now - seq["t0"] < 3.0):
                    if now > seq["deadline"]:
                        return self._fail("the switch did not finish heating in time")
                    return None
                if self._heater() is not True:
                    return self._fail("the switch heater is off again")
                self._forget()                  # driven: the supply carries the current
                self.checked("RAMP")            # hold the field (the 420 PAUSEs after heating)
                self._state = None
                return self._done("driven at %.5g T" % self._field_or_nan())
        return None
