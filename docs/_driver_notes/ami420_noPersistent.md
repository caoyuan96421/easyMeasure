American Magnetics Model 420 power supply programmer for a superconducting magnet
**without a persistent switch**. For a magnet with one, use
[`ami420_Persistent`](ami420_Persistent.md). There is no setting for this: the 420
cannot be asked, so the driver you choose says it.

It was written from the Model 420 manual (Rev. 7, chapter 4). The 420 is not an
older 430: when it gets a query it does not know, it beeps and sends no reply, so the
`ami430` driver times out on it (it now refuses a 420 when connecting).

**Address.**

* RS-232, e.g. `COM3`. The default is 9600 baud, 8 data bits, no parity, 1 stop bit
  (the Comm Setup menu).
* IEEE-488, e.g. `GPIB0::22::INSTR`. 22 is the default address.

Field units (kG or T) and ramp-rate units are read and converted: `field` is always
in T and `ramp_rate` in T/min.

**The persistent switch is ignored completely.** A 420 without a switch does not
report its heater or persistent state correctly, so this driver never sends a
`PSwitch` command or query. It does not read a record of a persistent field, and it
does not refuse anything because of the switch. The magnet is driven directly at all
times, and `status` is the programmer's own state.

## Setting the field

Setting `field` sets the programmed field and starts the ramp. It returns at once:
`status` reads `RAMPING`, then `HOLDING`. A sweep waits at every step with a
[point action](../../guide/point-actions.md):

```python
set('magnet.wait_steady')
```

`wait_steady` waits until `status` is `HOLDING` or `PAUSED`, checking every 0.5 s.
The magnet stays readable meanwhile, so the monitor keeps updating. An abort or **Stop**
ends the wait, a quench makes it fail, and it gives up after 24 h.

`field` and `current` are measured at the 420's shunt, as on the front panel.
`supply_voltage` is the voltage the 420 commands. A field beyond the current limit is
refused. `pause` stops the ramp.

## Quench

The programmer detects a quench and takes the supply to zero. The driver checks for
one at every call and at every watchdog poll. A quench stops sweeps, scripts, the
queue and user actions. Field writes are then refused until `clear_quench` (or RESET
QUENCH). See [Robustness](../../guide/robustness.md#safety-faults).
