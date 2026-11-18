import ctypes as ct
import json
import numpy as np
import pytest
from shg_addon.shg_spectrometer import SHGSpectrometer
from shg_addon.workflows import run,acquire

@pytest.fixture
def camera():
 d=SHGSpectrometer('camera','NONE',dict(simulate=True,save_acquisitions=False));d.connect()
 yield d
 d.disconnect()

def test_hardware_roi_buffer_and_padding(camera,monkeypatch):
 camera.height=3672
 calls=[]
 def check(name,*args):
  calls.append((name,args))
  if name=='ASIGetExpStatus':ct.cast(args[1],ct.POINTER(ct.c_int))[0]=2
  if name=='ASIGetDataAfterExp':
   assert args[2]==1000*200*2
   np.ctypeslib.as_array(ct.cast(args[1],ct.POINTER(ct.c_uint16)),shape=(200000,))[:]=7
 camera.options['simulate']=False;camera.camera_id=0
 monkeypatch.setattr(camera,'_check',check)
 monkeypatch.setattr(camera,'_control',lambda k:1000000)
 camera.configure_roi([1100,1300,2300,3300])
 assert camera.hardware_roi==(2296,1098,1000,200)
 f=camera._frame()
 assert f.shape==(3672,5496)
 assert f.sum()==7*200000
 assert np.all(f[1098:1298,2296:3296]==7)
 assert np.all(f[1099:1300].sum(axis=0)[2296:3296]==7*199)
 camera.options['simulate']=True

def test_background_recipe_retention_and_failure(camera,monkeypatch):
 exposures=[]
 def frame():
  e=camera._control(1);exposures.append(e)
  return np.full((camera.height,camera.width),10+e/1e6*3.)
 monkeypatch.setattr(camera,'_frame',frame)
 camera.write('exposure',1.)
 camera._action_capture_background()
 assert exposures==[32]*25+[1000000]*10
 np.testing.assert_allclose(camera.background,13.)
 camera.write('exposure',2.)
 np.testing.assert_allclose(camera.read('image'),3.)
 assert 'without rescaling' in camera.read('acquisition_status')
 camera.write('roi',[32,96,2310,3310]);assert camera.background is None
 def fail():raise IOError('camera failure')
 monkeypatch.setattr(camera,'_frame',fail)
 with pytest.raises(IOError):camera._action_capture_background()
 assert camera.read('exposure')==2. and camera.background is None

def test_loop_exposure_and_conditional_mirror():
 values={'SampRef':0,'halfwave':28};writes=[];sleeps=[];frames=[]
 def set_(n,v):values[n]=v;writes.append((n,v))
 class Stop(Exception):pass
 def check():
  if len(frames)>=2:raise Stop()
 ns={'check':check,'set':set_,'get':lambda n:values[n],'sleep':sleeps.append,'action':frames.append}
 with pytest.raises(Stop):run(ns,'loop_sample')
 assert ('camera.exposure',1.) in writes and len(frames)==2
 assert not any(n=='SampRef' for n,v in writes) and 1.5 not in sleeps
 frames.clear();writes.clear();sleeps.clear()
 with pytest.raises(Stop):run(ns,'loop_reference')
 assert writes.count(('SampRef',1))==1 and sleeps.count(1.5)==1

def test_background_workflow_order():
 calls=[]
 ns={'set':lambda *a:calls.append(('set',)+a),'sleep':lambda x:calls.append(('sleep',x)), 'action':lambda x:calls.append(('action',x))}
 run(ns,'background')
 assert calls==[('set','Shutter',1),('sleep',.5),('set','camera.exposure',1.),('action','camera.clear_background'),('action','camera.capture_background'),('set','Shutter',0)]

def test_diagnostic_snapshot(camera):
 camera._action_capture_sample();camera._action_analyze_single()
 assert json.loads(camera.read('acquisition_status'))['diagnostic_id']==1
 np.testing.assert_array_equal(camera.read('diagnostic_spectrum'),camera.read('sample_spectrum'))
 assert camera.read('diagnostic_magnitude').shape[1]==5496
