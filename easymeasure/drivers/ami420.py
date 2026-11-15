"""American Magnetics Model 420 programmer, magnet **without** a persistent switch.

Setting ``field`` ramps to it and returns at once (``status``: RAMPING, then
HOLDING); a quench raises :class:`~easymeasure.common.errors.InstrumentFault` and
stops sweeps, scripts and the queue until ``clear_quench``.  Written from the
Model 420 manual (its command set is not the 430's; see
:mod:`easymeasure.drivers._ami420_base`).  For a magnet with a persistent switch
use ``ami420_persistent``.  RS-232 (9600 8N1 by default) or IEEE-488.
"""

from ._ami420_base import AMI420Base, memory_file  # noqa: F401


class AMI420(AMI420Base):
    DESCRIPTION = "American Magnetics 420 magnet supply (no persistent switch)"
    PERSISTENT = False


DRIVER = AMI420
