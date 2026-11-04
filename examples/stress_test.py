"""Long-running robustness / soak test against simulated faulty hardware.

    python examples/stress_test.py --minutes 10

Starts a backend *process* with ``faulty_setup.json``, then for the given time:

* keeps the run queue full of sweeps and scripts (some of which abort/fail),
* runs several clients that hammer the backend with reads, writes (including
  out-of-limit writes), queue edits and malformed requests,
* periodically kills clients abruptly and "freezes" one client (never reads),
* randomly pauses / resumes / aborts.

At the end it checks that the backend process is still alive and responsive,
that every finished run's data file loads, and prints statistics.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from easymeasure.backend.storage import DataSet  # noqa: E402
from easymeasure.client import BackendClient  # noqa: E402
from easymeasure.common import codec  # noqa: E402

EX = ROOT / "examples"


def small_sweep(i):
    return {"name": "stress_%d" % i, "dimensions": [
        {"points": random.randint(5, 25), "sweep": [{"channel": "Vg", "start": -0.3, "stop": 0.3}],
         "record": random.sample(["I", "X", "XY", "spectrum", "image", "chaos_garbage",
                                  "chaos_vector", "chaos_text"], 3)},
        {"points": random.randint(1, 4), "sweep": [{"channel": "Vb", "start": -0.1, "stop": 0.1}],
         "record": ["T"]}],
        "on_read_error": "nan", "on_set_error": "skip",
        "post": {"constants": [{"channel": "Vg", "value": 0.0}]}}


SCRIPTS = [
    "set('Vg', 0.1)\nsleep(0.2)\nlog('I=%s' % get('I'))",
    "for i in range(20):\n    set('detuning', 0.01*i)\n    sleep(0.01)",
    "raise RuntimeError('deliberate script failure')",
    "x = 0\nwhile True:\n    x += 1",          # needs an abort
    "set('Vg', 99)",                           # limit violation
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=2.0)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    random.seed(args.seed)
    home = Path(tempfile.mkdtemp(prefix="em_stress_"))
    env = dict(os.environ, EASYMEASURE_HOME=str(home), PYTHONPATH=str(ROOT))
    print("state dir:", home)
    be = subprocess.Popen([sys.executable, "-m", "easymeasure.backend", "--port", "0", "--quiet",
                           "--force", "--setup", str(EX / "faulty_setup.json")], env=env, cwd=str(ROOT))
    rt = home / "backend.json"
    for _ in range(200):
        if rt.exists():
            break
        time.sleep(0.1)
    info = json.loads(rt.read_text())
    port, token = info["port"], info["token"]
    stop = threading.Event()
    stats = {"calls": 0, "remote_errors": 0, "client_kills": 0, "timeouts": 0}
    lock = threading.Lock()

    def bump(k):
        with lock:
            stats[k] += 1

    def worker(wid):
        n = 0
        while not stop.is_set():
            try:
                c = BackendClient("127.0.0.1", port, token, name="stress-%d" % wid)
            except Exception:
                time.sleep(0.5)
                continue
            life = time.time() + random.uniform(5, 30)
            while time.time() < life and not stop.is_set():
                n += 1
                op = random.random()
                try:
                    if op < 0.35:
                        c.call("read_channel", _timeout=60,
                               name=random.choice(["I", "X", "T", "Vg", "spectrum", "chaos_crash",
                                                   "chaos_garbage", "detuning", "nonexistent"]))
                    elif op < 0.45:
                        c.call("write_channel", name="Vx", value=random.uniform(-1.5, 1.5))
                    elif op < 0.55:
                        c.call("queue_add", item={"kind": "sweep", "definition": small_sweep(n)},
                               start=True)
                    elif op < 0.62:
                        c.call("queue_add", item={"kind": "script", "code": random.choice(SCRIPTS)},
                               start=True)
                    elif op < 0.70:
                        c.call(random.choice(["pause", "resume", "abort", "resume", "queue_start"]))
                    elif op < 0.75:
                        c.call("get_state")
                    elif op < 0.80:
                        c.call("run_snapshot")
                    elif op < 0.83:
                        c.call(random.choice(["bogus_cmd", "read_channel"]), junk=1)
                    else:
                        c.call("ping")
                    bump("calls")
                except TimeoutError:
                    bump("timeouts")
                except Exception:
                    bump("remote_errors")
                time.sleep(random.uniform(0, 0.05))
            if random.random() < 0.5:
                c.sock.close()        # abrupt death
                bump("client_kills")
            else:
                c.close()

    def frozen_client():
        s = socket.create_connection(("127.0.0.1", port))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        s.sendall(codec.encode({"type": "hello", "token": token, "client": "frozen"}))
        while not stop.is_set():
            time.sleep(0.5)        # never reads
        s.close()

    def garbage_client():
        while not stop.is_set():
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=2)
                s.sendall(os.urandom(random.randint(1, 5000)))
                s.close()
            except OSError:
                pass
            time.sleep(1)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(6)]
    threads += [threading.Thread(target=frozen_client, daemon=True),
                threading.Thread(target=garbage_client, daemon=True)]
    for t in threads:
        t.start()
    t_end = time.time() + args.minutes * 60
    while time.time() < t_end:
        time.sleep(5)
        alive = be.poll() is None
        print("[%5.0fs] backend alive=%s %s" % (args.minutes * 60 - (t_end - time.time()), alive, stats),
              flush=True)
        if not alive:
            break
    stop.set()
    ok = be.poll() is None
    try:
        c = BackendClient("127.0.0.1", port, token, name="checker")
        c.call("abort_all")
        c.call("queue_clear")
        t0 = time.time()
        c.call("ping")
        print("ping after stress: %.3f s" % (time.time() - t0))
        for _ in range(600):
            if c.call("queue_state")["state"] == "idle":
                break
            time.sleep(0.1)
        hist = c.call("queue_state")["history"]
        insts = c.call("list_instruments")
        c.call("shutdown", force=True)
        c.close()
    except Exception as exc:
        print("backend NOT responsive:", exc)
        ok = False
        hist, insts = [], []
    from collections import Counter
    print("queue item results:", Counter(h["status"] for h in hist))
    loaded = bad = 0
    for p in (home / "data").glob("*/*.emj"):
        try:
            DataSet.load(p)
            loaded += 1
        except Exception as exc:
            bad += 1
            print("cannot load", p, exc)
    print("data files loaded: %d, unreadable: %d" % (loaded, bad))
    for i in insts:
        print("  %-8s %-10s %s" % (i["name"], i["state"], i["stats"]))
    try:
        be.wait(15)
    except subprocess.TimeoutExpired:
        be.kill()
    print("RESULT:", "PASS" if ok and bad == 0 else "FAIL")
    return 0 if ok and bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
