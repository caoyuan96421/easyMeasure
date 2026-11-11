Keysight B2901A/B2911A (and B2961) precision source/measure unit, one output. It
works like the [Keithley 2400](keithley2400.md): `mode` Voltage / Current, the
measured quantity read-only, `Icompliance` ↔ `Vcompliance`. In addition:

* `low`: the low terminal grounded or floating;
* NPLC up to 100.

The measured quantity reads NaN while the output is off. The B2900 would otherwise
switch its output on by itself for a measurement. The mode can only be changed with
the output off.

Reachable over GPIB, USB-TMC, LAN (VISA or a raw socket on port 5025) and serial.
