"""Continuing aborted sweeps: a new data file starts with the old data (the old
file stays as it is), goes on from the first point not measured, and lists every
abort; after any number of aborts the data are the data of an uninterrupted run."""

import hashlib
import struct
import time
from pathlib import Path

import numpy as np
import pytest

from easymeasure.backend.storage import EVENT, FOOTER, MAGIC, POINT, DataSet, iter_frames
from easymeasure.backend.sweep import run_order
from easymeasure.common.errors import EasyMeasureError


def definition(**dim0):
    """2-D, inner snake, an outer record after the inner line, a composite swept."""
    d0 = dict({"points": 6, "sweep": [{"channel": "gates.eps", "start": -0.1, "stop": 0.1}],
               "record": ["gates.eps", "Vx", "@time"], "snake": True, "pre_delay": 0.02},
              **dim0)
    return {"name": "cont", "dimensions": [
        d0,
        {"points": 4, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.3}],
         "record": ["Vg", "@elapsed"], "record_after_inner": True},
    ], "pre": {"record": ["Vg"]}, "post": {"constants": [{"channel": "Vg", "value": 0}]}}


def setup(be):
    be.define_composite(type="multi", name="gates", variables=["eps"],
                        targets={"Vx": "0.2 + eps", "Vy": "0.2 - eps"})


def abort_after(be, n):
    """Abort the running sweep when it has *n* points."""
    fired = []

    def cb(kind, data):
        if kind == "sweep_point" and not fired and data["progress"]["done"] >= n:
            fired.append(1)
            be.queue.abort("stopped by the test at %d" % n)
    be.add_listener(cb)
    return cb


def last(be):
    assert be.queue.wait_idle(60)
    return be.queue_state()["history"][-1]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_continued_twice_equals_an_uninterrupted_run(demo):
    be = demo
    setup(be)
    d = definition()
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    ref = DataSet.load(last(be)["path"])
    assert ref.status == "finished"
    # the same sweep, aborted twice (in the middle of an inner line, both times)
    abort_after(be, 7)
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    first = last(be)
    assert first["status"] == "aborted"
    sums = {first["path"]: digest(first["path"])}
    info = be.continue_info(first["path"])
    assert info["continuable"] and 7 <= info["points"] < 24 and info["next_index"]
    i0, i1 = info["next_index"]
    assert info["next_setpoints"]["Vg"] == pytest.approx(0.1 * i1)
    abort_after(be, 16)
    starts = []
    be.add_listener(lambda kind, data: kind == "sweep_start" and starts.append(
        (data, be.run_snapshot())))
    be.continue_sweep(first["path"], wait=0.3, start=True)
    second = last(be)
    # it starts where the aborted run stopped: the progress, and the data a viewer loads
    ev, snap = starts[0]
    assert ev["progress"]["done"] == info["points"] and ev["progress"]["total"] == 24
    assert ev["progress"]["eta"] is None and ev["continues"] == first["run"]
    assert snap["running"] and snap["points"] == info["points"]
    assert DataSet.from_snapshot(snap).filled[0].sum() == info["points"]
    assert second["status"] == "aborted" and second["path"] != first["path"]
    sums[second["path"]] = digest(second["path"])
    be.continue_sweep(second["path"], wait=0.0, rerun_pre=False, start=True)
    third = last(be)
    assert third["status"] == "finished", third
    # the files of the aborted runs are untouched
    for p, h in sums.items():
        assert digest(p) == h
    ds = DataSet.load(third["path"])
    assert ds.status == "finished" and ds.points == ref.points == 24
    # the data are those of the uninterrupted run (times aside)
    for name in ("gates.eps", "Vx", "Vg"):
        np.testing.assert_allclose(ds.data[name], ref.data[name], err_msg=name)
    assert ds.pre == ref.pre
    # visited in the right order (snake directions kept across the continuations)
    t = [ds.data["@time"][idx] for idx in run_order(ds.shape, [True, False])]
    assert all(b > a for a, b in zip(t, t[1:]))
    # no point measured twice: one POINT frame per recording event
    frames = [o for f, o in iter_frames(third["path"]) if f == POINT]
    assert len([o for o in frames if o["k"] == 0]) == 24
    assert len([o for o in frames if o["k"] == 1]) == 4
    # every abort is in the header and in the events, in order
    aborts = ds.header["aborts"]
    assert [a["points"] for a in aborts] == [first_points(first), first_points(second)]
    assert [a["file"] for a in aborts] == [first["path"], second["path"]]
    assert all("stopped by the test" in a["error"] for a in aborts)
    assert aborts[0]["wait"] == 0.3 and aborts[1]["rerun_pre"] is False
    ev = [e["event"] for e in ds.events if e["event"] in ("aborted", "continued")]
    assert ev == ["aborted", "continued", "aborted", "continued"]
    assert ds.header["continued_from"] == second["path"]
    assert ds.header["first_run"] == first["run"] and ds.footer["aborts"] == 2
    # the wait: the first point measured again came after it
    cont = [e for e in ds.events if e["event"] == "continued"][0]
    t_after = [ds.data["@time"][idx] for idx in run_order(ds.shape, [True, False])
               if ds.data["@time"][idx] > cont["t"]]
    assert min(t_after) - cont["t"] >= 0.3
    # the queue history says what each item continued
    assert be.queue_state()["history"][-1]["continues_run"] == second["run"]


def first_points(item):
    return DataSet.load(item["path"]).points


def test_what_cannot_be_continued(demo):
    be = demo
    setup(be)
    be.queue_add({"kind": "sweep", "definition": definition()}, start=True)
    done = last(be)
    assert not be.continue_info(done["path"])["continuable"]
    with pytest.raises(EasyMeasureError, match="nothing to continue"):
        be.continue_sweep(done["path"])
    with pytest.raises(EasyMeasureError, match="not a data file"):
        be.continue_sweep("/no/such/file.emj")
    # a run whose channels are gone cannot go on either
    abort_after(be, 3)
    be.queue_add({"kind": "sweep", "definition": definition()}, start=True)
    ab = last(be)
    be.remove_composite(name="gates")
    with pytest.raises(Exception, match="gates.eps"):
        be.continue_sweep(ab["path"])


def test_a_run_cut_off_without_its_end_continues(demo):
    """The backend stopped during a run: the file has no footer (``incomplete``)."""
    be = demo
    setup(be)
    abort_after(be, 9)
    be.queue_add({"kind": "sweep", "definition": definition()}, start=True)
    ab = last(be)
    # cut the footer (and the post-sweep frames after the last point) off a copy
    raw = Path(ab["path"]).read_bytes()
    pos, keep = len(MAGIC), len(MAGIC)
    while pos < len(raw):
        _, ftype, n, _ = struct.unpack_from("<cBII", raw, pos)
        pos += 10 + n
        if ftype == POINT:
            keep = pos
    cut = Path(ab["path"]).with_name("cut.emj")
    cut.write_bytes(raw[:keep])
    info = be.continue_info(str(cut))
    assert info["status"] == "incomplete" and info["continuable"]
    be.continue_sweep(str(cut), start=True)
    fin = last(be)
    ds = DataSet.load(fin["path"])
    assert ds.status == "finished" and ds.points == 24
    assert ds.header["aborts"][0]["status"] == "incomplete"
    assert "backend stopped" in ds.header["aborts"][0]["error"]


def test_continuing_with_skipped_points_and_record_before_inner(demo):
    be = demo
    setup(be)
    d = definition(hooks={"post_sweep": {"enabled": True,
                                         "code": "if index[0] == 2: skip_point()"}})
    d["dimensions"][1]["record_after_inner"] = False
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    ref = DataSet.load(last(be)["path"])
    abort_after(be, 10)
    be.queue_add({"kind": "sweep", "definition": d}, start=True)
    ab = last(be)
    be.continue_sweep(ab["path"], start=True)
    ds = DataSet.load(last(be)["path"])
    assert ds.status == "finished"
    for name in ("gates.eps", "Vx", "Vg"):
        np.testing.assert_allclose(ds.data[name], ref.data[name], err_msg=name)
    assert ds.skipped == ref.skipped
    frames = [o for f, o in iter_frames(ds.header["file"]) if f == POINT and o["k"] == 1]
    assert len(frames) == 4                         # the outer records: once each
