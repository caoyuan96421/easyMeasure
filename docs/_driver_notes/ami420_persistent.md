American Magnetics Model 420 power supply programmer for a superconducting magnet
**with a persistent switch**. For a magnet without one, use [`ami420`](ami420.md).
There is no setting for this: the driver you choose says whether there is a switch.
The 420 cannot be asked, so the choice is yours. The driver was written from the
Model 420 manual (Rev. 7, chapter 4).

The 420 is not an older 430. Many of its commands differ, and when it gets a query it
does not know, it beeps, logs error -201 and **sends no reply**, so every such query
ends in a timeout. That is what happens with an `ami430` driver on a 420, which is why the 420 and 430
drivers each refuse the other model when they connect.

The magnet's configuration (coil constant, current limit, ramp rate, switch heater
current and heating time, quench detection) is set on the programmer's front panel.
The driver only reads it.

**Address.**

* RS-232, e.g. `COM3` or `ASRL3::INSTR`. The 420's default is 9600 baud, 8 data bits,
  no parity, 1 stop bit, no handshaking (its Comm Setup menu). Use the cable described
  in its manual: the 420 is a DCE.
* IEEE-488, e.g. `GPIB0::22::INSTR`. 22 is its default address.

The field units (kG or T) and the ramp-rate units are read and converted: `field` is
always in T and `ramp_rate` in T/min.

## What differs from the 430

| | Model 420 | Model 430 |
|---|---|---|
| target | the *programmed* field (`CONFigure:FIELD:PROGram`) | `CONFigure:FIELD:TARGet` |
| current measurement | one: the shunt in the supply output | supply and magnet |
| field in persistent mode | not known to the programmer: **recorded by the driver** | kept by the programmer |
| `STATE?` 8 / 9 | HEATING SWITCH / AT ZERO | AT ZERO / HEATING SWITCH |
| switch cooling | no state, no time: **timed by the driver** (`cool_time`) | COOLING SWITCH state |
| switch installed? | no query: you choose the driver | `PSwitch:INSTalled?` (must agree with the driver) |

## Setting the field

Setting `field` starts the ramp and returns at once. `status` reads `RAMPING`, then
`HOLDING`. A sweep waits at every step with a
[point action](../../guide/point-actions.md):

```python
wait_until(lambda: get('magnet.status') == 'HOLDING', timeout=3600)
```

## Persistent switch

The field can only be changed in **driven** mode (heater on). The two
actions do their steps in the order that cannot quench the magnet. Before every step
they read the programmer's state again:

| Enter Persistent | Exit Persistent |
|---|---|
| 1. The supply must hold the field. Wait `settle_time` s, and until the magnet voltage is below `max_magnet_voltage`. | 1. Ramp the supply to the **recorded** current and wait until it holds there. |
| 2. **Record the current**, then turn the heater off. Wait `cool_time` s (default 60) while the switch cools; field changes are refused meanwhile. | 2. Heat the switch, **only if** the supply current matches the recorded current within `match_tolerance` A. |
| 3. Ramp the leads to zero (option `zero_leads`) and wait for `AT ZERO`. Status: `PERSISTENT`. | 3. Wait for the 420's heating time (`HEATING SWITCH`), then hold the field. Status: `HOLDING`. |

### The recorded persistent current

The 420 only measures the supply current. Once the switch is cold, it no longer knows
the magnet's current: its manual tells the operator to write it down. The driver does
that.

* **Where it is kept.** Entering persistent mode saves the current to a file in
  the easyMeasure home folder (`~/.easymeasure/ami420/<address>.json`), *before* the
  heater goes off. So it survives a restart of the backend or of the computer.
* **Reading it.** In persistent mode, `field` and `current` read the recorded values,
  and `persistent_field` shows the recorded field. `supply_current` and
  `supply_field` are the leads.
* **After exiting.** Once the magnet is driven again, the record is deleted.

A magnet made persistent by hand (on the front panel, or by another program) has no
record. A 420 also starts with its heater off at power-on. In both cases, `status`
reads `PERSISTENT`, `field` reads **NaN**, and *Exit Persistent* is refused until you
set **`persistent_field`** to the field the magnet was left at (0 for a magnet that
was never energized).

Setting `persistent_field` by hand needs care. *Exit Persistent* ramps the supply to
that value, then heats the switch. A wrong value forces the difference into the
magnet when the switch opens, and that can quench it.

`pause` stops the supply and a running procedure. It does not stop the cooling time:
after the heater goes off, the field cannot be changed until `cool_time` has passed.

## Quench

As with the 430: the programmer detects the quench and takes the supply to zero. The
driver checks for one at every call and at every watchdog poll.

* A running sweep or script, the run queue and the user actions are stopped.
* Field writes and the persistent actions are refused until the quench is cleared
  with `clear_quench` (or RESET QUENCH on the front panel).

See [Robustness](../../guide/robustness.md#safety-faults).
