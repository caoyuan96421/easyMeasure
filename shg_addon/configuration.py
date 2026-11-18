from pathlib import Path
import json

ROOT=Path(__file__).resolve().parent

def setup(simulate=False,cygnus_port=""):
    session="simulation" if simulate else "laboratory"
    inst=[dict(name="camera",driver="shg_spectrometer",address="NONE",options=dict(simulate=simulate,
        calibration_path=str(ROOT/"state"/session/"calibration.json"),data_dir=str(ROOT/"data"/session/"frames"))),
        dict(name="stage",driver="ni6211",address="DAQ::SIM" if simulate else "DAQ::Dev1",
             options=dict(start_counters=False,ao_min=0.,ao_max=10.,digital_readback="commanded")),
        dict(name="rotator",driver="newport_smc100",address="NONE" if simulate else "COM5",options=dict(simulate=simulate)),
        dict(name="gate",driver="keithley2400",address="SIM::shg_gate" if simulate else "GPIB0::5::INSTR",options={}),
        dict(name="coordinates",driver="shg_coordinates",address="NONE",options={})]
    aliases=[dict(name="X",target="stage.ao0",min=0,max=100,scale=10),
        dict(name="Y",target="stage.ao1",min=0,max=100,scale=10),
        dict(name="Shutter",target="stage.do0",allowed=[0,1]),
        dict(name="SampRef",target="stage.do1",allowed=[0,1]),
        dict(name="halfwave",target="rotator.angle",min=0,max=340),
        dict(name="Vbg",target="gate.voltage",min=-75,max=75,ramp_rate=10),
        dict(name="Ibg",target="gate.current")]
    for name in ("x","y","polarization"):
        lo,hi=(-56,624) if name=="polarization" else (0,100)
        aliases.append(dict(name=name,target="coordinates."+name,min=lo,max=hi))
    for alias,ch in dict(ZWOSpectrum="sample_spectrum",ZWORefSpectrum="reference_spectrum",
        ZWOAmplitude="amplitude",ZWORefAmplitude="reference_amplitude",ZWOPhase="phase",
        ZWORefPhase="reference_phase",SHGframe="frame_id").items():
        aliases.append(dict(name=alias,target="camera."+ch))
    if simulate or cygnus_port:
        inst.append(dict(name="supply",driver="cygnus16430",address="SIM::shg_supply" if simulate else cygnus_port,options={}))
        for k in range(1,9):
            aliases.append(dict(name="V%d"%k,target="supply.voltage%d"%k,min=0,max=150,ramp_rate=1))
    return dict(format="easyMeasure-setup-1",instruments=inst,aliases=aliases,composites=[],monitor=[])

def microscope(simulate=False):
    return dict(name="microscope",driver="toup_microscope",address="NONE",options=dict(simulate=simulate))
