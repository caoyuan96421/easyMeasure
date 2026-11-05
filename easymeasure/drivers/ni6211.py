"""National Instruments USB-6211 multifunction DAQ (also USB-6210/6215, through NI-DAQmx).

Needs the NI-DAQmx driver runtime and the ``nidaqmx`` Python package
(``pip install nidaqmx``).  The address names the device as shown in NI MAX:
``DAQ::Dev1``.  ``DAQ::SIM`` (or option ``simulate``) uses a built-in emulation
of the card (no NI software needed); NI MAX can also create a simulated
USB-6211 that this driver drives like a real one.

Channels

==================  ====  ==================================================
``ai0`` … ``ai15``  V     analog inputs: mean of ``samples`` samples taken at
                          ``rate``; with ``terminal`` = DIFF only ``ai0 … ai7``
                          (``ai8 … ai15`` are the negative inputs)
``ai``              V     vector of all analog inputs from one simultaneous scan
``ao0``, ``ao1``    V     analog outputs, ±10 V (read back: last value written)
``di0`` … ``di3``         digital inputs P0.0 … P0.3 (PFI 0 … 3), 0 / 1
``do0`` … ``do3``         digital outputs P1.0 … P1.3 (PFI 4 … 7), 0 / 1
``ctr0``, ``ctr1``        edge counters (rising edges since connect / reset)
==================  ====  ==================================================

Settings: ``rate`` (samples/s per channel; all scanned channels together at most
250 kS/s), ``samples`` (per reading, averaged; 1 = a single on-demand
conversion, fastest), ``terminal`` (RSE / NRSE / DIFF) and ``ai_range``
(±10 / ±5 / ±1 / ±0.2 V).  Option ``ai_channels`` (e.g. ``"0-3"``) limits the
scanned (and exposed) analog inputs, which allows higher rates.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List

import numpy as np

from ..backend.instrument import ActionSpec, ChannelSpec, Instrument
from ..common.errors import ChannelError

AI_RANGES = ["±10 V", "±5 V", "±1 V", "±0.2 V"]
TERMINALS = ["RSE", "NRSE", "DIFF"]
MAX_AGGREGATE = 250e3


def _parse_list(spec: str, hi: int) -> List[int]:
    out = []
    for part in str(spec).replace(" ", "").split(","):
        if not part or part.lower() == "all":
            out += list(range(hi + 1))
            continue
        m = re.fullmatch(r"(\d+)(?:[-:](\d+))?", part)
        if not m:
            raise ValueError("bad channel list %r (e.g. '0-3,8')" % spec)
        a = int(m.group(1))
        b = int(m.group(2) or a)
        out += list(range(min(a, b), max(a, b) + 1))
    bad = [k for k in out if k > hi]
    if bad:
        raise ValueError("no analog input ai%d" % bad[0])
    return sorted(set(out))


class NI6211(Instrument):
    DESCRIPTION = "NI USB-6211 DAQ: 16 AI, 2 AO, 4 DI, 4 DO, 2 counters (NI-DAQmx)"
    ACTIONS = [ActionSpec("reset_counters", "restart both edge counters at 0")]
    DEFAULT_OPTIONS = {"ai_channels": "all", "simulate": False, "rate": 10000.0,
                       "samples": 100, "terminal": "RSE", "ai_range": "±10 V"}

    terminal = "RSE"
    rate = 10000.0
    samples = 100

    # -- connection ---------------------------------------------------------------------
    def _module(self):
        if self.options.get("simulate") or self.device.upper().startswith("SIM"):
            from .emulators import nidaqmx_sim
            return nidaqmx_sim
        try:
            import nidaqmx                          # noqa: F401
        except ImportError:
            raise IOError("the nidaqmx package is missing: pip install nidaqmx "
                          "(and install the NI-DAQmx driver)") from None
        return nidaqmx

    @property
    def device(self) -> str:
        a = self.address
        if a.upper().startswith("DAQ::"):
            return a[5:]
        return self.options.get("device") or "Dev1"

    def connect(self):
        self.daq = self._module()
        C = self.daq.constants
        self.C = C
        try:
            product = self.daq.system.Device(self.device).product_type
        except Exception as exc:
            raise IOError("NI-DAQmx: cannot open device %r: %s" % (self.device, exc)) from None
        if "621" not in str(product):
            raise IOError("%s is a %s, not a USB-621x" % (self.device, product))
        self.product = product
        self.tasks: Dict[str, object] = {}
        self.ao_last: Dict[int, float] = {}
        self.rate = float(self.options.get("rate", 10000.0))
        self.samples = int(self.options.get("samples", 100))
        self.terminal = str(self.options.get("terminal", "RSE")).upper()
        self.ai_range = str(self.options.get("ai_range", "±10 V"))
        if self.terminal not in TERMINALS or self.ai_range not in AI_RANGES:
            raise ChannelError("options terminal / ai_range must be one of %s / %s"
                               % (TERMINALS, AI_RANGES))
        self._check_rate(self.rate)
        self._start_counters()

    def disconnect(self):
        self._close_all()

    def recover(self):
        self._close_all()                         # tasks are recreated on demand
        self._start_counters()

    def _close_all(self):
        for t in list(getattr(self, "tasks", {}).values()):
            try:
                t.close()
            except Exception:
                pass
        self.tasks = {}

    def _close(self, key: str) -> None:
        t = self.tasks.pop(key, None)
        if t is not None:
            try:
                t.close()
            except Exception:
                pass

    # -- channel list -----------------------------------------------------------------------
    def ai_list(self) -> List[int]:
        hi = 7 if self.terminal == "DIFF" else 15
        try:
            chans = _parse_list(self.options.get("ai_channels", "all"), 15)
        except ValueError as exc:
            raise ChannelError(str(exc)) from None
        return [k for k in chans if k <= hi]

    def get_channels(self):
        ai = self.ai_list()
        t_read = 5.0 + self.samples / self.rate
        ch = [ChannelSpec("ai%d" % k, "r", "float", (), "V", "analog input %d (%s)"
                          % (k, self.terminal), timeout=t_read) for k in ai]
        ch.append(ChannelSpec("ai", "r", "float", (len(ai),), "V", "all analog inputs (%s) from "
                              "one scan" % ", ".join("ai%d" % k for k in ai), timeout=t_read))
        ch += [ChannelSpec("ao%d" % k, "rw", "float", (), "V", "analog output %d (±10 V)" % k)
               for k in (0, 1)]
        ch += [ChannelSpec("di%d" % k, "r", "float", (), "", "digital input P0.%d / PFI %d"
                           % (k, k)) for k in range(4)]
        ch += [ChannelSpec("do%d" % k, "rw", "float", (), "", "digital output P1.%d / PFI %d"
                           % (k, k + 4), allowed=[0, 1]) for k in range(4)]
        ch += [ChannelSpec("ctr%d" % k, "r", "float", (), "", "rising edges counted on the "
                           "counter %d source (PFI) since connect / reset_counters" % k)
               for k in (0, 1)]
        ch += [
            ChannelSpec("rate", "rw", "float", (), "S/s", "sample rate per analog input (all "
                        "scanned inputs together ≤ 250 kS/s)", config=True),
            ChannelSpec("samples", "rw", "float", (), "", "samples averaged per analog reading "
                        "(1 = single conversion)", config=True),
            ChannelSpec("terminal", "rw", "str", (), "", "analog input terminal configuration",
                        config=True, allowed=TERMINALS),
            ChannelSpec("ai_range", "rw", "str", (), "", "analog input range", config=True,
                        allowed=AI_RANGES),
        ]
        return ch

    # -- analog input ---------------------------------------------------------------------
    def _check_rate(self, rate: float) -> None:
        n = max(1, len(self.ai_list()))
        if not rate > 0 or rate * n > MAX_AGGREGATE:
            raise ChannelError("rate %.6g S/s × %d inputs exceeds the 250 kS/s of the USB-6211 "
                               "(max %.6g S/s per input; fewer inputs: option ai_channels)"
                               % (rate, n, MAX_AGGREGATE / n))

    def _ai_task(self):
        t = self.tasks.get("ai")
        if t is not None:
            return t
        C = self.C
        v = float(self.ai_range.strip("±V "))
        t = self.daq.Task()
        try:
            chans = ",".join("%s/ai%d" % (self.device, k) for k in self.ai_list())
            t.ai_channels.add_ai_voltage_chan(chans, terminal_config=getattr(
                C.TerminalConfiguration, self.terminal), min_val=-v, max_val=v)
            if self.samples > 1:
                t.timing.cfg_samp_clk_timing(self.rate, sample_mode=C.AcquisitionType.FINITE,
                                             samps_per_chan=self.samples)
            t.control(C.TaskMode.TASK_COMMIT)       # fast start/stop for every reading
        except Exception:
            t.close()
            raise
        self.tasks["ai"] = t
        return t

    def scan(self) -> np.ndarray:
        """One acquisition of every scanned input; returns the per-input means."""
        t = self._ai_task()
        n_ch = len(self.ai_list())
        if self.samples > 1:
            t.start()
            try:
                data = t.read(number_of_samples_per_channel=self.samples,
                              timeout=2.0 + self.samples / self.rate)
            finally:
                t.stop()
        else:
            data = t.read()
        arr = np.asarray(data, dtype=float).reshape(n_ch, -1)
        return arr.mean(axis=1)

    # -- outputs ------------------------------------------------------------------------
    def _out_task(self, key: str, make):
        t = self.tasks.get(key)
        if t is None:
            t = self.daq.Task()
            try:
                make(t)
            except Exception:
                t.close()
                raise
            self.tasks[key] = t
        return t

    def _ao_task(self, k: int):
        return self._out_task("ao%d" % k, lambda t: t.ao_channels.add_ao_voltage_chan(
            "%s/ao%d" % (self.device, k), min_val=-10.0, max_val=10.0))

    def _do_task(self, k: int):
        return self._out_task("do%d" % k, lambda t: t.do_channels.add_do_chan(
            "%s/port1/line%d" % (self.device, k), line_grouping=self.C.LineGrouping.CHAN_PER_LINE))

    def _di_task(self):
        return self._out_task("di", lambda t: t.di_channels.add_di_chan(
            "%s/port0/line0:3" % self.device, line_grouping=self.C.LineGrouping.CHAN_PER_LINE))

    def _ao_readback(self, k: int) -> float:
        """The value of an output nobody has written since connect: measured on the
        card's internal ``_aoK_vs_aognd`` channel (the AI task must make way)."""
        self._close("ai")
        t = self.daq.Task()
        try:
            t.ai_channels.add_ai_voltage_chan("%s/_ao%d_vs_aognd" % (self.device, k),
                                              min_val=-10.0, max_val=10.0)
            return float(np.asarray(t.read(), dtype=float).ravel()[0])
        except Exception:
            return math.nan
        finally:
            t.close()

    def _start_counters(self):
        for k in (0, 1):
            self._close("ctr%d" % k)
            t = self._out_task("ctr%d" % k, lambda t, k=k: t.ci_channels.add_ci_count_edges_chan(
                "%s/ctr%d" % (self.device, k)))
            t.start()

    # -- channel I/O ------------------------------------------------------------------------
    def read(self, channel):
        if channel == "ai":
            return self.scan()
        m = re.fullmatch(r"(ai|ao|di|do|ctr)(\d+)", channel)
        if m:
            kind, k = m.group(1), int(m.group(2))
            if kind == "ai":
                ai = self.ai_list()
                if k not in ai:
                    raise ChannelError("ai%d is not scanned" % k)
                return float(self.scan()[ai.index(k)])
            if kind == "ao" and k in (0, 1):
                if k not in self.ao_last:
                    v = self._ao_readback(k)
                    if not math.isnan(v):
                        self.ao_last[k] = v
                    return v
                return self.ao_last[k]
            if kind == "di" and k < 4:
                return 1.0 if list(np.ravel(self._di_task().read()))[k] else 0.0
            if kind == "do" and k < 4:
                return 1.0 if bool(np.ravel(self._do_task(k).read())[0]) else 0.0
            if kind == "ctr" and k < 2:
                t = self.tasks.get("ctr%d" % k)
                if t is None:
                    self._start_counters()
                    t = self.tasks["ctr%d" % k]
                return float(t.read())
        if channel == "rate":
            return self.rate
        if channel == "samples":
            return float(self.samples)
        if channel == "terminal":
            return self.terminal
        if channel == "ai_range":
            return self.ai_range
        raise ChannelError("%s has no channel %s" % (self.name, channel))

    def write(self, channel, value):
        m = re.fullmatch(r"(ao|do)(\d)", channel)
        if m:
            k = int(m.group(2))
            if m.group(1) == "ao" and k in (0, 1):
                if not -10.0 <= value <= 10.0:
                    raise ChannelError("analog outputs are limited to ±10 V")
                self._ao_task(k).write(float(value), auto_start=True)
                self.ao_last[k] = float(value)
                return
            if m.group(1) == "do" and k < 4:
                self._do_task(k).write(bool(value), auto_start=True)
                return
        if channel == "rate":
            self._check_rate(float(value))
            self.rate = float(value)
        elif channel == "samples":
            if value != int(value) or not 1 <= value <= 1_000_000:
                raise ChannelError("samples must be an integer 1 … 1000000")
            self.samples = int(value)
        elif channel == "terminal":
            old = self.terminal
            self.terminal = value
            try:
                self._check_rate(self.rate)
            except ChannelError:
                self.terminal = old
                raise
        elif channel == "ai_range":
            self.ai_range = value
        else:
            raise ChannelError("%s: channel %s is not writable" % (self.name, channel))
        self._close("ai")                       # rebuilt with the new settings

    def _action_reset_counters(self):
        self._start_counters()


NI6211.CHANNELS = NI6211("static", "DAQ::Dev1").get_channels()
DRIVER = NI6211
