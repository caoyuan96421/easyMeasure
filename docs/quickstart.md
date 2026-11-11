# Quick start

Five minutes, no hardware: the demo setup uses simulated instruments. These include a
four-channel source and a multimeter measuring a simulated quantum dot, a lock-in,
a magnet, a spectrometer, a camera, a waveform generator and a temperature controller.

## 1. Start the backend

```bash
python -m easymeasure.backend --setup examples/demo_setup.json
```

The backend is the process that owns the instruments and runs measurements. It
keeps running when you close the GUI. A **tray icon** shows its state:

| colour | state |
|---|---|
| green | idle |
| blue | running |
| orange | paused |
| red | error |
| gray | unreachable |

The tray menu opens the GUI or the web page, pauses, resumes or aborts the run, opens the data
folder or the log, and shuts the backend down.

!!! tip "One command"
    `python -m easymeasure.frontend --start-backend` starts a detached backend
    (restoring the last session's setup) if none is running, then opens the GUI.

## 2. Open the GUI

```bash
python -m easymeasure.frontend
```

Start it as often as you like, from any terminal. Every GUI shows the same live
state. The first time, it asks for the **data folder** (every run is saved there)
and a **PowerPoint file** that a summary slide is appended to after each run
(leave it empty for none).

![GUI layout](screenshots/layout.png)

* **Instruments & channels**: the instruments of the demo setup, their channels, and the
  *aliases* (`Vg`, `Vb`, `I`, `T` …) that carry limits and ramp rates.
* **Sweep editor**: where measurements are defined.
* **Queue**: what runs next, what ran.
* **Live data** (right): the running measurement.
* **Monitor**, **Manual** and **Log** docks.

## 3. Run a measurement

1. *Sweep editor → Load…* → `examples/sweep_dot_map.json`.

    This 2-D sweep steps the gate voltage `Vg` over 101 points inside a 41-point
    sweep of the bias `Vb`, recording the current `I` and the lock-in `X`.

2. *Validate* computes and limit-checks all 4141 setpoints. *Add & start* queues the sweep
   and starts it.
3. Watch **Live data**: the current map fills in as a heat map. Choose
   *curve* to see the latest line, move the slider to look at earlier lines, and drag
   the colour bar to set the levels.

![Sweep editor](screenshots/sweep_editor.png)

Close the GUI in the middle of the sweep and open it again. The sweep kept
running and the live view resynchronises.

## 4. Look at the data

When the run ends, the data is in `<data folder>/<date>/00001_coulomb_diamonds.emj`,
with an HDF5 copy next to it. Open it again in the GUI with *File → Open data file…*,
or in Python:

```python
from easymeasure.backend.storage import DataSet
ds = DataSet.load("00001_coulomb_diamonds.emj")
ds.data["I"].shape          # (101, 41): dimension 0 (Vg) is the innermost
ds.setpoints[0]["Vg"]       # the 101 gate voltages
ds.status                   # 'finished'
```

## 5. The same from Python

With the backend from step 1 still running:

```python
import json, time
from easymeasure.client import BackendClient

c = BackendClient.connect()
print(c.call("read_channel", name="Vg"))
c.call("write_channel", name="Vg", value=0.2)        # ramps at Vg's ramp rate (2 V/s)

sweep = json.load(open("examples/sweep_dot_map.json"))
sweep["dimensions"][0]["points"] = 41                 # a quicker version
sweep["dimensions"][1]["points"] = 11
print(c.call("validate_sweep", definition=sweep)["shape"])   # [41, 11]
c.call("queue_add", item={"kind": "sweep", "definition": sweep}, start=True)

while c.call("queue_state")["state"] != "idle":
    time.sleep(0.5)
print(c.call("queue_state")["history"][-1]["path"])
```

`examples/client_example.py` is this script.

## 6. See the robustness machinery at work

```bash
python -m easymeasure.backend --setup examples/faulty_setup.json
```

then run `examples/sweep_faulty.json`. The instruments of this setup drop
commands, garble replies, hang, disconnect and raise random exceptions, and a
deliberately pathological `chaos` instrument returns wrong types and never answers.
The sweep completes anyway, and the log shows what was retried, reconnected and
quarantined.

## Next

* [Tutorial](tutorial/index.md): every part of easyMeasure, with examples.
* [Instruments](tutorial/instruments.md): connect your own hardware.
* [Supported instruments](instruments.md).
