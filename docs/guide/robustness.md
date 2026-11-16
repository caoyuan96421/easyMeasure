# Robustness

A measurement may run for days, with instruments that hang, drop commands, return
garbage, lose their USB connection or crash their drivers. easyMeasure is built so
that none of this takes the measurement, the backend or the data down with it.

## Separate processes

* The **backend** owns the instruments and runs measurements. It does not depend
  on any front end.
* **GUIs**, the **tray icon** and the **remote gateway** are separate processes.
  Killing a frozen GUI never touches the backend; a reopened GUI picks up the
  running sweep. A crash of the gateway cannot affect a running measurement, and
  the backend restarts it.
* Clients talk to the backend over framed JSON with raw numpy buffers, with no
  pickle. Every client has bounded queues:
    * live data for a slow client is dropped above 16 MB, and the client is told to
      resynchronise;
    * a client whose socket accepted nothing for 60 s, or has more than 64 MB waiting
      (a frozen GUI), is disconnected;
    * idle connections are never closed.

## Guarded instrument calls

Every driver call goes through an `InstrumentHandle`:

* **Deadline.** It runs on the instrument's own worker thread with a hard deadline
  (`call_timeout`) for the driver's own time. A driver that does not return in time
  is abandoned and the instrument is *hung*. Time spent waiting for a shared GPIB
  bus, in the transfers themselves (up to their time-outs) and between their
  attempts does not count against the deadline (see [GPIB buses](#gpib-buses)).
* **Port arbitration.** It runs inside a transaction on its port.
* **Contained exceptions.** Every exception is caught and converted to an
  `InstrumentError`, including `SystemExit`, `KeyboardInterrupt` and `MemoryError`
  raised by buggy driver code.
* **Retries in the communication layer.** Every `write`, `read` and `query` of a
  driver is tried again by the port when it fails: a time-out, a port error, no
  listener (`retries`, 3 by default, with `retry_delay` between attempts; the bus is
  free for others meanwhile). Only after the last attempt does the driver get the
  error, and it may catch it. The driver call itself runs once, so a spurious error
  costs at most that one point.
* **Validation.** The result is converted to the channel's declared type and shape;
  a wrong one is an error, not data.
* **Failures stay until Reconnect.** When a transfer failed every attempt and the
  driver let the error through, or the connection failed, the instrument goes to
  *error*; when the call did not return in time, to *hung*. It **stays there**:
  every later call is refused at once, without touching the bus, until you press
  *Reconnect*. Nothing reconnects behind your back: on a shared GPIB bus that would
  keep disturbing the instruments that do work. A bug in a driver, a reply that
  makes no sense, or a refused value fails only that call.
* **Every front end sees it.** A change of state, also inside a call, is announced
  at once (`instrument_state`), so the instrument list shows *error* or *hung*
  straight away.

### GPIB buses

All instruments on one GPIB bus share one lock, because the bus does one transfer at
a time: a GPIB card (`GPIB0::…`), a Prologix adapter (`PROLOGIX::COM4::…`) or a
simulated bus (`SIM::lab::…`). Every write, read and query (write and read together) holds
the bus only for that transfer.

* An instrument waiting for the bus is not timing out: the wait is not part of its
  `call_timeout`. If the bus stays busy for longer than `bus_wait` (60 s), the call
  fails as *bus busy*, and the waiting instrument keeps its state.
* An instrument that is switched off, or set to the wrong address, fails at once
  ("no listener"), not after a time-out.
* The first reply on connect waits only `identify_timeout` (2 s), so an instrument
  that is not there does not hold the bus for long.

### Guard options

Per instrument, in its options:

| option | default | meaning |
|---|---|---|
| `call_timeout` | 10 s | deadline per call (channels may set their own) |
| `retries` | 3 | how often the port tries a failed transfer again |
| `retry_delay` | 0.05 s | pause between attempts (grows with each attempt) |
| `bus_wait` | 60 s | longest wait for a shared GPIB bus before a call fails as *bus busy* |
| `identify_timeout` | 2 s | time-out of the first reply on connect (SCPI drivers, HP 4145, Yokogawa 7651) |

## Ports

Every physical port is one `Resource`:

* a second instrument asking for an exclusively owned port is refused;
* ports declared `shared` are arbitrated by a fair, re-entrant lock per I/O sequence;
* GPIB instruments each have their own port object and take turns per transfer on
  their bus (see [GPIB buses](#gpib-buses));
* the measurement thread gets priority over monitor and GUI traffic, so monitoring
  never slows a sweep down.

## Protection of the sample

* Limits are checked before anything is written. For sweeps, **every setpoint** is
  checked before the first one is set.
* NaN and infinity are never written.
* Ramps are time-based and abortable. They start from where the instrument really is,
  since the cached value is discarded after actions and raw writes.
* Composite channels move all their targets together, each within its ramp rate.
* The post phase of a sweep runs after errors and aborts, to bring things back to a
  safe state.

## Safety faults

Some instruments can report a fault that endangers the hardware, such as a magnet
quench (the AMI magnet drivers, e.g.
[`ami430`](../reference/drivers/ami430.md)). Such a fault is not an ordinary
error:

* it is **never retried**, and never turned into a NaN reading or a skipped point,
  whatever the sweep's `on_read_error` and `on_set_error` say;
* the backend checks these instruments by itself (a watchdog, every `poll_interval`
  seconds), even when no sweep or monitor reads them;
* when one is detected, the backend aborts the running sweep or script (its status is
  *aborted* or *error*, with the fault as the reason), **stops the run queue** so that
  nothing else starts unattended, aborts running user actions, and sends
  `instrument_fault` to every front end. The desktop GUI shows a warning window, the
  web app an error message;
* the instrument's last error reads `FAULT: …` until the fault is cleared. The driver
  refuses what is unsafe meanwhile (for a magnet: changing the field), and reports the
  fault only once.

After clearing the fault on the instrument, start the queue again by hand.

## Scripts and code

* Abort reaches scripts on **every line**, so even `while True: pass` stops.
* Composite and mapping formulas are evaluated by a sandboxed evaluator, with an
  allow-list of functions, no attribute or file access, and bounded array sizes and
  powers.
* Dependency loops between composite channels are refused when defined, and a runtime
  guard stops any that slip through, so a call cannot hang the backend.

## Data

* The journal is append-only and CRC-checked, and is `fsync`-ed every 0.5 s. After a
  crash it loads up to the last fraction of a second.
* If the disk fails mid-run, the sweep keeps running in memory and the error is
  reported.
* An aborted or crashed sweep can be [continued](queue.md#continuing-an-aborted-sweep)
  from the first point it did not measure. The run goes into a new file that holds
  the old data and the new data together, and lists every abort.
* PowerPoint slides that cannot be written to the target go to a `_pending` file.

## Seeing it work

```bash
python -m easymeasure.backend --setup examples/faulty_setup.json
```

Then run `examples/sweep_faulty.json`. The instruments drop commands, garble
replies, hang, disconnect and raise random exceptions, and `sim_chaos` does worse.
The sweep completes. Lost replies are retried by the port; a garbled one costs that
point (NaN, `on_read_error` = `nan`). An instrument that hangs or drops its port
goes to *error* or *hung* and stays there, and its readings become NaN until you
press *Reconnect*. The log shows each retry and failure, and the instrument list
shows each instrument's statistics.

The [stress tests](testing.md#stress-tests) go much further.
