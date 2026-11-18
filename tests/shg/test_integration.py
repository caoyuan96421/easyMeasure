import importlib.util
import json
import os
from pathlib import Path
import numpy as np
import pytest
from shg_addon.analysis import baseline,circular_mean,fit_retardation,retardation,morse_cwt,strongest,extract,corrected_xy
from shg_addon.configuration import setup,ROOT
from shg_addon.backend import SHGBackend
from shg_addon.workflows import run,before_point,sweep

@pytest.fixture
def backend(tmp_path):
    b=SHGBackend(home=tmp_path/"home",log_to_console=False)
    config=setup(True)
    config["instruments"][0]["options"].update(save_acquisitions=False,calibration_path=str(tmp_path/"calibration.json"))
    problems=b.load_setup(setup=config)
    assert problems==[],problems
    yield b
    b.close()

def namespace(b):
    ns=b.scripts.namespace("test")
    ns["sleep"]=lambda seconds:None
    return ns

def test_matlab_cwt_equivalence():
    n=5496
    golden=np.load(Path(__file__).with_name("fixtures")/"synthetic_cwt_golden.npz",allow_pickle=False)
    signals=golden["cwt_signals"].reshape(3,n)
    for k,y in enumerate(signals,1):
        ref=golden[f"cwt_reference_{k}_real"].reshape(34,n)+1j*golden[f"cwt_reference_{k}_imag"].reshape(34,n)
        cc,f=morse_cwt(y)
        np.testing.assert_allclose(cc,ref,atol=1e-12,rtol=1e-12)
        assert strongest(cc)==strongest(ref)
    cc,_=morse_cwt(signals[0],diagnostic=True)
    ref=golden["diagnostic_reference_real"].reshape(-1,n)+1j*golden["diagnostic_reference_imag"].reshape(-1,n)
    np.testing.assert_allclose(cc,ref,atol=1e-12,rtol=1e-12)

def test_baseline_matlab_indexing():
    y=np.arange(600,dtype=float); expected=y.copy()
    expected[96:396]-=np.sort(y[96:396])[49:100].mean()
    np.testing.assert_array_equal(baseline(y,100,400),expected)

def test_circular_average_and_retardation():
    assert abs(np.angle(np.exp(1j*circular_mean([.01,2*np.pi-.01]))))<1e-12
    x=np.linspace(28,328,11); truth=[-.6,.8,.02,1.2]
    coefficients,error=fit_retardation(x,retardation(truth,x))
    np.testing.assert_allclose(retardation(coefficients,x),retardation(truth,x),atol=1e-7)

def test_configuration_mapping():
    c=setup(False)
    a={v["name"]:v for v in c["aliases"]}
    assert a["X"]["scale"]==10 and a["X"]["target"]=="stage.ao0"
    assert a["Shutter"]["target"]=="stage.do0"
    assert not any(i["name"]=="supply" for i in c["instruments"])

def test_driver_guard_defaults_reach_backend(backend):
    h=backend.instruments.get("camera")
    assert h.call_timeout==90 and h.options["retries"]==0
    r=backend.instruments.get("rotator")
    assert r.options["baudrate"]==921600 and r.options["xonxoff"] is True

def test_output_controls_available_without_hardware_access(backend,monkeypatch):
    from easymeasure.backend.instrument import InstrumentManager,InstrumentHandle
    assert type(backend.instruments) is InstrumentManager
    calls=[]
    monkeypatch.setattr(backend.resources,"acquire",lambda *a,**k:calls.append(a))
    for name,driver,address in [("real_gate","keithley2400","GPIB0::5::INSTR"),
        ("real_supply","cygnus16430","COM6"),("real_stage","ni6211","DAQ::Dev1"),
        ("real_rotator","newport_smc100","COM5")]:
        backend.add_instrument(name,driver,address,connect=False)
        handle=backend.instruments.get(name)
        assert type(handle) is InstrumentHandle
        assert handle.resource is None
    assert calls==[]


def test_full_calibration_and_cached_results(backend):
    ns=namespace(backend)
    run(ns,"calibrate_all")
    assert backend.read_channel("camera.analysis_status")=="calibrated"
    fid=backend.read_channel("SHGframe")
    phase=backend.read_channel("ZWOPhase")
    assert abs(np.angle(np.exp(1j*phase)))<1e-10
    assert backend.read_channel("ZWOAmplitude")>0
    assert backend.read_channel("ZWOSpectrum").shape==(5496,)
    assert backend.read_channel("SHGframe")==fid
    run(ns,"single_diagnostic")
    assert backend.read_channel("camera.diagnostic_magnitude").shape[1]==5496
    assert np.isfinite(backend.read_channel("camera.diagnostic_summary")).all()

def test_reference_refresh_restores_exposure_and_path(backend):
    ns=namespace(backend)
    run(ns,"reference_center")
    backend.write_channel("camera.exposure",.7)
    run(ns,"refresh_reference",exposure=.25)
    assert backend.read_channel("camera.exposure")==.7
    assert backend.read_channel("SampRef")==0

def test_autocal_first_point(backend):
    ns=namespace(backend);run(ns,"calibrate_all"); run(ns,"autocal")
    fid=backend.read_channel("SHGframe")
    before_point(ns)
    assert backend.read_channel("SHGframe")==fid+2
    before_point(ns)
    assert backend.read_channel("SHGframe")==fid+3

def test_background_failure_stays_blocked(backend):
    ns=namespace(backend)
    ns["action"]=lambda ref:(_ for _ in ()).throw(RuntimeError("failed frame"))
    with pytest.raises(RuntimeError):run(ns,"background")
    assert backend.read_channel("Shutter")==1

def test_reference_failure_restores_exposure(backend):
    ns=namespace(backend);backend.write_channel("camera.exposure",.7)
    ns["action"]=lambda ref:(_ for _ in ()).throw(RuntimeError("failed frame"))
    with pytest.raises(RuntimeError):run(ns,"refresh_reference",exposure=.25)
    assert backend.read_channel("camera.exposure")==.7
    assert backend.read_channel("SampRef")==0

def test_sweep_validation(backend):
    result=backend.validate_sweep(sweep("V1",0,1,3))
    assert result.get("valid",result.get("ok",False)),result

def test_actual_simulated_sweep_storage(backend):
    ns=namespace(backend);run(ns,"calibrate_all")
    result=backend._execute_sweep_item(sweep("V1",0,.1,3,delay=0),{"id":None})
    assert result["status"]=="finished",result
    from easymeasure.backend.storage import DataSet
    data=DataSet.load(result["path"])
    assert data.data["ZWOSpectrum"].shape==(3,5496)
    assert np.isfinite(data.data["ZWOPhase"]).all()
    np.testing.assert_array_equal(np.diff(data.data["SHGframe"]),[1,1])

def test_rotator_error_and_timeout():
    from shg_addon.newport_smc100 import NewportSMC100
    class Port:
        def query(self,cmd):return "1TS000032"
    driver=NewportSMC100("rotator","COM5");driver.resource=Port()
    assert driver.read("status")==50
    driver.resource.query=lambda cmd:"1TS000132"
    with pytest.raises(IOError,match="error flags"):driver.read("status")

def test_retardation_workflow(backend):
    ns=namespace(backend);run(ns,"calibrate_all");run(ns,"measure_retardation")
    assert len(ns["state"]["shg_retardation"][0])==11
    assert np.isfinite(backend.read_channel("camera.retardation_coefficients")).all()

def test_calibration_does_not_overwrite(backend,tmp_path):
    ns=namespace(backend);run(ns,"calibrate_all")
    original=(tmp_path/"calibration.json").read_bytes()
    backend.write_channel("camera.save_calibration")
    assert (tmp_path/"calibration.json").read_bytes()==original
    assert len(list(tmp_path.glob("calibration*.json")))==2

def test_simulated_microscope(backend,tmp_path):
    backend.add_instrument("microscope","toup_microscope","NONE",dict(simulate=True))
    frame=backend.read_channel("microscope.image")
    assert frame.shape==(480,640,3)
    backend.write_channel("microscope.ae_roi",[10,10,100,100])
    np.testing.assert_array_equal(backend.read_channel("microscope.ae_roi"),[10,10,100,100])
    path=tmp_path/"snapshot.png"
    result=backend.save_microscope_snapshot(str(path))
    assert result["width"]==640 and path.exists()
    with pytest.raises(FileExistsError):backend.save_microscope_snapshot(str(path))

def test_corrected_xy():
    x,y,a=corrected_xy(50,50,0)
    assert 0<x<100 and 0<y<100 and a==28

def test_network_preview_and_snapshot(backend,tmp_path):
    from easymeasure.backend.server import Server
    from easymeasure.client import BackendClient
    backend.add_instrument("microscope","toup_microscope","NONE",dict(simulate=True))
    server=Server(backend,host="127.0.0.1",port=0,runtime_file=tmp_path/"runtime.json")
    server.start()
    client=BackendClient("127.0.0.1",server.port,server.token)
    try:
        assert client.call("read_channel",name="microscope.preview").shape==(480,640,3)
        result=client.call("save_microscope_snapshot",path=str(tmp_path/"network_snapshot.png"))
        assert result["width"]==640
    finally:client.close();server.stop()

def test_gui_startup(tmp_path,monkeypatch):
    import subprocess,sys
    env=dict(os.environ,QT_QPA_PLATFORM="offscreen",EASYMEASURE_HOME=str(tmp_path/"gui"))
    p=subprocess.run([sys.executable,"-m","shg_addon.gui_smoke",str(tmp_path/"gui.png")],env=env,capture_output=True,text=True,timeout=30)
    assert p.returncode==0,p.stdout+p.stderr
    assert (tmp_path/"gui.png").exists()
