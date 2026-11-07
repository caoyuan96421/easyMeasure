"""Generic SCPI instrument configured entirely by options (no code needed).

Example options::

    {"idn_contains": "KEITHLEY",
     "init": ["*RST", ":SOUR:FUNC VOLT"],
     "channels": [
        {"name": "V", "access": "rw", "get": ":SOUR:VOLT?", "set": ":SOUR:VOLT {value:.6g}",
         "unit": "V"},
        {"name": "I", "access": "r", "get": ":MEAS:CURR?", "unit": "A", "index": 1},
        {"name": "trace", "access": "r", "get": ":TRAC:DATA?", "shape": [-1]},
        {"name": "output", "access": "rw", "dtype": "str", "get": ":OUTP?", "set": ":OUTP {value}",
         "config": true}
     ],
     "actions": [{"name": "trigger", "command": "*TRG"},
                 {"name": "reset", "command": ["*RST", "*CLS"]}]}

``index`` picks one element of a comma separated reply.  Vector channels parse
comma separated replies into arrays.  Works with any port type (VISA, serial,
TCP).
"""

from __future__ import annotations

from typing import List

from ..common.errors import ChannelError
from ..backend.instrument import ActionSpec, ChannelSpec, Instrument
from ..backend.resources import BYTE_PORTS


class SCPIGeneric(Instrument):
    INTERFACES = BYTE_PORTS
    DESCRIPTION = "Generic SCPI instrument defined by option 'channels'"
    CHANNELS: List[ChannelSpec] = []
    DEFAULT_OPTIONS = {"channels": [], "actions": [], "init": [], "idn_contains": "", "timeout": 3.0,
                       "read_termination": "\n", "write_termination": "\n"}

    def connect(self):
        self._defs = {}
        for c in self.options.get("channels", []):
            if "name" not in c:
                raise ChannelError("channel definition without name")
            self._defs[c["name"]] = c
        if self.resource is not None and self.resource.address != "NONE":
            idn = self.resource.query("*IDN?")
            want = self.options.get("idn_contains") or ""
            if want and want.upper() not in idn.upper():
                raise IOError("unexpected instrument: %r" % idn)
            self.idn = idn
            for cmd in self.options.get("init", []):
                self.resource.write(cmd)

    def get_channels(self):
        out = []
        for c in self.options.get("channels", []):
            access = c.get("access") or ("rw" if c.get("get") and c.get("set")
                                         else "w" if c.get("set") else "r")
            shape = c.get("shape", [])
            out.append(ChannelSpec(c["name"], access, c.get("dtype", "float"),
                                   None if shape is None else tuple(shape), c.get("unit", ""),
                                   c.get("description", ""), c.get("timeout"),
                                   config=bool(c.get("config", False))))
        return out

    def get_actions(self):
        return [ActionSpec(a["name"], a.get("description", a.get("command", "")), a.get("timeout"))
                for a in self.options.get("actions", [])]

    def action(self, name):
        for a in self.options.get("actions", []):
            if a.get("name") == name:
                for cmd in ([a["command"]] if isinstance(a.get("command"), str)
                            else a.get("command", [])):
                    self.resource.write(cmd)
                return
        raise ChannelError("no action %s" % name)

    def read(self, channel):
        c = self._defs.get(channel)
        if c is None or not c.get("get"):
            raise ChannelError("%s not readable" % channel)
        reply = self.resource.query(c["get"])
        if "index" in c:
            parts = reply.split(",")
            try:
                return parts[int(c["index"])]
            except IndexError:
                raise IOError("short reply %r" % reply) from None
        return reply

    def write(self, channel, value):
        c = self._defs.get(channel)
        if c is None or not c.get("set"):
            raise ChannelError("%s not writable" % channel)
        if hasattr(value, "tolist"):
            value = ",".join("%.9g" % v for v in value.ravel().tolist())
            self.resource.write(c["set"].replace("{value:.6g}", "{value}").format(value=value))
        else:
            self.resource.write(c["set"].format(value=value))
