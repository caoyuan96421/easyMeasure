Keithley 2400-series SourceMeter (2400, 2401, 2410, 2420, 2425, 2430, 2440): one
source-measure unit.

**Modes.** In `Voltage` mode, `voltage` is the source (read/write) and `current` is
measured (read-only); the compliance is the current limit `Icompliance` in A. In
`Current` mode it is the other way round, with `Vcompliance` in V. The front ends
follow the switch at once.

* Changing the mode requires the output to be OFF. The driver refuses otherwise,
  rather than switching under load.
* The measured quantity reads NaN while the output is off, because nothing is measured.

`sense` selects 2- or 4-wire sensing, `terminals` front or rear, and `nplc` the
integration time.

A typical I–V curve in current mode, with `Isrc` → `smu.current` and `Vmeas` →
`smu.voltage`:

```json
{"name": "iv", "dimensions": [{"points": 101,
   "sweep": [{"channel": "Isrc", "start": 0, "stop": 1e-6}], "record": ["Vmeas"]}],
 "pre":  {"constants": [{"channel": "smu.mode", "value": "Current"},
                        {"channel": "smu.Vcompliance", "value": 2},
                        {"channel": "smu.output", "value": "ON"}]},
 "post": {"constants": [{"channel": "Isrc", "value": 0}, {"channel": "smu.output", "value": "OFF"}]}}
```

Options: `verify_writes` reads every source value back, which is worth it on RS-232.
`check_errors` reads the error queue after every setting, so a refused value raises
the instrument's own message.
