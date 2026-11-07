"""Instrument drivers.

One module per driver; the module name is the driver name used in setups
(``"driver": "keithley2400"``).  The backend's :class:`~easymeasure.backend.registry.DriverRegistry`
discovers every module here (names starting with ``_`` and sub-packages are helpers,
not drivers) plus ``<home>/drivers`` (drivers made with the Driver Wizard, see
:mod:`._command`) and the user's own driver directories (``driver_paths`` setting).

Hardware drivers
    ``keithley2400``, ``keysight_b2901``, ``keysight_b2902`` (source-measure units),
    ``sr830`` (SR830 / SR850 lock-in amplifiers), ``hp34401a`` (6.5 digit DMM),
    ``ni6211`` (NI USB-6211 multifunction DAQ), ``ni63xx`` (NI USB-63xx X Series
    DAQs, channels by model), ``yokogawa7651`` (DC voltage /
    current source), ``hp4145`` (HP 4145A/B and 4155/4156 in 4145 syntax, user
    mode), ``scpi_generic`` (any SCPI device, configured by options).

Simulated instruments (``sim_*``)
    Demo / test devices with fault injection, see :mod:`.simbus`.

The SCPI hardware drivers also talk to built-in emulators (:mod:`.emulators`) when
given a ``SIM::<name>`` address, so setups can be tried without hardware.
"""
