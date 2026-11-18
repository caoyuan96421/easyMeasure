"""Logical scan coordinates, applied together by the SHG pre-record hook."""
import math
from easymeasure.backend.instrument import Instrument,ChannelSpec
from easymeasure.common.errors import ChannelError

class SHGCoordinates(Instrument):
    DESCRIPTION="Logical x/y/polarization for corrected SHG scans (use the SHG pre-record hook)"
    INTERFACES=("none",)
    CHANNELS=[ChannelSpec("x","rw"),ChannelSpec("y","rw"),ChannelSpec("polarization","rw",unit="deg")]
    def connect(self):self.values=dict(x=50.,y=50.,polarization=0.)
    def read(self,name):return self.values[name]
    def write(self,name,value):
        value=float(value);lo,hi=(-56,624) if name=="polarization" else (0,100)
        if not math.isfinite(value) or not lo<=value<=hi:raise ChannelError("Logical coordinate outside range")
        self.values[name]=value

DRIVER=SHGCoordinates
