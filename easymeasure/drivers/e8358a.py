"""Agilent (Keysight) E8358A PNA series vector network analyzer, 300 kHz - 9 GHz
(also the E8356A / E8357A, 3 / 6 GHz: the same commands).

Measurements
    The four S-parameters of channel 1 (option ``channel``) are read as whole
    frequency sweeps: ``S21`` (log magnitude, dB), ``S21_phase`` (degrees),
    ``S21_re`` / ``S21_im`` (the complex value), and the same for ``S11``, ``S12``,
    ``S22``.  Their x axis is ``frequencies`` (Hz), which the plots and data files
    use.  The driver creates a measurement (``EM_S21`` ...) on the analyzer the
    first time a parameter is read, shows it in window 1, and keeps it; option
    ``parameters`` (e.g. ``"S21,S11"``) creates them when connecting, so the first
    sweep already measures them all.

One sweep per point
    Reading a measurement channel triggers a **single sweep** of every measurement
    (``SENSe:SWEep:MODE SINGle``, or ``GROups`` with the average count when
    averaging is on, so a reading is always a fresh, complete average) and waits
    for it to end (``*OPC`` / ``*ESR?``).  All channels of that sweep are read from
    it: recording ``S21`` and ``S21_phase`` at a point costs one sweep, not two.
    The next reading of a channel already read starts the next sweep.  The analyzer
    then stays in HOLD, showing the last sweep; the ``continuous`` action lets it
    sweep freely again (for looking at the front panel).  Every measurement on the
    analyzer is swept, also ones made at its front panel: the ``delete_traces``
    action (a first pre-sweep constant) removes them all, settings unchanged, so a
    sweep measures only what it records.  A sweep that has not
    ended after ``3 × sweep time × sweeps + sweep_timeout`` s is an error.

Settings
    ``f_start`` / ``f_stop`` / ``f_center`` / ``f_span``, ``points`` (1 - 16001),
    ``if_bandwidth`` (the PNA's list, 1 Hz - 40 kHz), ``sweep_type`` (LIN / LOG),
    ``averaging`` and ``average_count``, ``output`` (RF on / off) and
    ``correction`` (the calibration of the channel, if one is applied) are
    configuration channels; ``power`` (dBm) is a data channel, so it can be aliased
    and swept.  A setting the analyzer refuses (a frequency out of range, ...)
    raises with its own message.

Connect over LAN (``TCPIP0::<ip>::inst0::INSTR`` or the SCPI socket
``TCPIP0::<ip>::5025::SOCKET``) or GPIB.  ``SIM::e8358a`` is an emulator.
"""

from __future__ import annotations

import time

import numpy as np

from ..backend.instrument import ActionSpec, ChannelSpec
from ..common.errors import ChannelError, InstrumentTimeout
from ._scpi import SCPIInstrument, fmt

PARAMS = ("S11", "S21", "S12", "S22")
REVERSE = ("S12", "S22")
KINDS = {"": ("dB", "log magnitude"), "_phase": ("deg", "phase"),
         "_re": ("", "real part"), "_im": ("", "imaginary part")}
IFBW = [1, 2, 3, 5, 7, 10, 15, 20, 30, 50, 70, 100, 150, 200, 300, 500, 700, 1e3, 1.5e3,
        2e3, 3e3, 5e3, 7e3, 10e3, 15e3, 20e3, 30e3, 35e3, 40e3]
F_RANGE = {"E8356A": 3e9, "E8357A": 6e9, "E8358A": 9e9}
MAX_POINTS = 16001
SETTINGS = ("f_start", "f_stop", "f_center", "f_span", "points", "if_bandwidth", "sweep_type",
            "averaging", "average_count", "output", "correction", "power")


def _measurement_channels():
    out = []
    for p in PARAMS:
        for suffix, (unit, what) in KINDS.items():
            out.append(ChannelSpec(p + suffix, "r", "float", (-1,), unit,
                                   "%s %s, one sweep" % (p, what), timeout=600.0,
                                   axes=("frequencies",)))
    return out


class E8358A(SCPIInstrument):
    DESCRIPTION = "Agilent / Keysight E8358A PNA network analyzer (E8356A / E8357A too)"
    IDN_MATCH = ("E8356A", "E8357A", "E8358A")
    CHANNELS = _measurement_channels() + [
        ChannelSpec("frequencies", "r", "float", (-1,), "Hz", "frequency of each point"),
        ChannelSpec("power", "rw", "float", (), "dBm", "source power (port power, coupled)"),
        ChannelSpec("sweep_time", "r", "float", (), "s", "the time of one sweep (one "
                    "direction; S12 / S22 add the reverse one)"),
        ChannelSpec("f_start", "rw", "float", (), "Hz", "start frequency", config=True),
        ChannelSpec("f_stop", "rw", "float", (), "Hz", "stop frequency", config=True),
        ChannelSpec("f_center", "rw", "float", (), "Hz", "center frequency", config=True),
        ChannelSpec("f_span", "rw", "float", (), "Hz", "frequency span", config=True),
        ChannelSpec("points", "rw", "float", (), "", "points per sweep (1 - 16001)",
                    config=True),
        ChannelSpec("if_bandwidth", "rw", "float", (), "Hz", "IF bandwidth: narrower is "
                    "slower and less noisy", config=True, allowed=IFBW),
        ChannelSpec("sweep_type", "rw", "str", (), "", "linear or logarithmic frequency "
                    "sweep", config=True, allowed=["LIN", "LOG"]),
        ChannelSpec("averaging", "rw", "str", (), "", "sweep averaging: every reading is a "
                    "complete average of average_count sweeps", config=True,
                    allowed=["ON", "OFF"]),
        ChannelSpec("average_count", "rw", "float", (), "", "sweeps per average (1 - 1024)",
                    config=True),
        ChannelSpec("output", "rw", "str", (), "", "RF power on / off", config=True,
                    allowed=["ON", "OFF"]),
        ChannelSpec("correction", "rw", "str", (), "", "the channel's calibration on / off "
                    "(ON needs a calibration on the analyzer)", config=True,
                    allowed=["ON", "OFF"]),
        ChannelSpec("idn", "r", "str", (), "", "identification", config=True),
    ]
    ACTIONS = [
        ActionSpec("single_sweep", "take one sweep (averaged, if averaging is on) now; the "
                   "next readings use it", timeout=600.0),
        ActionSpec("continuous", "let the analyzer sweep continuously (front panel); the "
                   "next reading goes back to single sweeps"),
        ActionSpec("delete_traces", "delete every measurement (trace) on the analyzer, the "
                   "settings stay; the next sweep measures only what is read (a first pre-sweep "
                   "constant: no stray S12 / S22 adding a reverse sweep)"),
        ActionSpec("preset", "factory preset (SYSTem:FPReset): every setting to its default - "
                   "e.g. 0 dBm with RF on - and no measurements; delete_traces keeps the "
                   "settings"),
    ]
    DEFAULT_OPTIONS = dict(SCPIInstrument.DEFAULT_OPTIONS, timeout=10.0, channel=1,
                           parameters="", sweep_timeout=30.0, poll_interval=0.05)

    @staticmethod
    def EMULATOR(options):                     # noqa: N802
        from .emulators.pna import PNAEmulator
        return PNAEmulator(options)

    # -- connection -----------------------------------------------------------------------
    def connect(self):
        super().connect()
        self.ch = int(self.options.get("channel", 1))
        model = next((m for m in F_RANGE if m in self.idn.upper()), "E8358A")
        self.f_max = F_RANGE[model]
        self.w("FORM:DATA ASC,0")                  # comma-separated numbers
        self.w("TRIG:SOUR IMM")                    # a single sweep starts at once
        self.w("INIT:CONT ON")
        self.measured = self._catalog()
        self._fresh()
        for p in [p.strip().upper() for p in str(self.options.get("parameters", "")).split(",")]:
            if p:
                if p not in PARAMS:
                    raise ChannelError("%s: option parameters: %r is not one of %s"
                                       % (self.name, p, ", ".join(PARAMS)))
                self._define(p)
        errs = self.read_errors()
        if errs:
            raise IOError("%s: the analyzer reports %s" % (self.name, "; ".join(errs)))

    def _name(self, p: str) -> str:
        return "EM_%s" % p

    def _catalog(self) -> dict:
        """The driver's measurements on the channel: {S21: "EM_S21", ...}."""
        txt = self.q("CALC%d:PAR:CAT?" % self.ch).strip().strip('"')
        items = [x.strip() for x in txt.split(",")] if txt and txt != "NO CATALOG" else []
        out = {}
        for name, par in zip(items[0::2], items[1::2]):
            if name == self._name(par.upper()):
                out[par.upper()] = name
        return out

    def _define(self, p: str) -> None:
        """Create the measurement of *p* and show it in window 1."""
        if p in self.measured:
            return
        name = self._name(p)
        self.checked("CALC%d:PAR:DEF '%s',%s" % (self.ch, name, p))
        try:
            self.checked("DISP:WIND1:STAT ON")
            self.checked("DISP:WIND1:TRAC%d:FEED '%s'" % (PARAMS.index(p) + 1, name))
        except ChannelError:
            pass                                   # not shown: still measured
        self.measured[p] = name
        self._fresh()                              # the next reading needs a sweep with it

    def _fresh(self) -> None:
        """Forget the last sweep: the next reading starts a new one."""
        self.data = {}
        self.taken = set()

    # -- the sweep ------------------------------------------------------------------------------
    def _sweep(self) -> None:
        """One single sweep of every measurement (an average of average_count when
        averaging is on); then their complex data."""
        avg = self.qb("SENS%d:AVER?" % self.ch)
        count = int(self.qf("SENS%d:AVER:COUN?" % self.ch)) if avg else 1
        t_sweep = self.qf("SENS%d:SWE:TIME?" % self.ch)
        directions = 2 if (any(p in REVERSE for p in self.measured) and
                           any(p not in REVERSE for p in self.measured)) else 1
        self.q("*ESR?")                            # clear the event status
        if avg:
            self.w("SENS%d:AVER:CLE" % self.ch)
            self.w("SENS%d:SWE:GRO:COUN %d" % (self.ch, count))
            self.w("SENS%d:SWE:MODE GRO;*OPC" % self.ch)
        else:
            self.w("SENS%d:SWE:MODE SING;*OPC" % self.ch)
        limit = 3.0 * t_sweep * directions * count + float(self.options.get("sweep_timeout", 30.0))
        deadline = time.monotonic() + limit
        poll = float(self.options.get("poll_interval", 0.05))
        while not int(self.qf("*ESR?")) & 1:
            if time.monotonic() > deadline:
                raise InstrumentTimeout("%s: the sweep did not end in %.0f s" % (self.name, limit))
            time.sleep(poll)
        data = {}
        for p, name in self.measured.items():
            self.w("CALC%d:PAR:SEL '%s'" % (self.ch, name))
            v = np.array([float(x) for x in self.q("CALC%d:DATA? SDATA" % self.ch).split(",")])
            if v.size % 2:
                raise IOError("%s: odd number of values for %s" % (self.name, p))
            data[p] = v[0::2] + 1j * v[1::2]
        errs = self.read_errors()
        if errs:
            raise IOError("%s: the analyzer reports %s" % (self.name, "; ".join(errs)))
        self.data = data
        self.taken = set()

    def _measurement(self, channel: str) -> np.ndarray:
        p, kind = channel[:3], channel[3:]
        if p not in self.measured:
            self._define(p)
        if p not in self.data or channel in self.taken:
            self._sweep()
        self.taken.add(channel)
        s = self.data[p]
        if kind == "":
            return 20 * np.log10(np.maximum(np.abs(s), 1e-15))
        if kind == "_phase":
            return np.degrees(np.angle(s))
        return s.real.copy() if kind == "_re" else s.imag.copy()

    def _frequencies(self) -> np.ndarray:
        start = self.qf("SENS%d:FREQ:STAR?" % self.ch)
        stop = self.qf("SENS%d:FREQ:STOP?" % self.ch)
        n = int(self.qf("SENS%d:SWE:POIN?" % self.ch))
        kind = self.q("SENS%d:SWE:TYPE?" % self.ch).strip().upper()
        if kind.startswith("LOG"):
            return np.geomspace(start, stop, n)
        if not kind.startswith("LIN"):
            raise ChannelError("%s: sweep type %s: frequencies only for LIN / LOG sweeps"
                               % (self.name, kind))
        return np.linspace(start, stop, n)

    # -- I/O --------------------------------------------------------------------------------------
    def read(self, channel):
        ch = self.ch
        if channel[:3] in PARAMS and channel[3:] in KINDS:
            return self._measurement(channel)
        if channel == "frequencies":
            return self._frequencies()
        if channel == "power":
            return self.qf("SOUR%d:POW1?" % ch)
        if channel == "sweep_time":
            return self.qf("SENS%d:SWE:TIME?" % ch)
        q = {"f_start": "SENS%d:FREQ:STAR?", "f_stop": "SENS%d:FREQ:STOP?",
             "f_center": "SENS%d:FREQ:CENT?", "f_span": "SENS%d:FREQ:SPAN?",
             "points": "SENS%d:SWE:POIN?", "if_bandwidth": "SENS%d:BAND?",
             "average_count": "SENS%d:AVER:COUN?"}.get(channel)
        if q is not None:
            return self.qf(q % ch)
        if channel == "sweep_type":
            t = self.q("SENS%d:SWE:TYPE?" % ch).strip().upper()
            return "LOG" if t.startswith("LOG") else "LIN" if t.startswith("LIN") else t
        if channel == "averaging":
            return "ON" if self.qb("SENS%d:AVER?" % ch) else "OFF"
        if channel == "output":
            return "ON" if self.qb("OUTP?") else "OFF"
        if channel == "correction":
            return "ON" if self.qb("SENS%d:CORR?" % ch) else "OFF"
        if channel == "idn":
            return self.idn
        raise ChannelError("%s has no channel %s" % (self.name, channel))

    def write(self, channel, value):
        ch = self.ch
        if channel in ("f_start", "f_stop", "f_center", "f_span"):
            v = float(value)
            if channel != "f_span" and not 300e3 <= v <= self.f_max:
                raise ChannelError("%s: %s must be 300 kHz … %g GHz" % (self.name, channel,
                                                                        self.f_max / 1e9))
            key = {"f_start": "STAR", "f_stop": "STOP", "f_center": "CENT", "f_span": "SPAN"}
            self.checked("SENS%d:FREQ:%s %s" % (ch, key[channel], fmt(v)))
        elif channel == "points":
            n = int(round(float(value)))
            if not 1 <= n <= MAX_POINTS:
                raise ChannelError("%s: points must be 1 … %d" % (self.name, MAX_POINTS))
            self.checked("SENS%d:SWE:POIN %d" % (ch, n))
        elif channel == "if_bandwidth":
            self.checked("SENS%d:BAND %s" % (ch, fmt(float(value))))
        elif channel == "sweep_type":
            self.checked("SENS%d:SWE:TYPE %s" % (ch, value))
        elif channel == "averaging":
            self.checked("SENS%d:AVER %s" % (ch, value))
        elif channel == "average_count":
            n = int(round(float(value)))
            if not 1 <= n <= 1024:
                raise ChannelError("%s: average_count must be 1 … 1024" % self.name)
            self.checked("SENS%d:AVER:COUN %d" % (ch, n))
        elif channel == "output":
            self.checked("OUTP %s" % value)
        elif channel == "correction":
            self.checked("SENS%d:CORR %s" % (ch, value))
        elif channel == "power":
            self.checked("SOUR%d:POW1 %s" % (ch, fmt(float(value))))
        else:
            raise ChannelError("%s: channel %s is read-only" % (self.name, channel))
        self._fresh()                              # the data no longer fits the settings

    # -- actions ----------------------------------------------------------------------------------
    def _action_single_sweep(self):
        for p in [p.strip().upper() for p in str(self.options.get("parameters", "")).split(",")]:
            if p in PARAMS:
                self._define(p)
        if not self.measured:
            self._define("S21")
        self._sweep()

    def _action_continuous(self):
        self.w("SENS%d:SWE:MODE CONT" % self.ch)
        self._fresh()

    def _action_delete_traces(self):
        self.checked("CALC%d:PAR:DEL:ALL" % self.ch)
        self.measured = {}
        self._fresh()

    def _action_preset(self):
        self.w("SYST:FPR")
        self.w("FORM:DATA ASC,0")
        self.w("TRIG:SOUR IMM")
        self.w("INIT:CONT ON")
        self.measured = {}
        self._fresh()
        self.clear_errors()


DRIVER = E8358A
