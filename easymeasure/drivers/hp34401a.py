"""HP / Agilent / Keysight 34401A 6½-digit multimeter.

One reading channel whose name and unit follow the ``mode`` setting, so the
channel list stays short and an alias can never record a current under a
voltage name:

=========  ==================  =====
mode       reading channel     unit
=========  ==================  =====
``vdc``    ``Vdc``             V
``vac``    ``Vac``             V (rms)
``idc``    ``Idc``             A
``iac``    ``Iac``             A (rms)
``ohm``    ``R2w``             Ω
``ohm4w``  ``R4w``             Ω
=========  ==================  =====

An alias on the reading channel reports an error once the mode no longer
matches (re-point it, or use a sweep's pre-sweep constants to set the mode).

Settings: ``mode``, ``range`` (``AUTO`` or a fixed range of the mode),
``nplc`` (0.02 / 0.2 / 1 / 10 / 100, DC and resistance modes), ``autozero``
(DC and resistance modes), ``ac_filter`` (3 / 20 / 200 Hz, AC modes).
Readings above the range (``OVLD``) are returned as ±inf.

Address e.g. ``GPIB0::22::INSTR``, or ``ASRL1::INSTR`` / ``COM1`` (RS-232: the
driver sends ``SYST:REM``; use 9600 baud, 8N1 / DTR-DSR handshake on the meter).
``SIM::<name>`` uses the built-in emulator.
"""

from __future__ import annotations

from ..backend.instrument import ActionSpec, ChannelSpec
from ..common.errors import ChannelError
from ._scpi import SCPIInstrument

MODES = ["vdc", "vac", "idc", "iac", "ohm", "ohm4w"]
#: mode -> (FUNC name, command prefix, reading channel, unit, description)
FUNCS = {
    "vdc": ("VOLT:DC", "VOLT:DC", "Vdc", "V", "DC voltage"),
    "vac": ("VOLT:AC", "VOLT:AC", "Vac", "V", "AC voltage (rms)"),
    "idc": ("CURR:DC", "CURR:DC", "Idc", "A", "DC current"),
    "iac": ("CURR:AC", "CURR:AC", "Iac", "A", "AC current (rms)"),
    "ohm": ("RES", "RES", "R2w", "Ω", "2-wire resistance"),
    "ohm4w": ("FRES", "FRES", "R4w", "Ω", "4-wire resistance"),
}
RANGES = {
    "vdc": [(0.1, "100 mV"), (1, "1 V"), (10, "10 V"), (100, "100 V"), (1000, "1000 V")],
    "vac": [(0.1, "100 mV"), (1, "1 V"), (10, "10 V"), (100, "100 V"), (750, "750 V")],
    "idc": [(0.01, "10 mA"), (0.1, "100 mA"), (1, "1 A"), (3, "3 A")],
    "iac": [(1, "1 A"), (3, "3 A")],
    "ohm": [(1e2, "100 Ω"), (1e3, "1 kΩ"), (1e4, "10 kΩ"), (1e5, "100 kΩ"), (1e6, "1 MΩ"),
            (1e7, "10 MΩ"), (1e8, "100 MΩ")],
}
RANGES["ohm4w"] = RANGES["ohm"]
NPLC = [0.02, 0.2, 1, 10, 100]
AC_FILTERS = ["3 Hz", "20 Hz", "200 Hz"]


def _is_ac(mode: str) -> bool:
    return mode in ("vac", "iac")


class HP34401A(SCPIInstrument):
    DESCRIPTION = "HP/Agilent 34401A 6.5 digit multimeter (V, I, R; DC and AC)"
    IDN_MATCH = ("34401",)
    ACTIONS = [ActionSpec("clear_status", "*CLS: clear the error queue"),
               ActionSpec("beep", "beep once")]
    DEFAULT_OPTIONS = dict(SCPIInstrument.DEFAULT_OPTIONS, timeout=5.0)

    @staticmethod
    def EMULATOR(options):                     # noqa: N802
        from .emulators.dmm import HP34401Emulator
        return HP34401Emulator(options)

    mode = "vdc"
    nplc = 10.0
    ac_filter = "20 Hz"

    # -- channel list ---------------------------------------------------------------
    def get_channels(self):
        m = self.mode
        _, _, name, unit, what = FUNCS[m]
        if _is_ac(m):
            # 3 Hz filter: ~7 s to settle; 20 Hz ~1 s
            t = {"3 Hz": 30.0, "20 Hz": 10.0}.get(self.ac_filter, 5.0)
        else:
            t = 5.0 + 0.1 * self.nplc            # NPLC 100 with auto-zero: ~4 s per reading
        ch = [
            ChannelSpec(name, "r", "float", (), unit, what + " (name follows the mode)", timeout=t),
            ChannelSpec("mode", "rw", "str", (), "", "measurement function", config=True,
                        allowed=MODES),
            ChannelSpec("range", "rw", "str", (), "", "measurement range", config=True,
                        allowed=["AUTO"] + [label for _, label in RANGES[m]]),
        ]
        if _is_ac(m):
            ch.append(ChannelSpec("ac_filter", "rw", "str", (), "", "AC filter (lowest signal "
                                  "frequency); slower filters settle longer", config=True,
                                  allowed=AC_FILTERS))
        else:
            ch.append(ChannelSpec("nplc", "rw", "float", (), "PLC", "integration time in "
                                  "power-line cycles (resolution vs. speed)", config=True,
                                  allowed=NPLC))
            ch.append(ChannelSpec("autozero", "rw", "str", (), "", "auto-zero (OFF: about twice "
                                  "as fast, drifts)", config=True, allowed=["ON", "OFF"]))
        return ch

    # -- connection -----------------------------------------------------------------
    def connect(self):
        super().connect()
        if self.address.upper().startswith(("ASRL", "COM", "/DEV/")):
            self.w("SYST:REM")                   # RS-232: remote mode, front panel locked
        self.w("TRIG:SOUR IMM")
        self.w("TRIG:COUN 1")
        self.w("SAMP:COUN 1")
        self.mode = self._read_mode()
        self._load_mode_settings()
        self.clear_errors()

    def _read_mode(self) -> str:
        f = self.q("FUNC?").strip().strip('"').upper()
        f = {"VOLT": "VOLT:DC", "CURR": "CURR:DC"}.get(f, f)
        for m, spec in FUNCS.items():
            if spec[0] == f:
                return m
        raise IOError("unsupported function %r (set the meter to V, I or Ω)" % f)

    def _load_mode_settings(self) -> None:
        if _is_ac(self.mode):
            self.ac_filter = self._read_filter()
        else:
            self.nplc = self.qf("%s:NPLC?" % FUNCS[self.mode][1])

    @property
    def prefix(self) -> str:
        return FUNCS[self.mode][1]

    # -- I/O ---------------------------------------------------------------------------
    def read(self, channel):
        if channel == FUNCS[self.mode][2]:
            return self.qf("READ?")
        if channel == "mode":
            self.mode = self._read_mode()
            return self.mode
        if channel == "range":
            if self.qb("%s:RANG:AUTO?" % self.prefix):
                return "AUTO"
            r = self.qf("%s:RANG?" % self.prefix)
            for v, label in RANGES[self.mode]:
                if abs(v - r) <= 1e-6 * v:
                    return label
            return "%g" % r
        if channel == "nplc" and not _is_ac(self.mode):
            self.nplc = self.qf("%s:NPLC?" % self.prefix)
            return self.nplc
        if channel == "autozero" and not _is_ac(self.mode):
            return "ON" if self.qb("ZERO:AUTO?") else "OFF"
        if channel == "ac_filter" and _is_ac(self.mode):
            self.ac_filter = self._read_filter()
            return self.ac_filter
        raise ChannelError("%s has no channel %s in mode %s" % (self.name, channel, self.mode))

    def _read_filter(self) -> str:
        b = self.qf("DET:BAND?")
        return min(AC_FILTERS, key=lambda f: abs(float(f.split()[0]) - b))

    def write(self, channel, value):
        if channel == "mode":
            self.checked('FUNC "%s"' % FUNCS[value][0])
            self.mode = value
            self._load_mode_settings()
        elif channel == "range":
            if value == "AUTO":
                self.checked("%s:RANG:AUTO ON" % self.prefix)
            else:
                v = next(v for v, label in RANGES[self.mode] if label == value)
                self.checked("%s:RANG %g" % (self.prefix, v))
        elif channel == "nplc" and not _is_ac(self.mode):
            self.checked("%s:NPLC %g" % (self.prefix, value))
            self.nplc = float(value)
        elif channel == "autozero" and not _is_ac(self.mode):
            self.checked("ZERO:AUTO %s" % value)
        elif channel == "ac_filter" and _is_ac(self.mode):
            self.checked("DET:BAND %s" % value.split()[0])
            self.ac_filter = value
        else:
            raise ChannelError("%s: channel %s is not writable in mode %s"
                               % (self.name, channel, self.mode))

    def _action_clear_status(self):
        self.w("*CLS")

    def _action_beep(self):
        self.w("SYST:BEEP")


HP34401A.CHANNELS = HP34401A.get_channels(HP34401A.__new__(HP34401A))
DRIVER = HP34401A
