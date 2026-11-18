"""Additive SHG camera driver; existing zwo_camera.py is unchanged."""
import json
import math
import time
from pathlib import Path
import numpy as np
from easymeasure.backend.instrument import ChannelSpec, ActionSpec
from easymeasure.common.errors import ChannelError
from shg_addon.zwo_camera import ZWOCamera, ControlCaps
from shg_addon.acquisition import SHGAcquisition
from shg_addon.analysis import baseline, morse_cwt, strongest, extract, retardation


class SHGSpectrometer(SHGAcquisition, ZWOCamera):
    DESCRIPTION = "SHG spectrometer: cached sample/reference spectra, Morse CWT amplitude and corrected phase"
    DEFAULT_OPTIONS = dict(ZWOCamera.DEFAULT_OPTIONS, simulate=False, calibration_path="",
                           matlab_baseline=True, save_acquisitions=True, data_dir="")
    CHANNELS = list(ZWOCamera.CHANNELS) + [
        ChannelSpec(n,"r","float",(-1,) if "spectrum" in n else (),unit)
        for n,unit in [("sample_spectrum","counts"),("reference_spectrum","counts"),
                       ("sample_phase_raw","rad"),("reference_phase","rad"),
                       ("phase","rad"),("amplitude","counts"),("reference_amplitude","counts"),
                       ("frame_id","" )]
    ] + [ChannelSpec("halfwave_angle","rw",unit="deg",config=True),
         ChannelSpec("calibration_phase","rw",unit="rad",config=True),
         ChannelSpec("retardation_coefficients","rw","float",(4,),config=True),
         ChannelSpec("calibration_file","rw","str",config=True),
         ChannelSpec("diagnostic_magnitude","r","float",(-1,-1),"counts"),
         ChannelSpec("diagnostic_phase_map","r","float",(-1,-1),"rad"),
         ChannelSpec("diagnostic_summary","r","float",(3,),"","amplitude, phase rad, period px"),
         ChannelSpec("roi","rw","float",(4,),"px","bottom, top, left, right",config=True),
         ChannelSpec("acquisition_status","r","str",config=True),
         ChannelSpec("diagnostic_spectrum","r","float",(-1,),"counts"),
         ChannelSpec("analysis_status","r","str",config=True)]
    CHANNELS += [ChannelSpec(path+"_cwt_"+kind,"r","float",(-1,-1),"counts" if kind=="magnitude" else "rad")
                 for path in ("sample","reference") for kind in ("magnitude","phase")]
    ACTIONS = list(ZWOCamera.ACTIONS) + [ActionSpec(n,n.replace("_"," "),timeout=90) for n in
        ("capture_sample","capture_reference","select_sample_center","select_reference_center",
         "clear_calibration","save_calibration","load_calibration","analyze_single")]

    def connect(self):
        if not self.options["simulate"]:
            super().connect()
        else:
            self.width,self.height=5496,128
            self.roi=dict(IntB=32,IntT=96,IntL=2300,IntR=3300)
            self.values={0:100,1:100000,8:-50,16:-5,17:0}
            self.caps={}
            for k in self.values:
                cap=ControlCaps(); cap.MinValue=-50; cap.MaxValue=100000000; cap.IsWritable=k!=8
                self.caps[k]=cap
            self.background=None
        if not self.options["simulate"]:
            self.roi=dict(IntB=1100,IntT=1300,IntL=2300,IntR=3300)
            if self.width<3300 or self.height<1300:
                self.roi=dict(IntB=1,IntT=self.height,IntL=4,IntR=self.width)
        self.centers={}
        self.calibration_phase=0.
        self.coefficients=[0.,0.,0.,0.]
        self.halfwave_angle=28.
        self.frame_id=0
        self.cache={}
        self.last_cc={}
        self.last_image=None
        self.path="sample"
        self.calibration_valid=False
        self.diagnostic_id=0
        self._action_clear_background()
        self.configure_roi([self.roi[k] for k in ("IntB","IntT","IntL","IntR")])
        if self.options.get("calibration_path") and Path(self.options["calibration_path"]).exists():
            self._action_load_calibration()

    def disconnect(self):
        if not self.options["simulate"]:
            super().disconnect()

    def _control(self,k):
        return self.values[k] if self.options["simulate"] else super()._control(k)

    def _set_control(self,k,v):
        if self.options["simulate"]:
            self.values[k]=v
        else:
            super()._set_control(k,v)

    def read(self,name):
        if "_cwt_" in name:
            path,kind=name.split("_cwt_")
            if path not in self.last_cc:raise ChannelError("Acquire "+path+" first")
            cc=self.last_cc[path]
            return np.abs(cc) if kind=="magnitude" else np.angle(cc)
        if name=="roi": return np.array([self.roi[k] for k in ("IntB","IntT","IntL","IntR")])
        if name=="acquisition_status": return self.acquisition_status()
        if name=="frame_id": return float(self.frame_id)
        if name=="calibration_file": return str(self.options.get("calibration_path",""))
        if name=="halfwave_angle": return self.halfwave_angle
        if name=="calibration_phase": return self.calibration_phase
        if name=="retardation_coefficients": return np.array(self.coefficients)
        if name=="analysis_status":
            ready=self.calibration_valid and set(self.centers)=={"sample","reference"} and "reference_phase" in self.cache
            return "calibrated" if ready else "calibrate centers, reference and phase offset before scientific use"
        if name in {c.name for c in self.CHANNELS[len(ZWOCamera.CHANNELS):]}:
            if name not in self.cache: raise ChannelError("Acquire/calibrate before reading "+name)
            return self.cache[name]
        return super().read(name)

    def write(self,name,value):
        if name=="roi":
            self.configure_roi(value)
            self.centers={}; self.last_cc={}; self.cache={}; self.calibration_valid=False
            return
        if name=="calibration_file":
            self.options["calibration_path"]=str(value); return
        if name in ("halfwave_angle","calibration_phase"):
            v=float(value)
            if not math.isfinite(v): raise ChannelError("Value must be finite")
            setattr(self,name,v)
            if name=="calibration_phase": self.calibration_valid=True
            self._correct_phase()
            return
        if name=="retardation_coefficients":
            a=np.asarray(value,float)
            if a.shape!=(4,) or not np.isfinite(a).all(): raise ChannelError("Four finite coefficients required")
            self.coefficients=a.tolist(); self._correct_phase(); return
        super().write(name,value)
        if name in self.roi:
            self.centers={}; self.last_cc={}; self.cache={}; self.calibration_valid=False

    def _correct_phase(self):
        if "sample_phase_raw" in self.cache:
            ref=self.cache.get("reference_phase",math.nan)
            self.cache["phase"]=float((self.cache["sample_phase_raw"]-ref-self.calibration_phase-
                retardation(self.coefficients,self.halfwave_angle))%(2*np.pi))

    def _capture(self,path):
        started=time.perf_counter()
        self.path=path
        frame=super().read("image")
        acquired=time.perf_counter()
        self.last_image=frame
        spectrum=frame[self.roi["IntB"]-1:self.roi["IntT"]].sum(axis=0)
        if self.options["matlab_baseline"]:
            spectrum=baseline(spectrum,self.roi["IntL"],self.roi["IntR"])
        cc,f=morse_cwt(spectrum)
        self.last_cc[path]=cc
        self.cache[path+"_spectrum"]=spectrum
        self.frame_id+=1
        if path in self.centers:
            phase,amplitude=extract(cc,self.centers[path])
            self.cache["sample_phase_raw" if path=="sample" else "reference_phase"]=phase
            self.cache["amplitude" if path=="sample" else "reference_amplitude"]=amplitude
        self._correct_phase()
        analyzed=time.perf_counter()
        if self.options["save_acquisitions"] and self.options["data_dir"]:
            root=Path(self.options["data_dir"]); root.mkdir(parents=True,exist_ok=True)
            metadata=dict(path=path,roi=self.roi,exposure=self._control(1)/1e6,gain=self._control(0),
                          halfwave=self.halfwave_angle,centers=self.centers,coefficients=self.coefficients,
                          calibration_phase=self.calibration_phase,background_applied=self.background is not None,
                          background_info=self.background_info,hardware_roi=self.hardware_roi,
                          phase_raw=self.cache.get("sample_phase_raw"),phase=self.cache.get("phase"),
                          reference_phase=self.cache.get("reference_phase"),amplitude=self.cache.get("amplitude"))
            with (root/(str(time.time_ns())+"_"+path+".npz")).open("xb") as stream:
                np.savez_compressed(stream,image_raw=self.last_raw,image_corrected=frame,spectrum=spectrum,metadata=json.dumps(metadata))

        self.last_timing=dict(acquisition=acquired-started,analysis=analyzed-acquired,
                              save=time.perf_counter()-analyzed,total=time.perf_counter()-started)

    def _action_capture_sample(self): self._capture("sample")
    def _action_capture_reference(self): self._capture("reference")

    def _action_analyze_single(self):
        if "sample_spectrum" not in self.cache: raise ChannelError("Acquire a sample spectrum first")
        cc,f=morse_cwt(self.cache["sample_spectrum"],diagnostic=True)
        center=strongest(cc);phase,amp=extract(cc,center)
        self.cache["diagnostic_magnitude"]=abs(cc)
        self.cache["diagnostic_phase_map"]=np.angle(cc)
        self.cache["diagnostic_summary"]=np.array([amp,phase,1/f[center[0]]])
        self.cache["diagnostic_spectrum"]=self.cache["sample_spectrum"].copy()
        self.diagnostic_id+=1

    def _select(self,path):
        if path not in self.last_cc: raise ChannelError("Acquire "+path+" first")
        self.centers[path]=strongest(self.last_cc[path])
        phase,amp=extract(self.last_cc[path],self.centers[path])
        self.cache["sample_phase_raw" if path=="sample" else "reference_phase"]=phase
        self.cache["amplitude" if path=="sample" else "reference_amplitude"]=amp
        self._correct_phase()

    def _action_select_sample_center(self): self._select("sample")
    def _action_select_reference_center(self): self._select("reference")
    def _action_clear_calibration(self):
        self.centers={}; self.coefficients=[0.]*4; self.calibration_phase=0.; self.calibration_valid=False
        self.cache={}; self.last_cc={}

    def _calibration_path(self):
        path=self.options.get("calibration_path")
        if not path: raise ChannelError("Set calibration_path in instrument options")
        return Path(path)

    def _action_save_calibration(self):
        p=self._calibration_path(); p.parent.mkdir(parents=True,exist_ok=True)
        if p.exists():
            import time
            p=p.with_name(p.stem+"_"+str(time.time_ns())+p.suffix)
        p.write_text(json.dumps(dict(version=1,width=self.width,roi=self.roi,centers=self.centers,
            calibration_phase=self.calibration_phase,coefficients=self.coefficients,
            calibrated=self.calibration_valid),indent=2))

    def _action_load_calibration(self):
        path=self._calibration_path()
        candidates=list(path.parent.glob(path.stem+"_*"+path.suffix))
        if path.exists(): candidates.append(path)
        if not candidates: raise ChannelError("No saved calibration found")
        d=json.loads(max(candidates,key=lambda p:p.stat().st_mtime_ns).read_text())
        if d["width"]!=self.width or d["roi"]!=self.roi: raise ChannelError("Calibration dimensions/ROI differ")
        if d.get("version")!=1 or set(d["centers"])!={"sample","reference"}:
            raise ChannelError("Calibration must contain sample and reference centers")
        for center in d["centers"].values():
            if len(center)!=2 or any(int(x)!=x for x in center) or not (0<=center[0]<34 and 0<=center[1]<self.width):
                raise ChannelError("Invalid calibration center")
        if len(d["coefficients"])!=4 or not np.isfinite(d["coefficients"]+[d["calibration_phase"]]).all():
            raise ChannelError("Invalid calibration coefficients")
        self.centers=d["centers"]; self.coefficients=d["coefficients"]
        self.calibration_phase=float(d["calibration_phase"]); self.calibration_valid=bool(d["calibrated"])


DRIVER=SHGSpectrometer
