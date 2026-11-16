"""Yokogawa 7651 programmable DC source (voltage or current), native GP-IB commands.

The 7651 only sources; it does not measure.  Its single source channel follows
the ``mode`` setting:

=========  ===========  ==================================  ==================
mode       channel      ranges                              compliance
=========  ===========  ==================================  ==================
Voltage    ``voltage``  10 mV, 100 mV, 1 V, 10 V, 30 V      ``Icompliance`` (A, 5 … 120 mA)
Current    ``current``  1 mA, 10 mA, 100 mA                 ``Vcompliance`` (V, 1 … 30 V)
=========  ===========  ==================================  ==================

Settings: ``mode``, ``range`` (``AUTO`` or a fixed range), the compliance,
``output`` (ON / OFF).  A range takes up to 120 % of its nominal value (the 30 V
range up to 32 V, the 100 mA range up to 120 mA); values are rounded to the
range's resolution.  ``overload`` (read only) is ``YES`` while the output data
carries the ``E`` header, i.e. the output is held at the limiter.

With ``range`` = ``AUTO`` the driver picks the smallest range that holds the new
value.  While the output is ON it only ever switches to a *larger* range, never
down, so a sweep does not glitch the output by changing range on the way down.
The current limiter does not work on the 10 mV and 100 mV ranges (the hardware
trip at about ±0.6 V still does).  Changing the mode needs the output OFF; the
new function starts on its 1 V / 1 mA range at 0.

Protocol (IM 7651-01E, as used by a driver verified on the hardware):

* A program message is commands written one after the other, **without**
  separators: ``F1R4S+1.5E+0E``.  ``F`` (function), ``R`` (range), ``S`` (value)
  and ``O`` (output) are stored until ``E`` (execute), so every change is sent
  with its ``E`` in the same message.  ``LV`` / ``LA`` (limits) take effect at
  once, without ``E``; ``LA`` is only accepted in voltage mode and ``LV`` only in
  current mode (error 11).  A message holds at most 50 characters.
* On connect: ``H1`` (header in the output data), ``DL0`` (CR LF + EOI), ``OC``
  (status ``STS1=<n>``: 16 output on, 4 previous command rejected, 2 program
  running, 1 program being edited); a running program is stopped with ``RU0``,
  one being edited is refused.  ``OS`` (five lines: model ``MDL7651REV…``,
  ``F1R4S+1.0000E+0``, program, ``LV30LA120``, ``END``) gives the settings;
  nothing is changed, so a source that is on stays on.
* ``OD`` gives the programmed value (``NDCV+1.0000E+0``), not a measurement.
  Every setting is read back; a value the 7651 rejected (``OC`` bit 4) is
  reported as an error, a different value as a lost command (retried).
* There is no ``*IDN?`` (an unknown command, then a time-out).  A device clear
  restores the power-on panel (output off, all settings lost) and leaves the
  interface dead for seconds, so this driver never sends one, not even to
  recover from a failed call.

Address e.g. ``GPIB0::1::INSTR``; ``SIM::<name>`` uses the built-in emulator.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_EVEN, Decimal

from ..backend.instrument import ActionSpec, ChannelSpec
from ..backend.resources import GPIB_LIKE
from ..common.errors import ChannelError
from ._scpi import SCPIInstrument

#: mode -> [(range code, full scale, label, resolution)]
RANGES = {
    "Voltage": [(2, 10e-3, "10 mV", "1E-7"), (3, 100e-3, "100 mV", "1E-6"),
                (4, 1.0, "1 V", "1E-5"), (5, 10.0, "10 V", "1E-4"), (6, 30.0, "30 V", "1E-3")],
    "Current": [(4, 1e-3, "1 mA", "1E-8"), (5, 10e-3, "10 mA", "1E-7"),
                (6, 100e-3, "100 mA", "1E-6")],
}
FUNC = {"Voltage": 1, "Current": 5}
DEFAULT_RANGE = {"Voltage": 4, "Current": 4}     # after a mode change: 1 V / 1 mA
OVER = 1.2                      # a range takes 120 % of its nominal value ...
MAX = {"Voltage": 32.0, "Current": 0.12}   # ... the top ranges up to these
MAX_MESSAGE = 50
OC_ON, OC_ERROR, OC_RUNNING, OC_EDITING = 16, 4, 2, 1
_OD = re.compile(r"^([NE])DC([VA])\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?)$", re.I)
_OC = re.compile(r"^STS1\s*=\s*(\d+)$", re.I)


def _limit(mode: str, full: float) -> float:
    return min(full * OVER, MAX[mode])


def number(value: Decimal) -> str:
    """A source value as the 7651 takes it: sign, mantissa, full exponent
    (``+1.5E+0``, ``-2E-3``) - the ``E`` that follows it is the execute command."""
    if value == 0:
        return "+0E+0"
    mag = abs(value)
    exp = mag.adjusted()
    mant = format(mag.scaleb(-exp), "f").rstrip("0").rstrip(".") or "0"
    return "%s%sE%+d" % ("+" if value > 0 else "-", mant, exp)


class Yokogawa7651(SCPIInstrument):
    DESCRIPTION = "Yokogawa 7651 programmable DC voltage / current source"
    INTERFACES = GPIB_LIKE    # GPIB; COM / TCP: a Prologix GPIB adapter
    ACTIONS = [ActionSpec("zero", "set the output value to 0 (range and output unchanged)")]
    DEFAULT_OPTIONS = dict(SCPIInstrument.DEFAULT_OPTIONS, write_termination="\n",
                           read_termination="\n")

    @staticmethod
    def EMULATOR(options):                     # noqa: N802
        from .emulators.yokogawa import Yokogawa7651Emulator
        return Yokogawa7651Emulator(options)

    mode = "Voltage"
    range_setting = "AUTO"

    # -- channel list ---------------------------------------------------------------
    def get_channels(self):
        v = self.mode == "Voltage"
        top = _limit(self.mode, RANGES[self.mode][-1][1])
        return [
            ChannelSpec("mode", "rw", "str", (), "", "source function (change with the output OFF)",
                        config=True, allowed=["Voltage", "Current"]),
            ChannelSpec("voltage" if v else "current", "rw", "float", (), "V" if v else "A",
                        "source %s (±%g %s max); the programmed value, not a measurement"
                        % ("voltage" if v else "current", top if v else top * 1e3,
                           "V" if v else "mA")),
            ChannelSpec("range", "rw", "str", (), "", "source range; AUTO picks one (only "
                        "upwards while the output is ON)", config=True,
                        allowed=["AUTO"] + [r[2] for r in RANGES[self.mode]]),
            ChannelSpec("Icompliance" if v else "Vcompliance", "rw", "float", (), "A" if v else "V",
                        "current limit, 5 … 120 mA (1 mA steps; not on the mV ranges)" if v else
                        "voltage limit, 1 … 30 V (1 V steps)", config=True),
            ChannelSpec("output", "rw", "str", (), "", "output", config=True,
                        allowed=["ON", "OFF"]),
            ChannelSpec("overload", "r", "str", (), "", "YES while the output is held at the "
                        "limiter (output data header E)", allowed=["NO", "YES"]),
        ]

    # -- connection -------------------------------------------------------------------
    def connect(self):
        self.attach_emulator()
        self.select(self.resource)
        self.w("H1")                              # header in OD: NDCV+1.0000E+0
        self.w("DL0")                             # replies end in CR LF + EOI
        # the first reply: short, so an absent instrument does not hold the bus
        with self.resource.timeout_for(float(self.options["identify_timeout"])):
            code = self._status()
        if code & OC_EDITING:
            raise IOError("%s is editing a stored program: leave program editing on the "
                          "front panel, then Reconnect" % self.name)
        if code & OC_RUNNING:
            self.w("RU0")                         # stop the program; settings unchanged
        st = self._settings()
        self.idn = st["model"]
        self.mode = st["mode"]
        self.range_code = st["range"]
        self.range_setting = "AUTO"
        self.limits = {"V": st["LV"], "I": st["LA"]}
        self.value = self._od()[1]
        self.on = bool(self._status() & OC_ON)

    def recover(self) -> None:
        """No device clear: on the 7651 it restores the power-on panel."""

    def _settings(self) -> dict:
        """``OS``: model, ``F<f>R<r>S<value>``, program, ``LV<V>LA<mA>``, ``END``."""
        out = {}
        self.w("OS")
        lines = []
        for _ in range(8):
            line = self.resource.read().strip()
            lines.append(line)
            if line.upper() == "END":
                break
            if line.upper().startswith("MDL"):
                out["model"] = line
            m = re.match(r"F(\d)R(\d)S", line, re.I)
            if m:
                out["mode"] = {"1": "Voltage", "5": "Current"}.get(m.group(1))
                out["range"] = int(m.group(2))
            m = re.match(r"LV(\d+)LA(\d+)", line, re.I)
            if m:
                out["LV"], out["LA"] = int(m.group(1)), int(m.group(2))
        else:
            raise IOError("settings (OS) did not end with END: %r" % lines)
        if not out.get("model", "").upper().startswith("MDL7651"):
            raise IOError("%s: not a Yokogawa 7651 (settings %r)" % (self.name, lines))
        if out.get("mode") is None or "LV" not in out:
            raise IOError("incomplete settings (OS): %r" % lines)
        return out

    def _status(self) -> int:
        r = self.q("OC")
        m = _OC.match(r)
        if not m:
            raise IOError("unexpected status %r" % r[:40])
        return int(m.group(1))

    def _od(self):
        """(overloaded, programmed value) from ``OD``."""
        r = self.q("OD")
        m = _OD.match(r)
        if not m:
            raise IOError("unexpected output data %r" % r[:40])
        mode = "Voltage" if m.group(2).upper() == "V" else "Current"
        if mode != self.mode:
            raise IOError("%s sources %s, expected %s" % (self.name, mode, self.mode))
        return m.group(1).upper() == "E", float(m.group(3))

    def _send(self, message: str) -> None:
        if len(message) > MAX_MESSAGE:
            raise ChannelError("%s: message %r is longer than %d characters"
                               % (self.name, message, MAX_MESSAGE))
        self.w(message)

    def _rejected(self, what: str) -> None:
        """Raise when the 7651 rejected the previous message (OC bit 4)."""
        if self._status() & OC_ERROR:
            raise ChannelError("%s rejected %s" % (self.name, what))

    # -- I/O ----------------------------------------------------------------------------
    def read(self, channel):
        src = "voltage" if self.mode == "Voltage" else "current"
        if channel == src:
            self.value = self._od()[1]
            return self.value
        if channel == "overload":
            return "YES" if self._od()[0] else "NO"
        if channel == "mode":
            return self.mode
        if channel == "range":
            return self.range_setting
        if channel in ("Icompliance", "Vcompliance"):
            self._check_compliance_name(channel)
            if channel == "Icompliance":
                return self.limits["I"] * 1e-3
            return float(self.limits["V"])
        if channel == "output":
            self.on = bool(self._status() & OC_ON)
            return "ON" if self.on else "OFF"
        raise ChannelError("%s has no channel %s" % (self.name, channel))

    def write(self, channel, value):
        src = "voltage" if self.mode == "Voltage" else "current"
        if channel == src:
            self._set_value(float(value))
        elif channel == "mode":
            if value == self.mode:
                return
            self.on = bool(self._status() & OC_ON)
            if self.on:
                raise ChannelError("%s: switch the output OFF before changing the mode" % self.name)
            code = DEFAULT_RANGE[value]
            self._send("F%dR%dS%sE" % (FUNC[value], code, number(Decimal(0))))
            self._rejected("the mode change")
            self.mode = value
            self.range_code = code
            self.range_setting = "AUTO"
            self._verify(0.0, code)
        elif channel == "range":
            if value != "AUTO":
                code, full, _, res = self._range(value)
                if abs(self.value) > _limit(self.mode, full):
                    raise ChannelError("%s: the present value %g does not fit the %s range: set a "
                                       "smaller value first" % (self.name, self.value, value))
                q = self._quantize(self.value, res)
                self._send("R%dS%sE" % (code, number(q)))   # the value stays, on the new range
                self._verify(float(q), code, "the %s range" % value)
            self.range_setting = value
        elif channel in ("Icompliance", "Vcompliance"):
            self._check_compliance_name(channel)
            if channel == "Icompliance":
                ma = value * 1e3
                if abs(ma - round(ma)) > 1e-6 or not 5 <= round(ma) <= 120:
                    raise ChannelError("current limit must be 5 … 120 mA in 1 mA steps")
                self._send("LA%d" % round(ma))            # at once: no E
                self._rejected("current limit %d mA" % round(ma))
                self.limits["I"] = int(round(ma))
            else:
                if value != int(value) or not 1 <= value <= 30:
                    raise ChannelError("voltage limit must be 1 … 30 V in 1 V steps")
                self._send("LV%d" % int(value))
                self._rejected("voltage limit %d V" % int(value))
                self.limits["V"] = int(value)
        elif channel == "output":
            self._send("O1E" if value == "ON" else "O0E")
            self.on = bool(self._status() & OC_ON)
            if self.on != (value == "ON"):
                raise IOError("output did not switch %s" % value)
        else:
            raise ChannelError("%s: channel %s is not writable in %s mode"
                               % (self.name, channel, self.mode))

    def _check_compliance_name(self, channel: str) -> None:
        want = "Icompliance" if self.mode == "Voltage" else "Vcompliance"
        if channel != want:
            raise ChannelError("%s is in %s mode: the compliance is %s" % (self.name, self.mode, want))

    def _range(self, label: str):
        return next(r for r in RANGES[self.mode] if r[2] == label)

    @staticmethod
    def _quantize(v: float, res: str) -> Decimal:
        q = Decimal(res)
        return (Decimal(repr(v)) / q).quantize(Decimal(1), rounding=ROUND_HALF_EVEN) * q

    def _set_value(self, v: float) -> None:
        ranges = RANGES[self.mode]
        unit = "V" if self.mode == "Voltage" else "A"
        if self.range_setting == "AUTO":
            fits = [r for r in ranges if abs(v) <= _limit(self.mode, r[1])]
            if not fits:
                raise ChannelError("%s: %g %s is beyond the largest range (±%g %s)"
                                   % (self.name, v, unit, _limit(self.mode, ranges[-1][1]), unit))
            want = fits[0][0]
            cur = self.range_code
            if cur is None or (want > cur) or (want < cur and not self.on):
                # only upwards while the output is on: no range-change glitch on the way down
                cur = want
            code, _, _, res = next(r for r in ranges if r[0] == cur)
        else:
            code, full, label, res = self._range(self.range_setting)
            if abs(v) > _limit(self.mode, full):
                raise ChannelError("%s: %g %s does not fit the %s range (±%g %s)"
                                   % (self.name, v, unit, label, _limit(self.mode, full), unit))
        q = self._quantize(v, res)
        if code != self.range_code:
            self._send("R%dS%sE" % (code, number(q)))
        else:
            self._send("S%sE" % number(q))
        self._verify(float(q), code, "%g %s" % (v, unit))

    def _verify(self, v: float, code: int, what: str = "the setting") -> None:
        """Read the value back: a rejected message is an error, a different value a lost
        one.  *code* is the range the message selected, kept once it is confirmed."""
        if not self.options["check_errors"]:
            self.value, self.range_code = v, code
            return
        got = self._od()[1]
        res = float(next(r[3] for r in RANGES[self.mode] if r[0] == code))
        if abs(got - v) > res / 2 + 1e-6 * abs(v):
            self._rejected(what)
            # nothing rejected: the message was lost on the bus (retried by the framework)
            raise IOError("%s read back %g after setting %g" % (self.name, got, v))
        self.value, self.range_code = got, code

    def _action_zero(self):
        self._send("S%sE" % number(Decimal(0)))
        self._verify(0.0, self.range_code)


Yokogawa7651.CHANNELS = Yokogawa7651("static", "NONE").get_channels()
DRIVER = Yokogawa7651
