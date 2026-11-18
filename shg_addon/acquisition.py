"""MATLAB-compatible SHG acquisition, kept outside the downloaded drivers."""
import ctypes as ct
import json
import math
import time
import numpy as np
from easymeasure.common.errors import ChannelError


class SHGAcquisition:
    def configure_roi(self, values):
        b,t,l,r = [self._integer(v, 'ROI') for v in values]
        if not (1 <= b <= t <= self.height and 4 <= l <= r <= self.width):
            raise ChannelError('ROI requires 1 <= bottom <= top <= height and 4 <= left <= right <= width')
        x,y=(l-1)//8*8,(b-1)//2*2
        w,h=(r-l+1)//8*8,(t-b+1)//2*2
        if w < 104 or h < 2:
            raise ChannelError('SHG ROI requires at least 104 aligned columns and 2 rows')
        previous=getattr(self,'hardware_roi',None)
        if not self.options['simulate']:
            try:
                self._check('ASISetROIFormat',self.camera_id,w,h,int(self.options['binning']),2)
                self._check('ASISetStartPos',self.camera_id,x,y)
            except BaseException:
                if previous:
                    px,py,pw,ph=previous
                    self._check('ASISetROIFormat',self.camera_id,pw,ph,int(self.options['binning']),2)
                    self._check('ASISetStartPos',self.camera_id,px,py)
                raise
        self.roi=dict(IntB=b,IntT=t,IntL=l,IntR=r)
        self.hardware_roi=(x,y,w,h)
        if previous != self.hardware_roi:
            self._action_clear_background()

    def _frame(self):
        x,y,w,h=self.hardware_roi
        exposure=self._control(1)/1e6
        if self.options['simulate']:
            columns=np.arange(x,x+w)
            phase=.4 if self.path=='reference' else .8+.1*np.sin(self.halfwave_angle*.02)
            fringe=(1000+600*np.cos(2*np.pi*columns/35+phase))*np.exp(-((columns-2800)/350)**2)
            light=0 if getattr(self,'_taking_background',False) else fringe*exposure/.1
            data=np.broadcast_to(200+2*exposure+light,(h,w)).copy()
        else:
            if exposure > float(self.options['max_exposure']):
                raise ChannelError('Exposure exceeds max_exposure')
            deadline=time.monotonic()+exposure+float(self.options['exposure_margin'])
            self._check('ASIStopExposure',self.camera_id)
            self._check('ASIStartExposure',self.camera_id,0)
            try:
                while True:
                    status=ct.c_int()
                    self._check('ASIGetExpStatus',self.camera_id,ct.byref(status))
                    if status.value==2:break
                    if status.value!=1:raise IOError('ZWO exposure failed (status %d)'%status.value)
                    if time.monotonic()>=deadline:raise TimeoutError('ZWO exposure exceeded its deadline')
                    time.sleep(.01)
                raw=np.empty((h,w),dtype=np.uint16)
                self._check('ASIGetDataAfterExp',self.camera_id,raw.ctypes.data_as(ct.POINTER(ct.c_ubyte)),raw.nbytes)
                data=raw.astype(float)
            except BaseException:
                self.sdk.ASIStopExposure(self.camera_id)
                raise
        frame=np.zeros((self.height,self.width))
        frame[y:y+h,x:x+w]=data
        self.last_raw=frame
        return frame.copy()

    def read(self,name):
        if name in ('image','spectrum','signal'):
            frame=self._frame()
            if self.background is not None:frame-=self.background
            if name=='image':return frame
            rows=frame[self.roi['IntB']-1:self.roi['IntT']]
            if name=='spectrum':return rows.sum(axis=0)
            return float(rows[:,self.roi['IntL']-1:self.roi['IntR']].sum())
        return super().read(name)

    def write(self,name,value):
        if name in self.roi:
            roi=dict(self.roi,**{name:value})
            self.configure_roi([roi[k] for k in ('IntB','IntT','IntL','IntR')])
            return
        # MATLAB retains bias/dark arrays when camera controls change; do not
        # silently discard them in the base driver's control setter.
        background=self.background
        super().write(name,value)
        if name in self.CONTROLS:self.background=background

    def _action_clear_background(self):
        self.background=None
        self.bias=None
        self.dark=None
        self.background_info=None

    def _action_capture_background(self):
        # The UI workflow sets exposure to one second and blocks the shutter.
        exposure=self._control(1)
        self._action_clear_background()
        self._taking_background=True
        try:
            self._set_control(1,32)
            bias=np.zeros((self.height,self.width))
            for _ in range(25):bias+=self._frame()
            bias/=25
            self._set_control(1,exposure)
            dark=np.zeros_like(bias)
            for _ in range(10):dark+=self._frame()-bias
            dark/=10
            self.bias,self.dark=bias,dark
            self.background=bias+dark
            self.background_info=dict(exposure=exposure/1e6,gain=self._control(0),temperature=self._control(8)/10,
                                      bias_frames=25,dark_frames=10,bias_exposure=32e-6,hardware_roi=self.hardware_roi)
        finally:
            self._taking_background=False
            self._set_control(1,exposure)

    def acquisition_status(self):
        bg='No background — take background before measurement'
        if self.background is not None:
            bg='MATLAB bias/dark applied'
            if self.background_info and self.background_info['exposure'] != self._control(1)/1e6:
                bg+=' (captured at %g s; reused without rescaling)'%self.background_info['exposure']
        return json.dumps(dict(frame_id=self.frame_id,path=self.path,diagnostic_id=getattr(self,'diagnostic_id',0),
            timing=getattr(self,"last_timing",{}),background=bg,exposure=self._control(1)/1e6,gain=self._control(0),hardware_roi=self.hardware_roi,
            sample_phase=self.cache.get('phase'),sample_amplitude=self.cache.get('amplitude'),
            reference_phase=self.cache.get('reference_phase'),reference_amplitude=self.cache.get('reference_amplitude')))
