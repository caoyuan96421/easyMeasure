# Python clients

Everything the GUI does goes through [backend commands](../reference/commands.md),
so a script or a notebook can do the same. There are three ways in:

| client | for | connects |
|---|---|---|
| `BackendClient` | scripts and notebooks on the backend computer | localhost socket, token from `~/.easymeasure/backend.json` |
| `RemoteClient` | scripts on other computers | HTTPS / WSS through the [remote gateway](../guide/remote.md) |
| `Backend` (in-process) | tests, tools, embedding | none: the backend runs in your process |

## `BackendClient`

```python
from easymeasure.client import BackendClient

c = BackendClient.connect(name="my notebook")      # finds the running backend
c.call("read_channel", name="Vg")
c.call("write_channel", name="Vg", value=0.2)      # ramps; call() waits for the end
c.call("queue_add", item={"kind": "sweep", "definition": sweep}, start=True)
c.close()
```

`call(cmd, **args)` blocks until the backend answers. The default timeout is 120 s;
pass `_timeout=` for long ramps. A ramp of `write_channel` / `set_many` can be stopped
from any client (or the **Stop ramps** button of the Manual panel) with
`c.call("stop_ramps")`; the write then returns the value reached. `ramp_state` lists
the ramps running now, and the event `ramps` announces every change. A refused command raises
`easymeasure.common.errors.RemoteError` with the backend's message:

```python
from easymeasure.common.errors import RemoteError
try:
    c.call("write_channel", name="Vg", value=5)
except RemoteError as e:
    print(e)            # Vg: value 5 above maximum 2
```

Any number of clients can be connected at once, together with any number of GUIs.
Every client sees the same live state. Requests run concurrently, but each instrument
serialises its own calls and the queue runs one item at a time. There is no locking
between users: the last action wins.

### Starting a backend from a script

```python
from easymeasure.client import BackendClient, start_backend_detached, wait_for_backend

if wait_for_backend(timeout=1) is None:
    start_backend_detached(["--setup", "examples/demo_setup.json"])
    wait_for_backend(timeout=30)
c = BackendClient.connect()
```

### Events

The backend pushes events. By default a client gets only the small status events:

| event | data |
|---|---|
| `queue` | the queue state (same as `queue_state`) |
| `sweep_start`, `sweep_end` | run number, path; header and progress (start) or status, error, footer (end). A [continued run](../guide/queue.md#continuing-an-aborted-sweep) starts with progress `done` > 0 and `continues` (the run it continues); `run_snapshot` gives its data so far |
| `sweep_paused`, `sweep_resumed`, `sweep_warning` | run state, warning text |
| `item_end` | a queue item finished (sweep or script) |
| `user_action` | a user action started / ended |
| `ppt` | a slide was written (or went to the pending file) |
| `instrument_fault` | a safety fault (magnet quench): instrument, message, whether a run was aborted |
| `changed`, `settings` | the setup or settings changed |

Ask for more with `subscribe`, or for everything with `"*"`:

| event | data |
|---|---|
| `monitor` | monitored channel values |
| `sweep_point`, `sweep_phase`, `sweep_progress` | live sweep data, progress |
| `log` | log records |

```python
c.on_event = lambda kind, data: print(kind, data)
c.subscribe(["monitor", "sweep_progress"])
```

The handler runs on the client's reader thread. Keep it short, and don't call
`c.call` from inside it. Events nobody subscribed to are not even encoded by the
backend.

### Waiting for a sweep

```python
import threading
done = threading.Event()
result = {}
def on_event(kind, data):
    if kind == "sweep_end":
        result.update(data)
        done.set()
c.on_event = on_event
c.call("queue_add", item={"kind": "sweep", "definition": sweep}, start=True)
done.wait()
print(result["status"], result["path"])     # also result["error"], result["footer"]
```

### In a notebook

```python
from easymeasure.client import BackendClient
from easymeasure.backend.storage import DataSet
import matplotlib.pyplot as plt

c = BackendClient.connect(name="notebook")
c.call("queue_add", item={"kind": "sweep", "definition": sweep}, start=True)
# … later, also while it runs:
snap = c.call("run_snapshot")
path = c.call("current_run")["path"]
ds = DataSet.load(path)                 # partial while running
plt.pcolormesh(ds.data["I"].T); plt.show()
```

An idle client is never disconnected. `examples/notebook_long_connection.ipynb`
shows a connection kept for days.

## `RemoteClient`

The same interface, through the [remote gateway](../guide/remote.md) from another computer:

```python
from easymeasure.client import RemoteClient

c = RemoteClient("https://lab-pc.example.org:8443", "alice", password,
                 fingerprint="87:68:0C:…")       # python -m easymeasure.remote cert
c.role, len(c.allowed)                           # 'operator', the permitted commands
c.call("read_channel", name="Vg")
c.subscribe(["monitor"])
c.on_event = lambda kind, data: print(kind, data)
c.close()
```

* **`fingerprint`** pins the gateway's self-signed certificate.
* **`cafile`** names a CA bundle.
* With a real certificate (e.g. Let's Encrypt), neither is needed.
* A command your role does not allow raises `RemoteError` ("permission denied: …").

## In-process `Backend`

```python
from easymeasure.backend.service import Backend

be = Backend(home="C:/emtest", log_to_console=False)
be.load_setup(path="examples/demo_setup.json")
be.write_channel(name="Vg", value=0.1)
be.queue_add(item={"kind": "sweep", "definition": sweep}, start=True)
be.queue.wait_idle(600)
be.close()
```

The commands are methods with the same names and arguments. This is what the
tests use. Only one backend should own a given set of instruments at a time.
