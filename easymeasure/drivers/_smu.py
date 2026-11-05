"""Common logic of the source-measure unit drivers (not a driver itself).

Each SMU output has a *mode*:

``Voltage``  the voltage channel is the source (read/write), the current channel
             is measured (read-only) and the compliance is a current limit
             (channel ``Icompliance``, unit A);
``Current``  the other way round, compliance ``Vcompliance`` in V.

The channel list follows the mode (the backend re-reads it after every setting),
so the front ends show a read-only measured channel and the right compliance
name and unit.  Changing the mode needs the output switched off; reading the
measured quantity while the output is off gives NaN (nothing is measured, and
some SMUs would otherwise switch the output on by themselves).

Option ``verify_writes`` reads every new source value back (one extra query) and
retries when it did not arrive - worth it on RS-232 lines, where a command can be
lost without the instrument noticing.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List

from ..backend.instrument import ChannelSpec
from ..common.errors import ChannelError
from ._scpi import SCPIInstrument

MODES = ["Voltage", "Current"]
ONOFF = ["ON", "OFF"]
SENSE = ["2-wire", "4-wire"]
_CH = re.compile(r"^(mode|voltage|current|Icompliance|Vcompliance|output|nplc|sense|[a-z_]+?)(\d?)$")


class SMUBase(SCPIInstrument):
    #: output identifiers ("1", "2"); channel names get the id as suffix when there
    #: is more than one output
    OUTPUTS: List[str] = ["1"]
    NPLC_RANGE = (0.01, 10.0)

    # -- channel list ---------------------------------------------------------
    def suffix(self, o: str) -> str:
        return o if len(self.OUTPUTS) > 1 else ""

    def output_channels(self, o: str, mode: str) -> List[ChannelSpec]:
        s = self.suffix(o)
        v = mode == "Voltage"
        what = (" (output %s)" % o) if s else ""
        return [
            ChannelSpec("mode" + s, "rw", "str", (), "", "source function: Voltage = force V / "
                        "measure I, Current = force I / measure V" + what, config=True,
                        allowed=MODES),
            ChannelSpec("voltage" + s, "rw" if v else "r", "float", (), "V",
                        ("source voltage" if v else "measured voltage") + what),
            ChannelSpec("current" + s, "r" if v else "rw", "float", (), "A",
                        ("measured current" if v else "source current") + what),
            ChannelSpec(("Icompliance" if v else "Vcompliance") + s, "rw", "float", (),
                        "A" if v else "V", ("current" if v else "voltage") + " compliance (limit)"
                        + what, config=True),
            ChannelSpec("output" + s, "rw", "str", (), "", "output relay" + what, config=True,
                        allowed=ONOFF),
            ChannelSpec("nplc" + s, "rw", "float", (), "PLC", "integration time of the measurement "
                        "in power-line cycles (%g … %g)" % self.NPLC_RANGE + what, config=True),
            ChannelSpec("sense" + s, "rw", "str", (), "", "2-wire or 4-wire (remote) sensing" + what,
                        config=True, allowed=SENSE),
        ] + self.extra_channels(o)

    def extra_channels(self, o: str) -> List[ChannelSpec]:
        return []

    def get_channels(self):
        st = getattr(self, "st", None)
        out = []
        for o in self.OUTPUTS:
            out += self.output_channels(o, st[o]["mode"] if st else "Voltage")
        return out

    @classmethod
    def static_channels(cls) -> List[ChannelSpec]:
        inst = cls.__new__(cls)
        inst.st = None
        return SMUBase.get_channels(inst)

    # -- state ------------------------------------------------------------------------
    def connect(self):
        super().connect()
        self.st: Dict[str, dict] = {}
        for o in self.OUTPUTS:
            self.setup_output(o)
            self.st[o] = {"mode": self.hw_mode(o), "on": self.hw_output(o)}
        self.clear_errors()

    def setup_output(self, o: str) -> None:
        """Bring the output into the state this driver relies on (once per connect)."""

    def _split(self, channel: str):
        m = _CH.match(channel)
        if not m:
            raise ChannelError("%s has no channel %s" % (self.name, channel))
        base, s = m.group(1), m.group(2)
        if len(self.OUTPUTS) > 1:
            if s not in self.OUTPUTS:
                raise ChannelError("%s has no channel %s" % (self.name, channel))
            return base, s
        if s:
            raise ChannelError("%s has no channel %s" % (self.name, channel))
        return base, self.OUTPUTS[0]

    def _sensed(self, o: str) -> str:
        return "I" if self.st[o]["mode"] == "Voltage" else "V"

    # -- channel I/O ------------------------------------------------------------
    def read(self, channel):
        base, o = self._split(channel)
        st = self.st[o]
        if base == "mode":
            st["mode"] = self.hw_mode(o)
            return st["mode"]
        if base in ("voltage", "current"):
            q = "V" if base == "voltage" else "I"
            if q != self._sensed(o):
                return self.hw_level(o, q)           # the programmed source value
            if not st["on"]:
                st["on"] = self.hw_output(o)          # maybe switched on at the front panel
                if not st["on"]:
                    return math.nan                   # nothing is measured with the output off
            return self.hw_measure(o, q)
        if base in ("Icompliance", "Vcompliance"):
            self._check_compliance_name(base, o)
            return self.hw_compliance(o, self._sensed(o))
        if base == "output":
            st["on"] = self.hw_output(o)
            return "ON" if st["on"] else "OFF"
        if base == "nplc":
            return self.hw_nplc(o, self._sensed(o))
        if base == "sense":
            return "4-wire" if self.hw_sense(o) else "2-wire"
        return self.read_extra(base, o)

    def write(self, channel, value):
        base, o = self._split(channel)
        st = self.st[o]
        if base == "mode":
            if value == st["mode"]:
                return
            if st["on"] or self.hw_output(o):
                st["on"] = True
                raise ChannelError("%s: switch output%s OFF before changing the mode"
                                   % (self.name, self.suffix(o)))
            self.hw_set_mode(o, value)
            st["mode"] = value
            return
        if base in ("voltage", "current"):
            q = "V" if base == "voltage" else "I"
            if q == self._sensed(o):
                raise ChannelError("%s: %s%s is measured in %s mode; set mode%s first"
                                   % (self.name, base, self.suffix(o), st["mode"], self.suffix(o)))
            self.hw_set_level(o, q, float(value))
            if self.options.get("verify_writes"):
                got = self.hw_level(o, q)
                if abs(got - value) > 1e-6 * max(abs(value), 1e-9):
                    # e.g. a command lost on a noisy serial line: the framework retries
                    raise IOError("%s%s read back %g after setting %g" % (base, self.suffix(o),
                                                                          got, value))
            return
        if base in ("Icompliance", "Vcompliance"):
            self._check_compliance_name(base, o)
            self.hw_set_compliance(o, self._sensed(o), float(value))
            return
        if base == "output":
            on = value == "ON"
            self.hw_set_output(o, on)
            st["on"] = on
            return
        if base == "nplc":
            lo, hi = self.NPLC_RANGE
            if not lo <= value <= hi:
                raise ChannelError("%s: NPLC must be within %g … %g" % (self.name, lo, hi))
            self.hw_set_nplc(o, self._sensed(o), float(value))
            return
        if base == "sense":
            self.hw_set_sense(o, value == "4-wire")
            return
        self.write_extra(base, o, value)

    def _check_compliance_name(self, base: str, o: str) -> None:
        want = "Icompliance" if self.st[o]["mode"] == "Voltage" else "Vcompliance"
        if base != want:
            raise ChannelError("%s is in %s mode: the compliance is %s%s"
                               % (self.name, self.st[o]["mode"], want, self.suffix(o)))

    def read_extra(self, base, o):
        raise ChannelError("%s has no channel %s" % (self.name, base))

    def write_extra(self, base, o, value):
        raise ChannelError("%s: channel %s is not writable" % (self.name, base))

    def recover(self):
        super().recover()
        # the output may have been switched at the front panel / tripped
        try:
            for o in self.OUTPUTS:
                self.st[o]["on"] = self.hw_output(o)
        except Exception:
            pass

    # -- hardware commands (subclasses) ------------------------------------------------
    def hw_mode(self, o): raise NotImplementedError
    def hw_set_mode(self, o, mode): raise NotImplementedError
    def hw_level(self, o, q): raise NotImplementedError
    def hw_set_level(self, o, q, v): raise NotImplementedError
    def hw_measure(self, o, q): raise NotImplementedError
    def hw_compliance(self, o, q): raise NotImplementedError
    def hw_set_compliance(self, o, q, v): raise NotImplementedError
    def hw_output(self, o): raise NotImplementedError
    def hw_set_output(self, o, on): raise NotImplementedError
    def hw_nplc(self, o, q): raise NotImplementedError
    def hw_set_nplc(self, o, q, v): raise NotImplementedError
    def hw_sense(self, o): raise NotImplementedError
    def hw_set_sense(self, o, on): raise NotImplementedError
