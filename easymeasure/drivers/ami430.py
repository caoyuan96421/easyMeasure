"""American Magnetics Model 430 programmer, magnet **without** a persistent switch.

Setting ``field`` ramps to it and returns at once (``status``: RAMPING, then
HOLDING); a quench raises :class:`~easymeasure.common.errors.InstrumentFault` and
stops sweeps, scripts and the queue until ``clear_quench``.  The programmer must be
set up without a switch (Load menu); for a magnet with one use
``ami430_persistent``.  Ethernet (``TCP::<ip>::7180``) or RS-232.
"""

from ._ami430_base import AMI430Base


class AMI430(AMI430Base):
    DESCRIPTION = "American Magnetics 430 magnet supply (no persistent switch)"
    PERSISTENT = False
    OTHER = "ami430_persistent"


DRIVER = AMI430
