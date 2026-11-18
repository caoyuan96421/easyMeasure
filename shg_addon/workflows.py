"""Run on the easyMeasure measurement queue, with pause/abort checkpoints."""
import math
import numpy as np
from shg_addon.analysis import circular_mean, fit_retardation
from shg_addon.spot_tracking import corrected_xy, before_physical_point


def acquire(ns,path="sample",exposure=None,force_switch=False):
    ns["check"]()
    if "shg_require_optics" in ns:ns["shg_require_optics"]()
    if exposure is not None: ns["set"]("camera.exposure",exposure)
    target=int(path=="reference")
    if force_switch or ns["get"]("SampRef") != target:
        ns["set"]("SampRef",target)
        ns["sleep"](1.5)
    ns["set"]("camera.halfwave_angle",ns["get"]("halfwave"))
    ns["action"]("camera.capture_"+path)


def refresh_reference(ns,exposure=.25):
    original=ns["get"]("camera.exposure")
    try: acquire(ns,"reference",exposure)
    finally:
        ns["cleanup_set"]("camera.exposure",original)
        ns["cleanup_set"]("SampRef",0)
    ns["state"]["shg_last_reference"]=ns["time"].monotonic()


def before_point(ns,corrected=False):
    if corrected:
        run(ns,"corrected_move",x=ns["get"]("x"),y=ns["get"]("y"),polarization=ns["get"]("polarization"))
    else:
        before_physical_point(ns)
    cfg=ns["state"].get("shg_autocal",{})
    if cfg.get("enabled") and ns["time"].monotonic()-ns["state"].get("shg_last_reference",-math.inf)>=cfg.get("interval",300):
        refresh_reference(ns,cfg.get("exposure",.25))
    acquire(ns,"sample")


def run(ns,operation,**kwargs):
    if operation in ("sample","reference"):
        acquire(ns,operation,kwargs.get("exposure"),force_switch=True); return
    if operation=="single_diagnostic":
        acquire(ns,"sample",1.)
        ns["action"]("camera.analyze_single")
        return
    if operation.startswith("loop_"):
        path=operation[5:]
        if path not in ("sample","reference"): raise ValueError("Unknown loop path")
        ns["set"]("camera.exposure",1.)
        while True:
            ns["check"]()
            acquire(ns,path)
            ns["sleep"](.1)
    elif operation=="autocal":
        ns["state"]["shg_autocal"]=dict(enabled=kwargs.get("enabled",True),interval=300,exposure=.25)
    elif operation=="refresh_reference": refresh_reference(ns,kwargs.get("exposure",5.))
    elif operation=="background":
        ns["set"]("Shutter",1)
        ns["sleep"](.5)
        ns["set"]("camera.exposure",1.)
        ns["action"]("camera.clear_background")
        # Leave light blocked on failure/abort; reopen only after successful capture.
        ns["action"]("camera.capture_background")
        ns["set"]("Shutter",0)
    elif operation in ("sample_center","reference_center"):
        path=operation.split("_")[0]
        acquire(ns,path,1. if path=="sample" else 2.)
        ns["action"]("camera.select_"+path+"_center")
    elif operation=="calibration_phase":
        ns["set"]("halfwave",28.)
        ns["set"]("camera.calibration_phase",0.)
        phases=[]
        original=ns["get"]("camera.exposure")
        try:
            for _ in range(5):
                acquire(ns,"reference",5.)
                acquire(ns,"sample",1.)
                phases.append(ns["get"]("camera.phase"))
            if not np.isfinite(phases).all(): raise ValueError("Calibrate sample and reference centers first")
            offset=circular_mean(phases)
            ns["set"]("camera.calibration_phase",offset)
            ns["action"]("camera.save_calibration")
            ns["log"]("SHG calibration offset: %.8f radians from five pairs",offset)
        finally:
            ns["cleanup_set"]("camera.exposure",original)
            ns["cleanup_set"]("SampRef",0)
    elif operation=="calibrate_all":
        for step in ("sample_center","reference_center","calibration_phase"): run(ns,step)
    elif operation=="measure_retardation":
        ns["set"]("camera.retardation_coefficients",[0.,0.,0.,0.])
        angles=np.linspace(28,328,11); phases=[]
        original=ns["get"]("camera.exposure")
        try:
            for i,angle in enumerate(angles):
                ns["set"]("halfwave",float(angle)); acquire(ns,"sample",10.)
                phase=ns["get"]("camera.phase")-(np.pi if i%2 else 0)
                phases.append(float(np.angle(np.exp(1j*phase))))
            ns["state"]["shg_retardation"]=(angles.tolist(),phases)
            from shg_addon.configuration import ROOT
            root=ROOT/"data"/"retardation";root.mkdir(parents=True,exist_ok=True)
            with (root/(str(ns["time"].time_ns())+".npz")).open("xb") as stream:
                np.savez(stream,angles=angles,phases=phases)
            run(ns,"fit_retardation")
        finally: ns["cleanup_set"]("camera.exposure",original)
    elif operation=="fit_retardation":
        angles,phases=ns["state"]["shg_retardation"]
        coeff,error=fit_retardation(angles,phases)
        ns["set"]("camera.retardation_coefficients",coeff)
        ns["action"]("camera.save_calibration")
        ns["log"]("Retardation coefficients %s; squared residual %g",coeff,error)
    elif operation=="corrected_move":
        x,y,angle=corrected_xy(kwargs["x"],kwargs["y"],kwargs["polarization"])
        if not (0<=x<=100 and 0<=y<=100 and 0<=angle<=340): raise ValueError("Corrected move exceeds stage/rotator bounds")
        ns["set"]("X",float(x)); ns["set"]("Y",float(y)); ns["set"]("halfwave",float(angle))
    else: raise ValueError("Unknown SHG operation "+operation)


def code(operation,**kwargs):
    return "from shg_addon.workflows import run\nrun(globals(), %r, **%r)"%(operation,kwargs)


def sweep(channel,start,stop,points,delay=.1):
    corrected=channel in ("x","y","polarization")
    return dict(name="SHG_"+channel,dimensions=[dict(points=points,
        sweep=[dict(channel=channel,start=start,stop=stop)],
        record=["ZWOSpectrum","ZWOAmplitude","ZWOPhase","ZWORefPhase","ZWORefAmplitude","SHGframe"],
        pre_delay=delay,hooks=dict(pre_record=dict(enabled=True,code=
            "from shg_addon.workflows import before_point\nbefore_point(globals(), corrected=%r)"%corrected)))],
        pre=dict(hook="if get('camera.analysis_status') != 'calibrated': raise ValueError('Calibrate SHG first')"),
        on_read_error="abort",on_set_error="abort",run_post_on_abort=True,
        metadata=dict(analysis="Morse CWT, reference and retardation corrected",phase_unit="rad"))
