"""Qt review dialogs for native pixel-to-stage calibration."""
import numpy as np
import pyqtgraph as pg
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QCheckBox, QPushButton, QPlainTextEdit, QDialogButtonBox, QTabWidget, QWidget)
from shg_addon.spot_tracking_dialog import number
from shg_addon.pixel_calibration import local_path, matlab_path, prepared_path


class CalibrationConfigDialog(QDialog):
    def __init__(self, config):
        super().__init__()
        self.config = dict(config)
        self.setWindowTitle("easyMeasure — calibrate pixels versus X/Y")
        self.resize(800, 760)
        layout = QVBoxLayout(self)
        text = QLabel("Track a stationary, stage-attached flake feature while moving physical X/Y. "
                      "Keep microscope resolution, zoom, and orientation unchanged. Review every position below. "
                      "No half-wave or MEMS voltage commands are issued. Stage units are the X/Y alias units, not assumed micrometers.")
        text.setWordWrap(True); layout.addWidget(text)
        row = QHBoxLayout(); layout.addLayout(row)
        p = np.asarray(config["positions"])
        self.x, self.y = number(p[0,0], 0, 100), number(p[0,1], 0, 100)
        self.step, self.count = number(1, .001, 50), number(3, 1, 30, 0)
        for label, widget in (("Center X",self.x),("Y",self.y),("Step",self.step),("Steps/side",self.count)):
            row.addWidget(QLabel(label)); row.addWidget(widget)
        row = QHBoxLayout(); layout.addLayout(row)
        generate = QPushButton("Generate local bidirectional path")
        generate.clicked.connect(self.generate); row.addWidget(generate)
        legacy = QPushButton("Use original 22-point path (30 ± 15)")
        legacy.clicked.connect(lambda: self.set_path(matlab_path())); row.addWidget(legacy)
        layout.addWidget(QLabel("Editable stage positions: one X Y pair per row"))
        self.path = QPlainTextEdit(); self.path.setMinimumHeight(180); layout.addWidget(self.path)
        self.set_path(config["positions"])
        form = QFormLayout(); layout.addLayout(form)
        self.fields = {}
        for key, label, lo, hi, digits in (("settle","Settle after each move (s)",0,60,3),
             ("radius","Feature search radius (px)",1,10000,0),
             ("minimum_score","Minimum correlation",.001,1,3),
             ("max_rms","Maximum fit RMS per pixel axis (px)",.001,1000,3)):
            self.fields[key] = number(config[key],lo,hi,digits); form.addRow(label,self.fields[key])
        self.return_first = QCheckBox("Return to the first point on successful completion (include it in the fit)")
        self.return_first.setChecked(config["return_to_first"]); layout.addWidget(self.return_first)
        self.save_frames = QCheckBox("Save full images and feature-centered crops")
        self.save_frames.setChecked(config["save_frames"]); layout.addWidget(self.save_frames)
        self.motion = QCheckBox("Start this X/Y movement path after I accept; select the flake at the first point")
        layout.addWidget(self.motion)
        self.error = QLabel(); self.error.setWordWrap(True); self.error.setStyleSheet("color:#ef756d"); layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Move to first point and select flake")
        buttons.accepted.connect(self.validate); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def set_path(self, points):
        self.path.setPlainText("\n".join("%.6g %.6g" % tuple(p) for p in points))

    def generate(self):
        try:
            self.set_path(local_path(self.x.value(), self.y.value(), self.step.value(), int(self.count.value())))
            self.error.clear()
        except Exception as exc: self.error.setText(str(exc))

    def validate(self):
        try:
            points = []
            for row in self.path.toPlainText().splitlines():
                if row.strip(): points.append([float(x) for x in row.replace(","," ").split()])
            cfg = dict(self.config, positions=points, save_frames=self.save_frames.isChecked(),
                       return_to_first=self.return_first.isChecked())
            cfg.update({k:s.value() for k,s in self.fields.items()})
            path = prepared_path(cfg)
            if not self.motion.isChecked(): raise ValueError("Check the X/Y movement box when the path is ready")
            self.config = cfg
            self.accept()
        except Exception as exc: self.error.setText(str(exc))


class CalibrationResultDialog(QDialog):
    def __init__(self, fit, max_rms):
        super().__init__()
        self.setWindowTitle("easyMeasure — pixel/X/Y calibration result")
        self.resize(950, 760)
        layout = QVBoxLayout(self)
        with np.load(fit, allow_pickle=False) as data:
            values = {k:data[k].copy() for k in data.files}
        m, size, rms = values["matrix"], values["camera_size"], values["rms"]
        good = np.all(rms <= max_rms)
        text = QLabel("Camera: %d × %d pixels\n[du; dv] = M × [dX; dY]\n"
                      "M = [[% .8f, % .8f],\n         [% .8f, % .8f]] pixels per X/Y alias unit\n"
                      "RMS residual [u, v] = [%.4f, %.4f] px; limit %.4f px\n"
                      "Matrix condition: %.4g; maximum return-to-origin error: %.4f px\nSaved: %s" %
                      (*size,*m.ravel(),*rms,max_rms,float(values["condition"]),float(values["max_return_error"]),fit))
        text.setWordWrap(True); text.setTextInteractionFlags(__import__("PySide6.QtCore",fromlist=["Qt"]).Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(text)
        tabs = QTabWidget(); layout.addWidget(tabs)
        for name, actual_key, predicted_key in (("Measured / fitted pixel shifts","pixel_shift","predicted_shift"),
                                                ("Residuals","residual",None)):
            plot = pg.PlotWidget(); plot.addLegend(); plot.setLabel("bottom","Stage path point")
            plot.setLabel("left","Camera displacement (pixels)")
            for axis,color in ((0,"#ed756d"),(1,"#5bb6ff")):
                label = "u" if axis==0 else "v"
                plot.plot(values[actual_key][:,axis],pen=color,symbol="o",symbolSize=4,name=label)
                if predicted_key:
                    plot.plot(values[predicted_key][:,axis],pen=pg.mkPen(color,style=__import__("PySide6.QtCore",fromlist=["Qt"]).Qt.PenStyle.DashLine),name=label+" fitted")
            tabs.addTab(plot,name)
        self.use = QCheckBox("Use this matrix and camera resolution in future Combine dialogs")
        self.use.setEnabled(bool(good)); layout.addWidget(self.use)
        message = ("Review feature tracking, residuals, and return errors before saving. "
                   "This changes calibration preferences only; it does not move hardware or enable a correction table. "
                   "Reopen Combine to use the saved matrix.") if good else (
                   "RMS exceeds the chosen limit. The run is saved for diagnosis; its matrix cannot be selected. "
                   "Check the feature, illumination, step size, settling, and any stage hysteresis, then repeat calibration.")
        label = QLabel(message); label.setWordWrap(True); layout.addWidget(label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.validate); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def validate(self):
        self.value = dict(use=self.use.isEnabled() and self.use.isChecked()); self.accept()
