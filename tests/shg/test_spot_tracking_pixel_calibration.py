from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from scipy.ndimage import shift
from shg_addon import spot_tracking as t
from shg_addon import pixel_calibration as p


class PixelCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        for attr, value in (("HOME",self.root/"state"),("DATA",self.root/"data")):
            patcher=patch.object(t,attr,value); patcher.start(); self.addCleanup(patcher.stop)
        self.writes, self.reads = [], []
        self.ns=dict(check=lambda:None,sleep=lambda _:None,
                     instrument=lambda _:SimpleNamespace(state="connected"),
                     get=self.get, set_many=lambda values:self.writes.append(values),
                     log=lambda *args:None)

    def get(self, name):
        self.reads.append(name)
        return {"microscope.sensor_size":[220,180],"microscope.exposure":.01}[name]

    def config(self):
        return dict(positions=[[10,20],[11,20],[12,20],[10,20],[10,21],[10,22],[10,20]],
                    settle=0.,radius=25.,minimum_score=.8,max_rms=.2,
                    save_frames=False,return_to_first=True)

    def frames(self, cfg):
        yy,xx=np.mgrid[:180,:220]
        reference=8+190*np.exp(-((xx-104)**2+(yy-89)**2)/50)+30*np.exp(-((xx-113)**2+(yy-98)**2)/15)
        matrix=np.array([[-2.1,.3],[-.4,-1.8]])
        points=p.prepared_path(cfg)
        displacements=(points-points[0])@matrix.T
        return [shift(reference,(v,u),order=3,mode="nearest") for u,v in displacements],matrix

    def measure(self, cfg=None, frames=None, use=True):
        cfg=cfg or self.config()
        frames,matrix=self.frames(cfg) if frames is None else (frames,None)
        def response(ns,task,payload,frame=None):
            if task=="roi": return dict(roi=[79,64,51,51],position=None)
            return dict(use=use)
        with patch.object(t,"fresh_frame",side_effect=frames),patch.object(t,"dialog",side_effect=response):
            path=p.measure(self.ns,cfg)
        return path,matrix

    def test_least_squares_matches_full_matrix_and_cross_axis_terms(self):
        stage=np.array(p.matlab_path(),float)
        matrix=np.array([[-18.2,.61],[-.14,-18.4]])
        pixels=np.array([1500.,1000.])+(stage-stage[0])@matrix.T
        result=p.fit_matrix(stage,pixels)
        np.testing.assert_allclose(result["matrix"],matrix,atol=1e-13)
        np.testing.assert_allclose(result["rms"],0,atol=1e-13)

    def test_native_scan_saves_resolution_and_prefills_combine(self):
        t.write_json(t.HOME/"settings.json",dict(laser_run="preserve-laser",flake_run="preserve-flake",active_table="preserve-table"))
        path,matrix=self.measure()
        with np.load(path,allow_pickle=False) as fit:
            np.testing.assert_allclose(fit["matrix"],matrix,atol=.025)
            self.assertTrue(np.all(fit["rms"]<.02))
        self.assertEqual(len(self.writes),7)
        self.assertTrue(all(set(w)=={"X","Y"} for w in self.writes))
        self.assertEqual(self.writes[-1],self.writes[0])
        saved=t.settings()
        self.assertEqual(saved["calibration_size"],[220,180])
        self.assertEqual(saved["laser_run"],"preserve-laser")
        self.assertEqual(saved["active_table"],"preserve-table")
        self.assertEqual(saved["pixel_calibration_file"],str(path))
        self.assertTrue(path.with_name("calibration.csv").exists())

    def test_cancel_result_preserves_existing_matrix(self):
        before=t.settings()["pixel_per_piezo"]
        self.measure(use=False)
        self.assertEqual(t.settings()["pixel_per_piezo"],before)

    def test_poor_rms_is_not_applied_even_if_ui_returns_use(self):
        cfg=self.config(); cfg["max_rms"]=1e-9
        before=t.settings()["pixel_per_piezo"]
        with self.assertRaisesRegex(ValueError,"RMS exceeds"):
            self.measure(cfg)
        self.assertEqual(t.settings()["pixel_per_piezo"],before)

    def test_low_correlation_preserves_partial_and_prior_calibration(self):
        frames,_=self.frames(self.config())
        with self.assertRaisesRegex(ValueError,"tracking lost"):
            self.measure(frames=[frames[0],np.ones_like(frames[0])])
        root=next(t.DATA.iterdir())
        meta=json.loads((root/"run.json").read_text())
        self.assertEqual(meta["status"],"incomplete")
        self.assertEqual(meta["completed"],1)
        self.assertNotIn("pixel_calibration_file",t.settings())

    def test_abort_keeps_partial_and_does_not_return_automatically(self):
        class Abort(Exception):pass
        frames,_=self.frames(self.config())
        with self.assertRaises(Abort): self.measure(frames=[frames[0],Abort("stop")])
        self.assertEqual(len(self.writes),2)
        self.assertEqual(json.loads(next(t.DATA.glob("*/run.json")).read_text())["status"],"incomplete")

    def test_configuration_cancel_has_no_stage_or_camera_io(self):
        with patch.object(t,"dialog",return_value=None): p.execute(self.ns)
        self.assertEqual(self.writes,[]); self.assertEqual(self.reads,[])

    def test_refuses_calibration_while_combine_dialog_is_open(self):
        t.LOCK.acquire()
        try:
            with self.assertRaisesRegex(ValueError,"Close the running"):
                p.execute(self.ns)
        finally:t.LOCK.release()
        self.assertEqual(self.writes,[]); self.assertEqual(self.reads,[])

    def test_path_validation_rejects_limits_and_rank_deficiency_before_motion(self):
        for positions in ([[0,0],[1,0],[2,0]],[[0,0],[101,0],[0,1]],[[0,0],[float('nan'),1],[1,0]]):
            cfg=dict(self.config(),positions=positions)
            with self.assertRaises(ValueError):p.measure(self.ns,cfg)
        self.assertEqual(self.writes,[]);self.assertEqual(self.reads,[])

    def test_generated_path_returns_to_origin_and_both_sides(self):
        path=np.array(p.local_path(30,30,5,3))
        np.testing.assert_array_equal(path[0],path[-1])
        np.testing.assert_array_equal(path.min(axis=0),[15,15])
        np.testing.assert_array_equal(path.max(axis=0),[45,45])
        self.assertTrue(np.all(np.linalg.norm(np.diff(path,axis=0),axis=1)<=5))

    def test_singular_measured_matrix_rejected(self):
        positions=p.local_path(10,20)
        with self.assertRaisesRegex(ValueError,"singular"):
            p.fit_matrix(positions,np.ones_like(positions))

    def test_frame_saving_and_feature_crops(self):
        cfg=dict(self.config(),save_frames=True)
        path,_=self.measure(cfg)
        self.assertEqual(len(list(path.parent.glob('images/*.png'))),7)
        self.assertEqual(len(list(path.parent.glob('tracked_crops/*.png'))),7)


class PixelCalibrationDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])

    def test_configuration_motion_consent_and_editable_path(self):
        from PySide6.QtWidgets import QDialog
        from shg_addon.pixel_calibration_dialog import CalibrationConfigDialog
        w=CalibrationConfigDialog(p.defaults())
        w.validate();self.assertNotEqual(w.result(),QDialog.DialogCode.Accepted)
        w.motion.setChecked(True);w.validate()
        self.assertEqual(w.result(),QDialog.DialogCode.Accepted)
        self.assertEqual(np.linalg.matrix_rank(np.asarray(w.config["positions"])-w.config["positions"][0]),2)
        w.close()

    def test_result_uses_native_resolution_and_requires_opt_in(self):
        from shg_addon.pixel_calibration_dialog import CalibrationResultDialog
        with tempfile.TemporaryDirectory() as tmp:
            fit=Path(tmp)/"fit.npz"
            stage=np.asarray(p.local_path(10,20));pixels=stage@np.array([[-6,.2],[-.04,-6.1]]).T
            result=p.fit_matrix(stage,pixels)
            t._save_npz(fit,**result,camera_size=[3040,2048])
            w=CalibrationResultDialog(str(fit),3)
            w.show();self.app.processEvents()
            self.assertTrue(w.use.isEnabled());self.assertFalse(w.use.isChecked())
            w.use.setChecked(True);w.validate();self.assertTrue(w.value["use"]);w.close()
            result["rms"]=np.array([5,0])
            t._save_npz(fit,**result,camera_size=[3040,2048])
            w=CalibrationResultDialog(str(fit),3)
            self.assertFalse(w.use.isEnabled());w.close()


if __name__=="__main__":unittest.main()
