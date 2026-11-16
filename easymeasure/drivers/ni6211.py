"""National Instruments USB-6211 multifunction DAQ (also USB-6210/6215, through NI-DAQmx).

Needs NI's NI-DAQmx driver runtime (from ni.com); the ``nidaqmx`` Python package
is installed with easyMeasure.  The address names the device as shown in NI MAX:
``DAQ::Dev1``.  ``DAQ::SIM`` uses a built-in emulation
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

Settings: ``rate`` (samples/s per input: a single input ``aiK`` is acquired alone,
up to 250 kS/s; the ``ai`` vector acquires all scanned inputs, together at most
250 kS/s), ``samples`` (per reading, averaged; 1 = a single on-demand
conversion, fastest), ``terminal`` (RSE / NRSE / DIFF) and ``ai_range``
(±10 / ±5 / ±1 / ±0.2 V).  Option ``ai_channels`` (e.g. ``"0-3"``) limits the
exposed inputs and those of the ``ai`` vector (a higher rate for it).
"""

from __future__ import annotations

import math
import re
from typing import Dict, List

import numpy as np

from ..backend.instrument import ActionSpec, ChannelSpec
from ..common.errors import ChannelError
from ._nidaq import DaqBase, parse_list

AI_RANGES = ["±10 V", "±5 V", "±1 V", "±0.2 V"]
TERMINALS = ["RSE", "NRSE", "DIFF"]
MAX_AGGREGATE = 250e3


class NI6211(DaqBase):
    DESCRIPTION = "NI USB-6211 DAQ: 16 AI, 2 AO, 4 DI, 4 DO, 2 counters (NI-DAQmx)"
    IDN_MATCH = ("USB-621",)
    ACTIONS = [ActionSpec("reset_counters", "restart both edge counters at 0")]
    DEFAULT_OPTIONS = {"ai_channels": "all", "rate": 10000.0, "samples": 100,
                       "terminal": "RSE", "ai_range": "±10 V"}

    def __init__(self, name, address, options=None):
        super().__init__(name, address, options)
        self._settings_from_options()

    def _settings_from_options(self) -> None:
        self.rate = float(self.options["rate"])
        self.samples = int(self.options["samples"])
        self.terminal = str(self.options["terminal"]).upper()
        self.ai_range = str(self.options["ai_range"])

    # -- connection ---------------------------------------------------------------------
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
        self.tasks = {}
        self.ao_last: Dict[int, float] = {}
        self._settings_from_options()
        if self.terminal not in TERMINALS or self.ai_range not in AI_RANGES:
            raise ChannelError("options terminal / ai_range must be one of %s / %s"
                               % (TERMINALS, AI_RANGES))
        self._check_rate(self.rate, 1)
        self._start_counters()

    def _reset_tasks(self):
        self._close_all()                         # tasks are recreated on demand
        self._start_counters()

    # -- channel list -----------------------------------------------------------------------
    def ai_list(self) -> List[int]:
        hi = 7 if self.terminal == "DIFF" else 15
        try:
            chans = parse_list(self.options["ai_channels"], 15)
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
            ChannelSpec("rate", "rw", "float", (), "S/s", "sample rate per analog input: one "
                        "input (aiK) up to 250 kS/s; the ai vector (%d inputs) up to %.4g kS/s "
                        "each" % (len(ai), self._ai_limit(len(ai)) / 1e3), config=True),
            ChannelSpec("samples", "rw", "float", (), "", "samples averaged per analog reading "
                        "(1 = single conversion)", config=True),
            ChannelSpec("terminal", "rw", "str", (), "", "analog input terminal configuration",
                        config=True, allowed=TERMINALS),
            ChannelSpec("ai_range", "rw", "str", (), "", "analog input range", config=True,
                        allowed=AI_RANGES),
        ]
        return ch

    # -- analog input ---------------------------------------------------------------------
    def _ai_limit(self, n: int) -> float:
        return MAX_AGGREGATE / max(1, n)          # one converter, shared by the scanned inputs

    def _ai_device_text(self) -> str:
        return "USB-6211: 250 kS/s for all inputs of one acquisition together"

    def scan(self) -> np.ndarray:
        """One acquisition of every scanned input; returns the per-input means."""
        return self.acquire(self.ai_list())

    # -- outputs ------------------------------------------------------------------------
    def _ao_task(self, k: int):
        return self._task("ao%d" % k, lambda t: t.ao_channels.add_ao_voltage_chan(
            "%s/ao%d" % (self.device, k), min_val=-10.0, max_val=10.0))

    def _do_task(self, k: int):
        return self._task("do%d" % k, lambda t: t.do_channels.add_do_chan(
            "%s/port1/line%d" % (self.device, k), line_grouping=self.C.LineGrouping.CHAN_PER_LINE))

    def _di_task(self):
        return self._task("di", lambda t: t.di_channels.add_di_chan(
            "%s/port0/line0:3" % self.device, line_grouping=self.C.LineGrouping.CHAN_PER_LINE))

    def _start_counters(self):
        for k in (0, 1):
            self._close("ctr%d" % k)
            t = self._task("ctr%d" % k, lambda t, k=k: t.ci_channels.add_ci_count_edges_chan(
                "%s/ctr%d" % (self.device, k)))
            t.start()

    # -- channel I/O ------------------------------------------------------------------------
    def daq_read(self, channel):
        if channel == "ai":
            return self.scan()
        m = re.fullmatch(r"(ai|ao|di|do|ctr)(\d+)", channel)
        if m:
            kind, k = m.group(1), int(m.group(2))
            if kind == "ai":
                ai = self.ai_list()
                if k not in ai:
                    raise ChannelError("ai%d is not scanned" % k)
                return float(self.acquire([k])[0])
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

    def daq_write(self, channel, value):
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
            self._check_rate(float(value), 1)
            self.rate = float(value)
        elif channel == "samples":
            if value != int(value) or not 1 <= value <= 1_000_000:
                raise ChannelError("samples must be an integer 1 … 1000000")
            self.samples = int(value)
        elif channel == "terminal":
            self.terminal = value
        elif channel == "ai_range":
            self.ai_range = value
        else:
            raise ChannelError("%s: channel %s is not writable" % (self.name, channel))
        self._close_ai()                        # rebuilt with the new settings

    def _action_reset_counters(self):
        self._start_counters()


NI6211.CHANNELS = NI6211("static", "DAQ::Dev1").get_channels()
DRIVER = NI6211
