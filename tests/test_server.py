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
    with client_for(server) as c:
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
