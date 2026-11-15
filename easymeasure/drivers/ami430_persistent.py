"""American Magnetics Model 430 programmer, magnet **with** a persistent switch.

Field changes only in driven mode (switch heater on); ``enter_persistent`` and
``exit_persistent`` switch between the modes in the order that cannot quench the
magnet (see :mod:`easymeasure.drivers._ami430_base`).  The programmer must be set up
with a switch (Load menu); for a magnet without one use ``ami430``.
"""

from ._ami430_base import AMI430Base


class AMI430Persistent(AMI430Base):
    DESCRIPTION = "American Magnetics 430 magnet supply with a persistent switch"
    PERSISTENT = True
    OTHER = "ami430"
    CHANNELS = AMI430Base.CHANNELS + AMI430Base.PERSISTENT_CHANNELS
    ACTIONS = AMI430Base.PERSISTENT_ACTIONS + AMI430Base.ACTIONS


DRIVER = AMI430Persistent
