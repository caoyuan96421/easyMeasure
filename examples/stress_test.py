"""Long-running robustness / soak test against simulated faulty hardware.

    python examples/stress_test.py --minutes 10

Starts a backend *process* with ``faulty_setup.json``, then for the given time:

* keeps the run queue full of sweeps and scripts (some of which abort/fail),
* runs several clients that hammer the backend with reads, writes (including
  out-of-limit writes), queue edits and malformed requests,
* runs every instrument action (including ones that fail, hang or crash), writes
  configuration channels and makes rapid ramped writes like the Manual panel,
* runs sweeps whose pre/post hooks call actions,
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


ACTIONS = ["dmm.reset", "dmm.trigger", "lockin.auto_phase", "lockin.auto_gain", "spec.preset",
           "cam.snap", "awg.start", "awg.stop", "awg.trigger", "magnet.hold", "src.clear_status",
           "chaos.ok", "chaos.fail", "chaos.stall", "chaos.explode", "dmm.nope"]
CONFIG_WRITES = [("dmm.nplc", lambda: random.choice([0.1, 1, 10])),
                 ("lockin.sensitivity", lambda: random.choice(["1e-4", "1e-3", "1e-2", "bad"])),
                 ("spec.points", lambda: random.choice([51, 201, 401, 0])),
                 ("cam.exposure", lambda: random.choice([0.001, 0.01, -1])),
                 ("src.output", lambda: random.choice(["ON", "OFF"])),
                 ("chaos.text", lambda: random.choice(["a", "b"]))]


def small_sweep(i):
    hooks = random.random() < 0.4
    return {"name": "stress_%d" % i,
            "pre": {"hook": "set('dmm.reset')\nset('awg.start')" if hooks else "",
                    "record": ["dmm.nplc", "lockin.sensitivity"] if hooks else []},
            "post": {"hook": "set('awg.stop')" if hooks else "",
                     "constants": [{"channel": "Vg", "value": 0.0}]},
            "dimensions": [
        {"points": random.randint(5, 25), "sweep": [{"channel": "Vg", "start": -0.3, "stop": 0.3}],
         "record": random.sample(["I", "X", "XY", "spectrum", "image", "chaos_garbage",
                                  "chaos_vector", "chaos.text"], 3)},
        {"points": random.randint(1, 4), "sweep": [{"channel": "Vb", "start": -0.1, "stop": 0.1}],
         "record": ["T"]}],
        "on_read_error": "nan", "on_set_error": "skip"}


SCRIPTS = [
    "set('Vg', 0.1)\nsleep(0.2)\nlog('I=%s' % get('I'))",
    "for i in range(20):\n    set('dot.detuning', 0.01*i)\n    sleep(0.01)",
    "raise RuntimeError('deliberate script failure')",
    "x = 0\nwhile True:\n    x += 1",          # needs an abort
    "set('Vg', 99)",                           # limit violation
    "set('lockin.auto_gain')\nset('dmm.nplc', 2)\nlog(get('lockin.sensitivity'))",
    "set('chaos.stall')",                    # hanging action inside a script
]


class _Attached:
    """Stands in for the Popen of a backend that this script did not start."""

    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        try:
            os.kill(self.pid, 0)
            return None
        except OSError:
            return 1

    def wait(self, timeout=None):
        return None

    def kill(self):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=2.0)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--attach", default=None, metavar="HOME",
                    help="hammer an already running backend (its state directory, which "
                         "holds backend.json) instead of starting one; it is left running")
    args = ap.parse_args()
    random.seed(args.seed)
    if args.attach:
        home = Path(args.attach)
        be = _Attached(json.loads((home / "backend.json").read_text())["pid"])
        print("attached to backend pid %d in %s" % (be.pid, home))
    else:
        home = Path(tempfile.mkdtemp(prefix="em_stress_"))
        env = dict(os.environ, EASYMEASURE_HOME=str(home), PYTHONPATH=str(ROOT))
        print("state dir:", home)
        be = subprocess.Popen([sys.executable, "-m", "easymeasure.backend", "--port", "0",
                               "--quiet", "--force", "--setup", str(EX / "faulty_setup.json")],
                              env=env, cwd=str(ROOT))
    rt = home / "backend.json"
    for _ in range(200):
        if rt.exists():
            break
        time.sleep(0.1)
    info = json.loads(rt.read_text())
    port, token = info["port"], info["token"]
    stop = threading.Event()
    stats = {"calls": 0, "remote_errors": 0, "client_kills": 0, "timeouts": 0, "actions": 0,
             "action_errors": 0}
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
                    if op < 0.08:
                        try:
                            c.call("write_channel", _timeout=60, name=random.choice(ACTIONS))
                            bump("actions")
                        except TimeoutError:
                            raise
                        except Exception:
                            bump("action_errors")
                            raise
                    elif op < 0.13:
                        ref, val = random.choice(CONFIG_WRITES)
                        c.call("write_channel", name=ref, value=val())
                    elif op < 0.18:
                        # Manual panel style: a quick burst of ramped writes
                        for _ in range(random.randint(2, 6)):
                            c.call("write_channel", _timeout=60, name="Vy",
                                   value=random.uniform(-0.8, 0.8))
                    elif op < 0.35:
                        c.call("read_channel", _timeout=60,
                               name=random.choice(["I", "X", "T", "Vg", "spectrum", "chaos_crash",
                                                   "chaos_garbage", "dot.detuning", "nonexistent"]))
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
        # a frozen GUI: subscribed to everything, never reads
        s.sendall(codec.encode({"type": "hello", "token": token, "client": "frozen",
                                "events": "*"}))
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
    def rss_mb():
        try:
            for line in open("/proc/%d/status" % be.pid):
                if line.startswith("VmRSS"):
                    return int(line.split()[1]) / 1024.0
        except OSError:
            return None
    t_end = time.time() + args.minutes * 60
    mem = []
    while time.time() < t_end:
        time.sleep(10)
        alive = be.poll() is None
        m = rss_mb()
        if m is not None:
            mem.append(m)
        print("[%5.0fs] backend alive=%s rss=%s MB %s" % (
            args.minutes * 60 - (t_end - time.time()), alive,
            "%.0f" % m if m is not None else "?", stats), flush=True)
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
        # every protected value is still inside its limits
        for a in c.call("list_channels")["aliases"]:
            if a.get("dtype") == "str" or a.get("shape") or not a.get("readable"):
                continue
            if a.get("min") is None and a.get("max") is None:
                continue
            try:
                v = float(c.call("read_channel", _timeout=30, name=a["name"]))
            except Exception:
                continue
            lo = a["min"] if a["min"] is not None else -1e300
            hi = a["max"] if a["max"] is not None else 1e300
            if not lo - 1e-9 <= v <= hi + 1e-9:
                print("LIMIT VIOLATION: %s = %g outside [%g, %g]" % (a["name"], v, lo, hi))
                ok = False
        # the backend still runs a normal sweep to completion
        c.call("queue_add", item={"kind": "sweep", "definition": {
            "name": "final_check", "on_read_error": "nan", "dimensions": [
                {"points": 20, "sweep": [{"channel": "Vg", "start": -0.2, "stop": 0.2}],
                 "record": ["I", "spectrum"]}],
            "pre": {"hook": "set('dmm.reset')"}}}, start=True)
        for _ in range(600):
            st = c.call("queue_state")
            if st["state"] == "idle" and st["history"][-1]["name"] == "final_check":
                break
            time.sleep(0.2)
        final = c.call("queue_state")["history"][-1]
        print("final sweep after stress:", final["status"], final.get("error", ""))
        if final["status"] != "finished":
            ok = False
        if not args.attach:
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
    if mem:
        print("backend memory: start %.0f MB, max %.0f MB, end %.0f MB" % (mem[0], max(mem), mem[-1]))
    print("RESULT:", "PASS" if ok and bad == 0 else "FAIL")
    return 0 if ok and bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
