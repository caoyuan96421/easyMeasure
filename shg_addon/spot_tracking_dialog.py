"""Interactive settings, ROI confirmation, and correction review for three actions."""
from pathlib import Path
import sys
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLabel, QPushButton, QDoubleSpinBox, QSpinBox, QCheckBox,
    QLineEdit, QFileDialog, QMessageBox, QDialogButtonBox, QGroupBox)
from shg_addon.spot_tracking import (read_json, write_json, angles_from_config,
    initial_spot, centered_roi, roi, crop, gray, combine_runs, load_run)


def number(value, minimum, maximum, decimals=3):
    s = QDoubleSpinBox()
    s.setDecimals(decimals); s.setRange(minimum, maximum); s.setValue(value)
    return s


class ConfigDialog(QDialog):
    def __init__(self, mode, config):
        super().__init__()
        self.mode, self.config = mode, dict(config)
        self.setWindowTitle("SHG tracking — " + mode)
        self.setMinimumWidth(660)
        layout = QVBoxLayout(self)
        hint = QLabel("Combine two completed easyMeasure tracking runs. This action takes no pictures and moves no hardware."
                      if mode == "combine" else
                      "Connect the microscope in easyMeasure first. This action moves only the physical half-wave plate. "
                      "Arrange illumination for the %s run; X/Y, shutter, optical path, and voltages stay as they are. "
                      "You will confirm the image ROI at the first angle. Stop/abort closes the dialog and stops at the next checkpoint." % mode)
        hint.setWordWrap(True); layout.addWidget(hint)
        form = QFormLayout(); layout.addLayout(form)
        self.fields = {}
        if mode != "combine":
            for key, label, lo, hi, digits in [
                ("start", "First physical half-wave angle (°)", 0, 340, 3),
                ("stop", "Last physical half-wave angle (°)", 0, 340, 3),
                ("step", "Angle step (°)", .001, 340, 3),
                ("settle", "Settle after rotation (seconds)", 0, 60, 3),
                ("radius", "Search radius (camera pixels)", 1, 10000, 0),
                ("minimum_score", "Minimum correlation score", .001, 1, 3),
                ("patch_size", "Laser template width/height (pixels)", 8, 4096, 0)]:
                self.fields[key] = number(config[key], lo, hi, digits)
                form.addRow(label, self.fields[key])
            self.save_frames = QCheckBox("Save full frames and ROI images in this run's own folder")
            self.save_frames.setChecked(config["save_frames"]); layout.addWidget(self.save_frames)
            self.motion = QCheckBox("Start the half-wave scan after I accept these settings")
            layout.addWidget(self.motion)
        else:
            for key, label in (("laser_run", "Laser run"), ("flake_run", "Flake run")):
                row = QHBoxLayout(); field = QLineEdit(config[key]); self.fields[key] = field
                row.addWidget(field)
                button = QPushButton("Browse…")
                button.clicked.connect(lambda _, f=field: self.choose(f))
                row.addWidget(button); form.addRow(label + " (tracking.npz)", row)
            notice = QLabel("Pixel calibration: [du; dv] = M × [dX; dY]. The defaults come from your MATLAB script. "
                            "They are valid only for the same camera scale and orientation. No image resizing is applied.")
            notice.setWordWrap(True); layout.addWidget(notice)
            matrix_box = QGroupBox("Pixel-per-piezo matrix and its calibration resolution")
            mf = QFormLayout(matrix_box); layout.addWidget(matrix_box)
            self.matrix = []
            for i in range(2):
                row = QHBoxLayout(); widgets = []
                for j in range(2):
                    s = number(config["pixel_per_piezo"][i][j], -1e6, 1e6, 8)
                    row.addWidget(s); widgets.append(s)
                mf.addRow("du row" if i == 0 else "dv row", row); self.matrix.append(widgets)
            self.resolution = [number(config["calibration_size"][i], 8, 50000, 0) for i in range(2)]
            row = QHBoxLayout()
            for s in self.resolution: row.addWidget(s)
            mf.addRow("Calibration frame: width, height (px)", row)
            self.calibration_confirm = QCheckBox("I confirm this matrix matches the runs' resolution, scale, orientation, and X/Y alias units")
            layout.addWidget(self.calibration_confirm)
        self.error = QLabel(); self.error.setStyleSheet("color: #e86e65"); self.error.setWordWrap(True); layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Calculate correction" if mode == "combine" else "Move to first angle and select ROI")
        buttons.accepted.connect(self.validate); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def choose(self, field):
        path, _ = QFileDialog.getOpenFileName(self, "Select native completed tracking run", field.text(), "easyMeasure native tracking (*.npz)")
        if path: field.setText(path)

    def validate(self):
        try:
            cfg = dict(self.config)
            if self.mode != "combine":
                if not self.motion.isChecked(): raise ValueError("Check the half-wave scan box when ready")
                cfg.update({k: s.value() for k, s in self.fields.items()})
                cfg["patch_size"] = int(cfg["patch_size"])
                cfg["save_frames"] = self.save_frames.isChecked()
                angles_from_config(cfg)
            else:
                if not self.calibration_confirm.isChecked(): raise ValueError("Confirm the pixel calibration before combining")
                cfg.update({k: s.text() for k, s in self.fields.items()})
                cfg["pixel_per_piezo"] = [[s.value() for s in row] for row in self.matrix]
                cfg["calibration_size"] = [int(s.value()) for s in self.resolution]
                combine_runs(load_run(cfg["laser_run"], "laser"), load_run(cfg["flake_run"], "flake"),
                             cfg["pixel_per_piezo"], cfg["calibration_size"])
            self.config = cfg
            self.accept()
        except Exception as exc:
            self.error.setText(str(exc))


class RoiDialog(QDialog):
    def __init__(self, mode, config, frame):
        super().__init__()
        self.mode, self.config, self.frame = mode, config, frame
        self.manual_position = False
        self.setWindowTitle("SHG %s — confirm full-frame ROI" % mode)
        self.resize(1100, 820)
        layout = QVBoxLayout(self)
        hint = QLabel("Drag the yellow box around the laser's broad search region. The red + is its automatic center; "
                      "click the actual laser spot to override it. The red box shows the fixed reference template."
                      if mode == "laser" else
                      "Drag and resize the yellow box around one distinctive flake edge, corner, or internal feature. "
                      "This compact box becomes the fixed tracking template.")
        hint.setWordWrap(True); layout.addWidget(hint)
        self.view = pg.PlotWidget(); self.view.setAspectLocked(True); self.view.invertY(True)
        self.view.setLabel("bottom", "Camera x (pixels, zero-based)")
        self.view.setLabel("left", "Camera y (pixels, zero-based)")
        layout.addWidget(self.view)
        self.image = pg.ImageItem(axisOrder="row-major"); self.image.setImage(frame, levels=(0, 255))
        self.view.addItem(self.image)
        h, w = frame.shape[:2]
        side = min(121, h, w)
        origin, size = ([0, 0], [w, h]) if mode == "laser" else ([(w-side)/2, (h-side)/2], [side, side])
        self.region = pg.RectROI(origin, size, pen=pg.mkPen("y", width=2),
                                 maxBounds=__import__("PySide6.QtCore", fromlist=["QRectF"]).QRectF(0, 0, w, h))
        self.region.addScaleHandle([1, 1], [0, 0]); self.view.addItem(self.region)
        self.cross = pg.ScatterPlotItem(symbol="+", size=20, pen=pg.mkPen("r", width=2))
        self.view.addItem(self.cross)
        self.patch = pg.PlotCurveItem(pen=pg.mkPen("r", width=2)); self.view.addItem(self.patch)
        self.info = QLabel(); self.info.setWordWrap(True); layout.addWidget(self.info)
        row = QHBoxLayout(); layout.addLayout(row)
        if mode == "laser":
            reset = QPushButton("Redetect laser in ROI"); reset.clicked.connect(self.redetect); row.addWidget(reset)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Confirm feature and start tracking")
        buttons.accepted.connect(self.validate); buttons.rejected.connect(self.reject); row.addWidget(buttons)
        self.region.sigRegionChanged.connect(self.changed)
        self.view.scene().sigMouseClicked.connect(self.clicked)
        self.position = None; self.changed()

    def current_roi(self):
        p, s = self.region.pos(), self.region.size()
        # Clip fractional drag endpoints while retaining zero-based full pixels.
        x, y = max(0, int(np.floor(p.x()))), max(0, int(np.floor(p.y())))
        w, h = min(int(np.ceil(s.x())), self.frame.shape[1]-x), min(int(np.ceil(s.y())), self.frame.shape[0]-y)
        return roi([x, y, w, h], self.frame.shape)

    def changed(self):
        try:
            r = self.current_roi()
            if self.mode == "laser":
                if not self.manual_position: self.position = initial_spot(self.frame, r)
                patch = centered_roi(self.position, self.config["patch_size"], self.frame.shape)
                x, y, w, h = patch
                self.cross.setData([self.position[0]], [self.position[1]])
                self.patch.setData([x, x+w-1, x+w-1, x, x], [y, y, y+h-1, y+h-1, y])
                self.info.setText("ROI %s; laser center [%.3f, %.3f] px. Verify the red marker before confirming." % (r, *self.position))
            else:
                self.info.setText("Flake template ROI: %s full-frame pixels" % (r,))
        except Exception as exc:
            self.info.setText(str(exc))

    def clicked(self, event):
        if self.mode != "laser" or event.button() != Qt.MouseButton.LeftButton:
            return
        point = self.view.getViewBox().mapSceneToView(event.scenePos())
        if 0 <= point.x() < self.frame.shape[1] and 0 <= point.y() < self.frame.shape[0]:
            self.manual_position = True; self.position = np.array([point.x(), point.y()]); self.changed()

    def redetect(self):
        self.manual_position = False; self.changed()

    def validate(self):
        try:
            r = self.current_roi()
            patch = centered_roi(self.position, self.config["patch_size"], self.frame.shape) if self.mode == "laser" else r
            if np.std(gray(crop(self.frame, patch))) <= np.finfo(float).eps:
                raise ValueError("Select a feature with usable intensity variation")
            self.value = dict(roi=list(r), position=None if self.position is None else self.position.tolist())
            self.accept()
        except Exception as exc:
            self.info.setText(str(exc))


class ResultDialog(QDialog):
    def __init__(self, table, config):
        super().__init__()
        self.setWindowTitle("SHG beam-walk correction — review")
        self.resize(900, 650)
        layout = QVBoxLayout(self)
        with np.load(table, allow_pickle=False) as data:
            self.angles, self.correction = data["angles"].copy(), data["correction"].copy()
        text = QLabel("Saved native correction: %s\n%d points, physical half-wave %.3f–%.3f°. "
                      "Combining has not moved any hardware. Enabling below affects future corrected moves and "
                      "standard sweeps using shg_acquire_point. The old sinusoidal correction is replaced while enabled." %
                      (table, len(self.angles), self.angles[0], self.angles[-1]))
        text.setWordWrap(True); layout.addWidget(text)
        plot = pg.PlotWidget(); plot.addLegend(); plot.setLabel("bottom", "Physical half-wave angle (degrees)")
        plot.setLabel("left", "Stage correction (X/Y alias units)")
        from scipy.interpolate import PchipInterpolator
        dense = np.linspace(self.angles[0], self.angles[-1], max(200, len(self.angles)))
        smooth = PchipInterpolator(self.angles, self.correction, axis=0)(dense)
        for i, (name, color) in enumerate((("dX", "#ef6f6c"), ("dY", "#5ab0ff"))):
            plot.plot(dense, smooth[:, i], name=name+" PCHIP", pen=color)
            plot.plot(self.angles, self.correction[:, i], pen=None, symbol="o", symbolSize=4, symbolBrush=color)
        layout.addWidget(plot)
        self.enable = QCheckBox("Enable this correction table for future SHG corrected moves / acquire_point hooks")
        self.enable.setChecked(False); layout.addWidget(self.enable)
        form = QFormLayout(); layout.addLayout(form)
        self.base_x, self.base_y = number(config["base_x"], 0, 100), number(config["base_y"], 0, 100)
        form.addRow("Base X for raw half-wave sweeps", self.base_x)
        form.addRow("Base Y for raw half-wave sweeps", self.base_y)
        hint = QLabel("Raw half-wave sweep hook: X = base X + dX(angle), Y = base Y + dY(angle). "
                      "Logical polarization moves use halfwave = 28 + polarization/2. Angles outside the measured range are rejected. "
                      "Cancel keeps the existing activation setting. Save with the box unchecked disables tracking correction.")
        hint.setWordWrap(True); layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.validate); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def validate(self):
        if self.enable.isChecked():
            corrected = self.correction + [self.base_x.value(), self.base_y.value()]
            if np.any(corrected < 0) or np.any(corrected > 100):
                QMessageBox.warning(self, "Stage bounds", "Base X/Y plus correction must stay within 0..100 over the measured range")
                return
        self.value = dict(enable=self.enable.isChecked(), base_x=self.base_x.value(), base_y=self.base_y.value())
        self.accept()


def main(path):
    request = Path(path); response = request.parent / "response.json"
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    try:
        data = read_json(request); payload = data["payload"]
        if data["task"] == "configure":
            window = ConfigDialog(payload["mode"], payload["config"])
        elif data["task"] == "roi":
            window = RoiDialog(payload["mode"], payload["config"], np.load(request.parent / "frame.npy", allow_pickle=False))
        elif data["task"] == "result":
            window = ResultDialog(payload["table"], payload["config"])
        elif data["task"] == "pixel_calibration_configure":
            from shg_addon.pixel_calibration_dialog import CalibrationConfigDialog
            window = CalibrationConfigDialog(payload["config"])
        elif data["task"] == "pixel_calibration_result":
            from shg_addon.pixel_calibration_dialog import CalibrationResultDialog
            window = CalibrationResultDialog(payload["fit"], payload["max_rms"])
        else:
            raise ValueError("Unknown tracking dialog")
        accepted = window.exec() == QDialog.DialogCode.Accepted
        value = (window.config if data["task"] in ("configure", "pixel_calibration_configure") else window.value) if accepted else None
        write_json(response, dict(value=value))
    except Exception as exc:
        write_json(response, dict(error=str(exc)))


if __name__ == "__main__":
    main(sys.argv[1])
