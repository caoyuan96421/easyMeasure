"""Simulated lock-in amplifier (no port; address ``NONE``).

Option ``sample`` chooses what it measures:

* ``dot`` (default): the differential conductance of the simulated quantum dot
  (gate / bias from the options ``gate`` and ``bias``);
* ``graphene``: a dual-gated bilayer graphene Hall bar (gates from ``back_gate`` /
  ``top_gate``, normally two ``sim_smu`` instruments ``bg`` and ``tg``).  The AC
  current is driven by a lock-in's sine output through a ``bias_resistor``:
  I = V_out / (bias_resistor + R_2t), where the two-terminal resistance R_2t
  (3 Rxx + 2 contacts) is not negligible when the sample is insulating, so the
  current is *not* constant.  ``measure`` chooses what this lock-in sees:
  ``voltage`` (X = I · Rxx, the four-probe voltage) or ``current`` (X = I, a
  current input).  The driving output is this lock-in's own ``amplitude``, or
  with ``source`` = ``"<other lock-in>.amplitude"`` that of another lock-in.

Both see the field of ``field`` (a ``sim_magnet``).  Besides scalar channels it
offers ``XY`` - a 2-element vector reading.
"""

from __future__ import annotations

import math

import numpy as np

from ..common.errors import ChannelError
from ..backend.instrument import ActionSpec, ChannelSpec
from ._simbase import FAULT_OPTION_DOC, SimDirectInstrument
from .simworld import WORLD

_SENS = ["1e-6", "1e-5", "1e-4", "1e-3", "1e-2", "1e-1", "1"]


class SimLockin(SimDirectInstrument):
    DESCRIPTION = "Simulated lock-in amplifier (X, Y, R, theta, XY vector)"
    CHANNELS = [
        ChannelSpec("X", "r", "float", (), "V"),
        ChannelSpec("Y", "r", "float", (), "V"),
        ChannelSpec("R", "r", "float", (), "V"),
        ChannelSpec("theta", "r", "float", (), "deg"),
        ChannelSpec("XY", "r", "float", (2,), "V", "X and Y as a vector"),
        ChannelSpec("frequency", "rw", "float", (), "Hz", config=True),
        ChannelSpec("amplitude", "rw", "float", (), "V", config=True),
        ChannelSpec("sensitivity", "rw", "str", (), "", "one of " + ", ".join(_SENS), config=True),
    ]
    ACTIONS = [ActionSpec("auto_phase", "rotate the phase so that Y = 0"),
               ActionSpec("auto_gain", "choose the sensitivity for the present signal")]
    DEFAULT_OPTIONS = dict(sample="dot", gate="src.V1", bias="src.V2", back_gate="bg.V",
                           top_gate="tg.V", measure="voltage", source="",
                           bias_resistor=1e6, contact_resistance=1e3, field="magnet.field",
                           **FAULT_OPTION_DOC)

    def connect(self):
        self.gate()
        if str(self.options.get("sample", "dot")).lower() not in ("dot", "graphene"):
            raise ChannelError("option sample must be 'dot' or 'graphene'")
        if str(self.options.get("measure", "voltage")).lower() not in ("voltage", "current"):
            raise ChannelError("option measure must be 'voltage' or 'current'")
        self.freq = getattr(self, "freq", 17.77)
        self.amp = getattr(self, "amp", 0.01)
        self.sens = getattr(self, "sens", "1e-3")
        WORLD.set(self.name + ".amplitude", self.amp)     # another lock-in may use it

    def _xy(self):
        o = self.options
        if str(o.get("sample", "dot")).lower() == "graphene":
            g = WORLD.graphene(float(WORLD.get(o["back_gate"])), float(WORLD.get(o["top_gate"])),
                               float(WORLD.get(o["field"])))
            src = str(o.get("source") or "").strip()
            v_out = float(WORLD.get(src)) if src else self.amp
            r2t = 3.0 * g["Rxx"] + 2.0 * float(o.get("contact_resistance", 1e3))
            i = v_out / (float(o.get("bias_resistor", 1e6)) + r2t)
            if str(o.get("measure", "voltage")).lower() == "current":
                x = i * (1.0 + 5e-4 * float(self.rng.standard_normal()))
                return x, 0.002 * x + 2e-13 * float(self.rng.standard_normal())
            x = i * g["Rxx"]
            return x, 0.01 * x + 2e-8 * float(self.rng.standard_normal())
        return WORLD.lockin(float(WORLD.get(o["gate"])), float(WORLD.get(o["bias"])),
                            float(WORLD.get(o["field"])), self.amp)

    def _action_auto_phase(self):
        self.gate()
        self.phase_offset = 0.0

    def _action_auto_gain(self):
        self.gate()
        x, y = self._xy()
        r = max(abs(x), abs(y), 1e-9)
        self.sens = next((s for s in _SENS if float(s) >= 1.5 * r), _SENS[-1])

    def read(self, channel):
        self.gate()
        if channel in ("X", "Y", "R", "theta", "XY"):
            x, y = self._xy()
            val = {"X": x, "Y": y, "R": math.hypot(x, y),
                   "theta": math.degrees(math.atan2(y, x)), "XY": np.array([x, y])}[channel]
            return self.finish(val)
        if channel == "frequency":
            return self.freq
        if channel == "amplitude":
            return self.amp
        if channel == "sensitivity":
            return self.sens
        raise ChannelError("unknown channel %s" % channel)

    def write(self, channel, value):
        self.gate()
        if channel == "frequency":
            if not 0.001 <= value <= 1e5:
                raise ChannelError("frequency out of range")
            self.freq = value
        elif channel == "amplitude":
            self.amp = max(0.0, min(5.0, value))
            WORLD.set(self.name + ".amplitude", self.amp)
        elif channel == "sensitivity":
            if value not in _SENS:
                raise ChannelError("sensitivity must be one of %s" % _SENS)
            self.sens = value
        else:
            raise ChannelError("channel %s not writable" % channel)
