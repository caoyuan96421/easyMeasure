"""Newport SMC100 commands and serial settings from the existing smcRotator.m."""
import math
import time
from easymeasure.backend.instrument import Instrument, ChannelSpec, ActionSpec
from easymeasure.common.errors import ChannelError


class NewportSMC100(Instrument):
    DESCRIPTION="Newport SMC100 half-wave plate rotator"
    INTERFACES=("serial","none")
    DEFAULT_OPTIONS=dict(baudrate=921600,xonxoff=True,read_termination="\r\n",
                         write_termination="\r\n",timeout=5,call_timeout=65,
                         motion_timeout=60,axis=1,simulate=False,retries=0)
    CHANNELS=[ChannelSpec("angle","rw",unit="deg"),ChannelSpec("status","r",config=True)]
    ACTIONS=[ActionSpec("home","Home the rotator (physical motion)",timeout=65)]
    def connect(self): self.position=28.
    def _query(self,command):
        axis=int(self.options["axis"])
        text=self.resource.query(str(axis)+command+"?").strip()
        prefix=str(axis)+command
        if not text.startswith(prefix): raise IOError("Unexpected SMC100 response: "+text)
        return text[len(prefix):]
    def _read_status(self):
        if self.options["simulate"]: return 50.
        response=self._query("TS")
        if len(response)!=6: raise IOError("Invalid SMC100 status "+response)
        error=int(response[:4],16)
        if error: raise IOError("SMC100 error flags 0x%04x"%error)
        return float(int(response[4:],16))
    def _read_angle(self):
        return self.position if self.options["simulate"] else float(self._query("TP"))
    def _wait(self):
        end=time.monotonic()+float(self.options["motion_timeout"])
        while time.monotonic()<end:
            status=int(self._read_status())
            if status in (50,51,52,53): return
            if status not in (30,40): raise IOError("Rotator not ready; status 0x%02x"%status)
            time.sleep(.1)
        raise TimeoutError("SMC100 motion timed out")
    def _write_angle(self,value):
        value=float(value)
        if not math.isfinite(value) or not 0<=value<=340: raise ChannelError("Angle must be 0..340 degrees")
        if self.options["simulate"]: self.position=value; return
        if int(self._read_status()) not in (50,51,52,53): raise ChannelError("Rotator must be ready; home explicitly if needed")
        self.resource.write("%dPA%.8f"%(int(self.options["axis"]),value)); self._wait()
    def _action_home(self):
        if self.options["simulate"]: self.position=0.; return
        self.resource.write("%dOR"%int(self.options["axis"])); self._wait()

DRIVER=NewportSMC100
