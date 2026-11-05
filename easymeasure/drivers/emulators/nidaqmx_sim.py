"""A stand-in for the ``nidaqmx`` package emulating one USB-6211 per device name.

Only the part of the nidaqmx API the ``ni6211`` driver uses is provided, with
the same names, argument conventions and return shapes as nidaqmx 1.x
(``task.read()`` gives a float for one channel, a list per channel for several,
lists of samples for timed reads).  The emulated card is wired as a loop-back
test box: ``ai0``/``ai1`` see ``ao0``/``ao1``, ``ai2 … ai15`` carry
``0.1 V × index`` plus a little noise, ``port0`` (digital inputs) reads back
``port1`` (digital outputs), and both counters count a 1 kHz clock.

Errors follow DAQmx: invalid channels, ranges, rates or a resource that is
already reserved raise :class:`errors.DaqError` with DAQmx's error codes.
"""

from __future__ import annotations

import enum
import re
import threading
import time
import types
from typing import Dict, List

import numpy as np

AI_RANGES = (0.2, 1.0, 5.0, 10.0)
MAX_AGGREGATE_RATE = 250e3
PRODUCT = "USB-6211"


# -- constants ---------------------------------------------------------------------
class TerminalConfiguration(enum.Enum):
    DEFAULT = -1
    RSE = 10083
    NRSE = 10078
    DIFF = 10106
    PSEUDO_DIFF = 12529


class AcquisitionType(enum.Enum):
    FINITE = 10178
    CONTINUOUS = 10123
    HW_TIMED_SINGLE_POINT = 12522


class LineGrouping(enum.Enum):
    CHAN_PER_LINE = 0
    CHAN_FOR_ALL_LINES = 1


class TaskMode(enum.Enum):
    TASK_START = 0
    TASK_STOP = 1
    TASK_VERIFY = 2
    TASK_COMMIT = 3
    TASK_RESERVE = 4
    TASK_UNRESERVE = 5
    TASK_ABORT = 6


class Edge(enum.Enum):
    RISING = 10280
    FALLING = 10171


class CountDirection(enum.Enum):
    COUNT_UP = 10128
    COUNT_DOWN = 10124


constants = types.SimpleNamespace(TerminalConfiguration=TerminalConfiguration,
                                  AcquisitionType=AcquisitionType, LineGrouping=LineGrouping,
                                  TaskMode=TaskMode, Edge=Edge, CountDirection=CountDirection)


class DaqError(Exception):
    def __init__(self, message, error_code):
        super().__init__("%s\nStatus Code: %d" % (message, error_code))
        self.error_code = error_code


errors = types.SimpleNamespace(DaqError=DaqError, Error=Exception)


# -- the emulated card ---------------------------------------------------------------
class _Card:
    def __init__(self, name: str):
        self.name = name
        self.ao = [0.0, 0.0]
        self.do = [False] * 4
        self.t0 = time.monotonic()
        self.reserved: Dict[str, object] = {}       # physical resource -> task
        self.lock = threading.Lock()
        self.rng = np.random.default_rng(1)

    def ai_value(self, ch: str) -> float:
        m = re.fullmatch(r"_ao(\d)_vs_aognd", ch)
        if m:
            return self.ao[int(m.group(1))]
        k = int(ch[2:])
        if k in (0, 1):
            return self.ao[k] + float(self.rng.normal(0, 1e-4))
        return 0.1 * k + float(self.rng.normal(0, 1e-4))


_cards: Dict[str, _Card] = {}
_cards_lock = threading.Lock()


def card(name: str) -> _Card:
    with _cards_lock:
        c = _cards.get(name)
        if c is None:
            c = _cards[name] = _Card(name)
        return c


def reset() -> None:
    with _cards_lock:
        _cards.clear()


class Device:
    def __init__(self, name: str):
        self.name = name

    @property
    def product_type(self) -> str:
        return PRODUCT


system = types.SimpleNamespace(Device=Device)


# -- tasks -----------------------------------------------------------------------------
def _split(physical: str):
    """``Dev1/ai0:3`` -> (card, ['ai0', 'ai1', 'ai2', 'ai3']); lists with commas too."""
    out_card, names = None, []
    for part in [p.strip() for p in physical.split(",") if p.strip()]:
        if "/" not in part:
            raise DaqError("Physical channel name not valid: %s" % part, -200170)
        dev, rest = part.split("/", 1)
        if out_card is not None and dev != out_card:
            raise DaqError("Channels of several devices in one task", -200170)
        out_card = dev
        m = re.fullmatch(r"([a-z_0-9]*?)(\d+):(\d+)", rest)
        if m:
            a, b = int(m.group(2)), int(m.group(3))
            step = 1 if b >= a else -1
            names += ["%s%d" % (m.group(1), i) for i in range(a, b + step, step)]
        else:
            m = re.fullmatch(r"(port\d+)/line(\d+):(\d+)", rest)
            if m:
                names += ["%s/line%d" % (m.group(1), i)
                          for i in range(int(m.group(2)), int(m.group(3)) + 1)]
            else:
                names.append(rest)
    return card(out_card), names


class _Collection:
    def __init__(self, task: "Task", kind: str):
        self.task = task
        self.kind = kind

    def _add(self, physical, check):
        c, names = _split(physical)
        if self.task.card is not None and self.task.card is not c:
            raise DaqError("Channels of several devices in one task", -200170)
        self.task.card = c
        for n in names:
            check(n)
            self.task.channels.append((self.kind, n))
        return names

    # AI
    def add_ai_voltage_chan(self, physical_channel, name_to_assign_to_channel="",
                            terminal_config=TerminalConfiguration.DEFAULT, min_val=-5.0,
                            max_val=5.0, units=None, custom_scale_name=""):
        span = max(abs(min_val), abs(max_val))
        if span > 10.0 + 1e-9 or min_val >= max_val:
            raise DaqError("Requested range is not supported by the device", -200077)

        def check(n):
            m = re.fullmatch(r"ai(\d+)", n)
            internal = re.fullmatch(r"_ao[01]_vs_aognd", n)
            if not m and not internal:
                raise DaqError("Physical channel %s does not exist" % n, -200170)
            if m:
                k = int(m.group(1))
                if k > 15 or (terminal_config == TerminalConfiguration.DIFF and k > 7):
                    raise DaqError("Physical channel %s does not support this terminal "
                                   "configuration" % n, -200077)
        return self._add(physical_channel, check)

    def add_ao_voltage_chan(self, physical_channel, name_to_assign_to_channel="", min_val=-10.0,
                            max_val=10.0, units=None, custom_scale_name=""):
        if min(min_val, max_val) < -10.0 - 1e-9 or max(min_val, max_val) > 10.0 + 1e-9:
            raise DaqError("Requested range is not supported by the device", -200077)

        def check(n):
            if not re.fullmatch(r"ao[01]", n):
                raise DaqError("Physical channel %s does not exist" % n, -200170)
        return self._add(physical_channel, check)

    def add_di_chan(self, lines, name_to_assign_to_lines="",
                    line_grouping=LineGrouping.CHAN_FOR_ALL_LINES):
        def check(n):
            if not re.fullmatch(r"port0/line[0-3]", n):
                raise DaqError("Line %s cannot be used for digital input" % n, -200170)
        return self._add(lines, check)

    def add_do_chan(self, lines, name_to_assign_to_lines="",
                    line_grouping=LineGrouping.CHAN_FOR_ALL_LINES):
        def check(n):
            if not re.fullmatch(r"port1/line[0-3]", n):
                raise DaqError("Line %s cannot be used for digital output" % n, -200170)
        return self._add(lines, check)

    def add_ci_count_edges_chan(self, counter, name_to_assign_to_channel="", edge=Edge.RISING,
                                initial_count=0, count_direction=CountDirection.COUNT_UP):
        def check(n):
            if not re.fullmatch(r"ctr[01]", n):
                raise DaqError("Physical channel %s does not exist" % n, -200170)
        names = self._add(counter, check)
        self.task.count0 = int(initial_count)
        return names


class _Timing:
    def __init__(self, task):
        self.task = task

    def cfg_samp_clk_timing(self, rate, source="", active_edge=Edge.RISING,
                            sample_mode=AcquisitionType.FINITE, samps_per_chan=1000):
        if rate <= 0:
            raise DaqError("Sample clock rate must be positive", -200081)
        if samps_per_chan < 2 and sample_mode == AcquisitionType.FINITE:
            raise DaqError("Finite acquisitions need at least 2 samples per channel", -200077)
        self.task.rate = float(rate)
        self.task.samps = int(samps_per_chan)


class Task:
    def __init__(self, new_task_name="", **_):
        self.name = new_task_name
        self.card = None
        self.channels: List[tuple] = []
        self.rate = None
        self.samps = None
        self.running = False
        self.closed = False
        self.committed = False
        self.count0 = 0
        self.t_start = None
        self.ai_channels = _Collection(self, "ai")
        self.ao_channels = _Collection(self, "ao")
        self.di_channels = _Collection(self, "di")
        self.do_channels = _Collection(self, "do")
        self.ci_channels = _Collection(self, "ci")
        self.timing = _Timing(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- resources ---------------------------------------------------------------------
    def _resources(self):
        kinds = {k for k, _ in self.channels}
        out = []
        if "ai" in kinds:
            out.append("ai")                    # one AI task at a time (single ADC)
        for k, n in self.channels:
            if k in ("ao", "ci"):
                out.append(n)
        if self.rate is not None and "ao" in kinds:
            out.append("ao_timing")
        return out

    def _reserve(self):
        if self.committed or self.running:
            return
        c = self.card
        with c.lock:
            for r in self._resources():
                owner = c.reserved.get(r)
                if owner is not None and owner is not self and not owner.closed:
                    raise DaqError("The specified resource is reserved. The operation could not "
                                   "be completed as specified. (%s)" % r, -50103)
            for r in self._resources():
                c.reserved[r] = self

    def _release(self):
        c = self.card
        if c is None:
            return
        with c.lock:
            for r, owner in list(c.reserved.items()):
                if owner is self:
                    del c.reserved[r]

    def _check(self):
        if self.closed:
            raise DaqError("Task specified is invalid or does not exist", -200088)
        if not self.channels:
            raise DaqError("Task contains no channels", -200478)

    def control(self, action):
        self._check()
        if action == TaskMode.TASK_COMMIT:
            self._validate_timing()
            self._reserve()
            self.committed = True
        elif action == TaskMode.TASK_UNRESERVE:
            self._release()
            self.committed = False

    def _validate_timing(self):
        if self.rate is not None:
            n = len(self.channels)
            if self.rate * n > MAX_AGGREGATE_RATE * 1.0000001:
                raise DaqError("Sample clock rate exceeds the device's maximum (%.0f S/s "
                               "aggregate)" % MAX_AGGREGATE_RATE, -200077)

    def start(self):
        self._check()
        self._validate_timing()
        self._reserve()
        self.running = True
        self.t_start = time.monotonic()

    def stop(self):
        self.running = False
        if not self.committed:
            self._release()

    def close(self):
        self.running = False
        self._release()
        self.closed = True

    def is_task_done(self):
        return True

    # -- data ---------------------------------------------------------------------------
    def read(self, number_of_samples_per_channel=None, timeout=10.0):
        self._check()
        kind = self.channels[0][0]
        c = self.card
        if kind == "ai":
            n = 1 if number_of_samples_per_channel is None else int(number_of_samples_per_channel)
            if self.rate is not None:
                need = n / self.rate
                if need > timeout:
                    raise DaqError("Some or all of the samples requested have not yet been "
                                   "acquired (timeout)", -200284)
                time.sleep(min(need, 0.05))
            if not self.running:
                self._reserve()
            data = [[c.ai_value(ch) for _ in range(n)] for _, ch in self.channels]
            if not self.running and not self.committed:
                self._release()
            if number_of_samples_per_channel is None:
                return data[0][0] if len(data) == 1 else [d[0] for d in data]
            return data[0] if len(data) == 1 else data
        if kind == "ao":
            raise DaqError("Read is not supported on output tasks of this device", -200473)
        if kind == "di":
            vals = [c.do[int(ch[-1])] for _, ch in self.channels]
            return vals[0] if len(vals) == 1 else vals
        if kind == "do":
            vals = [c.do[int(ch[-1])] for _, ch in self.channels]
            return vals[0] if len(vals) == 1 else vals
        if kind == "ci":
            if not self.running:
                raise DaqError("Counter task must be started before reading", -200473)
            return self.count0 + int((time.monotonic() - self.t_start) * 1000)
        raise DaqError("unsupported read", -200473)

    def write(self, data, auto_start=True, timeout=10.0):
        self._check()
        vals = data if isinstance(data, (list, tuple, np.ndarray)) else [data]
        if len(vals) != len(self.channels):
            raise DaqError("Write data does not match the number of channels", -200524)
        c = self.card
        self._reserve()
        for (k, ch), v in zip(self.channels, vals):
            if k == "ao":
                v = float(v)
                if not -10.0 - 1e-9 <= v <= 10.0 + 1e-9:
                    raise DaqError("Requested value is not a supported value for this property",
                                   -200077)
                c.ao[int(ch[-1])] = v
            elif k == "do":
                c.do[int(ch[-1])] = bool(v)
            else:
                raise DaqError("Write is not supported for input tasks", -200473)
        return 1
