"""What the magnet drivers share: the ``wait_steady`` action."""

from __future__ import annotations

from ..backend.instrument import ActionSpec

#: the supply is not moving
STEADY_STATUS = ("HOLDING", "PAUSED")

#: polled every 0.5 s (the instrument stays usable meanwhile), abortable, at most 24 h
WAIT_STEADY = ActionSpec("wait_steady", "wait until status is HOLDING or PAUSED (the ramp is "
                         "done); an abort or Stop ends it, a quench fails it", 86400.0, 0.5)
