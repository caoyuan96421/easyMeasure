"""What the NI-DAQmx drivers (``ni6211``, ``ni63xx``) share: the device, the
NI-DAQmx module (or the built-in emulation for ``DAQ::SIM…``), the task bookkeeping
and the analog input.

Every reading acquires only the inputs it returns: ``ai3`` converts one input
(so it may run at the single-input rate), the ``ai`` vector all scanned inputs
(their rates add up on a multiplexed card).  One task is kept per channel set;
only one can own the card's converter at a time, so switching from one set to
another unreserves the previous task and commits the next (a few ms); repeated
readings of the same set stay committed and start fast.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Sequence

import numpy as np

from ..backend.instrument import Instrument
from ..common.errors import ChannelError

MAX_AI_TASKS = 8                         # channel sets kept ready (oldest closed first)


def parse_list(spec: str, hi: int) -> List[int]:
    """``"0-3,8"`` / ``"all"`` -> input numbers (0 … *hi*)."""
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


class DaqBase(Instrument):
    """An NI-DAQmx device at ``DAQ::<name in NI MAX>`` (``DAQ::SIM…``: emulated).
    Subclasses provide ``_ai_limit(n)`` and set ``rate``, ``samples``, ``terminal``
    and ``ai_range``."""

    INTERFACES = ("daq",)
    _ai_active = None

    def __init__(self, name, address, options=None):
        super().__init__(name, address, options)
        self.tasks: Dict[str, object] = {}

    @classmethod
    def suggested_addresses(cls, driver_name: str):
        return [{"address": "DAQ::SIM", "description": "built-in emulation"}]

    @property
    def device(self) -> str:
        return self.address[5:]                  # DAQ::<device>

    @property
    def simulated(self) -> bool:
        return self.device.upper().startswith("SIM")

    def _module(self):
        if self.simulated:
            from .emulators import nidaqmx_sim
            return nidaqmx_sim
        try:
            import nidaqmx
        except ImportError:
            raise IOError("the nidaqmx package is missing (it comes with easyMeasure: "
                          "pip install -e . again, or pip install nidaqmx)") from None
        return nidaqmx

    # -- calls: a DAQmx error leaves the tasks rebuilt for the next call -----------------
    def read(self, channel):
        return self._fresh_after_error(self.daq_read, channel)

    def write(self, channel, value):
        return self._fresh_after_error(self.daq_write, channel, value)

    def action(self, name):
        return self._fresh_after_error(super().action, name)

    def _fresh_after_error(self, fn, *args):
        try:
            return fn(*args)
        except ChannelError:
            raise
        except Exception:
            self._reset_tasks()
            raise

    def _reset_tasks(self) -> None:
        """After an error: drop the tasks (they are made again on demand)."""
        self._close_all()

    # -- tasks ----------------------------------------------------------------------------
    def disconnect(self):
        self._close_all()

    def _close_all(self) -> None:
        for t in list(self.tasks.values()):
            try:
                t.close()
            except Exception:                    # closing what may be broken already
                pass
        self.tasks = {}
        self._ai_active = None

    def _close(self, key: str) -> None:
        t = self.tasks.pop(key, None)
        if t is not None:
            try:
                t.close()
            except Exception:
                pass

    def _task(self, key: str, make):
        """The task *key*, made by ``make(task)`` the first time."""
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

    def _ao_readback(self, k: int) -> float:
        """An output nobody has written since connect: measured on the internal
        ``_aoK_vs_aognd`` channel (the AI task makes way for it).  NaN where the device
        cannot measure it."""
        self._ai_release()
        t = self.daq.Task()
        try:
            t.ai_channels.add_ai_voltage_chan("%s/_ao%d_vs_aognd" % (self.device, k),
                                              min_val=-10.0, max_val=10.0)
            return float(np.asarray(t.read(), dtype=float).ravel()[0])
        except Exception:
            return math.nan
        finally:
            t.close()

    # -- analog input ---------------------------------------------------------------------

    def _ai_limit(self, n: int) -> float:
        """Highest rate per input with *n* inputs in one acquisition."""
        raise NotImplementedError

    def _ai_device_text(self) -> str:
        return self.name

    def _check_rate(self, rate: float, n: int = 1) -> None:
        top = self._ai_limit(max(1, n))
        if not rate > 0 or rate > top * 1.0000001:
            raise ChannelError(
                "%s: %d input%s at %.6g S/s each is too fast for the %s (at most %.6g S/s each "
                "for %d input%s)%s" % (
                    self.name, n, "s" if n != 1 else "", rate, self._ai_device_text(), top, n,
                    "s" if n != 1 else "",
                    "; lower the rate, or scan fewer inputs (option ai_channels)" if n > 1 else ""))

    def _ai_release(self) -> None:
        """The task owning the converter gives it back (for another AI task)."""
        key, self._ai_active = self._ai_active, None
        t = self.tasks.get(key) if key else None
        if t is not None:
            try:
                t.control(self.C.TaskMode.TASK_UNRESERVE)
            except Exception:
                self._close(key)

    def _close_ai(self) -> None:
        """Close every AI task (settings changed: they are rebuilt on demand)."""
        for k in [k for k in self.tasks if k.startswith("ai:")]:
            self._close(k)
        self._ai_active = None

    def _ai_task(self, chans: Sequence[int]):
        key = "ai:" + ",".join(str(k) for k in chans)
        if self._ai_active == key and key in self.tasks:
            return self.tasks[key]
        self._ai_release()
        t = self.tasks.pop(key, None)
        if t is None:
            for old in [k for k in self.tasks if k.startswith("ai:")][:-(MAX_AI_TASKS - 1)]:
                self._close(old)
            C = self.C
            v = float(str(self.ai_range).strip("±V "))
            t = self.daq.Task()
            try:
                t.ai_channels.add_ai_voltage_chan(
                    ",".join("%s/ai%d" % (self.device, k) for k in chans),
                    terminal_config=getattr(C.TerminalConfiguration, self.terminal),
                    min_val=-v, max_val=v)
                if self.samples > 1:
                    t.timing.cfg_samp_clk_timing(self.rate, sample_mode=C.AcquisitionType.FINITE,
                                                 samps_per_chan=self.samples)
            except Exception:
                t.close()
                raise
        self.tasks[key] = t                     # (re)inserted last: most recently used
        try:
            t.control(self.C.TaskMode.TASK_COMMIT)    # fast start/stop for every reading
        except Exception:
            self._close(key)
            raise
        self._ai_active = key
        return t

    def acquire(self, chans: List[int]) -> np.ndarray:
        """One acquisition of *chans*; the mean of ``samples`` samples per input."""
        if not chans:
            raise ChannelError("%s: no analog input is scanned" % self.name)
        if self.samples > 1:
            self._check_rate(self.rate, len(chans))
        t = self._ai_task(chans)
        if self.samples > 1:
            t.start()
            try:
                data = t.read(number_of_samples_per_channel=self.samples,
                              timeout=2.0 + self.samples / self.rate)
            finally:
                t.stop()
        else:
            data = t.read()
        return np.asarray(data, dtype=float).reshape(len(chans), -1).mean(axis=1)
