import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from PySide6.QtWidgets import QApplication, QDialog
from shg_addon import spot_tracking as t
from shg_addon.spot_tracking_dialog import ConfigDialog, RoiDialog, ResultDialog

APP = QApplication.instance() or QApplication([])


class DialogTests(unittest.TestCase):
    def test_acquisition_requires_explicit_start_and_valid_grid(self):
        w = ConfigDialog("laser", t.defaults())
        w.validate()
        self.assertNotEqual(w.result(), QDialog.DialogCode.Accepted)
        self.assertIn("Check", w.error.text())
        w.motion.setChecked(True); w.validate()
        self.assertEqual(w.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(len(t.angles_from_config(w.config)), 61)
        w.close()

    def test_laser_and_flake_roi_confirmation_full_frame_pixels(self):
        yy, xx = np.mgrid[:240, :320]
        frame = np.stack([200*np.exp(-((xx-150.2)**2+(yy-110.3)**2)/80)]*3, axis=-1)
        for mode in ("laser", "flake"):
            w = RoiDialog(mode, t.defaults(), frame)
            w.show(); APP.processEvents()
            w.validate()
            self.assertEqual(w.result(), QDialog.DialogCode.Accepted)
            if mode == "laser":
                np.testing.assert_allclose(w.value["position"], [150.2, 110.3], atol=.04)
            else:
                self.assertEqual(w.value["roi"][2:], [121, 121])
            w.close()

    def test_result_activation_is_opt_in(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            table = Path(tmp)/"correction.npz"
            t._save_npz(table, angles=np.array([28, 31, 34]), correction=np.array([[0, 0], [1, 2], [2, 3.]]))
            w = ResultDialog(str(table), t.defaults())
            w.show(); APP.processEvents()
            self.assertFalse(w.enable.isChecked())
            w.validate()
            self.assertFalse(w.value["enable"])
            w.close()

    def test_new_backend_installs_tracking_and_pixel_actions_without_connecting_instruments(self):
        from shg_addon.backend import SHGBackend
        with tempfile.TemporaryDirectory() as tmp:
            backend = SHGBackend(home=Path(tmp), log_to_console=False)
            try:
                with patch.object(t, "HOME", Path(tmp)/"tracking"):
                    problems = backend.load_setup(setup=dict(format="easyMeasure-setup-1", instruments=[], aliases=[], composites=[], monitor=[]), connect=False)
                    self.assertEqual(problems, [])
                    actions = {a["name"] for a in backend.list_user_actions()}
                    self.assertTrue(set(t.ACTION_MODES) <= actions)
                    self.assertIn("shg_calibrate_pixels_xy", actions)
                    self.assertIn("shg_sample", actions)
                    for action in t.definitions(): compile(action["code"], "action", "exec")
            finally:
                backend.close()
                import logging
                logging.shutdown()


if __name__ == "__main__": unittest.main()
