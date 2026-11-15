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

**Refused at connection**, with a message naming `ami420_Persistent`, when:

* the programmer's switch heater is on (the 420 heats only with a switch installed);
* a persistent field is on record for this address. `ami420_Persistent` leaves a
  record when it puts a magnet in persistent mode.

## Setting the field

Setting `field` sets the programmed field and starts the ramp. It returns at once:
`status` reads `RAMPING`, then `HOLDING`. A sweep waits at every step with a
[point action](../../guide/point-actions.md):

```python
wait_until(lambda: get('magnet.status') == 'HOLDING', timeout=3600)
```

`field` and `current` are measured at the 420's shunt, as on the front panel.
`supply_voltage` is the voltage the 420 commands. A field beyond the current limit is
refused. `pause` stops the ramp.

## Quench

The programmer detects a quench and takes the supply to zero. The driver checks for
one at every call and at every watchdog poll. A quench stops sweeps, scripts, the
queue and user actions. Field writes are then refused until `clear_quench` (or RESET
QUENCH). See [Robustness](../../guide/robustness.md#safety-faults).
