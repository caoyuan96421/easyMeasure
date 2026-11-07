"""Analog input for the NI-DAQmx drivers (``ni6211``, ``ni63xx``).

Every reading acquires only the inputs it returns: ``ai3`` converts one input
(so it may run at the single-input rate), the ``ai`` vector all scanned inputs
(their rates add up on a multiplexed card).  One task is kept per channel set;
only one can own the card's converter at a time, so switching from one set to
another unreserves the previous task and commits the next (a few ms); repeated
readings of the same set stay committed and start fast.
"""

from __future__ import annotations

from typing import List, Sequence

import numpy as np

from ..common.errors import ChannelError

MAX_AI_TASKS = 8                         # channel sets kept ready (oldest closed first)


class DaqAnalogInput:
    """Mixin: needs ``daq``, ``C``, ``device``, ``tasks``, ``rate``, ``samples``,
    ``terminal``, ``ai_range``, ``name``, ``_close(key)`` and ``_ai_limit(n)``."""

    _ai_active = None

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
