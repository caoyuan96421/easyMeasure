"""IPC server tests, including misbehaving clients."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

from easymeasure.backend.server import Server
from easymeasure.client import BackendClient
from easymeasure.common import codec
from easymeasure.common.errors import RemoteError

from conftest import EXAMPLES


@pytest.fixture
def server(demo, tmp_path):
    srv = Server(demo, port=0, runtime_file=tmp_path / "rt.json")
    srv.start()
    yield srv
    srv.stop()


def client_for(srv, name="test"):
    return BackendClient("127.0.0.1", srv.port, srv.token, name=name)


def long_sweep(points=300, delay=0.005):
    return {"name": "long", "dimensions": [
        {"points": points, "sweep": [{"channel": "Vg", "start": 0, "stop": 1}],
         "record": ["I", "image"], "pre_delay": delay}]}


def test_basic_commands_and_events(server):
    events = []
    with BackendClient("127.0.0.1", server.port, server.token, events="*") as c:
        c.on_event = lambda k, d: events.append(k)
        assert "pong" in c.call("ping")
        assert c.call("read_channel", name="Vg") == 0.0
        c.call("write_channel", name="Vg", value=0.1)
        assert c.call("read_channel", name="Vg") == pytest.approx(0.1)
        assert isinstance(c.call("read_channel", name="image"), np.ndarray)
        with pytest.raises(RemoteError) as ei:
            c.call("write_channel", name="Vg", value=99)
        assert ei.value.kind == "LimitError"
        with pytest.raises(RemoteError):
            c.call("no_such_command")
        with pytest.raises(RemoteError):
            c.call("read_channel", wrong_arg=1)
        st = c.call("get_state")
        assert {i["name"] for i in st["instruments"]} >= {"src", "dmm"}
        c.call("queue_add", item={"kind": "sweep", "definition": long_sweep(5, 0)}, start=True)
        t = time.time()
        while "sweep_end" not in events and time.time() - t < 20:
            time.sleep(0.05)
    assert "sweep_point" in events and "sweep_end" in events and "queue" in events


def test_bad_token_and_garbage(server):
    with pytest.raises(Exception):
        BackendClient("127.0.0.1", server.port, "wrong-token")
    for junk in (b"\xff" * 100, b"\x10\x00\x00\x00" + b"x" * 16, b"GET / HTTP/1.1\r\n\r\n"):
        s = socket.create_connection(("127.0.0.1", server.port))
        s.sendall(junk)
        time.sleep(0.1)
        s.close()
    # unauthenticated request is rejected
    s = socket.create_connection(("127.0.0.1", server.port))
    s.sendall(codec.encode({"type": "req", "id": 1, "cmd": "shutdown", "args": {}}))
    time.sleep(0.2)
    s.close()
    with client_for(server) as c:
        assert "pong" in c.call("ping")


def test_frontend_crash_and_reconnect_during_sweep(server, demo):
    c = client_for(server)
    c.call("queue_add", item={"kind": "sweep", "definition": long_sweep(200, 0.005)}, start=True)
    time.sleep(0.3)
    # abrupt death of the frontend (no bye, socket torn down)
    c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    c.sock.close()
    time.sleep(0.3)
    c2 = client_for(server, "reopened")
    cur = c2.call("current_run")
    assert cur is not None and cur["status"] == "running"
    snap = c2.call("run_snapshot")
    assert snap["running"] and snap["points"] > 0
    assert demo.queue.wait_idle(60)
    assert c2.call("queue_state")["history"][-1]["status"] == "finished"
    c2.close()


def test_frozen_frontend_does_not_block_backend(server, demo, monkeypatch):
    import easymeasure.backend.server as srvmod
    monkeypatch.setattr(srvmod, "MAX_QUEUE_BYTES", 2_000_000)
    # authenticate, then never read again ("frozen GUI")
    s = socket.create_connection(("127.0.0.1", server.port))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    s.sendall(codec.encode({"type": "hello", "token": server.token, "client": "frozen"}))
    time.sleep(0.2)
    t0 = time.time()
    with client_for(server, "healthy") as c:
        c.call("queue_add", item={"kind": "sweep", "definition": long_sweep(150, 0)}, start=True)
        assert demo.queue.wait_idle(60)
        # the healthy client is still served promptly
        t1 = time.time()
        c.call("ping")
        assert time.time() - t1 < 1.0
    assert demo.queue_state()["history"][-1]["status"] == "finished"
    s.close()


def test_backend_process_survives_killed_frontend_process(tmp_path):
    home = tmp_path / "proc_home"
    env = dict(os.environ, EASYMEASURE_HOME=str(home))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = root
    be = subprocess.Popen([sys.executable, "-m", "easymeasure.backend", "--port", "0", "--quiet",
                           "--setup", str(EXAMPLES / "demo_setup.json")], env=env, cwd=root)
    try:
        rt = home / "backend.json"
        for _ in range(150):
            if rt.exists():
                break
            time.sleep(0.1)
        info = json.loads(rt.read_text())
        # a "frontend" process that starts a sweep and is then SIGKILLed
        code = ("import sys, time; sys.path.insert(0, %r)\n"
                "from easymeasure.client import BackendClient\n"
                "c = BackendClient('127.0.0.1', %d, %r)\n"
                "c.call('queue_add', item={'kind':'sweep','definition':%r}, start=True)\n"
                "print('started', flush=True)\n"
                "time.sleep(60)\n" % (root, info["port"], info["token"], long_sweep(150, 0.005)))
        fe = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE)
        assert fe.stdout.readline().strip() == b"started"
        time.sleep(0.3)
        fe.send_signal(signal.SIGKILL)
        fe.wait(5)
        assert be.poll() is None, "backend died with the frontend"
        c = BackendClient("127.0.0.1", info["port"], info["token"])
        for _ in range(300):
            st = c.call("queue_state")
            if st["state"] == "idle" and st["history"]:
                break
            time.sleep(0.1)
        assert st["history"][-1]["status"] == "finished"
        c.call("shutdown")
        c.close()
        be.wait(10)
    finally:
        if be.poll() is None:
            be.kill()


def test_review_unauthenticated_cannot_exhaust_memory(server):
    import struct
    s = socket.create_connection(("127.0.0.1", server.port))
    s.sendall(struct.pack("<I", (1 << 30) - 1))      # claims a 1 GB frame
    closed = False
    try:
        for _ in range(64):
            s.sendall(b"\0" * (1 << 20))
    except OSError:
        closed = True                                # server dropped us early
    s.close()
    assert closed
    with client_for(server) as c:
        assert "pong" in c.call("ping")


def test_review_control_commands_not_starved_by_hung_instrument(server, demo):
    demo.add_instrument("chaos", "sim_chaos", options={"call_timeout": 1.0, "retries": 0,
                                                       "breaker_threshold": 1000})
    with client_for(server) as c:
        for i in range(40):          # far more than the worker pool
            c._send({"type": "req", "id": 10_000 + i, "cmd": "read_channel",
                     "args": {"name": "chaos.hang"}})
        time.sleep(0.3)
        t = time.time()
        c.call("ping")
        c.call("queue_state")
        c.call("abort")
        assert time.time() - t < 1.0


def test_stop_leaves_no_threads(demo, tmp_path):
    before = {t.ident for t in threading.enumerate()}
    for i in range(3):
        srv = Server(demo, port=0, runtime_file=tmp_path / ("rt%d.json" % i))
        srv.start()
        with client_for(srv) as c:
            c.call("ping")
        srv.stop()
    time.sleep(0.5)
    left = [t.name for t in threading.enumerate() if t.ident not in before
            and t.name in ("accept",) or t.name.startswith("client-")]
    assert left == [], left


def test_frozen_client_is_disconnected_and_memory_bounded(server, demo, monkeypatch):
    """A client that stops reading must not make the backend buffer without bound."""
    import easymeasure.backend.server as srvmod
    monkeypatch.setattr(srvmod, "MAX_QUEUE_BYTES", 200_000)
    monkeypatch.setattr(srvmod, "HOPELESS_BYTES", 1_000_000)
    monkeypatch.setattr(srvmod, "STALL_TIMEOUT", 1.0)
    s = socket.create_connection(("127.0.0.1", server.port))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    s.sendall(codec.encode({"type": "hello", "token": server.token, "client": "frozen"}))
    time.sleep(0.3)
    assert any(c["name"] == "frozen" for c in server.clients())
    with client_for(server, "healthy") as c:
        c.call("queue_add", item={"kind": "sweep", "definition": long_sweep(200, 0.005)},
               start=True)
        peak = 0
        while demo.queue.state != "idle" or not demo.queue_state()["history"]:
            with server._clients_lock:
                peak = max([peak] + [cl._qbytes for cl in server._clients.values()
                                     if cl.name == "frozen"])
            time.sleep(0.05)
        assert c.call("ping")                          # healthy client unaffected
    assert peak < 2_000_000                            # backlog stays bounded
    # a client whose socket write has been blocked longer than STALL_TIMEOUT is dropped
    with server._clients_lock:
        frozen = [cl for cl in server._clients.values() if cl.name == "frozen"]
    if frozen:
        frozen[0]._send_started = time.monotonic() - 5.0
        frozen[0].send({"type": "event", "event": "queue", "data": {}})
        time.sleep(0.2)
    assert not any(x["name"] == "frozen" for x in server.clients())
    s.close()


def test_event_subscriptions(server, demo):
    """Scripts get status events only by default; '*' gets everything; the
    subscription can be changed at runtime."""
    got = {"default": [], "all": []}
    demo.monitor_add("Vg", 0.1)
    c1 = client_for(server, "script")
    c2 = BackendClient("127.0.0.1", server.port, server.token, name="gui-like", events="*")
    c1.on_event = lambda k, d: got["default"].append(k)
    c2.on_event = lambda k, d: got["all"].append(k)
    try:
        c1.call("queue_add", item={"kind": "sweep", "definition": long_sweep(10, 0)}, start=True)
        assert demo.queue.wait_idle(30)
        time.sleep(0.5)
        d, a = set(got["default"]), set(got["all"])
        assert {"queue", "sweep_start", "sweep_end"} <= d
        assert not d & {"sweep_point", "sweep_progress", "monitor", "log"}
        assert {"sweep_point", "sweep_progress", "monitor", "log"} <= a
        c1.subscribe(["monitor"])
        time.sleep(0.5)
        got["default"].clear()
        time.sleep(0.6)
        assert set(got["default"]) == {"monitor"}
    finally:
        c1.close()
        c2.close()
        demo.monitor_remove("Vg")


def test_idle_non_reading_client_is_never_dropped(server, demo, monkeypatch):
    """A notebook/script connection that just sits there (never reads) is kept,
    even with sweeps running and monitors active: it is only sent small status
    events, so its socket never blocks."""
    import easymeasure.backend.server as srvmod
    monkeypatch.setattr(srvmod, "STALL_TIMEOUT", 0.5)
    demo.monitor_add("spectrum", 0.05)
    s = socket.create_connection(("127.0.0.1", server.port))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    s.sendall(codec.encode({"type": "hello", "token": server.token, "client": "idle-notebook"}))
    try:
        with client_for(server, "driver") as c:
            for _ in range(2):
                c.call("queue_add", item={"kind": "sweep", "definition": long_sweep(60, 0.005)},
                       start=True)
                assert demo.queue.wait_idle(60)
            time.sleep(1.5)
        assert any(x["name"] == "idle-notebook" for x in server.clients())
    finally:
        s.close()
        demo.monitor_remove("spectrum")


def test_stop_ramps_from_another_client(server, demo):
    """A slow ramp started by one client is listed (ramp_state, event "ramps") and
    stopped by another; the write returns where the channel got to.  stop_ramps
    is a control command: served while the write still blocks a worker."""
    demo.define_alias(name="Vg", target="src.V1", min=-2, max=2, ramp_rate=0.2, unit="V",
                      replace=True)
    demo.write_channel(name="Vg", value=0.0, ramp=False)
    events = []
    c1, c2 = client_for(server, "gui-1"), client_for(server, "gui-2")
    c2.on_event = lambda k, d: events.append(d) if k == "ramps" else None
    res = {}
    try:
        th = threading.Thread(target=lambda: res.update(v=c1.call("write_channel", name="Vg",
                                                                   value=2.0, _timeout=60)))
        th.start()
        t0 = time.time()
        while not c2.call("ramp_state")["active"] and time.time() - t0 < 5:
            time.sleep(0.05)
        act = c2.call("ramp_state")["active"]
        assert [r["channels"] for r in act] == [["Vg"]] and act[0]["targets"] == {"Vg": 2.0}
        time.sleep(0.5)
        assert c2.call("stop_ramps") == ["Vg"]
        th.join(5)
        assert not th.is_alive() and 0.05 < res["v"] < 0.5
        assert demo.read_channel(name="Vg") == pytest.approx(res["v"], abs=1e-9)
        assert c2.call("ramp_state")["active"] == []
        t0 = time.time()
        while len(events) < 2 and time.time() - t0 < 5:
            time.sleep(0.05)
        assert events[0]["active"][0]["channels"] == ["Vg"] and events[-1]["active"] == []
        assert c2.call("stop_ramps") == []
    finally:
        c1.close()
        c2.close()
