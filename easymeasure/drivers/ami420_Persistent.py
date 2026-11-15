"""American Magnetics Model 420 programmer, magnet **with** a persistent switch.

Field changes only in driven mode (switch heater on); ``enter_persistent`` and
``exit_persistent`` switch between the modes in the order that cannot quench the
magnet.  The 420 does not know the persistent field: the driver records it (see
:mod:`easymeasure.drivers._ami420_base`).  For a magnet without a switch use
``ami420_noPersistent``.
"""

from ._ami420_base import AMI420Base, memory_file  # noqa: F401


class AMI420Persistent(AMI420Base):
    DESCRIPTION = "American Magnetics 420 magnet supply with a persistent switch"
    PERSISTENT = True
    CHANNELS = AMI420Base.CHANNELS + AMI420Base.PERSISTENT_CHANNELS
    ACTIONS = AMI420Base.PERSISTENT_ACTIONS + AMI420Base.ACTIONS


DRIVER = AMI420Persistent
