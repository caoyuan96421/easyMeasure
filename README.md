# easyMeasure

A generic Python framework for controlling test instrumentation and running
automatic, arbitrary-dimensional measurements — with a crash-proof backend and
a PySide6 GUI that can be closed, killed or restarted at any time without
disturbing a running measurement.

![data view](docs/screenshots/data_view.png)

## Highlights

* **Backend / frontend split.** The backend is its own process. The GUI talks to
  it only over non-blocking sockets; killing a frozen GUI never touches the
  backend, and a reopened GUI picks up the running sweep where it is.
* **Hardened against buggy hardware.** Every driver call runs on a guarded
  worker thread with a hard deadline. Hung drivers are abandoned and the
  instrument reconnected; *any* exception (even `SystemExit`) is contained;
  replies are validated against the channel type/shape; transient errors are
  retried; a channel that keeps hanging is quarantined by a circuit breaker so
  it cannot block the rest of its instrument.
* **Crash-safe data.** Sweeps stream into an append-only, CRC-checked journal
  that is `fsync`-ed continuously. After a crash or power cut, everything up to
  the last fraction of a second loads as a partial dataset (missing points = NaN).
* **Arbitrary N-D sweeps** with any number of swept and recorded channels per
  dimension, mapping functions for non-uniform or vector/tensor setpoints,
  per-dimension delays, snake scans, pre/post constants, recordings and hooks.
* **Protection.** Limits and ramp rates live on aliases; every setpoint of a
  sweep is limit-checked *before* it starts; composite channels respect the ramp
  rate of every target.
* **Live N-D plotting** (curves, stacked curves, heat maps with draggable levels
  and colormaps, sliders for extra dimensions), a monitor panel, and automatic
  PowerPoint slides at the end of every run.

## Install

```bash
pip install -e .                 # numpy, PySide6, pyqtgraph, h5py, python-pptx, matplotlib
pip install -e ".[hardware]"     # + pyserial, pyvisa for real instruments
pip install -e ".[test]"         # + pytest
```

Python ≥ 3.9.

## Quick start (simulated instruments)

```bash
# 1. start the backend with the simulated demo setup
python -m easymeasure.backend --setup examples/demo_setup.json

# 2. start the GUI (any number of times, from any terminal)
python -m easymeasure.frontend
```

Or just `python -m easymeasure.frontend --start-backend` — it launches a
detached backend (restoring the last session's setup) if none is running.

While the backend runs, a **tray icon** shows its state (green idle, blue running,
orange paused, red error, gray unreachable) with the current run and progress in the
tooltip; its menu opens the GUI, pauses / resumes / aborts, opens the data folder or
log and shuts the backend down, and a notification appears when a run ends. The tray
is a separate helper process (`easymeasure.tray`), so it can never affect the backend;
start the backend with `--no-tray` to disable it.

On Windows: `setup_venv.bat` creates `.venv` and installs everything,
`run_demo_backend.bat` starts a backend with the simulated instruments and
`run_gui.bat` starts the GUI.

In the GUI: *Sweep editor → Load…* `examples/sweep_dot_map.json` → *Add & start*,
then watch *Live data*. Close the GUI mid-sweep and reopen it: the sweep keeps
running and the live view resynchronises.

To see the robustness machinery at work, start the backend with
`examples/faulty_setup.json` (instruments that drop commands, garble replies,
hang, disconnect and raise random exceptions, plus a deliberately pathological
`chaos` instrument) and run `examples/sweep_faulty.json`.

## Architecture

```
 ┌──────────────── GUI process (PySide6) ───────────────┐      ┌─ scripts / notebooks ─┐
 │ Setup │ Sweep editor │ Running │ Queue │ Live/File data│      │  easymeasure.client   │
 │ Monitor dock │ Log dock │  N-D plotting (pyqtgraph)   │      └──────────┬────────────┘
 └────────────── BackendConnection (QTcpSocket, async) ──┘                 │
                              │  framed JSON + raw numpy buffers (no pickle)│
 ┌────────────────────────── backend process ─────────────────────────────┴───────┐
 │ server.py   per-client reader/writer threads, bounded queues, droppable events │
 │ service.py  command API, event bus                                             │
 │ runqueue.py state machine idle/running/paused/aborting; sweeps + scripts        │
 │ sweep.py    definition, validation, N-D execution    scripting.py  script API   │
 │ storage.py  crash-safe journal (.emj), HDF5 export   monitor.py    polling      │
 │ channels.py aliases (limits, ramps, scale), composite channels                  │
 │ instrument.py  guarded handles: deadlines, retries, reconnect, circuit breaker  │
 │ resources.py   one object per port, exclusive/shared, fair priority arbitration │
 │ drivers/       one file per instrument (+ simulated instruments)                │
 │ logsys.py      rotating log file + ring buffer pushed to GUIs                   │
 └─────────────────────────────────────────────────────────────────────────────────┘
```

## Concepts

### Resources (`backend/resources.py`)
Every physical port (`COM3`, `GPIB0::12::INSTR`, `USB0::…`, `TCPIP0::…`,
`TCP::host::port`, simulated `SIM::bus`, or `NONE`) is represented by exactly
one `Resource`. A second instrument asking for an exclusively owned port is
refused. Ports declared `shared` (an instrument option, or a driver default) are
arbitrated: each I/O sequence runs inside a transaction holding a fair,
re-entrant lock; when ownership changes the new owner's `select()` runs (e.g.
`++addr 5` on a Prologix GPIB adapter). The measurement thread gets priority
over monitor/GUI traffic, so monitoring never slows a sweep down.

### Instruments and drivers (`backend/instrument.py`, `backend/drivers/`)
An instrument exposes **channels** for read / write / query. A channel is
`float` (scalar or an array of any dimension — e.g. a spectrum or a 2-D image)
or `str`. Each driver is one Python file with a class deriving from
`Instrument`, declaring `CHANNELS` and implementing `_read_<ch>` / `_write_<ch>`
(or `read`/`write`). See `examples/driver_template.py`. Drivers are found in
`easymeasure/backend/drivers/` and in extra directories (`driver_paths` setting);
a driver that fails to import is reported, never fatal. `scpi_generic` builds a
SCPI instrument purely from options.

**Configuration channels** (`ChannelSpec(..., config=True)`): settings such as
sensitivity, range, integration time or output on/off. They can be read and set
directly (`write_channel("lockin.sensitivity", "1e-3")`, GUI *Instrument channels →
Set…*, sweep pre/post constants and one-off records) but are never aliased or swept,
which keeps the channel lists short with many instruments.

**Actions** (`ACTIONS = [ActionSpec("reset", ...)]`, implemented by `_action_reset()`):
commands without arguments such as trigger, reset, auto-phase. Run them from the GUI
(*Instrument channels & actions → Run action*), the API (`run_action(ref="dmm.reset")`),
or scripts and sweep hooks (`action('dmm.reset')`; the sweep editor has a *+ action*
button that appends this to the pre/post hook). Actions are guarded like every other
call but never retried automatically (a trigger must not fire twice); afterwards the
cached values of that instrument's aliases are discarded, so the next ramp reads back.

Per-instrument guard options: `call_timeout` (deadline per call, default 10 s),
`retries` (2), `retry_delay`, `reconnect_after` (consecutive failures before a
reconnect, 2), `timeout_retries` (1), `breaker_threshold` (2) and
`breaker_cooldown` (10 s, doubling on repeated trips).

### Aliases and protection (`backend/channels.py`)
An alias names one instrument channel and carries the protection: `min`, `max`,
`ramp_rate` (units/s), `ramp_step`, `scale`/`offset` (alias = raw·scale+offset),
`allowed` values for text channels, `read_only`. NaN is never written. Ramps
are time-based and abortable. Raw `instrument.channel` references can be read
anywhere but only *set* through `set_raw()` in scripts.

### Composite channels
Virtual channels that drive several aliases together, e.g.
`{"name": "detuning", "targets": {"VL": "x", "VR": "-x + 0.1"}, "read": "(VL-VR)/2"}`.
Every target value is limit-checked before anything is written, and the ramp
duration is chosen so that no target exceeds its own ramp rate.

### Sweeps (`backend/sweep.py`)
```json
{"name": "map",
 "dimensions": [
   {"points": 101, "sweep": [{"channel": "Vg", "start": -0.5, "stop": 0.5},
                             {"channel": "wave", "mapping": "x*np.sin(np.linspace(0, 2*np.pi, 64))",
                              "start": 0, "stop": 1}],
    "record": ["I", "spectrum", "image"], "pre_delay": 0.05, "post_delay": 0, "snake": false},
   {"points": 21, "sweep": [{"channel": "Vb", "values": [-0.3, -0.2, ...]}], "record": ["T"],
    "record_after_inner": false}],
 "pre":  {"hook": "set('output','ON')", "constants": [{"channel": "mode", "value": "AC"}], "record": ["T"]},
 "post": {"record": ["T"], "constants": [{"channel": "Vg", "value": 0}], "hook": ""},
 "on_read_error": "nan", "on_set_error": "abort", "run_post_on_abort": true,
 "metadata": {"sample": "D17"}}
```
* Dimension 0 is the innermost (fastest) loop.
* Sweep channels: `start`/`stop` (linear), `values` (explicit list) or `mapping`
  — an expression of `i`, `N`, `t = i/(N-1)`, `start`, `stop`, `x` and numpy.
  A mapping is mandatory for vector/tensor channels. Text channels cannot be swept.
* Recording: a channel recorded in dimension *k* is stored as an array of shape
  `(N_k, …, N_{D-1}) + value_shape`. `@time` / `@elapsed` are built in.
* Order per step: set sweep channels → `pre_delay` → record → `post_delay` →
  inner dimensions. Pre phase: hook → constants → record. Post phase (always run,
  also after errors and — by default — aborts): record → constants → hook.
* All setpoints are computed and limit-checked before the sweep starts.

### Run queue (`backend/runqueue.py`)
States `idle`, `running`, `paused`, `aborting`. Items are sweeps or **scripts**
(arbitrary Python with `get`, `set`, `set_raw`, `sleep`, `wait_until`, `log`,
`instrument(name).raw_query()`, `run_sweep(definition)`, a persistent `state`
dict, `np`). Pause / resume / abort (current) / abort all / stop after current.
Abort reaches scripts on *every line*, so even `while True: pass` stops.

### Storage (`backend/storage.py`)
`<data_dir>/<date>/<run>_<name>.emj`: an append-only sequence of frames, each
with length + CRC32. The first frame holds the full sweep definition and all
setpoints, so any data file can be loaded back into the sweep editor. Frames
are flushed immediately and `fsync`-ed every `fsync_interval` (0.5 s). A torn
or corrupted tail is detected and ignored; the rest loads. At the end an HDF5
copy (`.h5`) is written. If the disk fails mid-run the sweep keeps running in
memory and the error is reported.

```python
from easymeasure.backend.storage import DataSet
ds = DataSet.load("00012_map.emj")        # complete or partial
ds.data["I"]; ds.setpoints[0]["Vg"]; ds.header["definition"]; ds.status
```

### Monitor, logging, setups
* Monitored channels are polled at their own intervals by one thread per
  instrument (a hung instrument never stalls the others) and pushed to all GUIs.
* All activity is logged to `~/.easymeasure/logs/backend.log` (rotating) and
  streamed to the GUI log dock.
* Setups (instruments, aliases, composites, monitor list) save/load as JSON and
  are auto-saved on every change (`--restore` reloads the last one).

### PowerPoint
When a run ends — normally, by error or by abort — a slide (plots of the
recorded channels + run summary) is appended to the configured `.pptx`
(*File → Settings*). The *Add slide to PPT* buttons do it on demand for the
running run or any loaded data file.

The file may be open in PowerPoint: on Windows the slide is inserted into the open
presentation through PowerPoint itself (needs `pywin32`, installed automatically on
Windows) and the presentation is saved. Only if that is impossible (file locked by a
sync client or open on another computer) does the slide go to
`<name>_pending.pptx`, so it is never lost.

## GUI

The window is split in two: instruments, sweep editor and queue on the left, data
(live view and any loaded files) on the right, the monitor on the far right and the
log at the bottom. The read-only *Running sweep* view can be shown from the *View* menu.

![layout](docs/screenshots/layout.png)

| | |
|---|---|
| ![setup](docs/screenshots/setup.png) | ![editor](docs/screenshots/sweep_editor.png) |

* **Instruments & channels** – add/edit/reconnect instruments (driver list with
  default options), inspect raw channels, define aliases and composites, manual
  get/set, save/load setups.
* **Sweep editor** – any number of dimensions; save/load JSON, load from a data
  file, validate against the backend, queue. **Running sweep** shows the
  executing definition read-only.
* **Queue** – controls, queued items (reorder/remove), history (*Rerun selected*
  queues the same sweep/script again, *Open data* opens its file), script editor.
* **Live data / data files** – N-D plot grid: choose X and Y among all sweep and
  value axes, other axes become sliders (*follow latest* tracks new data); curve,
  stacked curves or heat map (Y is grayed out for 1-D data); drag the histogram to set levels, pick colormaps.
  Redraws are coalesced, so fast sweeps don't load the GUI.
* **Monitor** dock, **Manual** dock below it and **Log** dock. The Manual panel lists
  every writable scalar alias and composite with a spin box, a slider (when the alias
  has a minimum and a maximum) and, on right click, *Set from expression…* (a function
  of the present value `x`, e.g. `x + 0.01`). Writes are protected and ramped; while a
  ramp runs, further changes to that channel are coalesced. The mouse wheel only acts
  on a focused control, so scrolling the panel never moves an instrument.
* **File → Settings → Display**: font size (applied immediately, also to plot axes)
  and an overall UI scale (50–300 %, applied on the next GUI start) to suit any
  screen resolution. The window can be made small: toolbars wrap, panels scroll.

Nothing in the GUI blocks on the backend: every request is asynchronous with a
callback, files load and slides render in worker threads.

## Simulated instruments

| driver | what it simulates |
|---|---|
| `sim_source` | 4-channel DC source, SCPI over a shared `SIM::` bus |
| `sim_dmm` | DMM measuring a simulated quantum dot (Coulomb diamonds) on the same bus |
| `sim_lockin` | lock-in: X, Y, R, θ and a 2-vector `XY` |
| `sim_spectrum` | 1-D trace per reading, peak shifts with field/voltage |
| `sim_camera` | 2-D image per reading, spot moves with two voltages |
| `sim_awg` | vector-valued write channel (waveform) |
| `sim_magnet` | field that ramps in real time; `status` RAMPING/HOLDING |
| `sim_temperature` | setpoint with first-order thermal lag |
| `sim_chaos` | pathological: wrong types, random shapes, hangs forever, slow, raises `SystemExit`/`KeyboardInterrupt`/`MemoryError`, flaky writes, binary garbage |

Fault injection options for every simulated driver: `error_rate` (lost
command), `garble_rate` (corrupted reply / wrong type), `hang_rate` +
`hang_time`, `disconnect_rate`, `exception_rate`, `nan_rate`, `latency`, `seed`.

## Testing

```bash
python -m pytest -q tests                   # ~3 min, GUI tests run offscreen
python examples/stress_test.py --minutes 10  # soak test against faulty hardware
```

The suite covers codec fuzzing, fair/priority locks, port arbitration, hung /
crashing / garbage-returning drivers, circuit breaker, limits and ramp rates,
composites, N-D sweeps with vector/image data, abort/pause, scripts (including
an un-cooperative infinite loop), crash recovery of truncated/corrupted files,
disk failure during a sweep, a frozen client, a SIGKILLed frontend process, a
backend restart under a live GUI, and the GUI panels themselves.

The stress test runs a backend process with the faulty setup while six clients
hammer it with reads, out-of-limit writes, configuration-channel writes, every
instrument action (including ones that fail, hang forever or raise `SystemExit`),
sweeps (some with actions in their hooks), scripts, pause/abort, malformed requests
and abrupt disconnects, plus a frozen client and a client sending random bytes. At
the end it checks that the backend is alive and responsive, every protected value is
within its limits, a normal sweep still completes, every data file loads, and it
reports the backend's memory over time (it levels off; a client that stops reading is
disconnected instead of being buffered for without bound).

## Several clients at once

Any number of frontends and scripts can be connected to one backend at the same
time (several GUIs on different screens, a notebook, the tray icon). Every client
sees the same live state: monitor values, sweep data, queue and log are pushed to all
of them. Requests are executed concurrently, but each instrument serialises its own
calls, and the run queue executes one item at a time. There is no locking between
users: two GUIs can both edit the queue or set channels, the last action wins.

Clients choose which events are pushed to them. The GUI subscribes to everything.
Scripts and notebooks (`BackendClient`) get only status events by default (queue
changes, sweep start/end, warnings) and can ask for more with
`client.subscribe(["monitor", "sweep_point"])` or `"*"`; the tray uses the tiny
`sweep_progress` event. Events nobody subscribed to are not even encoded.

Idle connections are never closed. A client is only disconnected when the backend
has data waiting for it and its socket has accepted nothing for 60 s, or more than
64 MB are waiting (a frozen GUI); live data for a slow client is dropped above 16 MB
and the client is told to resynchronise. GUIs reconnect by themselves.

## Using it from Python

```python
from easymeasure.client import BackendClient
c = BackendClient.connect()
c.call("write_channel", name="Vg", value=0.2)
c.call("queue_add", item={"kind": "sweep", "definition": sweep}, start=True)
```
See `examples/client_example.py`. The backend can also be used in-process:
`from easymeasure.backend.service import Backend`.

## Layout

```
easymeasure/
  backend/   resources, instrument, channels, sweep, runqueue, storage, monitor,
             scripting, service, server, logsys, setup_io, drivers/
  frontend/  connection, model, main_window, panels/, plotting/
  client/    blocking client for scripts
  common/    codec, safe expressions, N-D slicing, errors
  report/    figure rendering, PowerPoint
examples/    setups, sweeps, driver template, client example, stress test
tests/
```

## License

MIT
