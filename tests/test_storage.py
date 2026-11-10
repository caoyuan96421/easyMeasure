import os
import zlib

import numpy as np
import pytest

from easymeasure.backend.storage import (EVENT, FOOTER, HEADER, POINT, DataSet, DataStore,
                                         JournalWriter, StorageError, export_hdf5, iter_frames,
                                         read_header)


def make_header():
    return {"shape": [4, 3], "dims": [{"sweep": ["x"], "record": ["a", "img"]},
                                      {"sweep": ["y"], "record": ["s"]}],
            "setpoints": {"0": {"x": np.linspace(0, 1, 4)}, "1": {"y": np.arange(3.0)}},
            "definition": {"name": "t"}, "name": "t", "run": 1}


def write_journal(path, n_points=None, footer=True):
    w = JournalWriter(path, fsync_interval=0.0)
    w.write(HEADER, make_header(), sync=True)
    count = 0
    for j in range(3):
        w.write(POINT, {"k": 1, "i": [j], "v": {"s": "label%d" % j}})
        for i in range(4):
            if n_points is not None and count >= n_points:
                w.close()
                return
            w.write(POINT, {"k": 0, "i": [i, j], "v": {"a": float(i + 10 * j),
                                                      "img": np.full((2, 5), i)}})
            count += 1
    if footer:
        w.write(FOOTER, {"status": "finished"}, sync=True)
    w.close()


def test_full_roundtrip(tmp_path):
    p = tmp_path / "r.emj"
    write_journal(p)
    ds = DataSet.load(p)
    assert ds.status == "finished" and ds.points == 12
    assert ds.data["a"].shape == (4, 3) and ds.data["a"][2, 1] == 12
    assert ds.data["img"].shape == (4, 3, 2, 5)
    assert list(ds.data["s"]) == ["label0", "label1", "label2"]
    assert read_header(p)["name"] == "t"
    with pytest.raises(FileExistsError):
        JournalWriter(p)          # never overwrite data


def test_crash_recovery_truncated_and_corrupt(tmp_path):
    p = tmp_path / "r.emj"
    write_journal(p, n_points=7, footer=False)
    raw = p.read_bytes()
    # simulate power loss in the middle of the last frame
    p.write_bytes(raw[:-13])
    ds = DataSet.load(p)
    assert ds.status == "incomplete"
    assert ds.points == 6
    assert np.isnan(ds.data["a"]).sum() == 6
    # corrupt a byte in the middle: everything before it is kept
    p2 = tmp_path / "c.emj"
    write_journal(p2)
    b = bytearray(p2.read_bytes())
    b[len(b) // 2] ^= 0xFF
    p2.write_bytes(bytes(b))
    ds2 = DataSet.load(p2)
    assert 0 < ds2.points < 12
    # garbage file
    p3 = tmp_path / "g.emj"
    p3.write_bytes(b"hello world")
    with pytest.raises(StorageError):
        DataSet.load(p3)


def test_shape_change_mid_sweep(tmp_path):
    ds = DataSet(make_header())
    ds.apply_point(0, (0, 0), {"img": np.ones((2, 5))})
    ds.apply_point(0, (1, 0), {"img": np.ones((3, 3)) * 2})      # bigger in one axis: grow
    ds.apply_point(0, (2, 0), {"img": 7.0})
    assert ds.data["img"].shape == (4, 3, 3, 5)
    assert (ds.data["img"][0, 0, :2, :5] == 1).all() and np.isnan(ds.data["img"][0, 0, 2]).all()
    assert ds.data["img"][1, 0, 2, 2] == 2 and np.isnan(ds.data["img"][1, 0, 0, 3])
    assert (ds.data["img"][2, 0] == 7).all()
    with pytest.raises(StorageError):
        ds.apply_point(0, (9, 0), {"a": 1.0})


def test_corrupt_middle_frame_resync(tmp_path):
    p = tmp_path / "m.emj"
    write_journal(p)
    raw = bytearray(p.read_bytes())
    # damage a byte inside the 4th frame payload; frames after it must still load
    from easymeasure.backend.storage import MAGIC, _FRAME
    pos = len(MAGIC)
    for _ in range(3):
        n = int.from_bytes(raw[pos + 2:pos + 6], "little")
        pos += _FRAME.size + n
    raw[pos + _FRAME.size + 5] ^= 0xFF
    p.write_bytes(bytes(raw))
    ds = DataSet.load(p)
    assert ds.points == 11 and ds.status == "finished"


def test_transient_write_failure_loses_nothing(tmp_path, monkeypatch):
    p = tmp_path / "t.emj"
    w = JournalWriter(p, fsync_interval=0.0)
    w.write(HEADER, make_header(), sync=True)
    real = w._f
    calls = {"n": 0}

    class Flaky:
        def __getattr__(self, k):
            return getattr(real, k)

        def write(self, b):
            calls["n"] += 1
            if 2 <= calls["n"] <= 4:
                real.write(b[: len(b) // 2])      # torn write, then error
                raise OSError(28, "No space left on device")
            return real.write(b)
    w._f = Flaky()
    errors = 0
    for i in range(4):
        try:
            w.write(POINT, {"k": 0, "i": [i, 0], "v": {"a": float(i)}})
        except StorageError:
            errors += 1
    w.write(FOOTER, {"status": "finished"}, sync=True)
    w._f = real
    w.close()
    assert errors >= 1 and w.pending == 0
    ds = DataSet.load(p)
    assert ds.points == 4 and ds.status == "finished"


def test_run_counter_reset_does_not_collide(tmp_path):
    st = DataStore(tmp_path / "data")
    r1, p1 = st.new_path("x")
    p1.parent.mkdir(parents=True, exist_ok=True)
    p1.write_bytes(b"x")
    (tmp_path / "data" / "run_counter.txt").write_text("")       # zero-length after power loss
    r2, p2 = st.new_path("x")
    assert r2 > r1 and not p2.exists()
    (tmp_path / "data" / "run_counter.txt").write_text("0")      # stale counter
    r3, p3 = st.new_path("x")
    assert r3 > r1 and p3 != p1


def test_snapshot_and_hdf5(tmp_path):
    p = tmp_path / "r.emj"
    write_journal(p)
    ds = DataSet.load(p)
    snap = ds.snapshot()
    from easymeasure.common import codec
    ds2 = DataSet.from_snapshot(codec.decode(codec.encode(snap)))
    np.testing.assert_array_equal(ds2.data["a"], ds.data["a"])
    h5 = export_hdf5(ds, tmp_path / "r.h5")
    ds3 = DataSet.load(h5)
    np.testing.assert_array_equal(ds3.data["a"], ds.data["a"])
    assert list(ds3.data["s"]) == ["label0", "label1", "label2"]
    assert ds3.status == "finished"


def test_datastore_numbering(tmp_path):
    st = DataStore(tmp_path / "data")
    r1, p1 = st.new_path("my sweep/../x")
    r2, p2 = st.new_path("")
    assert r2 == r1 + 1
    assert ".." not in p1.name and "/" not in p1.name
    (tmp_path / "data" / "run_counter.txt").write_text("garbage")
    p1.parent.mkdir(parents=True, exist_ok=True)
    p1.write_bytes(b"x")
    r3, _ = st.new_path("a")
    assert r3 == r1 + 1 or r3 > r1   # rebuilt from existing files, never reuses older numbers


def test_live_journal_readable_while_writing(tmp_path):
    p = tmp_path / "live.emj"
    w = JournalWriter(p)
    w.write(HEADER, make_header(), sync=True)
    w.write(POINT, {"k": 0, "i": [0, 0], "v": {"a": 1.0}})
    ds = DataSet.load(p)          # another process may read it any time
    assert ds.points == 1 and ds.status == "incomplete"
    w.close()


def test_unusable_data_folder_is_refused(backend, tmp_path):
    from easymeasure.common.errors import ChannelError
    blocker = tmp_path / "a_file"
    blocker.write_text("x")
    before = backend.get_settings()["data_dir"]
    with pytest.raises(ChannelError, match="cannot be used"):
        backend.set_settings(data_dir=str(blocker / "data"))
    assert backend.get_settings()["data_dir"] == before
    backend.set_settings(data_dir="")                        # empty: kept
    assert backend.get_settings()["data_dir"] == before
