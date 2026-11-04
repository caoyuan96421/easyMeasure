"""Control a running backend from a script or notebook.

    python -m easymeasure.backend --setup examples/demo_setup.json   # terminal 1
    python examples/client_example.py                                # terminal 2
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from easymeasure.backend.storage import DataSet  # noqa: E402
from easymeasure.client import BackendClient  # noqa: E402

c = BackendClient.connect(name="example")
print("Vg =", c.call("read_channel", name="Vg"))
c.call("write_channel", name="Vg", value=0.2)          # ramps at the alias' rate

sweep = json.loads((Path(__file__).parent / "sweep_dot_map.json").read_text())
sweep["dimensions"][0]["points"] = 41
sweep["dimensions"][1]["points"] = 11
print(c.call("validate_sweep", definition=sweep)["shape"])
c.on_event = lambda kind, data: kind == "sweep_end" and print("sweep ended:", data["status"])
c.call("queue_add", item={"kind": "sweep", "definition": sweep}, start=True)

while c.call("queue_state")["state"] != "idle":
    time.sleep(0.5)
path = c.call("queue_state")["history"][-1]["path"]
ds = DataSet.load(path)
print("I map:", ds.data["I"].shape, "file:", path)
c.close()
