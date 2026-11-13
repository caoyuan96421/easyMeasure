"""Soak test of remote access under attack, with faulty hardware.

    python examples/remote_stress_test.py --minutes 10 [--browser]

Starts a backend process (``faulty_setup.json``) with the internet gateway, then
runs at the same time, for the given time:

* ``stress_test.py --attach``: local clients hammering the faulty hardware
  (sweeps, scripts, actions, aborts, abrupt client kills, a frozen client);
* legitimate remote clients over TLS (operator, viewer, admin): reads, ramped
  writes, actions, sweeps, snapshots, subscriptions; reconnecting with their
  session token when the gateway drops them;
* attackers: password guessing (locks the address out), random garbage and
  random-but-well-formed messages, forbidden commands, path traversal, sandbox
  escapes in sweep mappings, oversized messages, request floods, slow-loris
  sockets, connections dropped mid-handshake, a client that never reads;
* optionally (``--browser``) the web app in Chromium, left open on the live view.

Every 20 s it samples gateway/backend memory and the latency seen by a probe
client.  At the end it checks: gateway and backend processes never died, the
probe never failed, legitimate clients saw no protocol errors, no forbidden
command ever succeeded, memory stayed bounded, a fresh remote sweep completes
and the local stress test passes.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import random
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from easymeasure.client.remote import RemoteClient  # noqa: E402
from easymeasure.common import codec  # noqa: E402
from easymeasure.common.errors import RemoteError  # noqa: E402

EX = ROOT / "examples"
USERS = {"ada": ("admin", "admin-password-1"), "olga": ("operator", "operator-password-1"),
         "vic": ("viewer", "viewer-password-1")}
STOP = threading.Event()
STATS = {"remote_calls": 0, "remote_errors": 0, "unexpected": [], "reconnects": 0,
         "fuzz_msgs": 0, "fuzz_conns": 0, "forbidden_ok": [], "guesses": 0, "lockouts": 0,
         "rate_limited": 0, "denied": 0}
LOCK = threading.Lock()


def rss_mb(pid: int) -> float:
    try:
        with open("/proc/%d/status" % pid) as f:
            for line in f:
                if line.startswith("VmRSS"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    try:
        import psutil
        return psutil.Process(pid).memory_info().rss / 2 ** 20
    except Exception:
        return float("nan")


def enc(msg):
    return codec.encode(msg)[4:]      # WebSocket messages: frames without length prefix


dec = codec.decode_body


def insecure():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def note(key, n=1):
    with LOCK:
        STATS[key] += n


def unexpected(msg):
    with LOCK:
        if len(STATS["unexpected"]) < 200:
            STATS["unexpected"].append(msg)


# ----------------------------------------------------------------- legit ----
def legit_client(url, fp, token, user, seed):
    rnd = random.Random(seed)
    role = USERS[user][0]
    c = None
    while not STOP.is_set():
        try:
            if c is None or c.closed:
                if c is not None:
                    note("reconnects")
                c = RemoteClient(url, token=token, fingerprint=fp, events="*" if rnd.random() < 0.5 else None)
            r = rnd.random()
            note("remote_calls")
            try:
                if r < 0.30:
                    c.call("read_channel", name=rnd.choice(["Vg", "I", "T", "X", "spectrum", "image", "dmm.nplc"]))
                elif r < 0.45 and role != "viewer":
                    c.call("write_channel", name="Vg", value=round(rnd.uniform(-0.3, 0.3), 3), ramp=True)
                elif r < 0.52 and role != "viewer":
                    c.call("write_channel", name=rnd.choice(["dmm.reset", "lockin.auto_gain", "chaos.fail", "awg.start"]))
                elif r < 0.58 and role != "viewer":
                    c.call("queue_add", item={"kind": "sweep", "definition": {
                        "name": "remote_%s" % user, "dimensions": [
                            {"points": rnd.randint(3, 15), "sweep": [{"channel": "Vg", "start": -0.2, "stop": 0.2}],
                             "record": ["I", "spectrum"], "pre_delay": 0.01}]}}, start=True)
                elif r < 0.66:
                    c.call("run_snapshot", _timeout=60)
                elif r < 0.74:
                    c.call("get_state")
                elif r < 0.80:
                    runs = c.call("list_runs", limit=5)
                    if runs:
                        c.call("read_run_header", path=runs[0]["path"])
                elif r < 0.85:
                    c.call("monitor_history")
                elif r < 0.90:
                    c.subscribe(rnd.choice(["*", ["queue"], ["monitor", "sweep_point"]]))
                else:
                    c.call("ping")
            except RemoteError as exc:
                note("remote_errors")
                if exc.kind in ("ProtocolError",):
                    unexpected("%s: protocol error: %s" % (user, exc))
                if exc.kind == "RateLimitError":
                    note("rate_limited")
            except TimeoutError as exc:
                unexpected("%s: timeout: %s" % (user, exc))
            except ConnectionError:
                pass                       # dropped (gateway policy / restart): reconnect
            time.sleep(rnd.uniform(0.0, 0.08))
        except Exception as exc:
            if not STOP.is_set():
                note("reconnects")
                time.sleep(0.5)
            c = None
    if c is not None:
        c.close()


# ----------------------------------------------------------------- attackers -
def raw_ws(port, token=None, rcvbuf=None, timeout=10):
    s = socket.socket()
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    s.settimeout(timeout)
    s.connect(("127.0.0.1", port))
    ss = insecure().wrap_socket(s, server_hostname="localhost")
    key = base64.b64encode(os.urandom(16)).decode()
    hdr = ["GET /ws HTTP/1.1", "Host: 127.0.0.1:%d" % port, "Upgrade: websocket",
           "Connection: Upgrade", "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13"]
    if token:
        hdr.append("Authorization: Bearer " + token)
    ss.sendall(("\r\n".join(hdr) + "\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = ss.recv(4096)
        if not d:
            break
        buf += d
    return ss, buf


def ws_frame(payload: bytes, opcode: int = 2) -> bytes:
    mask = os.urandom(4)
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    import numpy as np
    m = np.frombuffer((mask * (n // 4 + 1))[:n], dtype=np.uint8)
    return head + mask + (np.frombuffer(payload, dtype=np.uint8) ^ m).tobytes()


FORBIDDEN = [  # (user, cmd, args) that must never succeed
    ("olga", "run_script_now", {"code": "open('PWNED', 'w')"}),
    ("olga", "queue_add", {"item": {"kind": "script", "code": "open('PWNED','w')"}}),
    ("olga", "queue_add", {"item": {"kind": "sweep", "definition": {"name": "x", "pre": {"hook": "open('PWNED','w')"},
                                                                     "dimensions": [{"points": 2}]}}}),
    ("olga", "raw_write", {"instrument": "src", "command": "*RST"}),
    ("olga", "define_alias", {"name": "Vg", "target": "src.V1", "max": 1000, "replace": True}),
    ("olga", "set_settings", {"data_dir": "/"}),
    ("olga", "load_run", {"path": "/etc/passwd"}),
    ("olga", "load_run", {"path": "../../../etc/passwd"}),
    ("olga", "save_setup", {"path": "PWNED.json"}),
    ("olga", "make_ppt", {"ppt_path": "PWNED.pptx"}),
    ("olga", "shutdown", {"force": True}),
    ("vic", "write_channel", {"name": "Vg", "value": 0.5}),
    ("vic", "abort_all", {}),
    ("ada", "start_tray", {}),
    ("ada", "remote_stop", {}),
    ("ada", "remote_set_user", {"name": "mallory", "password": "mallory-password", "role": "admin"}),
    ("ada", "set_settings", {"remote_access": False}),
]
EVIL_MAPPINGS = ["__import__('os').system('touch PWNED')", "np.save('PWNED', 1)",
                 "().__class__.__base__.__subclasses__()", "9**9**9", "np.zeros(10**10)",
                 "'a'*10**10", "open('PWNED','w')", "np.lib.npyio.savetxt('PWNED', [1])"]


def forbidden_prober(url, fp, tokens):
    clients = {u: RemoteClient(url, token=tokens[u], fingerprint=fp) for u in tokens}
    rnd = random.Random(7)
    while not STOP.is_set():
        user, cmd, args = rnd.choice(FORBIDDEN)
        try:
            if clients[user].closed:
                clients[user] = RemoteClient(url, token=tokens[user], fingerprint=fp)
            clients[user].call(cmd, **args)
            with LOCK:
                STATS["forbidden_ok"].append("%s %s" % (user, cmd))
        except RemoteError as exc:
            if exc.kind == "PermissionError":
                note("denied")
        except Exception:
            pass
        try:   # sandbox escapes through sweep mappings (operators may send those)
            clients["olga"].call("validate_sweep", definition={"dimensions": [{"points": 3, "sweep": [
                {"channel": "Vg", "start": 0, "stop": 0.1, "mapping": rnd.choice(EVIL_MAPPINGS)}]}]})
            with LOCK:
                STATS["forbidden_ok"].append("evil mapping accepted")
        except RemoteError:
            note("denied")
        except Exception:
            pass
        time.sleep(0.05)
    for c in clients.values():
        c.close()


def random_obj(rnd, depth=0):
    r = rnd.random()
    if depth > 3 or r < 0.3:
        return rnd.choice([0, -1, 2 ** 63, 1e308, float("nan"), "", "x" * rnd.randint(0, 3000),
                           None, True, "../../etc/passwd", "\u0000￿", [1, 2]])
    if r < 0.6:
        return [random_obj(rnd, depth + 1) for _ in range(rnd.randint(0, 5))]
    return {rnd.choice(["name", "value", "path", "item", "definition", "__nd__", "x", "ref"]):
            random_obj(rnd, depth + 1) for _ in range(rnd.randint(0, 5))}


CMDS = ["ping", "read_channel", "write_channel", "set_many", "queue_add", "load_run", "make_ppt",
        "set_settings", "run_script_now", "define_alias", "monitor_add", "validate_sweep",
        "remote_stop", "list_runs", "get_state", "channel_info", "nonsense", ""]


def fuzzer(port, token, seed):
    rnd = random.Random(seed)
    while not STOP.is_set():
        try:
            s, _ = raw_ws(port, token=token, timeout=3)
            note("fuzz_conns")
            for _ in range(rnd.randint(1, 30)):
                kind = rnd.random()
                if kind < 0.45:
                    msg = {"type": "req", "id": rnd.randint(1, 2 ** 40), "cmd": rnd.choice(CMDS),
                           "args": random_obj(rnd)}
                    if rnd.random() < 0.2:
                        msg["id"] = random_obj(rnd)
                    data = enc(msg)
                    if rnd.random() < 0.1:              # corrupt a byte
                        b = bytearray(data)
                        b[rnd.randrange(len(b))] ^= 0xFF
                        data = bytes(b)
                    frame = ws_frame(data)
                elif kind < 0.6:
                    frame = ws_frame(os.urandom(rnd.randint(1, 5000)))
                elif kind < 0.7:
                    frame = ws_frame(enc({"type": rnd.choice(["hello", "subscribe", "bye", "x"]),
                                                   "events": random_obj(rnd), "token": "steal"}))
                elif kind < 0.78:
                    frame = ws_frame(b"x" * rnd.choice([100, 9 * 2 ** 20]))     # oversized
                elif kind < 0.85:
                    frame = os.urandom(rnd.randint(1, 3000))                    # not WebSocket
                elif kind < 0.92:
                    frame = ws_frame(b"", opcode=rnd.choice([1, 8, 9, 10, 3, 15]))
                else:
                    frame = ws_frame(enc({"type": "req", "id": 1, "cmd": "ping"}))[:-3]
                s.sendall(frame)
                note("fuzz_msgs")
            if rnd.random() < 0.5:
                s.settimeout(0.5)
                try:
                    while s.recv(65536):
                        pass
                except Exception:
                    pass
            s.close()
        except Exception:
            pass
        time.sleep(rnd.uniform(0, 0.2))


def guesser(url):
    """Password guessing from this address: gets locked out, again and again."""
    async def go():
        import aiohttp
        async with aiohttp.ClientSession() as s:
            while not STOP.is_set():
                user = random.choice(["ada", "olga", "admin", "root"])
                try:
                    async with s.post(url + "/api/login", ssl=insecure(), timeout=aiohttp.ClientTimeout(total=10),
                                      json={"user": user, "password": "guess%d" % random.randint(0, 10**9)}) as r:
                        note("guesses")
                        if r.status == 429:
                            note("lockouts")
                            await asyncio.sleep(1.0)
                        elif r.status == 200:
                            unexpected("a guessed password was accepted")
                except Exception:
                    await asyncio.sleep(0.5)
    asyncio.run(go())


def slowloris(port):
    while not STOP.is_set():
        socks = []
        for i in range(20):
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=5)
                if i % 3 == 1:
                    s = insecure().wrap_socket(s, server_hostname="localhost")
                    s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")
                elif i % 3 == 2:
                    s.sendall(b"\x16\x03\x01\x00")          # start of a TLS hello, then nothing
                socks.append(s)
            except Exception:
                pass
        STOP.wait(20)
        for s in socks:
            try:
                s.close()
            except Exception:
                pass


def staller(port, token):
    """Subscribes to everything and never reads; reconnects after being dropped."""
    while not STOP.is_set():
        try:
            s, _ = raw_ws(port, token=token, rcvbuf=4096, timeout=300)
            s.sendall(ws_frame(enc({"type": "subscribe", "events": "*"})))
            t0 = time.time()
            while not STOP.is_set() and time.time() - t0 < 200:
                time.sleep(1)
                try:                       # dropped by the gateway?
                    s.settimeout(0.001)
                    s.send(b"")
                except Exception:
                    break
            s.close()
        except Exception:
            STOP.wait(2)


def flooder(url, fp, token):
    while not STOP.is_set():
        try:
            c = RemoteClient(url, token=token, fingerprint=fp)
            ths = [threading.Thread(target=lambda: [_safe(c.call, "ping") for _ in range(300)]) for _ in range(8)]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            c.close()
        except Exception:
            pass
        STOP.wait(3)


def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except RemoteError as exc:
        if exc.kind == "RateLimitError":
            note("rate_limited")
    except Exception:
        pass


def browser_session(url, user, pw, out):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        out["browser"] = "playwright not installed"
        return
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_context(ignore_https_errors=True, viewport={"width": 1500, "height": 950}).new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(url + "/")
        page.wait_for_selector("#login-user", state="visible")
        page.fill("#login-user", user)
        page.fill("#login-password", pw)
        page.click("#login-button")
        page.wait_for_selector(".conn.ok", timeout=20000)
        page.click(".tab:has-text('Queue')")
        while not STOP.is_set():
            STOP.wait(5)
            page.click(".tab:has-text('Live data')")
        time.sleep(3)
        out["browser_connected"] = page.locator(".conn.ok").count() == 1
        out["browser_title"] = page.text_content(".dv-title")
        out["browser_errors"] = errs
        page.screenshot(path=str(Path(tempfile.gettempdir()) / "em_remote_stress_browser.png"))
        b.close()


# ----------------------------------------------------------------- roles ----
# Every group of clients runs in its own process (like separate remote machines), so
# a busy attacker never slows down the measurement of the probe.
def run_role(a) -> int:
    tokens = json.loads(a.tokens)

    def watch_stop():
        while not os.path.exists(a.stop_file):
            time.sleep(0.3)
        STOP.set()
    threading.Thread(target=watch_stop, daemon=True).start()
    out = {}
    threads = []
    if a.role == "probe":
        from easymeasure.client import BackendClient
        info = json.loads((Path(a.home) / "backend.json").read_text())
        local = BackendClient("127.0.0.1", info["port"], info["token"], name="local-probe")
        probe = RemoteClient(a.url, token=tokens["vic"], fingerprint=a.fp)
        with open(a.out + ".jsonl", "a") as f:
            while not STOP.is_set():
                rec = {"t": time.time(), "remote": [], "local": [], "fail": []}
                for _ in range(3):
                    t = time.perf_counter()
                    try:
                        if probe.closed:
                            probe = RemoteClient(a.url, token=tokens["vic"], fingerprint=a.fp)
                        probe.call("ping", _timeout=10)
                        rec["remote"].append((time.perf_counter() - t) * 1e3)
                    except Exception as exc:
                        rec["fail"].append(str(exc)[:120])
                    t = time.perf_counter()
                    try:
                        local.call("ping", _timeout=10)
                        rec["local"].append((time.perf_counter() - t) * 1e3)
                    except Exception as exc:
                        rec["fail"].append("local: " + str(exc)[:100])
                f.write(json.dumps(rec) + "\n")
                f.flush()
                STOP.wait(1.0)
        probe.close()
        local.close()
    elif a.role == "legit":
        for i, u in enumerate(["olga", "olga", "vic", "ada", "vic", "olga"]):
            threads.append(threading.Thread(target=legit_client, args=(a.url, a.fp, tokens[u], u, i), daemon=True))
    elif a.role == "forbidden":
        threads.append(threading.Thread(target=forbidden_prober, args=(a.url, a.fp, tokens), daemon=True))
    elif a.role == "fuzz":
        for i in range(4):
            threads.append(threading.Thread(target=fuzzer, args=(a.port, tokens["olga" if i % 2 else "vic"], 100 + i), daemon=True))
    elif a.role == "misc":
        threads.append(threading.Thread(target=guesser, args=(a.url,), daemon=True))
        threads.append(threading.Thread(target=slowloris, args=(a.port,), daemon=True))
        threads.append(threading.Thread(target=staller, args=(a.port, tokens["vic"]), daemon=True))
        threads.append(threading.Thread(target=flooder, args=(a.url, a.fp, tokens["olga"]), daemon=True))
    elif a.role == "browser":
        threads.append(threading.Thread(target=browser_session, args=(a.url, "vic", USERS["vic"][1], out), daemon=True))
    for t in threads:
        t.start()
    STOP.wait()
    for t in threads:
        t.join(60)
    with LOCK:
        out.update(STATS)
    Path(a.out).write_text(json.dumps(out, default=str))
    os._exit(0)


# ----------------------------------------------------------------------- main --
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=3.0)
    ap.add_argument("--browser", action="store_true", help="also keep the web app open in Chromium")
    ap.add_argument("--keep", action="store_true", help="keep the temporary directory (logs)")
    for k in ("--role", "--url", "--fp", "--tokens", "--stop-file", "--out", "--home"):
        ap.add_argument(k, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.role:
        return run_role(args)
    home = Path(tempfile.mkdtemp(prefix="em_remote_stress_"))
    env = dict(os.environ, EASYMEASURE_HOME=str(home), PYTHONPATH=str(ROOT))
    for u, (role, pw) in USERS.items():
        subprocess.run([sys.executable, "-m", "easymeasure.remote", "adduser", u, "--role", role,
                        "--password-stdin"], input=pw + "\n", text=True, env=env, check=True,
                       stdout=subprocess.DEVNULL)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    subprocess.run([sys.executable, "-m", "easymeasure.remote", "config", "host=\"127.0.0.1\"",
                    "port=%d" % port], env=env, check=True, stdout=subprocess.DEVNULL)
    print("home %s, gateway port %d, %d CPU cores" % (home, port, os.cpu_count() or 0))
    be = subprocess.Popen([sys.executable, "-m", "easymeasure.backend", "--setup", str(EX / "faulty_setup.json"),
                           "--quiet", "--no-tray", "--port", "0", "--remote"], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = "https://127.0.0.1:%d" % port
    st = None
    for _ in range(120):
        time.sleep(0.5)
        try:
            st = json.loads((home / "remote" / "gateway.json").read_text())
            if st.get("port") and time.time() - st.get("t", 0) < 10:
                break
        except Exception:
            pass
    fp = st["fingerprint"]
    gw_pid = st["pid"]
    print("backend pid %d, gateway pid %d, fingerprint %s…" % (be.pid, gw_pid, fp[:23]))
    tokens = {}
    for u, (role, pw) in USERS.items():                 # log in before the guesser locks us out
        c = RemoteClient(url, u, pw, fingerprint=fp, logout_on_close=False)
        tokens[u] = c._token
        c.close()
    stop_file = home / "STOP"
    roles = ["probe", "legit", "forbidden", "fuzz", "misc"] + (["browser"] if args.browser else [])
    procs = {}
    for r in roles:
        procs[r] = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--role", r, "--url", url, "--fp", fp,
             "--port", str(port), "--tokens", json.dumps(tokens), "--stop-file", str(stop_file),
             "--out", str(home / ("role_%s.json" % r)), "--home", str(home)], env=env)
    t_start = time.time()
    t_end = t_start + args.minutes * 60
    local = subprocess.Popen([sys.executable, str(EX / "stress_test.py"), "--attach", str(home),
                              "--minutes", str(args.minutes)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    probe_file = home / "role_probe.json.jsonl"

    def probe_records():
        try:
            return [json.loads(x) for x in probe_file.read_text().splitlines() if x.strip()]
        except Exception:
            return []
    samples = []
    seen = 0
    while time.time() < t_end:
        time.sleep(min(20, max(0.1, t_end - time.time())))
        recs = probe_records()
        new, seen = recs[seen:], len(recs)
        rl = sorted(x for r in new for x in r["remote"])
        ll = sorted(x for r in new for x in r["local"])
        fails = sum(len(r["fail"]) for r in new)
        try:
            gst = json.loads((home / "remote" / "gateway.json").read_text())
        except Exception:
            gst = {}
        q = lambda a, f: round(a[min(len(a) - 1, int(f * len(a)))], 1) if a else None
        s = {"t": round(time.time() - t_start), "gw_mb": round(rss_mb(gw_pid), 1), "be_mb": round(rss_mb(be.pid), 1),
             "r50": q(rl, .5), "r99": q(rl, .99), "rmax": rl[-1] if rl else None, "l50": q(ll, .5), "lmax": ll[-1] if ll else None,
             "fails": fails, "live": len(gst.get("connections", [])), "gw_pid": gst.get("pid"),
             "load": os.getloadavg()[0] if hasattr(os, "getloadavg") else None}
        samples.append(s)
        print("  t=%4ds gateway %6.1f MB backend %6.1f MB | remote ping p50 %s p99 %s max %.0f ms | backend direct p50 %s max %.0f ms | fails %d live %d load %.1f"
              % (s["t"], s["gw_mb"], s["be_mb"], s["r50"], s["r99"], s["rmax"] or 0, s["l50"], s["lmax"] or 0,
                 fails, s["live"], s["load"] or 0), flush=True)
    stop_file.write_text("stop")
    res = {}
    for r, p in procs.items():
        try:
            p.wait(120)
        except Exception:
            p.kill()
        try:
            res[r] = json.loads((home / ("role_%s.json" % r)).read_text())
        except Exception:
            res[r] = {}
    tot = {k: sum(int(res[r].get(k, 0) or 0) for r in res if isinstance(res[r].get(k, 0), (int, float)))
           for k in ("remote_calls", "remote_errors", "reconnects", "fuzz_conns", "fuzz_msgs", "guesses",
                     "lockouts", "rate_limited", "denied")}
    forbidden_ok = [x for r in res.values() for x in r.get("forbidden_ok", [])]
    unexpected_ = [x for r in res.values() for x in r.get("unexpected", [])]
    local_out = local.communicate(timeout=600)[0]
    local_pass = "RESULT: PASS" in local_out
    ok_final = False
    try:
        c = RemoteClient(url, token=tokens["olga"], fingerprint=fp)
        c.call("abort_all")
        c.call("queue_clear")
        time.sleep(1)
        iid = c.call("queue_add", item={"kind": "sweep", "definition": {"name": "final", "dimensions": [
            {"points": 10, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}], "record": ["I"]}]}}, start=True)
        t0 = time.time()
        while time.time() - t0 < 60:
            h = [x for x in c.call("queue_state")["history"] if x["id"] == iid]
            if h:
                ok_final = h[0]["status"] in ("finished", "error")   # error = injected hardware fault
                print("final remote sweep: %s %s" % (h[0]["status"], h[0].get("error") or ""))
                break
            time.sleep(0.5)
        c.close()
    except Exception as exc:
        print("final sweep failed: %s" % exc)
    gw_alive = (os.path.exists("/proc/%d" % gw_pid) if os.name == "posix" else True)
    be_alive = be.poll() is None
    pwned = list(home.rglob("PWNED*")) + list(Path.cwd().glob("PWNED*"))
    recs = probe_records()
    rl = sorted(x for r in recs for x in r["remote"])
    ll = sorted(x for r in recs for x in r["local"])
    probe_fail = sum(len(r["fail"]) for r in recs)
    pct = lambda a, f: a[min(len(a) - 1, int(f * len(a)))] if a else float("nan")
    mem0 = min(x["gw_mb"] for x in samples[: max(1, len(samples) // 4)])
    mem1 = max(x["gw_mb"] for x in samples[len(samples) // 2:]) if len(samples) > 1 else mem0
    print("\n---- summary ----")
    for k, v in tot.items():
        print("%-14s %s" % (k, v))
    print("forbidden that succeeded: %s" % (forbidden_ok or "none"))
    print("files created by attacks: %s" % (pwned or "none"))
    print("unexpected: %d %s" % (len(unexpected_), unexpected_[:5]))
    print("probe: %d pings, %d failures; remote p50 %.1f ms, p99 %.1f ms, max %.0f ms; direct to backend p50 %.1f ms, p99 %.1f ms, max %.0f ms"
          % (len(rl), probe_fail, pct(rl, .5), pct(rl, .99), rl[-1] if rl else 0, pct(ll, .5), pct(ll, .99), ll[-1] if ll else 0))
    print("gateway pid unchanged: %s, alive: %s; backend alive: %s" % (
        all(x["gw_pid"] in (gw_pid, None) for x in samples), gw_alive, be_alive))
    print("gateway memory: first quarter min %.0f MB, second half max %.0f MB" % (mem0, mem1))
    print("local faulty-hardware stress test: %s" % ("PASS" if local_pass else "FAIL"))
    if args.browser:
        b = res.get("browser", {})
        print("browser: connected=%s title=%r errors=%s" % (b.get("browser_connected"), b.get("browser_title"),
                                                          b.get("browser_errors")))
    print("\n".join("  " + line for line in local_out.splitlines()[-14:]))
    ok = (be_alive and gw_alive and not forbidden_ok and not pwned and probe_fail == 0
          and not unexpected_ and local_pass and ok_final
          and (mem1 != mem1 or mem1 < max(400, 3 * mem0))      # NaN: memory not measurable here
          and (not args.browser or res.get("browser", {}).get("browser_connected")))
    be.terminate()
    try:
        be.wait(30)
    except Exception:
        be.kill()
    print("RESULT:", "PASS" if ok else "FAIL")
    if not args.keep:
        import shutil
        shutil.rmtree(home, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
