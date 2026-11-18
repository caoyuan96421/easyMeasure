"""SHG panel added to the existing easyMeasure window by subclassing."""

import json

import os

from pathlib import Path

import time

import numpy as np

import pyqtgraph as pg

from PySide6.QtCore import QTimer, Qt, QSettings

from PySide6.QtGui import QFont

from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QGroupBox,

    QPushButton,QLabel,QDoubleSpinBox,QSpinBox,QCheckBox,QComboBox,QLineEdit,QFileDialog,

    QTabWidget,QMessageBox)

from easymeasure.frontend.main_window import MainWindow

from shg_addon.configuration import ROOT,setup,microscope
from shg_addon.lab_profile import laboratory_setup, voltage_rows

from shg_addon.actions import action_code
from shg_addon.sweep_tools import install as install_sweep_tools, preset, add_action

from shg_addon.analysis import retardation
from shg_addon.cwt_axes import CWTView
from shg_addon.shg_mode import mode

class SHGWindow(MainWindow):

    def __init__(self):

        super().__init__(autostart_backend=False)

        self.qsettings=QSettings("easyMeasure","shg-gui")

        self.setFont(QFont("Segoe UI",10))

        self.setWindowTitle("easyMeasure Â· SHG laboratory Â· fd10964")

        self.polling=False; self.preview=False; self.preview_busy=False

        self.last_frame=None; self.last_id=-1; self.last_diagnostic_id=0; self.circle=None; self.ae_rectangle=None

        self.pending_path=None
        self.sensor_size=None

        self.status=QLabel("Choose simulation or connect the laboratory setup. Outputs are not enabled automatically.")

        self.status.setWordWrap(True)

        self.simulate=QCheckBox("Load simulated setup (synthetic data)"); self.simulate.setChecked(False)

        panel=QWidget(); layout=QVBoxLayout(panel)

        self.mode_label=QLabel("Checking loaded instruments..."); self.mode_label.setWordWrap(True); layout.addWidget(self.mode_label)
        self.loaded_mode="unknown"; self.mode_next=0
        layout.addWidget(self.status)

        row=QHBoxLayout(); row.addWidget(self.simulate)

        self.button(row,"Load selected setup",self.load_setup)

        self.button(row,"Stop / abort",lambda:self.conn.request("abort_all"))

        layout.addLayout(row)

        self.cygnus=QLineEdit(); self.cygnus.setPlaceholderText("Cygnus COM port (optional; COM5 is the rotator)")

        layout.addWidget(self.cygnus)

        self.group(layout,"Acquisition",[

            ("Sample spectrum",lambda:self.run("sample")),("Reference spectrum",lambda:self.run("reference")),

            ("Loop sample",lambda:self.run("loop_sample")),("Loop reference",lambda:self.run("loop_reference")),

            ("Refresh reference",lambda:self.run("refresh_reference")),

            ("Take single spectrum + CWT",lambda:self.run("single_diagnostic")),("Show CWT diagnostic",self.show_diagnostic),

            ("Camera only (no optical switching)",lambda:self.action("camera.capture_sample"))])

        row=QHBoxLayout(); self.auto=QCheckBox("Auto-reference every 300 s (0.25 s exposure)")

        self.auto.toggled.connect(lambda v:self.run("autocal",enabled=v)); row.addWidget(self.auto); layout.addLayout(row)

        self.group(layout,"Interferometer calibration",[

            ("Sample phase center",lambda:self.run("sample_center")),("Reference phase center",lambda:self.run("reference_center")),

            ("Five-pair phase offset",lambda:self.run("calibration_phase")),("Run center + phase calibration",lambda:self.run("calibrate_all")),

            ("Measure retardation",lambda:self.run("measure_retardation")),("Fit retardation",lambda:self.run("fit_retardation")),

            ("Plot retardation fit",self.plot_retardation),("Save calibration",lambda:self.action("camera.save_calibration")),

            ("Load calibration",self.load_calibration)])

        self.group(layout,"Optical path",[

            ("Open shutter",lambda:self.write("Shutter",0)),("Block shutter",lambda:self.write("Shutter",1)),

            ("Sample path",lambda:self.write("SampRef",0)),("Reference path",lambda:self.write("SampRef",1))])

        controls=QGroupBox("Stage, polarization and camera"); grid=QGridLayout(controls)

        self.fields={}

        for row,(label,target,lo,hi,default) in enumerate([

            ("X (0â€“100)","X",0,100,50),("Y (0â€“100)","Y",0,100,50),

            ("Half-wave angle (Â°)","halfwave",0,340,28),

            ("Exposure (s)","camera.exposure",.000001,60,1),

            ("Gain","camera.gain",0,600,100),("Cooler target (Â°C)","camera.target_temperature",-40,30,-5)]):

            spin=QDoubleSpinBox(); spin.setDecimals(6 if "exposure" in target else 3); spin.setRange(lo,hi); spin.setValue(default)

            self.fields[target]=spin; grid.addWidget(QLabel(label),row,0); grid.addWidget(spin,row,1)

            b=QPushButton("Set"); b.clicked.connect(lambda _,t=target,s=spin:self.write(t,s.value())); grid.addWidget(b,row,2)

        row=6; self.step=QDoubleSpinBox(); self.step.setRange(.001,100); self.step.setValue(1)

        grid.addWidget(QLabel("Stage step"),row,0); grid.addWidget(self.step,row,1)

        for col,(text,axis,direction) in enumerate([("Xâˆ’","X",-1),("X+","X",1),("Yâˆ’","Y",-1),("Y+","Y",1)]):

            b=QPushButton(text); b.clicked.connect(lambda _,a=axis,d=direction:self.jog(a,d)); grid.addWidget(b,7,col)

        b=QPushButton("Home half-wave plate"); b.clicked.connect(lambda:self.action("rotator.home")); grid.addWidget(b,8,0,1,2)

        b=QPushButton("Cooler ON"); b.clicked.connect(lambda:self.write("camera.cooler","ON")); grid.addWidget(b,8,2)

        b=QPushButton("Cooler OFF"); b.clicked.connect(lambda:self.write("camera.cooler","OFF")); grid.addWidget(b,8,3)

        self.pol=QDoubleSpinBox(); self.pol.setRange(-56,624); grid.addWidget(QLabel("Polarization (Â°)"),9,0); grid.addWidget(self.pol,9,1)

        b=QPushButton("Move corrected X/Y + polarization"); b.clicked.connect(lambda:self.run("corrected_move",x=self.fields["X"].value(),y=self.fields["Y"].value(),polarization=self.pol.value())); grid.addWidget(b,10,0,1,4)

        layout.addWidget(controls)

        box=QGroupBox("Spectrum integration region (1-based pixels)"); g=QGridLayout(box); self.roi_fields={}

        for col,(key,default) in enumerate([("IntB",1100),("IntT",1300),("IntL",2300),("IntR",3300)]):

            s=QSpinBox(); s.setRange(1,100000); s.setValue(default); self.roi_fields[key]=s

            g.addWidget(QLabel(key),0,col); g.addWidget(s,1,col)

        b=QPushButton("Apply ROI"); b.setToolTip("Apply ROI and clear phase centers"); b.clicked.connect(self.apply_roi); g.addWidget(b,2,0,1,2)
        b=QPushButton("Take background"); b.clicked.connect(lambda:self.run("background")); g.addWidget(b,2,2,1,2); layout.addWidget(box)

        box=QGroupBox("Measurements"); row=QVBoxLayout(box)
        hint=QLabel("Use aliases and SHG actions in the standard Sweep editor. The preset captures a new spectrum before each record.")
        hint.setWordWrap(True); row.addWidget(hint)
        self.button(row,"Open SHG preset in Sweep editor",self.open_shg_sweep)
        layout.addWidget(box)

        layout.addStretch()

        self.left_tabs.insertTab(0,self._scroll(panel),"SHG"); self.left_tabs.setCurrentIndex(0)

        self.build_voltage_panel(); self.build_microscope_panel(); self.build_shg_plots()

        self.build_diagnostic()
        install_sweep_tools(self)

        self.resize(1600,1000)

        self.left_tabs.setMinimumWidth(460)

        self.data_tabs.setMinimumWidth(360)

        QTimer.singleShot(0,lambda:self.splitter.setSizes([560,760]))

        self.data_tabs.setCurrentWidget(self.shg_plot_widget)

        self.timer=QTimer(self); self.timer.setInterval(250); self.timer.timeout.connect(self.refresh); self.timer.start()

        self.preview_timer=QTimer(self); self.preview_timer.setInterval(200); self.preview_timer.timeout.connect(self.refresh_preview); self.preview_timer.start()

    @staticmethod

    def button(layout,text,callback):

        b=QPushButton(text); b.clicked.connect(callback); layout.addWidget(b); return b

    def group(self,layout,title,buttons):

        box=QGroupBox(title); grid=QGridLayout(box)

        for i,(text,callback) in enumerate(buttons):

            b=QPushButton(text); b.clicked.connect(callback); grid.addWidget(b,i//2,i%2)

        layout.addWidget(box)

    def error(self,text): self.status.setText("Error: "+str(text))

    def request(self,command,args=None,ok=None):

        self.conn.request(command,args,on_ok=ok,on_err=self.error)

    def run(self,operation,**kwargs):

        if operation in ("sample","reference","loop_sample","loop_reference"):

            self.data_tabs.setCurrentWidget(self.shg_plot_widget)

            self.pending_path="reference" if "reference" in operation else "sample"
            self.spectrum_tabs.setCurrentIndex(int(self.pending_path=="reference"))
            (self.reference_curve if self.pending_path=="reference" else self.sample_curve).clear()
            for view in self.live_cwt[self.pending_path]:view.clear()
            self.spectrum_status[self.pending_path].setText("Acquiring a new "+self.pending_path+" spectrum...")

        self.request("run_script_now",dict(code=action_code(operation,**kwargs),name="SHG "+operation),

                     lambda _:self.status.setText("Queued: "+operation+" â€” progress/errors in Queue and Log"))

    def write(self,name,value):

        # Manual physical changes go through the queue, not concurrent with a measurement.

        self.request("run_script_now",dict(code="set(%r, %r)"%(name,value),name="Set "+name))

    def action(self,ref): self.request("run_script_now",dict(code="action(%r)"%ref,name=ref))

    def jog(self,axis,direction):

        script="set(%r, get(%r) + %r)"%(axis,axis,direction*self.step.value())

        self.request("run_script_now",dict(code=script,name="Jog "+axis))

    def load_setup(self):

        port=self.cygnus.text().strip()

        if port.upper()=="COM5" and not self.simulate.isChecked(): self.error("COM5 is assigned to the half-wave rotator; choose the actual Cygnus port"); return

        config=None if self.simulate.isChecked() else laboratory_setup(cygnus_port=port)
        if config is None:
            config=setup(self.simulate.isChecked(),port)
            if not self.simulate.isChecked():
                names={"camera","stage","rotator"} | ({"supply"} if port else set())
                config["instruments"]=[i for i in config["instruments"] if i["name"] in names]
                config["aliases"]=[a for a in config["aliases"] if a["target"].split('.')[0] in names]
        self.request("load_setup",dict(setup=config),

            lambda problems:self.status.setText("Setup loaded" if not problems else "Setup problems: "+str(problems)))

        self.last_id=-1; self.mode_next=0

    def apply_roi(self):

        d={key:s.value() for key,s in self.roi_fields.items()}

        if d["IntB"]>d["IntT"] or d["IntL"]>d["IntR"]: self.error("Invalid ROI order"); return

        values=[d[k] for k in ("IntB","IntT","IntL","IntR")]

        script="set('camera.roi',%r)"%values

        self.request("run_script_now",dict(code=script,name="Set SHG ROI"))

    def open_shg_sweep(self, preset_key='shg_halfwave'):
        # Loading the preset is an editor-only action, never a queue submission.
        if not isinstance(preset_key, str):
            preset_key = 'shg_halfwave'  # QPushButton.clicked may supply a bool.
        options = {key:spin.value() for key,spin in self.shg_z_fields.items()} if preset_key == 'shg_halfwave_mems_z' else {}
        try:
            definition = preset(preset_key, **options)
        except ValueError as exc:
            self.editor.status.setText(str(exc))
            return
        self.editor.load_definition(definition)
        index = self.shg_preset_choice.findData(preset_key)
        if index >= 0:
            self.shg_preset_choice.setCurrentIndex(index)
        self.left_tabs.setCurrentWidget(self._scrolled[self.editor])
        label = self.shg_preset_choice.currentText()
        self.status.setText(label + " loaded in Sweep editor. Review channels, limits, calibration and outputs, then Validate and Add to queue.")
        self.editor.status.setText(label + " loaded for review; no measurement started.")

    def insert_shg_action(self):
        try:
            add_action(self.editor,self.shg_action_choice.currentData(),self.shg_action_position.currentData())
            self.editor.status.setText("SHG action inserted; review its position before queueing.")
        except ValueError as exc:
            self.editor.status.setText(str(exc))

    def build_shg_plots(self):

        widget=QWidget(); layout=QVBoxLayout(widget)

        self.plot_mode_label=QLabel("Checking loaded instruments..."); self.plot_mode_label.setWordWrap(True); layout.addWidget(self.plot_mode_label)
        self.acquisition_readback=QLabel("Camera disconnected"); self.acquisition_readback.setWordWrap(True); layout.addWidget(self.acquisition_readback)
        self.metrics=QLabel("Acquire sample/reference and select phase centers."); layout.addWidget(self.metrics)

        self.spectrum_tabs=QTabWidget()

        self.spectrum_plot=pg.PlotWidget(title="Sample spectrum")

        self.reference_plot=pg.PlotWidget(title="Reference spectrum")

        for plot in (self.spectrum_plot,self.reference_plot):

            plot.setLabel("bottom","Sensor pixel");plot.setLabel("left","Integrated counts")

        self.sample_curve=self.spectrum_plot.plot(pen="#3bc6d1")

        self.reference_curve=self.reference_plot.plot(pen="#aa88ff")

        self.reference_plot.setXRange(2000,3500,padding=0)

        self.live_cwt={};self.spectrum_status={}
        for path,plot in [("sample",self.spectrum_plot),("reference",self.reference_plot)]:
            page=QWidget();column=QVBoxLayout(page)
            label=QLabel("No "+path+" spectrum acquired");column.addWidget(label)
            self.spectrum_status[path]=label
            column.addWidget(plot,3)
            maps=[]
            for title in ("CWT magnitude (0.01â€“0.1 cycles/pixel)","CWT phase (radians)"):
                column.addWidget(QLabel(title))
                view=CWTView(plot,phase="phase" in title)
                column.addWidget(view,2);maps.append(view)
            self.live_cwt[path]=maps
            self.spectrum_tabs.addTab(page,path.title())
        layout.addWidget(self.spectrum_tabs)
        self.shg_plot_widget=widget
        self.data_tabs.addTab(widget,"SHG spectra + CWT")
        phase_widget=QWidget();phase_layout=QVBoxLayout(phase_widget)
        self.phase_widget=phase_widget
        self.history_plot=pg.PlotWidget(title="Corrected sample phase (radians)")
        self.phase_curve=self.history_plot.plot(pen="#f4bb57")
        self.phase_history=[];phase_layout.addWidget(self.history_plot)
        self.fit_plot=pg.PlotWidget(title="Half-wave retardation fit (radians)");phase_layout.addWidget(self.fit_plot)
        self.data_tabs.addTab(phase_widget,"Phase history & calibration")

    def refresh(self):

        if not self.conn.is_connected or self.polling:return

        if time.monotonic()>=self.mode_next:
            self.mode_next=time.monotonic()+2
            self.conn.request("list_instruments",{},on_ok=self.update_mode,on_err=lambda e:None)
        self.polling=True

        def received(raw):

            self.polling=False

            try:info=json.loads(raw)

            except (ValueError,TypeError):return

            self.acquisition_readback.setText("Exposure %g s | Gain %g | %s | ROI %s"%(info["exposure"],info["gain"],info["background"],info["hardware_roi"]))

            def metric(phase,amp):

                if phase is None or amp is None or not np.isfinite([phase,amp]).all():return "phase/amplitude: calibrate first"

                return "phase %.3fÂ° | amplitude %.6g"%(phase*180/np.pi,amp)

            self.metrics.setText("Sample: %s\nReference: %s"%(metric(info["sample_phase"],info["sample_amplitude"]),metric(info["reference_phase"],info["reference_amplitude"])))

            fid=info["frame_id"]
            if fid==0 and self.last_id>0:self.clear_optical_plots()

            if fid>0 and fid!=self.last_id:

                self.last_id=fid

                path=info["path"]
                if self.pending_path is not None and path!=self.pending_path:return
                self.pending_path=None
                curve=self.reference_curve if path=="reference" else self.sample_curve
                self.conn.request("read_channel",dict(name="camera."+path+"_spectrum"),
                    on_ok=lambda v,c=curve:c.setData(np.arange(1,len(v)+1),v),on_err=self.error)
                for kind,view in zip(("magnitude","phase"),self.live_cwt[path]):
                    self.conn.request("read_channel",dict(name="camera."+path+"_cwt_"+kind),
                        on_ok=lambda v,w=view:w.set_cwt(v),on_err=self.error)
                timing=info.get("timing",{})
                self.spectrum_status[path].setText(path.title()+" spectrum | frame %s | acquisition %.2f s, analysis %.2f s, save %.2f s"%
                    (fid,timing.get("acquisition",0),timing.get("analysis",0),timing.get("save",0)))
                if fid>0:
                    self.spectrum_tabs.setCurrentIndex(int(path=="reference"))
                    if info["path"]=="sample" and info["sample_phase"] is not None and np.isfinite(info["sample_phase"]):

                        self.phase_history.append(info["sample_phase"]);self.phase_history=self.phase_history[-1000:]

                        self.phase_curve.setData(self.phase_history)

            if info["diagnostic_id"]>0 and info["diagnostic_id"]!=self.last_diagnostic_id:

                self.last_diagnostic_id=info["diagnostic_id"]

                self.show_diagnostic()

        self.conn.request("read_channel",dict(name="camera.acquisition_status"),on_ok=received,on_err=lambda e:setattr(self,"polling",False))

    def clear_optical_plots(self):
        self.last_id=-1;self.last_diagnostic_id=0
        self.sample_curve.clear();self.reference_curve.clear();self.diagnostic_curve.clear()
        self.cwt_magnitude.clear();self.cwt_phase.clear()
        for path,maps in self.live_cwt.items():
            for view in maps:view.clear()
            self.spectrum_status[path].setText("No "+path+" spectrum acquired in this connection")

    def update_mode(self,instruments):
        previous=self.loaded_mode
        self.loaded_mode,text=mode(instruments)
        if previous!=self.loaded_mode:self.clear_optical_plots()
        color="#ffb74d" if self.loaded_mode!="real" else "#80cbc4"
        for label in (self.mode_label,self.plot_mode_label,self.diagnostic_mode_label):
            label.setText(text);label.setStyleSheet("font-weight:bold; color:"+color)
        prefix="SIMULATED " if self.loaded_mode=="simulation" else ""
        self.spectrum_plot.setTitle(prefix+"Sample spectrum")
        self.reference_plot.setTitle(prefix+"Reference spectrum")

    def plot_retardation(self):

        self.data_tabs.setCurrentWidget(self.phase_widget)
        def show(coeff):

            x=np.linspace(0,340,500); self.fit_plot.clear(); self.fit_plot.plot(x,retardation(coeff,x),pen="#aa88ff")

        self.request("read_channel",dict(name="camera.retardation_coefficients"),show)

    def build_diagnostic(self):

        self.diagnostic_widget=QWidget();layout=QVBoxLayout(self.diagnostic_widget)
        self.diagnostic_mode_label=QLabel("Checking loaded instruments...");self.diagnostic_mode_label.setWordWrap(True);layout.addWidget(self.diagnostic_mode_label)

        self.diagnostic_label=QLabel("Single-spectrum diagnostic: 0.01â€“0.333 cycles/pixel (separate from calibrated phase)")

        layout.addWidget(self.diagnostic_label)

        self.diagnostic_spectrum_plot=pg.PlotWidget(title="Single sample spectrum")

        self.diagnostic_spectrum_plot.setLabel("bottom","Sensor pixel")

        self.diagnostic_curve=self.diagnostic_spectrum_plot.plot(pen="#3bc6d1")

        layout.addWidget(self.diagnostic_spectrum_plot)

        self.cwt_magnitude=CWTView(self.diagnostic_spectrum_plot,diagnostic=True)
        self.cwt_phase=CWTView(self.diagnostic_spectrum_plot,diagnostic=True,phase=True)

        layout.addWidget(QLabel("CWT magnitude"));layout.addWidget(self.cwt_magnitude)

        layout.addWidget(QLabel("CWT phase (radians)"));layout.addWidget(self.cwt_phase)

        self.data_tabs.addTab(self.diagnostic_widget,"CWT diagnostic")

    def show_diagnostic(self):

        self.request("read_channel",dict(name="camera.diagnostic_spectrum"),lambda v:self.diagnostic_curve.setData(np.arange(1,len(v)+1),v))

        for name,view in [("diagnostic_magnitude",self.cwt_magnitude),("diagnostic_phase_map",self.cwt_phase)]:

            self.request("read_channel",dict(name="camera."+name),lambda v,w=view:w.set_cwt(v))

        self.request("read_channel",dict(name="camera.diagnostic_summary"),lambda v:self.diagnostic_label.setText("Peak amplitude %.6g | phase %.3fÂ° | period %.3f pixels"%(v[0],v[1]*180/np.pi,v[2])))

        self.data_tabs.setCurrentWidget(self.diagnostic_widget)

    def load_calibration(self):

        filename,_=QFileDialog.getOpenFileName(self,"Select SHG calibration",str(ROOT/"state"),"Calibration (*.json)")

        if filename:

            script="set('camera.calibration_file',%r)\naction('camera.load_calibration')"%filename

            self.request("run_script_now",dict(code=script,name="Load SHG calibration"))

    def build_voltage_panel(self):

        widget=QWidget(); layout=QVBoxLayout(widget)

        layout.addWidget(QLabel("Setpoint, current limit and output enable are separate controls."))

        grid=QGridLayout(); layout.addLayout(grid)

        for col,label in enumerate(["Channel","Voltage (V)","Current limit (A)","","","",""]):grid.addWidget(QLabel(label),0,col)

        config=laboratory_setup() or setup(True)
        rows=voltage_rows(config)
        for row,(name,prefix,suffix,lo,hi,max_current) in enumerate(rows,1):

            v=QDoubleSpinBox();v.setRange(lo,hi);v.setDecimals(4)

            limit=QDoubleSpinBox();limit.setDecimals(8);limit.setRange(0,max_current);limit.setValue(.0000001)

            grid.addWidget(QLabel(name),row,0);grid.addWidget(v,row,1);grid.addWidget(limit,row,2)

            out=prefix+".output"+suffix; lim=prefix+(".Ilimit"+suffix if suffix else ".Icompliance")

            for col,text,callback in [(3,"Set V",lambda _,n=name,w=v:self.write(n,w.value())),

                (4,"Set limit",lambda _,n=lim,w=limit:self.write(n,w.value())),

                (5,"ON",lambda _,n=out:self.write(n,"ON")),(6,"OFF",lambda _,n=out:self.write(n,"OFF"))]:

                b=QPushButton(text);b.clicked.connect(callback);grid.addWidget(b,row,col)

        for instrument in config["instruments"]:
            if instrument["driver"]=="cygnus16430":
                self.button(layout,"All Cygnus outputs OFF",lambda _=False,n=instrument["name"]:self.action(n+".all_off"))

        layout.addStretch();self.left_tabs.addTab(self._scroll(widget),"Voltages")

    def build_microscope_panel(self):

        widget=QWidget(); layout=QVBoxLayout(widget); row=QHBoxLayout();layout.addLayout(row)

        self.button(row,"Connect microscope",self.connect_microscope)

        self.button(row,"Start preview",lambda:setattr(self,"preview",True))

        self.button(row,"Stop preview",lambda:setattr(self,"preview",False))

        self.button(row,"Disconnect",self.disconnect_microscope)

        self.button(row,"Save snapshot",self.snapshot)

        self.image_view=pg.ImageView();layout.addWidget(self.image_view)

        row=QHBoxLayout();layout.addLayout(row)

        self.button(row,"Circle measurement",self.add_circle)

        self.circle_text=QLabel("Circle radius in sensor pixels");row.addWidget(self.circle_text)

        self.button(row,"Auto-exposure ROI",self.add_ae_roi)

        self.button(row,"Apply AE ROI",self.apply_ae_roi)

        controls=QHBoxLayout();layout.addLayout(controls)

        auto=QCheckBox("Auto exposure");auto.toggled.connect(lambda v:self.write("microscope.auto_exposure","ON" if v else "OFF"));controls.addWidget(auto)

        for title,name,lo,hi,value in [("Exposure (s)","exposure",.000001,10,.01),("Gain","gain",0,10000,100),("AE target","target",16,220,120)]:

            controls.addWidget(QLabel(title));s=QDoubleSpinBox();s.setDecimals(6 if name=="exposure" else 0);s.setRange(lo,hi);s.setValue(value);controls.addWidget(s)

            self.button(controls,"Set",lambda _,n=name,w=s:self.write("microscope."+n,w.value()))

        self.data_tabs.addTab(self._scroll(widget),"Microscope")

    def connect_microscope(self):

        def connected(_):

            def size(v):self.sensor_size=v;self.preview=True

            self.request("read_channel",dict(name="microscope.sensor_size"),size)

        self.request("add_instrument",microscope(self.simulate.isChecked()),connected)

    def disconnect_microscope(self):

        self.preview=False;self.request("remove_instrument",dict(name="microscope"))

    def refresh_preview(self):

        if not self.preview or self.preview_busy or not self.conn.is_connected:return

        self.preview_busy=True

        def frame(value):

            first=self.last_frame is None

            self.preview_busy=False;self.last_frame=np.asarray(value)

            h,w=self.last_frame.shape[:2]

            scale=(self.sensor_size[0]/w,self.sensor_size[1]/h) if self.sensor_size is not None else (1,1)

            self.image_view.setImage(self.last_frame,axes=dict(x=1,y=0,c=2),scale=scale,autoRange=first,autoLevels=False,levels=(0,255))

        def failure(e):self.preview_busy=False;self.preview=False;self.error(e)

        self.conn.request("read_channel",dict(name="microscope.preview"),on_ok=frame,on_err=failure)

    def snapshot(self):

        if self.last_frame is None:self.error("Start microscope preview first");return

        path,_=QFileDialog.getSaveFileName(self,"Save microscope snapshot",str(ROOT/"data"/(str(time.time_ns())+"_microscope.png")),"PNG (*.png)")

        if path:

            self.request("save_microscope_snapshot",dict(path=path),lambda result:self.status.setText("Saved full-resolution snapshot: "+result["path"]))

    def add_circle(self):

        view=self.image_view.getView()

        if self.circle is not None:view.removeItem(self.circle)

        self.circle=pg.CircleROI([100,100],[100,100],pen="#ffcc44");view.addItem(self.circle)

        self.circle.sigRegionChanged.connect(lambda:self.circle_text.setText("Radius %.2f pixels"%(self.circle.size().x()/2)))

    def add_ae_roi(self):

        if self.ae_rectangle is not None:self.image_view.getView().removeItem(self.ae_rectangle)

        self.ae_rectangle=pg.RectROI([50,50],[200,200],pen="#44ddff");self.image_view.getView().addItem(self.ae_rectangle)

    def apply_ae_roi(self):

        if self.ae_rectangle is None:return

        p,s=self.ae_rectangle.pos(),self.ae_rectangle.size()

        self.write("microscope.ae_roi",[round(p.x()),round(p.y()),round(s.x()),round(s.y())])

    def start_backend(self,force=False):

        import subprocess,sys

        subprocess.Popen([sys.executable,"-m","shg_addon.backend"],cwd=str(ROOT.parent),

            creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
