"""Stanford Research Systems SR830 / SR850 DSP lock-in amplifier (and SR810).

The model is detected from ``*IDN?``; the differences (reference modes, the
current-input gain, reserve names, how noise is read) are handled here.
Address e.g. ``GPIB0::8::INSTR``, or ``ASRL1::INSTR`` / ``COM1`` for RS-232
(set the instrument to 9600 baud; replies end with CR: option
``read_termination`` = ``"\\r"``).  ``SIM::<name>`` uses the built-in emulator.

Channels
    measured (read-only): ``X``, ``Y``, ``R`` (V, or A with a current input),
    ``theta`` (deg), ``Xn``, ``Yn`` (noise, V/√Hz or A/√Hz), ``aux_in1`` …
    ``aux_in4`` (V)

    sources (read/write, may be aliased and swept): ``sine_out`` (V rms,
    0.004 … 5), ``frequency`` (Hz; read-only with an external reference),
    ``phase`` (deg), ``aux_out1`` … ``aux_out4`` (V, ±10.5)

    settings: ``reference`` (Internal / External), ``sensitivity``,
    ``time_constant``, ``rolloff`` (low-pass filter slope 6 … 24 dB/oct),
    ``coupling`` (AC / DC), ``ground`` (Float / Ground), ``line_filter``
    (Off / Line / 2x Line / Both), ``input`` (A, A-B, I (1 MΩ), I (100 MΩ)),
    ``reserve``, ``harmonic``, ``sync_filter``

Sensitivities and time constants are human-readable (``"500 µV"``,
``"300 ms"``); with a current input the sensitivities are listed in A.

Noise (``Xn``/``Yn``): the SR830 measures noise only on its displays, so the
first read sets CH1/CH2 to "X noise"/"Y noise" (SR850: traces 3 and 4, option
``noise_traces``).  This changes the front panel display.
"""

from __future__ import annotations

import time
from typing import List

from ..backend.instrument import ActionSpec, ChannelSpec
from ..common.errors import ChannelError
from ._scpi import SCPIInstrument, fmt

_PREFIX = [(1e-15, "f"), (1e-12, "p"), (1e-9, "n"), (1e-6, "µ"), (1e-3, "m"), (1.0, ""), (1e3, "k")]


def _si(x: float, unit: str) -> str:
    for scale, p in reversed(_PREFIX):
        if x >= scale * 0.999:
            v = x / scale
            return "%g %s%s" % (round(v, 6), p, unit)
    return "%g %s" % (x, unit)


def _sens_values() -> List[float]:
    out, e = [], -9
    while len(out) < 27:
        for m in (2, 5, 10):
            out.append(m * 10.0 ** e)
        e += 1
    return out[:27]                             # 2 nV … 1 V


SENS_V = [_si(v, "V") for v in _sens_values()]
SENS_I = [_si(v * 1e-6, "A") for v in _sens_values()]          # 2 fA … 1 µA (×1e-6 A/V)
TIME_CONSTANTS = [_si(m * 10.0 ** e, "s") for e in range(-5, 5) for m in (1, 3)]   # 10 µs … 30 ks
ROLLOFF = ["6 dB/oct", "12 dB/oct", "18 dB/oct", "24 dB/oct"]
COUPLING = ["AC", "DC"]
GROUND = ["Float", "Ground"]
LINE = ["Off", "Line", "2x Line", "Both"]
INPUTS = ["A", "A-B", "I (1 MΩ)", "I (100 MΩ)"]


class SR830(SCPIInstrument):
    DESCRIPTION = "Stanford Research SR830 / SR850 lock-in amplifier"
    IDN_MATCH = ("SR830", "SR850", "SR810")
    ACTIONS = [ActionSpec("auto_gain", "choose the sensitivity for the present signal", 60.0),
               ActionSpec("auto_phase", "set the phase so that Y = 0", 30.0),
               ActionSpec("auto_reserve", "choose the dynamic reserve", 60.0),
               ActionSpec("clear_status", "*CLS")]
    DEFAULT_OPTIONS = dict(SCPIInstrument.DEFAULT_OPTIONS, noise_traces=[3, 4], interface="auto")

    @staticmethod
    def EMULATOR(options):                     # noqa: N802
        from .emulators.lockin import SR830Emulator
        return SR830Emulator(options)

    model = "SR830"
    inp = "A"
    ref = "Internal"

    # -- connection ---------------------------------------------------------------
    def connect(self):
        self.attach_emulator()
        self.select(self.resource)
        iface = str(self.options.get("interface", "auto")).upper()
        if iface == "AUTO":
            a = self.address.upper()
            iface = "RS232" if a.startswith(("ASRL", "COM", "/DEV/")) else "GPIB"
        # replies go to the interface named by OUTX: say which one we are on first
        self.w("OUTX %d" % (0 if iface == "RS232" else 1))
        idn = self.identify()
        self.model = "SR850" if "SR850" in idn.upper() else ("SR810" if "SR810" in idn.upper() else "SR830")
        if self.options.get("reset"):
            self.w("*RST")
        self.clear_errors()
        self._noise_ready = set()
        self.inp = self._read_input()
        self.ref = self._read_reference()

    def read_errors(self):
        esr = int(float(self.q("*ESR?")))
        out = []
        if esr & 32:
            out.append("command error (illegal command or parameter)")
        if esr & 16:
            out.append("execution error (value out of range)")
        return out

    # -- channel list -----------------------------------------------------------------
    @property
    def current_input(self) -> bool:
        return self.inp.startswith("I")

    def get_channels(self):
        u = "A" if self.current_input else "V"
        sr850 = self.model == "SR850"
        refs = ["Internal", "Sweep", "External"] if sr850 else ["Internal", "External"]
        reserves = ["Max", "Manual", "Min"] if sr850 else ["High Reserve", "Normal", "Low Noise"]
        ext = self.ref != "Internal"
        ch = [
            ChannelSpec("X", "r", "float", (), u, "in-phase component"),
            ChannelSpec("Y", "r", "float", (), u, "quadrature component"),
            ChannelSpec("R", "r", "float", (), u, "magnitude"),
            ChannelSpec("theta", "r", "float", (), "deg", "phase"),
            ChannelSpec("Xn", "r", "float", (), u + "/√Hz", "X noise (uses display CH1 / trace)"),
            ChannelSpec("Yn", "r", "float", (), u + "/√Hz", "Y noise (uses display CH2 / trace)"),
        ]
        ch += [ChannelSpec("aux_in%d" % i, "r", "float", (), "V", "aux input %d" % i)
               for i in range(1, 5)]
        ch += [
            ChannelSpec("sine_out", "rw", "float", (), "V", "sine output amplitude (rms), "
                        "0.004 … 5 V"),
            ChannelSpec("frequency", "r" if ext else "rw", "float", (), "Hz",
                        "reference frequency" + (" (measured: external reference)" if ext else "")),
            ChannelSpec("phase", "rw", "float", (), "deg", "reference phase shift"),
        ]
        ch += [ChannelSpec("aux_out%d" % i, "rw", "float", (), "V", "aux output %d (±10.5 V)" % i)
               for i in range(1, 5)]
        ch += [
            ChannelSpec("reference", "rw", "str", (), "", "reference source", config=True,
                        allowed=refs),
            ChannelSpec("sensitivity", "rw", "str", (), "", "full-scale sensitivity",
                        config=True, allowed=SENS_I if self.current_input else SENS_V),
            ChannelSpec("time_constant", "rw", "str", (), "", "low-pass filter time constant",
                        config=True, allowed=TIME_CONSTANTS),
            ChannelSpec("rolloff", "rw", "str", (), "", "low-pass filter slope", config=True,
                        allowed=ROLLOFF),
            ChannelSpec("coupling", "rw", "str", (), "", "input coupling", config=True,
                        allowed=COUPLING),
            ChannelSpec("ground", "rw", "str", (), "", "input shield grounding", config=True,
                        allowed=GROUND),
            ChannelSpec("line_filter", "rw", "str", (), "", "line notch filters", config=True,
                        allowed=LINE),
            ChannelSpec("input", "rw", "str", (), "", "input configuration (voltage A, A-B or "
                        "current with 1 MΩ / 100 MΩ gain)", config=True, allowed=INPUTS),
            ChannelSpec("reserve", "rw", "str", (), "", "dynamic reserve", config=True,
                        allowed=reserves),
            ChannelSpec("harmonic", "rw", "float", (), "", "detection harmonic (1 … %d, "
                        "harmonic × f ≤ 102 kHz)" % (32767 if sr850 else 19999), config=True),
            ChannelSpec("sync_filter", "rw", "str", (), "", "synchronous filter (below 200 Hz)",
                        config=True, allowed=["Off", "On"]),
        ]
        return ch

    # -- reading ------------------------------------------------------------------------
    def read(self, channel):
        if channel in ("X", "Y", "R", "theta"):
            return self.qf("OUTP? %d" % (("X", "Y", "R", "theta").index(channel) + 1))
        if channel in ("Xn", "Yn"):
            return self._noise(channel)
        if channel.startswith("aux_in"):
            return self.qf("OAUX? %d" % self._aux(channel))
        if channel.startswith("aux_out"):
            return self.qf("AUXV? %d" % self._aux(channel))
        if channel == "sine_out":
            return self.qf("SLVL?")
        if channel == "frequency":
            return self.qf("FREQ?")
        if channel == "phase":
            return self.qf("PHAS?")
        if channel == "reference":
            self.ref = self._read_reference()
            return self.ref
        if channel == "input":
            self.inp = self._read_input()
            return self.inp
        if channel == "harmonic":
            return self.qf("HARM?")
        lists = self._index_settings()
        if channel in lists:
            cmd, values = lists[channel]
            i = int(float(self.q(cmd + "?")))
            if not 0 <= i < len(values):
                raise IOError("%s? returned %d" % (cmd, i))
            return values[i]
        raise ChannelError("%s has no channel %s" % (self.name, channel))

    def _index_settings(self):
        sr850 = self.model == "SR850"
        return {
            "sensitivity": ("SENS", SENS_I if self.current_input else SENS_V),
            "time_constant": ("OFLT", TIME_CONSTANTS),
            "rolloff": ("OFSL", ROLLOFF),
            "coupling": ("ICPL", COUPLING),
            "ground": ("IGND", GROUND),
            "line_filter": ("ILIN", LINE),
            "reserve": ("RMOD", ["Max", "Manual", "Min"] if sr850 else
                        ["High Reserve", "Normal", "Low Noise"]),
            "sync_filter": ("SYNC", ["Off", "On"]),
        }

    @staticmethod
    def _aux(channel: str) -> int:
        i = int(channel[-1])
        if not 1 <= i <= 4:
            raise ChannelError("aux channels are 1 … 4")
        return i

    def _read_reference(self) -> str:
        i = int(float(self.q("FMOD?")))
        if self.model == "SR850":
            return {0: "Internal", 1: "Sweep", 2: "External"}.get(i, "Internal")
        return "Internal" if i == 1 else "External"

    def _read_input(self) -> str:
        i = int(float(self.q("ISRC?")))
        if self.model == "SR850":
            if i < 2:
                return INPUTS[i]
            return INPUTS[3] if int(float(self.q("IGAN?"))) == 1 else INPUTS[2]
        return INPUTS[i]

    def _noise(self, channel: str) -> float:
        k = 0 if channel == "Xn" else 1
        if self.model == "SR850":
            tr = int(self.options.get("noise_traces", [3, 4])[k])
            if channel not in self._noise_ready:
                # trace = Xn (5) or Yn (6) × 1 / 1, not stored
                self.checked("TRCD %d,%d,0,0,0" % (tr, 5 + k))
                self._noise_ready.add(channel)
            return self.qf("OUTR? %d" % tr)
        if channel not in self._noise_ready:
            self.checked("DDEF %d,2,0" % (k + 1))       # CH1 = X noise / CH2 = Y noise
            self._noise_ready.add(channel)
        return self.qf("OUTR? %d" % (k + 1))

    # -- writing --------------------------------------------------------------------------
    def write(self, channel, value):
        if channel.startswith("aux_out"):
            if not -10.5 <= value <= 10.5:
                raise ChannelError("aux outputs are limited to ±10.5 V")
            self.checked("AUXV %d,%s" % (self._aux(channel), fmt(value)))
        elif channel == "sine_out":
            if not 0.004 <= value <= 5.0:
                raise ChannelError("sine_out must be within 0.004 … 5 V rms")
            self.checked("SLVL %s" % fmt(value))
        elif channel == "frequency":
            if self.ref != "Internal":
                raise ChannelError("the frequency can only be set with the internal reference")
            if not 0.001 <= value <= 102000:
                raise ChannelError("frequency must be within 1 mHz … 102 kHz")
            self.checked("FREQ %s" % fmt(value))
        elif channel == "phase":
            # the instrument accepts -360 … 729.99 and wraps to ±180
            if not -360 <= value <= 729.99:
                raise ChannelError("phase must be within -360 … 729.99 deg")
            self.checked("PHAS %s" % fmt(value))
        elif channel == "reference":
            if self.model == "SR850":
                code = {"Internal": 0, "Sweep": 1, "External": 2}[value]
            else:
                code = {"Internal": 1, "External": 0}[value]
            self.checked("FMOD %d" % code)
            self.ref = value
        elif channel == "input":
            i = INPUTS.index(value)
            if self.model == "SR850":
                if i < 2:
                    self.checked("ISRC %d" % i)
                else:
                    self.checked("ISRC 2", "IGAN %d" % (i - 2))
            else:
                self.checked("ISRC %d" % i)
            self.inp = value
        elif channel == "harmonic":
            hi = 32767 if self.model == "SR850" else 19999
            if value != int(value) or not 1 <= value <= hi:
                raise ChannelError("harmonic must be an integer 1 … %d" % hi)
            self.checked("HARM %d" % int(value))
        else:
            lists = self._index_settings()
            if channel not in lists:
                raise ChannelError("%s: channel %s is not writable" % (self.name, channel))
            cmd, values = lists[channel]
            self.checked("%s %d" % (cmd, values.index(value)))

    # -- actions ------------------------------------------------------------------------
    def _wait_idle(self, limit: float) -> None:
        """Auto functions take a while: poll the 'no command in progress' status bit."""
        t_end = time.monotonic() + limit
        while time.monotonic() < t_end:
            if self.q("*STB? 1").strip() == "1":
                return
            time.sleep(0.1)
        raise IOError("instrument still busy after %.0f s" % limit)

    def _action_auto_gain(self):
        self.w("AGAN")
        self._wait_idle(55)

    def _action_auto_phase(self):
        self.w("APHS")
        self._wait_idle(25)

    def _action_auto_reserve(self):
        self.w("ARSV")
        self._wait_idle(55)

    def _action_clear_status(self):
        self.w("*CLS")


SR830.CHANNELS = SR830.get_channels(SR830.__new__(SR830))
DRIVER = SR830
