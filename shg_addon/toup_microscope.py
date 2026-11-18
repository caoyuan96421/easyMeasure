import ctypes
import threading
import numpy as np
from easymeasure.backend.instrument import Instrument, ChannelSpec
from easymeasure.common.errors import ChannelError

class ToupMicroscope(Instrument):
    DESCRIPTION="ToupCam microscope: RGB preview, exposure, auto exposure and ROI"
    INTERFACES=("none",)
    DEFAULT_OPTIONS=dict(camera_index=0,simulate=False,call_timeout=15,retries=0)
    CHANNELS=[ChannelSpec("image","r","float",(-1,-1,3),"counts"),
        ChannelSpec("preview","r","float",(-1,-1,3),"counts"),
        ChannelSpec("sensor_size","r","float",(2,),"px",config=True),
        ChannelSpec("exposure","rw",unit="s",config=True),
        ChannelSpec("gain","rw",config=True),
        ChannelSpec("auto_exposure","rw","str",config=True,allowed=["ON","OFF"]),
        ChannelSpec("target","rw",config=True),
        ChannelSpec("ae_roi","rw","float",(4,),"px",config=True)]
    def connect(self):
        self.camera=None; self.ready=threading.Event(); self.error=None
        if self.options["simulate"]:
            self.width,self.height=640,480
            self.values=dict(exposure=.01,gain=100,auto_exposure="ON",target=120,ae_roi=[0,0,640,480]); return
        try:
            from shg_addon.vendor import toupcam
        except ImportError as exc:
            raise ImportError('Install the ToupTek SDK toupcam.py and matching native library in shg_addon/vendor; see the SHG README') from exc
        self.api=toupcam
        devices=toupcam.Toupcam.EnumV2(); index=int(self.options["camera_index"])
        if not 0<=index<len(devices): raise IOError("No microscope camera at index %d"%index)
        self.camera=toupcam.Toupcam.Open(devices[index].id)
        if self.camera is None: raise IOError("Cannot open microscope camera; close ToupView")
        try:
            self.width,self.height=self.camera.get_Size()
            self.camera.StartPullModeWithCallback(self._event,None)
        except BaseException:
            self.disconnect(); raise
    def _event(self,event,ctx):
        if event==self.api.TOUPCAM_EVENT_IMAGE: self.ready.set()
        elif event in (self.api.TOUPCAM_EVENT_ERROR,self.api.TOUPCAM_EVENT_DISCONNECTED):
            self.error="Microscope camera error/disconnection"; self.ready.set()
    def disconnect(self):
        if self.camera:
            try: self.camera.Stop()
            finally: self.camera.Close(); self.camera=None
    def read(self,name):
        if name=="sensor_size": return np.array([self.width,self.height],float)
        if self.options["simulate"]:
            if name in ("image","preview"):
                y,x=np.mgrid[:self.height,:self.width]; v=255*np.exp(-((x-320)**2+(y-240)**2)/6000)
                return np.stack([v,.6*v,.3*v],axis=-1)
            return self.values[name]
        c=self.camera
        if name in ("image","preview"):
            if not self.ready.wait(10): raise TimeoutError("Microscope frame timed out")
            self.ready.clear()
            if self.error: raise IOError(self.error)
            buf=ctypes.create_string_buffer(self.width*self.height*3)
            info=self.api.ToupcamFrameInfoV3()
            c.PullImageV3(buf,0,24,-1,info)
            image=np.frombuffer(buf,dtype=np.uint8,count=info.width*info.height*3).reshape(info.height,info.width,3)
            if name=="preview":
                step=max(1,int(np.ceil(info.width/960)),int(np.ceil(info.height/640)))
                image=image[::step,::step]
            return image.astype(float)
        if name=="exposure": return c.get_ExpoTime()/1e6
        if name=="gain": return float(c.get_ExpoAGain())
        if name=="auto_exposure": return "ON" if c.get_AutoExpoEnable() else "OFF"
        if name=="target": return float(c.get_AutoExpoTarget())
        if name=="ae_roi": return np.array(c.get_AEAuxRect(),float)
        raise ChannelError("Unknown microscope channel "+name)
    def write(self,name,value):
        if name=="ae_roi":
            a=np.asarray(value,float)
            if a.shape!=(4,) or not np.isfinite(a).all() or not np.equal(a,a.astype(int)).all(): raise ChannelError("ROI requires four integer pixels")
            x,y,w,h=a.astype(int)
            if x<0 or y<0 or w<1 or h<1 or x+w>self.width or y+h>self.height: raise ChannelError("ROI outside sensor")
        elif name=="auto_exposure":
            if value not in ("ON","OFF"): raise ChannelError("Use ON or OFF")
        elif not np.isfinite(float(value)) or float(value)<0: raise ChannelError("Invalid camera setting")
        if self.options["simulate"]: self.values[name]=value; return
        c=self.camera
        if name=="exposure": c.put_ExpoTime(round(float(value)*1e6))
        elif name=="gain": c.put_ExpoAGain(int(value))
        elif name=="auto_exposure": c.put_AutoExpoEnable(int(value=="ON"))
        elif name=="target": c.put_AutoExpoTarget(int(value))
        elif name=="ae_roi": c.put_AEAuxRect(int(x),int(y),int(w),int(h))
        else: raise ChannelError("Not writable: "+name)

DRIVER=ToupMicroscope
