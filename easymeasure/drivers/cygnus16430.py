"""Cygnus 16430: 8-channel programmable supply, 0 – 150 V / 0 – 40 mA per channel
(Cao RQM Lab, firmware ``cygnus-16430-firmware``).

USB CDC virtual serial port (address ``COM5`` / ``/dev/ttyACM0``; the baud rate
does not matter), SCPI-style ASCII as implemented in the firmware's ``scpi.c``:
``VOLT n,v`` / ``CURR n,i`` / ``OUTP n,ON|OFF`` with channels 1 … 8,
``MEAS:VOLT? n`` / ``MEAS:CURR? n`` readback, ``RATE V|I,SF`` ADC speed,
``FILT:VOLT|CURR n,α`` IIR filter.  The firmware has a one-slot error register
(``SYST:ERR?`` → ``0`` or ``-1,"message"``) and does not answer a query it
rejects; the driver only sends valid commands and checks the register after
every setting, so a refused value raises the firmware's own message.

Each output is a one-quadrant source: a voltage setpoint with a current limit
(constant voltage until the limit, then constant current) and its own output
relay.  Channels (``k`` = 1 … 8)

==================  =====  ====================================================
``voltage<k>``      V      voltage setpoint, 0 … 150 V (swept); reads the setpoint
``Vmeas<k>``        V      measured output voltage
``Imeas<k>``        A      measured output current
``Ilimit<k>``       A      current limit, 0 … 40 mA (config)
``output<k>``              ON / OFF: the output relay (config)
``voltAll``         V      all 8 setpoints as a vector (written at once, ``VOLTA``)
``IlimitAll``       A      all 8 current limits as a vector (written at once, ``CURRA``)
``VmeasAll``        V      all 8 measured voltages
``ImeasAll``        A      all 8 measured currents
``Vrate`` ``Irate``        ADC filter word SF 13 … 255 for all voltage / current
                           readings (13: fastest, 255: lowest noise) (config)
``Vfilter`` ``Ifilter``    IIR filter α (0 … 1], 1 = off, set on every channel (config)
==================  =====  ====================================================

Readback is round-robin over the 8 channels: a channel's reading is refreshed
every ``8·24·SF/32768`` s (76 ms at SF 13, 1.5 s at SF 255).  With option
``settle`` (default on) a reading of a channel whose setpoint, limit or relay
just changed waits until fresh conversions (and, with a filter, ~99 % of the
filter's step response) are in.

Actions: ``all_off`` (every relay off), ``reset`` (``*RST``: outputs off,
setpoints zero, filters cleared), ``auto_calibrate`` (``CAL:AUTO``, the
firmware's current-trim refresh; all outputs must be off; takes a while).
``SIM::<name>`` uses the built-in emulator (port of the firmware's parser,
resistive loads: option ``load_ohm``).
"""

from __future__ import annotations

import math
import re
import time
from typing import Dict, List

from ..backend.instrument import ActionSpec, ChannelSpec
from ..backend.resources import ResourceError
from ..common.errors import ChannelError
from ._scpi import SCPIInstrument, to_float
from .emulators.cygnus import Cygnus16430Emulator

N = 8
VMAX = 150.0
IMAX = 0.040
ONOFF = ["ON", "OFF"]


def adc_period(sf: int) -> float:
    """Seconds between two readings of one channel (round-robin over 8)."""
    return N * 24.0 * int(sf) / 32768.0


class Cygnus16430(SCPIInstrument):
    DESCRIPTION = "Cygnus 16430: 8 × (0–150 V, 0–40 mA) supply with output relays (USB serial)"
    IDN_MATCH = ("Cygnus-16430",)
    EMULATOR = Cygnus16430Emulator
    INTERFACES = ("serial", "sim")
    DEFAULT_OPTIONS = dict(SCPIInstrument.DEFAULT_OPTIONS, timeout=2.0, baudrate=115200,
                           read_termination="\n", write_termination="\n",
                           settle=True, Vfilter=1.0, Ifilter=1.0, cal_timeout=600.0)
    ACTIONS = [
        ActionSpec("all_off", "switch every output relay off"),
        ActionSpec("reset", "*RST: every output off, setpoints and limits zero, filters off"),
        ActionSpec("auto_calibrate", "CAL:AUTO: refresh the current-trim calibration of all "
                   "channels (all outputs must be off; the instrument is busy meanwhile)",
                   timeout=900.0),
    ]

    def __init__(self, name, address, options=None):
        super().__init__(name, address, options)
        self.changed: List[float] = [0.0] * N
        self.sf: Dict[str, int] = {"V": 13, "I": 13}
        self.alpha: Dict[str, float] = {"V": 1.0, "I": 1.0}

    # -- channels -----------------------------------------------------------------------
    def get_channels(self):
        ch = []
        for k in range(1, N + 1):
            ch += [
                ChannelSpec("voltage%d" % k, "rw", "float", (), "V",
                            "voltage setpoint of output %d (0 … 150 V)" % k),
                ChannelSpec("Vmeas%d" % k, "r", "float", (), "V",
                            "measured voltage of output %d" % k),
                ChannelSpec("Imeas%d" % k, "r", "float", (), "A",
                            "measured current of output %d" % k),
                ChannelSpec("Ilimit%d" % k, "rw", "float", (), "A",
                            "current limit of output %d (0 … 40 mA)" % k, config=True),
                ChannelSpec("output%d" % k, "rw", "str", (), "", "output %d relay" % k,
                            config=True, allowed=ONOFF),
            ]
        ch += [
            ChannelSpec("voltAll", "rw", "float", (N,), "V",
                        "all 8 voltage setpoints (written together; all or none)"),
            ChannelSpec("IlimitAll", "rw", "float", (N,), "A",
                        "all 8 current limits (written together; all or none)", config=True),
            ChannelSpec("VmeasAll", "r", "float", (N,), "V", "measured voltages of outputs 1 … 8"),
            ChannelSpec("ImeasAll", "r", "float", (N,), "A", "measured currents of outputs 1 … 8"),
            ChannelSpec("Vrate", "rw", "float", (), "", "voltage ADC filter word SF 13 … 255: "
                        "a channel is read every 8·24·SF/32768 s (13: 76 ms, 255: 1.5 s)",
                        config=True),
            ChannelSpec("Irate", "rw", "float", (), "", "current ADC filter word SF 13 … 255",
                        config=True),
            ChannelSpec("Vfilter", "rw", "float", (), "", "IIR filter α of the voltage "
                        "readings, all channels (1 = off, smaller = smoother and slower)",
                        config=True),
            ChannelSpec("Ifilter", "rw", "float", (), "", "IIR filter α of the current "
                        "readings, all channels", config=True),
        ]
        return ch

    # -- connection -----------------------------------------------------------------------
    def connect(self):
        self.identify()
        if self.options.get("reset"):
            self.w("*RST")
        self.clear_errors()
        for which in ("V", "I"):
            self.sf[which] = int(to_float(self.q("RATE? %s" % which)))
        # the filters cannot be read back: put them where the options say
        for which in ("V", "I"):
            self._set_filter(which, float(self.options.get(which + "filter", 1.0)))
        self.changed = [time.monotonic()] * N

    # -- errors (one-slot register, no *CLS) ------------------------------------------
    def read_errors(self) -> List[str]:
        r = self.q("SYST:ERR?")
        if r.strip() == "0":
            return []
        m = re.match(r'\s*-?\d+\s*,\s*"?(.*?)"?\s*$', r)
        return [m.group(1) if m else r]

    def clear_errors(self) -> None:
        self.read_errors()                       # reading clears the register

    # -- settling ---------------------------------------------------------------------------
    def settle_time(self) -> float:
        """How long after a change a reading is fresh: two conversions of the channel,
        or with a filter the conversions for ~99 % of a step."""
        out = 0.0
        for which in ("V", "I"):
            a = self.alpha[which]
            n = 2 if a >= 1.0 else min(200, math.ceil(math.log(0.01) / math.log(1.0 - a)) + 1)
            out = max(out, n * adc_period(self.sf[which]))
        return out

    def _wait_fresh(self, chans) -> None:
        if not self.options.get("settle", True):
            return
        due = max(self.changed[c] for c in chans) + self.settle_time()
        wait = due - time.monotonic()
        if wait > 0:
            time.sleep(min(wait, 60.0))

    def _touch(self, *chans) -> None:
        now = time.monotonic()
        for c in chans:
            self.changed[c] = now

    def _set_filter(self, which: str, alpha: float) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ChannelError("%s: filter α must be in (0, 1]" % self.name)
        axis = "VOLT" if which == "V" else "CURR"
        self.checked(*["FILT:%s %d,%.6g" % (axis, k, alpha) for k in range(1, N + 1)])
        self.alpha[which] = alpha
        self._touch(*range(N))

    # -- channel I/O ---------------------------------------------------------------------
    @staticmethod
    def _split(channel: str):
        m = re.fullmatch(r"(voltage|Vmeas|Imeas|Ilimit|output)([1-8])", channel)
        return (m.group(1), int(m.group(2))) if m else (channel, None)

    def read(self, channel):
        kind, k = self._split(channel)
        if k is not None:
            if kind == "voltage":
                return self.qf("VOLT? %d" % k)
            if kind == "Ilimit":
                return self.qf("CURR? %d" % k)
            if kind == "output":
                return "ON" if self.qb("OUTP? %d" % k) else "OFF"
            self._wait_fresh([k - 1])
            return self.qf("MEAS:%s? %d" % ("VOLT" if kind == "Vmeas" else "CURR", k))
        if channel in ("voltAll", "IlimitAll"):
            cmd = "VOLT? %d" if channel == "voltAll" else "CURR? %d"
            return [self.qf(cmd % k) for k in range(1, N + 1)]
        if channel in ("VmeasAll", "ImeasAll"):
            self._wait_fresh(range(N))
            cmd = "MEAS:VOLT? %d" if channel == "VmeasAll" else "MEAS:CURR? %d"
            return [self.qf(cmd % k) for k in range(1, N + 1)]
        if channel in ("Vrate", "Irate"):
            sf = int(to_float(self.q("RATE? %s" % channel[0])))
            self.sf[channel[0]] = sf
            return float(sf)
        if channel in ("Vfilter", "Ifilter"):
            return self.alpha[channel[0]]
        raise ChannelError("%s has no channel %s" % (self.name, channel))

    def write(self, channel, value):
        kind, k = self._split(channel)
        if kind == "voltage" and k is not None:
            v = float(value)
            if not 0.0 <= v <= VMAX:
                raise ChannelError("output %d: %.6g V is outside 0 … 150 V" % (k, v))
            self.checked("VOLT %d,%.6f" % (k, v))
            self._touch(k - 1)
        elif kind == "Ilimit" and k is not None:
            i = float(value)
            if not 0.0 <= i <= IMAX:
                raise ChannelError("output %d: %.6g A is outside 0 … 40 mA" % (k, i))
            self.checked("CURR %d,%.8f" % (k, i))
            self._touch(k - 1)
        elif kind == "output" and k is not None:
            on = str(value).strip().upper() in ("ON", "1", "TRUE")
            self.checked("OUTP %d,%s" % (k, "ON" if on else "OFF"))
            self._touch(k - 1)
        elif channel in ("voltAll", "IlimitAll"):
            vals = [float(v) for v in value]
            if len(vals) != N:
                raise ChannelError("%s takes 8 values (outputs 1 … 8)" % channel)
            volt = channel == "voltAll"
            top = VMAX if volt else IMAX
            bad = [i + 1 for i, v in enumerate(vals) if not 0.0 <= v <= top]
            if bad:
                raise ChannelError("output %d: outside 0 … %s (nothing was set)"
                                   % (bad[0], "150 V" if volt else "40 mA"))
            self.checked(("VOLTA " if volt else "CURRA ") +
                         ",".join(("%.6f" if volt else "%.8f") % v for v in vals))
            self._touch(*range(N))
        elif channel in ("Vrate", "Irate"):
            sf = float(value)
            if sf != int(sf) or not 13 <= sf <= 255:
                raise ChannelError("%s: SF is an integer 13 … 255" % channel)
            self.checked("RATE %s,%d" % (channel[0], int(sf)))
            self.sf[channel[0]] = int(sf)
            self._touch(*range(N))
        elif channel in ("Vfilter", "Ifilter"):
            self._set_filter(channel[0], float(value))
        else:
            raise ChannelError("%s: channel %s is not writable" % (self.name, channel))

    # -- actions ---------------------------------------------------------------------------
    def _action_all_off(self):
        self.checked(*["OUTP %d,OFF" % k for k in range(1, N + 1)])
        self._touch(*range(N))

    def _action_reset(self):
        self.checked("*RST")
        self.alpha = {"V": 1.0, "I": 1.0}
        for which in ("V", "I"):
            self.sf[which] = int(to_float(self.q("RATE? %s" % which)))
        self._touch(*range(N))

    def _action_auto_calibrate(self):
        on = [k for k in range(1, N + 1) if self.qb("OUTP? %d" % k)]
        if on:
            raise ChannelError("%s: switch every output off first (on: %s)"
                               % (self.name, ", ".join(map(str, on))))
        self.w("CAL:AUTO")
        # the firmware answers nothing while it calibrates: the error query waits
        self.w("SYST:ERR?")
        deadline = time.monotonic() + float(self.options.get("cal_timeout", 600.0))
        while True:
            try:
                r = self.resource.read().strip()
                break
            except ResourceError:
                if time.monotonic() > deadline:
                    raise
        if r != "0":
            raise ChannelError("%s: CAL:AUTO failed: %s" % (self.name, r))
        self._touch(*range(N))


DRIVER = Cygnus16430
Cygnus16430.CHANNELS = Cygnus16430("static", "SIM::cygnus").get_channels()
