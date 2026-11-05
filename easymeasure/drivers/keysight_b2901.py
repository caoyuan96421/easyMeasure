"""Keysight B2901A/B (and B2911A/B) single-channel precision SMU.

Same channels as ``keithley2400`` (``mode``, ``voltage``, ``current``,
``Icompliance``/``Vcompliance``, ``output``, ``nplc``, ``sense``) plus ``low``
(low terminal grounded or floating).  NPLC 0.0004 … 100.  Address e.g.
``USB0::0x2A8D::0x9201::MY12345678::INSTR``, ``TCPIP0::192.168.1.20::inst0::INSTR``
or ``GPIB0::23::INSTR``; ``SIM::<name>`` uses the built-in emulator.
"""

from __future__ import annotations

from ._b2900 import B2900Base


class KeysightB2901(B2900Base):
    DESCRIPTION = "Keysight B2901 precision source/measure unit (1 channel)"
    OUTPUTS = ["1"]

    @staticmethod
    def EMULATOR(options):                     # noqa: N802
        from .emulators.smu import B2900Emulator
        return B2900Emulator(options, outputs=1)


KeysightB2901.CHANNELS = KeysightB2901.static_channels()
DRIVER = KeysightB2901
