# Simulated instruments

easyMeasure can be learned, demonstrated and tested without hardware. There are two
kinds of simulation.

## Simulated instruments

These are drivers of their own, built to be measured: a simulated quantum dot,
a magnet that ramps in real time, a camera whose spot moves with two voltages.
`examples/demo_setup.json` uses all of them.

| driver | simulates |
|---|---|
| `sim_source` | 4-channel DC source, SCPI over a shared `SIM::` bus |
| `sim_dmm` | DMM measuring a simulated quantum dot (Coulomb diamonds) on the same bus |
| `sim_lockin` | lock-in: X, Y, R, θ and a 2-vector `XY` |
| `sim_spectrum` | 1-D trace per reading; the peak shifts with field and voltage |
| `sim_camera` | 2-D image per reading; the spot moves with two voltages |
| `sim_awg` | vector-valued write channel (waveform) |
| `sim_magnet` | field that ramps in real time; `status` RAMPING / HOLDING |
| `sim_temperature` | setpoint with first-order thermal lag |
| `sim_chaos` | pathological: wrong types, random shapes, hangs forever, slow, raises `SystemExit` / `KeyboardInterrupt` / `MemoryError`, flaky writes, binary garbage |

The simulated instruments share one simulated world, so their readings are
consistent. The dot's current depends on `src.V1` and `src.V2`, the spectrum on the
magnet's field, and so on.

??? example "From Python"

    ```python
    c.call("add_instrument", name="src", driver="sim_source", address="SIM::GPIB0")
    c.call("add_instrument", name="dmm", driver="sim_dmm",    address="SIM::GPIB0")  # same bus
    c.call("add_instrument", name="magnet", driver="sim_magnet", address="NONE")
    ```

### Fault injection

Every simulated driver takes options that make it misbehave like real hardware:

| option | effect |
|---|---|
| `error_rate` | a command is lost (time-out) |
| `garble_rate` | a corrupted reply, or a value of the wrong type |
| `hang_rate`, `hang_time` | a call hangs (for `hang_time` seconds) |
| `disconnect_rate` | the instrument drops off the bus |
| `exception_rate` | the driver raises a random exception |
| `nan_rate` | a NaN reading |
| `latency` | every call takes this long |
| `seed` | make the faults reproducible |

??? example "From Python"

    ```python
    c.call("add_instrument", name="flaky", driver="sim_dmm", address="SIM::GPIB0",
           options={"error_rate": 0.05, "hang_rate": 0.01, "hang_time": 30, "seed": 1})
    ```

`examples/faulty_setup.json` uses these options on every instrument, plus `sim_chaos`.
Run `examples/sweep_faulty.json` with it to watch the
[robustness machinery](robustness.md) at work.

## Emulated hardware

Every hardware driver can talk to a built-in **emulator** of its instrument
instead of a port. Give it the address `SIM::<anything>`, or `DAQ::SIM` for the NI
DAQs:

??? example "From Python"

    ```python
    c.call("add_instrument", name="smu", driver="keithley2400", address="SIM::smu")
    c.call("add_instrument", name="lockin", driver="sr830", address="SIM::li")
    c.call("add_instrument", name="daq", driver="ni63xx", address="DAQ::SIM",
           options={"model": "USB-6363"})
    ```

The emulators (`easymeasure/drivers/emulators/`) implement the instruments' command
tables from their programming manuals:

* short and long SCPI forms;
* optional nodes;
* the error queue and `*ESR?`.

Their physics is good enough to see something:

* an SMU drives a resistor, with compliance clamping;
* a lock-in sees a phase-shifted signal;
* the DAQ has loop-back wiring: `aiK` sees `aoK`, and each digital line is wired to
  the one half a port away;
* the Cygnus supply has resistive loads (option `load_ohm`).

Use emulators to rehearse a measurement before the instrument is connected, to
check a setup file, or to develop against a driver. The
[driver reference pages](../reference/drivers/index.md) list each driver's channels
as reported by its emulator.
