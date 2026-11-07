"""A stand-in for the ``nidaqmx`` package emulating one NI USB DAQ per device name.

Only the part of the nidaqmx API the ``ni6211`` / ``ni63xx`` drivers use is
provided, with the same names, argument conventions and return shapes as
nidaqmx 1.x (``task.read()`` gives a float for one channel, a list per channel
for several, lists of samples for timed reads).  Each card is a USB-6211 unless
:func:`define` makes it another model of :mod:`.._nidaq_models` (channel counts,
ranges, rates, digital port layout).  The card is wired as a loop-back test box:
``aiK`` sees ``aoK`` (for every output), the other inputs carry ``0.1 V × index``
plus a little noise, every counter counts a 1 kHz clock; USB-6211: ``port0``
(digital inputs) reads ``port1`` (outputs); X Series: every digital line is wired
to the line half a port away (P0.0 ↔ P0.4 on an 8-line port), an input reads
what its partner drives, otherwise 0 (pull-down).

Errors follow DAQmx: invalid channels, ranges, rates or a resource that is
already reserved (a digital line used by two tasks, an X Series counter's source
PFI used as a digital line) raise :class:`errors.DaqError` with DAQmx's codes.
"""

from __future__ import annotations

import enum
import re
import threading
import time
import types
from typing import Dict, List

import numpy as np

from .._nidaq_models import MODELS, X_COUNTER_SOURCE_PFI, DaqModel

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
    def __init__(self, name: str, product: str = PRODUCT):
        self.name = name
        self.model: DaqModel = MODELS[product]
        self.ao = [0.0] * self.model.ao
        self.dout: Dict[str, bool] = {}              # "port1/line2" -> driven level
        self.t0 = time.monotonic()
        self.reserved: Dict[str, object] = {}       # physical resource -> task
        self.lock = threading.Lock()
        self.rng = np.random.default_rng(1)

    def ai_value(self, ch: str) -> float:
        m = re.fullmatch(r"_ao(\d)_vs_aognd", ch)
        if m:
            return self.ao[int(m.group(1))]
        k = int(ch[2:])
        if k < len(self.ao):
            return self.ao[k] + float(self.rng.normal(0, 1e-4))
        return 0.1 * k + float(self.rng.normal(0, 1e-4))

    def line_in(self, line: str) -> bool:
        """Level seen by a digital input."""
        if line in self.dout:
            return self.dout[line]
        port, k = re.fullmatch(r"port(\d+)/line(\d+)", line).groups()
        port, k = int(port), int(k)
        if self.model.family == "M":                 # USB-6211: P0 inputs see P1 outputs
            return self.dout.get("port1/line%d" % k, False)
        n = self.model.ports[port]
        return self.dout.get("port%d/line%d" % (port, (k + n // 2) % n), False)

    def line_ok(self, line: str, direction: str) -> bool:
        m = re.fullmatch(r"port(\d+)/line(\d+)", line)
        if not m:
            return False
        port, k = int(m.group(1)), int(m.group(2))
        return (port < len(self.model.ports) and k < self.model.ports[port]
                and direction in self.model.port_dirs[port])


_cards: Dict[str, _Card] = {}
_cards_lock = threading.Lock()


def card(name: str) -> _Card:
    with _cards_lock:
        c = _cards.get(name)
        if c is None:
            c = _cards[name] = _Card(name)
        return c


def define(name: str, product: str) -> _Card:
    """Make the emulated device *name* a *product* (a fresh card, unless it already is one)."""
    if product not in MODELS:
        raise DaqError("Device type not supported by the emulator: %s" % product, -200220)
    with _cards_lock:
        c = _cards.get(name)
        if c is None or c.model.product != product:
            c = _cards[name] = _Card(name, product)
        return c


def reset() -> None:
    with _cards_lock:
        _cards.clear()


class Device:
    def __init__(self, name: str):
        self.name = name

    @property
    def product_type(self) -> str:
        return card(self.name).model.product


class System:
    """``nidaqmx.system.System``: ``System.local().devices`` lists the defined cards."""

    def __init__(self):
        with _cards_lock:
            self.devices = [Device(n) for n in sorted(_cards)]

    @classmethod
    def local(cls) -> "System":
        return cls()


system = types.SimpleNamespace(Device=Device, System=System)


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
        if self.kind == "ai" and self.task.channels and \
                any(k != "ai" for k, _ in self.task.channels):
            raise DaqError("Mixed channel types in one task", -200559)
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
            md = self.task.card.model
            m = re.fullmatch(r"ai(\d+)", n)
            internal = re.fullmatch(r"_ao(\d)_vs_aognd", n)
            if internal and int(internal.group(1)) < md.ao:
                return
            if not m or int(m.group(1)) >= md.ai:
                raise DaqError("Physical channel %s does not exist" % n, -200170)
            k = int(m.group(1))
            diff = terminal_config == TerminalConfiguration.DIFF
            if (diff and k not in md.diff_inputs()) or (md.simultaneous and terminal_config not in (
                    TerminalConfiguration.DIFF, TerminalConfiguration.DEFAULT)):
                raise DaqError("Physical channel %s does not support this terminal "
                               "configuration" % n, -200077)
        return self._add(physical_channel, check)

    def add_ao_voltage_chan(self, physical_channel, name_to_assign_to_channel="", min_val=-10.0,
                            max_val=10.0, units=None, custom_scale_name=""):
        if min(min_val, max_val) < -10.0 - 1e-9 or max(min_val, max_val) > 10.0 + 1e-9:
            raise DaqError("Requested range is not supported by the device", -200077)

        def check(n):
            m = re.fullmatch(r"ao(\d+)", n)
            if not m or int(m.group(1)) >= self.task.card.model.ao:
                raise DaqError("Physical channel %s does not exist" % n, -200170)
        return self._add(physical_channel, check)

    def add_di_chan(self, lines, name_to_assign_to_lines="",
                    line_grouping=LineGrouping.CHAN_FOR_ALL_LINES):
        def check(n):
            if not self.task.card.line_ok(n, "i"):
                raise DaqError("Line %s cannot be used for digital input" % n, -200170)
        return self._add(lines, check)

    def add_do_chan(self, lines, name_to_assign_to_lines="",
                    line_grouping=LineGrouping.CHAN_FOR_ALL_LINES):
        def check(n):
            if not self.task.card.line_ok(n, "o"):
                raise DaqError("Line %s cannot be used for digital output" % n, -200170)
        return self._add(lines, check)

    def add_ci_count_edges_chan(self, counter, name_to_assign_to_channel="", edge=Edge.RISING,
                                initial_count=0, count_direction=CountDirection.COUNT_UP):
        def check(n):
            m = re.fullmatch(r"ctr(\d+)", n)
            if not m or int(m.group(1)) >= self.task.card.model.counters:
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
        md = self.card.model
        for k, n in self.channels:
            if k in ("ao", "ci", "di", "do"):
                out.append(n)
            if k == "ci" and md.family == "X":       # the counter's source terminal
                pfi = X_COUNTER_SOURCE_PFI[int(n[3:])]
                out.append("port%d/line%d" % (1 + pfi // 8, pfi % 8))
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
            for k, n in self.channels:
                if k == "di":
                    c.dout.pop(n, None)          # an input task makes the line an input

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
            md = self.card.model
            n = len(self.channels)
            tol = 1.0000001
            if self.rate > md.ai_single * tol or (not md.simultaneous and n > 1 and
                                                  self.rate * n > md.ai_aggregate * tol):
                raise DaqError("Sample clock rate exceeds the device's maximum (%.0f S/s per "
                               "channel, %.0f S/s aggregate)" % (md.ai_single, md.ai_aggregate),
                               -200077)

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
        self.closed = True                  # a closed DO task's lines keep their level

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
        if kind in ("di", "do"):
            self._reserve()
            if kind == "di":
                vals = [c.line_in(ch) for _, ch in self.channels]
            else:
                vals = [c.dout.get(ch, False) for _, ch in self.channels]
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
                lim = max(c.model.ao_ranges)
                if not -lim - 1e-9 <= v <= lim + 1e-9:
                    raise DaqError("Requested value is not a supported value for this property",
                                   -200077)
                c.ao[int(ch[2:])] = v
            elif k == "do":
                c.dout[ch] = bool(v)
            else:
                raise DaqError("Write is not supported for input tasks", -200473)
        return 1
