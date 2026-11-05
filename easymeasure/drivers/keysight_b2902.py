"""Keysight B2902A/B (and B2912A/B) two-channel precision SMU.

Like ``keysight_b2901``, once per output with the output number appended:
``mode1``, ``voltage1``, ``current1``, ``Icompliance1``/``Vcompliance1``,
``output1``, ``nplc1``, ``sense1``, ``low1`` and the same for output 2.  Each
output has its own mode.  ``SIM::<name>`` uses the built-in emulator.
"""

from __future__ import annotations

from ._b2900 import B2900Base


class KeysightB2902(B2900Base):
    DESCRIPTION = "Keysight B2902 precision source/measure unit (2 channels)"
    OUTPUTS = ["1", "2"]


KeysightB2902.CHANNELS = KeysightB2902.static_channels()
DRIVER = KeysightB2902
