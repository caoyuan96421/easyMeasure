"""Long-lived notebook connection check.

Runs ``examples/notebook_long_connection.ipynb`` in a real Jupyter kernel against a
backend with the fault-injecting instruments.  While the notebook sits idle (default
11 minutes), another client (``stress_test.py --attach``) hammers the same backend.
When that client is done, the notebook verifies that it is still on the very same
connection and then takes control of the hardware (ramped sets, config channels,
actions, a 2-D sweep).

    pip install nbclient nbformat ipykernel
    python examples/long_connection_check.py          # ~13 minutes

Environment: EM_MIN_IDLE_S (notebook idle seconds, default 660), EM_OTHER_MIN (minutes
of load from the other client, default 10.5).
"""
import json, os, shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import nbformat  # noqa: E402
from nbclient import NotebookClient  # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="em_longconn_"))
HOME = WORK / "home"
shutil.rmtree(HOME, ignore_errors=True); HOME.mkdir()
DONE = WORK / "other_client_done"
DONE.unlink(missing_ok=True)
env = dict(os.environ, EASYMEASURE_HOME=str(HOME), PYTHONPATH=str(ROOT),
           EM_MIN_IDLE_S=os.environ.get("EM_MIN_IDLE_S", "660"), EM_DONE_FILE=str(DONE))
os.environ.update(env)          # the notebook kernel inherits this environment

def stamp(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)

be = subprocess.Popen([sys.executable, "-m", "easymeasure.backend", "--port", "0", "--quiet",
                       "--no-tray", "--setup", str(ROOT / "examples/faulty_setup.json")], env=env,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(300):
    if (HOME / "backend.json").exists(): break
    time.sleep(0.1)
stamp("backend pid %d up" % be.pid)

nb = nbformat.read(ROOT / "examples/notebook_long_connection.ipynb", as_version=4)
client = NotebookClient(nb, timeout=3600, kernel_name="python3", resources={"metadata": {"path": str(ROOT)}})
nb_result = {}
def run_nb():
    try:
        client.execute()
        nb_result["ok"] = True
    except Exception as exc:
        nb_result["ok"] = False
        nb_result["error"] = str(exc)[-3000:]
t_nb = threading.Thread(target=run_nb); t_nb.start()
time.sleep(25)
stamp("notebook started; starting the other client (stress test, attached)")
other = subprocess.run([sys.executable, "-u", str(ROOT / "examples/stress_test.py"), "--attach", str(HOME),
                        "--minutes", os.environ.get("EM_OTHER_MIN", "10.5"), "--seed", "21"],
                       env=env, capture_output=True, text=True)
tail = [l for l in other.stdout.splitlines() if l.startswith(("[", "RESULT", "final", "queue item",
                                                             "data files", "backend memory", "ping", "LIMIT"))]
stamp("other client finished (exit %d)" % other.returncode)
print("\n".join(tail[:2] + ["..."] + tail[-9:]))
DONE.write_text("done")
stamp("other client released the hardware; waiting for the notebook")
t_nb.join()
stamp("notebook finished: %s" % ("OK" if nb_result.get("ok") else "FAILED"))
for cell in nb.cells:
    if cell.cell_type != "code": continue
    for out in cell.get("outputs", []):
        txt = out.get("text") or "\n".join(out.get("traceback", [])) or ""
        if txt.strip(): print("  │ " + txt.strip().replace("\n", "\n  │ "))
if not nb_result.get("ok"):
    print(nb_result.get("error"))
nbformat.write(nb, WORK / "executed.ipynb")
log = (HOME / "logs" / "backend.log").read_text()
nb_lines = [l for l in log.splitlines() if "(notebook)" in l]
print("backend log lines about the notebook client:\n  " + "\n  ".join(nb_lines))
from easymeasure.client import BackendClient
c = BackendClient.connect(Path(HOME / "backend.json")); c.call("shutdown", force=True); c.close()
be.wait(20)
print("RESULT:", "PASS" if nb_result.get("ok") and other.returncode == 0 else "FAIL")
