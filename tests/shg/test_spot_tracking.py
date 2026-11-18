"""Offline regression tests. All instruments and UI interactions are fakes."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
from scipy.ndimage import shift
from shg_addon import spot_tracking as t


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(t, "HOME", self.root / "state"),
                        patch.object(t, "DATA", self.root / "data")]
        for p in self.patches: p.start()
        self.addCleanup(self.tmp.cleanup)
        for p in self.patches: self.addCleanup(p.stop)

    def namespace(self):
        self.writes, self.logs = [], []
        return dict(check=lambda: None, sleep=lambda _: None,
                    instrument=lambda _: SimpleNamespace(state="connected"),
                    get=lambda name: {"microscope.sensor_size": [220, 180],
                                      "microscope.exposure": .01, "halfwave": 28}[name],
                    set=lambda n, v: self.writes.append((n, v)),
                    log=lambda *args: self.logs.append(args))

    def image(self):
        yy, xx = np.mgrid[:180, :220]
        return 10+200*np.exp(-((xx-104.2)**2+(yy-89.8)**2)/50)

    def test_initial_spot(self):
        np.testing.assert_allclose(t.initial_spot(self.image(), [20, 20, 180, 140]), [104.2, 89.8], atol=.06)
        with self.assertRaises(ValueError): t.initial_spot(np.ones((80, 80)), [0, 0, 80, 80])

    def test_ncc_matches_direct_pearson(self):
        rng = np.random.default_rng(81)
        image, template = rng.normal(size=(19, 21)), rng.normal(size=(8, 9))
        actual = t.normalized_correlation(image, template)
        expected = np.array([[np.corrcoef(image[y:y+8, x:x+9].ravel(), template.ravel())[0, 1]
                              for x in range(13)] for y in range(12)])
        np.testing.assert_allclose(actual, expected, atol=2e-14)

    def test_subpixel_translation(self):
        image = self.image()
        template = image[65:116, 79:130]
        moved = shift(image, (2.35, -3.2), order=3, mode="nearest")
        pos, score = t.track_template(moved, template, [104, 90], 20)
        np.testing.assert_allclose(pos, [100.8, 92.35], atol=.025)
        self.assertGreater(score, .99)

    def scan(self, frames=None):
        cfg = dict(t.defaults(), start=28., stop=34., step=3., patch_size=51, save_frames=False)
        self.selection = dict(roi=[0, 0, 220, 180], position=[104, 90])
        ns = self.namespace()
        if frames is None:
            frames = [self.image(), shift(self.image(), (1, 2)), shift(self.image(), (2, 4))]
        with patch.object(t, "dialog", return_value=self.selection), patch.object(t, "fresh_frame", side_effect=frames):
            path = t.acquire_tracking(ns, "laser", cfg)
        return path, cfg

    def test_scan_moves_only_rotator_and_saves_complete_native_run(self):
        path, cfg = self.scan()
        self.assertEqual(self.writes, [("halfwave", 28.), ("halfwave", 31.), ("halfwave", 34.)])
        data = t.load_run(path, "laser")
        np.testing.assert_allclose(data["positions"]-data["positions"][0], [[0, 0], [2, 1], [4, 2]], atol=.01)
        self.assertEqual(t.settings()["laser_run"], str(path))
        self.assertEqual(json.loads(path.with_name("run.json").read_text())["status"], "complete")

    def test_weak_correlation_saves_incomplete_and_preserves_last_good(self):
        old = dict(t.defaults(), laser_run="previous-good.npz")
        t.write_json(t.HOME / "settings.json", old)
        with self.assertRaisesRegex(ValueError, "tracking lost"):
            self.scan([self.image(), np.ones_like(self.image())])
        path = next(t.DATA.glob("*/tracking.npz"))
        with self.assertRaisesRegex(ValueError, "completed"): t.load_run(path, "laser")
        self.assertEqual(t.settings()["laser_run"], "previous-good.npz")
        self.assertEqual(json.loads(path.with_name("run.json").read_text())["completed"], 1)

    def test_abort_during_scan_preserves_partial(self):
        class Abort(Exception): pass
        with self.assertRaises(Abort): self.scan([self.image(), Abort("user aborted")])
        path = next(t.DATA.glob("*/tracking.npz"))
        self.assertEqual(json.loads(path.with_name("run.json").read_text())["status"], "incomplete")

    def test_cancel_configuration_touches_no_hardware(self):
        ns = self.namespace()
        ns["get"] = lambda _: self.fail("Read on cancel")
        with patch.object(t, "dialog", return_value=None): t.execute(ns, "laser")
        self.assertFalse(self.writes)

    def test_missing_microscope_fails_before_dialog_reads_and_motion(self):
        from easymeasure.common.errors import ChannelError
        ns = self.namespace()
        ns["get"] = lambda _: self.fail("Missing microscope must not read hardware")
        def missing(_): raise ChannelError("no instrument microscope")
        ns["instrument"] = missing
        with patch.object(t, "dialog") as dialog:
            with self.assertRaisesRegex(ValueError, "Connect microscope"):
                t.execute(ns, "laser")
            dialog.assert_not_called()
        self.assertFalse(self.writes)
        self.assertFalse(t.LOCK.locked())

    def test_disconnected_microscope_fails_without_auto_connect(self):
        ns = self.namespace()
        ns["get"] = lambda _: self.fail("Disconnected microscope must not read hardware")
        ns["instrument"] = lambda _: SimpleNamespace(state="disconnected")
        with patch.object(t, "dialog") as dialog:
            with self.assertRaisesRegex(ValueError, "Reconnect the microscope"):
                t.execute(ns, "flake")
            dialog.assert_not_called()
        self.assertFalse(self.writes)

    def runs(self):
        angle = np.array([28., 31., 34., 37.])
        laser = dict(angles=angle, positions=np.array([[10, 20], [11, 19], [13, 17], [12, 20.]]), camera_size=np.array([1014, 683]))
        flake = dict(angles=angle, positions=np.array([[50, 60], [51, 59], [51, 58], [52, 56.]]), camera_size=np.array([1014, 683]))
        return laser, flake

    def test_combine_sign_and_matrix(self):
        laser, flake = self.runs()
        m = np.array([[-6, .2], [-.04, -6.1]])
        result = t.combine_runs(laser, flake, m, [1014, 683])
        np.testing.assert_allclose(result["correction"] @ m.T, result["relative_shift"], atol=1e-14)
        np.testing.assert_allclose(result["relative_shift"][-1], [0, 4])
        np.testing.assert_allclose(result["correction"][0], [0, 0])

    def test_mismatched_grid_resolution_and_singular_matrix_rejected(self):
        laser, flake = self.runs()
        with self.assertRaisesRegex(ValueError, "calibration is for"):
            t.combine_runs(laser, flake, np.eye(2), [640, 480])
        with self.assertRaisesRegex(ValueError, "nonsingular"):
            t.combine_runs(laser, flake, np.zeros((2, 2)), [1014, 683])
        flake["angles"] = flake["angles"] + .1
        with self.assertRaisesRegex(ValueError, "identical"):
            t.combine_runs(laser, flake, np.eye(2), [1014, 683])

    def make_table(self):
        laser, flake = self.runs()
        result = t.combine_runs(laser, flake, np.eye(2), [1014, 683])
        table = self.root / "table.npz"
        t._save_npz(table, **result)
        return table, result

    def test_pchip_sign_bounds_and_no_double_correction(self):
        table, result = self.make_table()
        t.write_json(t.HOME / "settings.json", dict(active_table=str(table)))
        np.testing.assert_allclose(t.table_offset(34., table), [2, -1])
        np.testing.assert_allclose(t.corrected_xy(20, 30, 12), [22, 29, 34])
        with self.assertRaisesRegex(ValueError, "outside"): t.table_offset(27., table)
        self.assertTrue(np.isfinite(t.table_offset(32., table)).all())

    def test_disabled_restores_original_correction_and_no_sweep_writes(self):
        table, _ = self.make_table()
        t.write_json(t.HOME / "settings.json", dict(active_table=str(table)))
        t.write_json(t.HOME / "disabled.json", dict(disabled=True))
        from shg_addon.analysis import corrected_xy
        np.testing.assert_allclose(t.corrected_xy(20, 30, 12), corrected_xy(20, 30, 12))
        self.assertEqual(t.definitions(), [])
        ns = self.namespace(); t.before_physical_point(ns)
        self.assertEqual(self.writes, [])

    def test_raw_halfwave_hook_uses_baseline_and_checks_all_bounds_first(self):
        table, _ = self.make_table()
        t.write_json(t.HOME / "settings.json", dict(active_table=str(table), base_x=10., base_y=20.))
        ns = self.namespace()
        ns["get"] = lambda _: 34.
        t.before_physical_point(ns)
        self.assertEqual(self.writes, [("X", 12.), ("Y", 19.)])
        self.writes.clear()
        t.write_json(t.HOME / "settings.json", dict(active_table=str(table), base_x=100., base_y=20.))
        with self.assertRaisesRegex(ValueError, "bounds"): t.before_physical_point(ns)
        self.assertEqual(self.writes, [])

    def test_sweep_endpoints_use_commanded_grid_despite_encoder_roundoff(self):
        from unittest.mock import Mock
        table=self.root/'endpoint_table.npz'
        t._save_npz(table,angles=np.array([28.,208.]),correction=np.array([[0.,0.],[-1.,-2.]]))
        t.write_json(t.HOME/'settings.json',dict(active_table=str(table),base_x=10.,base_y=20.))
        ns=self.namespace()
        for nominal,readback,expected in ((28.,27.9997,[("X",10.),("Y",20.)]),
                                         (208.,208.0003,[("X",9.),("Y",18.)])):
            self.writes.clear()
            ns['setpoints']={'halfwave':nominal}
            ns['get']=Mock(return_value=readback)
            t.before_physical_point(ns)
            self.assertEqual(self.writes,expected)
            ns['get'].assert_not_called()

    def test_genuinely_outside_sweep_point_is_rejected_before_xy_writes(self):
        table=self.root/'endpoint_table.npz'
        t._save_npz(table,angles=np.array([28.,208.]),correction=np.array([[0.,0.],[-1.,-2.]]))
        t.write_json(t.HOME/'settings.json',dict(active_table=str(table)))
        ns=self.namespace();ns['setpoints']={'halfwave':209.}
        with self.assertRaisesRegex(ValueError,'outside'):
            t.before_physical_point(ns)
        self.assertEqual(self.writes,[])

    def test_manual_lookup_retains_strict_bounds_and_reports_encoder_digits(self):
        table=self.root/'endpoint_table.npz'
        t._save_npz(table,angles=np.array([28.,208.]),correction=np.array([[0.,0.],[-1.,-2.]]))
        t.write_json(t.HOME/'settings.json',dict(active_table=str(table)))
        ns=self.namespace();ns['get']=lambda _:208.0003
        with self.assertRaisesRegex(ValueError,'208.0003'):
            t.before_physical_point(ns)
        self.assertEqual(self.writes,[])

    def test_combine_action_calls_no_hardware(self):
        laser, flake = self.runs()
        for name, data in (("laser", laser), ("flake", flake)):
            t._save_npz(self.root / (name+".npz"), **data, complete=True, mode=name)
        cfg = dict(t.defaults(), laser_run=str(self.root/"laser.npz"), flake_run=str(self.root/"flake.npz"))
        ns = self.namespace(); ns["get"] = lambda _: self.fail("Combine read hardware")
        with patch.object(t, "dialog", return_value=dict(enable=True, base_x=10., base_y=20.)):
            table = t.combine_tracking(ns, cfg)
        self.assertEqual(self.writes, [])
        self.assertEqual(t.settings()["active_table"], str(table))
        self.assertTrue(table.with_suffix(".csv").exists())

    def test_fresh_frame_flush_then_discards_first(self):
        calls = []
        class Proxy:
            def call(self, method): calls.append(method)
        images = iter([np.zeros((10, 10)), np.ones((10, 10))])
        ns = dict(check=lambda: None, instrument=lambda _: Proxy(), get=lambda _: next(images))
        np.testing.assert_array_equal(t.fresh_frame(ns), np.ones((10, 10)))
        self.assertEqual(calls, ["flush_tracking"])


if __name__ == "__main__": unittest.main(verbosity=2)
