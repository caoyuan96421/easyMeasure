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

* **Deadline.** It runs on a dedicated worker thread with a hard deadline
  (`call_timeout`). A driver that hangs forever is abandoned, its port is invalidated
  and a fresh worker is started.
* **Port arbitration.** It runs inside a transaction on its port.
* **Contained exceptions.** Every exception is caught and converted to an
  `InstrumentError`, including `SystemExit`, `KeyboardInterrupt` and `MemoryError`
  raised by buggy driver code.
* **Retries.** Failed reads and writes are retried (`retries`). After
  `reconnect_after` consecutive failures the instrument is reconnected. Actions are
  never retried, because a trigger must not fire twice.
* **Validation.** The result is converted to the channel's declared type and shape;
  a wrong one is an error, not data.
* **Circuit breaker.** A channel that times out `breaker_threshold` times in a row
  is quarantined. It fails fast for `breaker_cooldown` seconds, doubling on repeated
  trips, so one pathological channel cannot block the rest of its instrument.

### Guard options

Per instrument, in its options:

| option | default | meaning |
|---|---|---|
| `call_timeout` | 10 s | deadline per call (channels may set their own) |
| `retries` | 2 | retries of a failed read / write |
| `retry_delay` | 0.05 s | pause between retries |
| `reconnect_after` | 2 | consecutive failures before a reconnect |
| `timeout_retries` | 1 | retries after a time-out |
| `breaker_threshold` | 2 | consecutive time-outs before a channel is quarantined |
| `breaker_cooldown` | 10 s | quarantine time (doubles on repeated trips) |

## Ports

Every physical port is one `Resource`:

* a second instrument asking for an exclusively owned port is refused;
* ports declared `shared` are arbitrated by a fair, re-entrant lock per I/O sequence.
  When ownership changes, the new owner's `select()` runs (e.g. `++addr 5` on a
  Prologix adapter);
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
quench ([`ami430`](../reference/drivers/ami430.md)). Such a fault is not an ordinary
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
* PowerPoint slides that cannot be written to the target go to a `_pending` file.

## Seeing it work

```bash
python -m easymeasure.backend --setup examples/faulty_setup.json
```

Then run `examples/sweep_faulty.json`. The instruments drop commands, garble
replies, hang, disconnect and raise random exceptions, and `sim_chaos` does worse.
The sweep completes. The log shows each retry, reconnect and quarantine, and the
instrument list shows each instrument's statistics.

The [stress tests](testing.md#stress-tests) go much further.
