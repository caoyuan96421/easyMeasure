"""Yokogawa 7651 programmable DC source (voltage or current).

The 7651 only sources; it does not measure.  Its single source channel follows
the ``mode`` setting:

=========  ===========  ==================================  ==================
mode       channel      ranges                              compliance
=========  ===========  ==================================  ==================
Voltage    ``voltage``  10 mV, 100 mV, 1 V, 10 V, 30 V      ``Icompliance`` (A, 5 … 120 mA)
Current    ``current``  1 mA, 10 mA, 100 mA                 ``Vcompliance`` (V, 1 … 30 V)
=========  ===========  ==================================  ==================

Settings: ``mode``, ``range`` (``AUTO`` or a fixed range), the compliance,
``output`` (ON / OFF).  A range takes up to 120 % of its nominal value
(the 30 V range up to 32 V).

With ``range`` = ``AUTO`` the driver picks the smallest range that holds the new
value.  While the output is ON it only ever switches to a *larger* range, never
down, so a sweep does not glitch the output by changing range on the way down.
Changing the mode needs the output OFF.

Every setting is followed by ``E`` (execute) and confirmed by reading the output
data (``OD``) back: a value the instrument did not take raises an error
(option ``check_errors``).  Address e.g. ``GPIB0::1::INSTR``; ``SIM::<name>``
uses the built-in emulator.

Commands used (7651 HP-IB/GP-IB command set): ``F1``/``F5`` (function),
``R2`` … ``R6`` (range), ``S<value>`` (set), ``E`` (execute), ``O1``/``O0``
(output), ``LV<V>``/``LA<mA>`` (limits), ``OD`` (output data), ``OC`` (status)
and ``OS`` (settings).
"""

from __future__ import annotations

import math
import re

from ..backend.instrument import ActionSpec, ChannelSpec
from ..backend.resources import GPIB_LIKE
from ..common.errors import ChannelError
from ._scpi import SCPIInstrument

#: mode -> [(range code, full scale, label)]
RANGES = {
    "Voltage": [(2, 10e-3, "10 mV"), (3, 100e-3, "100 mV"), (4, 1.0, "1 V"), (5, 10.0, "10 V"),
                (6, 30.0, "30 V")],
    "Current": [(4, 1e-3, "1 mA"), (5, 10e-3, "10 mA"), (6, 100e-3, "100 mA")],
}
FUNC = {"Voltage": 1, "Current": 5}
OVER = 1.2                      # a range takes 120 % of its nominal value ...
MAX = {"Voltage": 32.0, "Current": 0.12}   # ... the top ranges up to these
_OD = re.compile(r"^\s*([NE])?(DC[VA])?\s*([+-]?\d+(?:\.\d*)?(?:E[+-]?\d+)?)\s*$", re.I)


def _limit(mode: str, full: float) -> float:
    return min(full * OVER, MAX[mode])


class Yokogawa7651(SCPIInstrument):
    DESCRIPTION = "Yokogawa 7651 programmable DC voltage / current source"
    INTERFACES = GPIB_LIKE    # GPIB; COM / TCP: RS-232 where it has one, or a Prologix GPIB adapter
    ACTIONS = [ActionSpec("zero", "set the output value to 0 (range and output unchanged)")]
    DEFAULT_OPTIONS = dict(SCPIInstrument.DEFAULT_OPTIONS, write_termination="\r\n")

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
                        "source %s (±%g %s max)" % ("voltage" if v else "current",
                                                    top if v else top * 1e3, "V" if v else "mA")),
            ChannelSpec("range", "rw", "str", (), "", "source range; AUTO picks one (only "
                        "upwards while the output is ON)", config=True,
                        allowed=["AUTO"] + [r[2] for r in RANGES[self.mode]]),
            ChannelSpec("Icompliance" if v else "Vcompliance", "rw", "float", (), "A" if v else "V",
                        "current limit, 5 … 120 mA (1 mA steps)" if v else
                        "voltage limit, 1 … 30 V (1 V steps)", config=True),
            ChannelSpec("output", "rw", "str", (), "", "output relay", config=True,
                        allowed=["ON", "OFF"]),
        ]

    # -- connection -------------------------------------------------------------------
    def connect(self):
        self.attach_emulator()
        self.select(self.resource)
        self.range_setting = str(self.options.get("range", "AUTO"))
        st = self._settings()                     # mode, range, limits from OS
        od = self._od()                           # value (and function) from OD
        self.mode = st.get("mode") or od[0] or "Voltage"
        self.range_code = st.get("range")         # None: unknown until we set one
        self.value = od[1]
        self.limits = {"V": st.get("LV"), "I": st.get("LA")}
        self.on = self._read_output()
        if self.range_setting != "AUTO" and self.range_setting not in [
                r[2] for r in RANGES[self.mode]]:
            self.range_setting = "AUTO"

    def _settings(self) -> dict:
        """``OS``: several lines ending with END, e.g. ``F1R4S+1.0000E+0`` / ``LV30LA120``."""
        out = {}
        try:
            self.w("OS")
            for _ in range(8):
                line = self.resource.read().strip()
                m = re.search(r"F(\d)R(\d)S", line, re.I)
                if m:
                    out["mode"] = "Voltage" if m.group(1) == "1" else "Current"
                    out["range"] = int(m.group(2))
                m = re.search(r"LV(\d+)LA(\d+)", line, re.I)
                if m:
                    out["LV"], out["LA"] = int(m.group(1)), int(m.group(2))
                if line.upper().startswith("END"):
                    break
        except Exception as exc:
            self.resource.clear()
            raise IOError("cannot read the settings (OS): %s" % exc) from None
        return out

    def _od(self):
        r = self.q("OD")
        m = _OD.match(r)
        if not m:
            raise IOError("unexpected output data %r" % r[:40])
        if (m.group(1) or "N").upper() == "E":
            raise ChannelError("%s reports an error (output data %r): value out of range "
                               "for the range or limit" % (self.name, r.strip()))
        mode = {"DCV": "Voltage", "DCA": "Current"}.get((m.group(2) or "").upper())
        return mode, float(m.group(3))

    def _read_output(self) -> bool:
        r = self.q("OC")                          # STS1=<status byte>, bit 4 = output on
        m = re.search(r"(\d+)\s*$", r)
        if not m:
            raise IOError("unexpected status %r" % r[:40])
        return bool(int(m.group(1)) & 16)

    def _cmd(self, c: str) -> None:
        self.w(c + ";E")                          # every setting is executed by E

    # -- I/O ----------------------------------------------------------------------------
    def read(self, channel):
        src = "voltage" if self.mode == "Voltage" else "current"
        if channel == src:
            mode, self.value = self._od()
            return self.value
        if channel == "mode":
            mode, _ = self._od()
            if mode:
                self.mode = mode
            return self.mode
        if channel == "range":
            return self.range_setting
        if channel in ("Icompliance", "Vcompliance"):
            self._check_compliance_name(channel)
            if channel == "Icompliance":
                return math.nan if self.limits["I"] is None else self.limits["I"] * 1e-3
            return math.nan if self.limits["V"] is None else float(self.limits["V"])
        if channel == "output":
            self.on = self._read_output()
            return "ON" if self.on else "OFF"
        raise ChannelError("%s has no channel %s" % (self.name, channel))

    def write(self, channel, value):
        src = "voltage" if self.mode == "Voltage" else "current"
        if channel == src:
            self._set_value(float(value))
        elif channel == "mode":
            if value == self.mode:
                return
            if self.on or self._read_output():
                self.on = True
                raise ChannelError("%s: switch the output OFF before changing the mode" % self.name)
            self._cmd("F%d" % FUNC[value])
            self.mode = value
            self.range_code = None
            self.range_setting = "AUTO"
            self._verify()
        elif channel == "range":
            self.range_setting = value
            if value != "AUTO":
                code, full, _ = next(r for r in RANGES[self.mode] if r[2] == value)
                if abs(self.value) > _limit(self.mode, full):
                    raise ChannelError("%s: the present value %g does not fit the %s range: set a "
                                       "smaller value first" % (self.name, self.value, value))
                self._cmd("R%d" % code)
                self.range_code = code
                self._cmd("S%+.5E" % self.value)  # keep the value on the new range
                self._verify()
        elif channel in ("Icompliance", "Vcompliance"):
            self._check_compliance_name(channel)
            if channel == "Icompliance":
                ma = value * 1e3
                if abs(ma - round(ma)) > 1e-6 or not 5 <= round(ma) <= 120:
                    raise ChannelError("current limit must be 5 … 120 mA in 1 mA steps")
                self._cmd("LA%d" % round(ma))
                self.limits["I"] = int(round(ma))
            else:
                if value != int(value) or not 1 <= value <= 30:
                    raise ChannelError("voltage limit must be 1 … 30 V in 1 V steps")
                self._cmd("LV%d" % int(value))
                self.limits["V"] = int(value)
        elif channel == "output":
            self._cmd("O1" if value == "ON" else "O0")
            self.on = self._read_output()
            if self.on != (value == "ON"):
                raise IOError("output did not switch %s" % value)
        else:
            raise ChannelError("%s: channel %s is not writable in %s mode"
                               % (self.name, channel, self.mode))

    def _check_compliance_name(self, channel: str) -> None:
        want = "Icompliance" if self.mode == "Voltage" else "Vcompliance"
        if channel != want:
            raise ChannelError("%s is in %s mode: the compliance is %s" % (self.name, self.mode, want))

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
                self._cmd("R%d" % want)
                self.range_code = want
        else:
            code, full, label = next(r for r in ranges if r[2] == self.range_setting)
            if abs(v) > _limit(self.mode, full):
                raise ChannelError("%s: %g %s does not fit the %s range (±%g %s)"
                                   % (self.name, v, unit, label, _limit(self.mode, full), unit))
        self._cmd("S%+.5E" % v)
        self._verify(v)

    def _verify(self, v: float = None) -> None:
        """Read the output data back: an error flag or a different value is refused."""
        if not self.options.get("check_errors", True):
            if v is not None:
                self.value = v
            return
        mode, got = self._od()
        if mode and mode != self.mode:
            raise IOError("function is %s, expected %s" % (mode, self.mode))
        if v is not None:
            full = next((r[1] for r in RANGES[self.mode] if r[0] == self.range_code), None)
            tol = 1e-5 * (full or abs(v) or 1.0) + 1e-12
            if abs(got - v) > tol:
                # e.g. a command lost on the bus: retried by the framework
                raise IOError("%s read back %g after setting %g" % (self.name, got, v))
        self.value = got

    def _action_zero(self):
        self._cmd("S%+.5E" % 0.0)
        self._verify(0.0)


Yokogawa7651.CHANNELS = Yokogawa7651.get_channels(Yokogawa7651.__new__(Yokogawa7651))
DRIVER = Yokogawa7651
