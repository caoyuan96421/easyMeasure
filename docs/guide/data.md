# Data files

Every sweep is written to `<data folder>/<date>/<run>_<name>.emj` while it runs,
for example `D:/data/2026-11-11/00042_coulomb_diamonds.emj`. When it ends, an HDF5 copy
(`.h5`) is written next to it.

The data folder is chosen when easyMeasure starts for the first time, and can be
changed under *File → Settings…*.

## Opening data in the GUI

* While a sweep runs, the **Live data** tab shows it, and it stays there when the
  sweep has ended.
* **Open data** in the queue history, or **File → Open data file…**, opens an earlier
  run in a new data tab, with the same plots as the live view
  ([Plotting](plotting.md)). Partial files from an aborted or crashed run open too.
* **File → Browse recent runs…** lists the runs in the data folder.
* **Details** in a data tab shows the sweep definition, the metadata and the
  pre/post-sweep readings.
* **Sweep → Load from data file…** loads the sweep that made a file, to
  run it again or change it.

## The journal (`.emj`)

The journal is an append-only sequence of frames, each with a length and a CRC32
checksum:

* **header**: the full sweep definition, every setpoint, units, metadata;
* **point**: the values recorded at one point;
* **phase**: pre/post recordings;
* **event**: status changes, notes;
* **footer**: final status, points, skipped points, read failures.

Every frame is flushed immediately and `fsync`-ed every `fsync_interval` (0.5 s,
see [settings](../reference/settings.md)). After a crash or a power cut, the file
loads up to the last fraction of a second:

* a torn or corrupted tail is detected and ignored;
* the rest loads as a partial dataset, with points that were not measured as NaN.

If the disk fails during a run, the sweep keeps running in memory and the error is
reported.

## Loading data in Python

```python
from easymeasure.backend.storage import DataSet

ds = DataSet.load("D:/data/2026-11-11/00001_coulomb_diamonds.emj")   # complete or partial
ds.status                    # 'finished', 'aborted', 'error', 'terminated'; partial file: header status
ds.shape                     # [101, 41]: points per dimension, 0 = innermost
ds.data["I"].shape           # (101, 41)
ds.data["T"].shape           # (41,): recorded in dimension 1
ds.dim_of                    # {'I': 0, 'X': 0, 'T': 1, '@elapsed': 1}
ds.setpoints[0]["Vg"]        # the 101 gate voltages
ds.setpoints[1]["Vb"]        # the 41 bias voltages
ds.pre, ds.post              # {'T': 1.501, 'B': 0.0}: pre/post recordings
ds.header["definition"]      # the sweep definition, ready to queue again
ds.header["units"]           # channel → unit
ds.header["parameters"]      # the setup's parameters at the start: {name: {value, unit, formula}}
ds.footer                    # {'status', 'points', 'skipped_points', 'read_failures', …}
ds.completed_points, ds.total_points
```

A channel recorded in dimension *k* has shape `(N_k, …, N_{D-1}) + value_shape`.
A spectrum of 512 points recorded in dimension 0 of a 101 × 41 sweep is a
`(101, 41, 512)` array.

### Plotting with matplotlib

```python
import matplotlib.pyplot as plt
ds = DataSet.load(path)
Vg, Vb = ds.setpoints[0]["Vg"], ds.setpoints[1]["Vb"]
plt.pcolormesh(Vg, Vb, ds.data["I"].T, shading="nearest")
plt.xlabel("Vg (V)"); plt.ylabel("Vb (V)"); plt.colorbar(label="I (nA)")
plt.show()
```

### Partial and running files

`DataSet.load` works on a file that is still being written. You get what has been
measured so far, with NaN elsewhere:

```python
import numpy as np
ds = DataSet.load(path)
done = ~np.isnan(ds.data["I"])
print("%d of %d points" % (done.sum(), done.size))
```

Through the backend (the same data, also for remote users):

??? example "From Python"

    ```python
    snap = c.call("run_snapshot")                 # the running (or last) sweep
    snap = c.call("load_run", path=path)          # any stored run
    hdr = c.call("read_run_header", path=path)    # just the header: definition, metadata
    runs = c.call("list_runs", limit=20)          # newest files: path, name, size, mtime
    ```

## HDF5

The `.h5` copy has the same content in a conventional layout:

| path | content |
|---|---|
| `/data/<channel>` | recorded arrays (attribute `dim`: the dimension it was recorded in) |
| `/setpoints/<k>/<channel>` | setpoints of dimension *k* |
| `/filled/<k>` | boolean mask of the points measured in dimension *k* |
| `/pre`, `/post` | pre/post recordings (attributes or datasets) |
| attributes `header_json`, `footer_json`, `status` | everything else |

```python
import h5py, json
with h5py.File("00001_coulomb_diamonds.h5") as f:
    I = f["data/I"][...]
    Vg = f["setpoints/0/Vg"][...]
    definition = json.loads(f.attrs["header_json"])["definition"]
```

Switch the copy off with the setting `export_hdf5 = false`.

## Data folder

The data folder is chosen when the GUI first meets a backend, and can be changed
in *File → Settings*. From Python:

??? example "From Python"

    ```python
    c.call("set_settings", data_dir="D:/data")    # refused if it cannot be created
    ```

Runs are numbered across days (`00001`, `00002` …). Remote users only reach files
inside the data folder.

## In the GUI

* *File → Open data file…*, or *Open data* in the queue history, opens a run in a data tab
  with the same plots as the live view (see [Plotting](plotting.md)).
* The web app lists stored runs, opens them and downloads the
  `.emj` / `.h5` files.
* *Sweep → Load from data file…* loads a run's definition.
