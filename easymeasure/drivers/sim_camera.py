"""Simulated camera: every reading is a 2-D image.

A Gaussian spot moves with two simulated voltages (``x_source`` / ``y_source``
options, default ``src.V3`` / ``src.V4``).
"""

from __future__ import annotations

import time

import numpy as np

from ..common.errors import ChannelError
from ..backend.instrument import ActionSpec, ChannelSpec
from ._simbase import FAULT_OPTION_DOC, SimDirectInstrument
from .simworld import WORLD


class SimCamera(SimDirectInstrument):
    DESCRIPTION = "Simulated camera (2-D image readings)"
    CHANNELS = [
        ChannelSpec("image", "r", "float", (-1, -1), "counts", "full frame"),
        ChannelSpec("total", "r", "float", (), "counts", "integrated intensity"),
        ChannelSpec("exposure", "rw", "float", (), "s", config=True),
        ChannelSpec("width", "rw", "float", (), "px", config=True),
        ChannelSpec("height", "rw", "float", (), "px", config=True),
    ]
    ACTIONS = [ActionSpec("snap", "take (and discard) one frame"),
               ActionSpec("cooler_on", "start the sensor cooler", timeout=5.0)]
    DEFAULT_OPTIONS = dict(x_source="src.V3", y_source="src.V4", simulate_exposure=False,
                           **FAULT_OPTION_DOC)

    def connect(self):
        self.gate()
        self.exposure = getattr(self, "exposure", 0.01)
        self.w = getattr(self, "w", 48)
        self.h = getattr(self, "h", 32)

    def _action_snap(self):
        self.gate()
        self._image()

    def _action_cooler_on(self):
        self.gate()
        self.cooler = True

    def _image(self):
        if self.options.get("simulate_exposure"):
            time.sleep(min(self.exposure, 5.0))
        x0 = float(WORLD.get(self.options["x_source"]))
        y0 = float(WORLD.get(self.options["y_source"]))
        return WORLD.image(int(self.w), int(self.h), x0, y0, self.exposure * 100)

    def read(self, channel):
        self.gate()
        if channel == "image":
            return self.finish(self._image())
        if channel == "total":
            return self.finish(float(self._image().sum()))
        if channel == "exposure":
            return self.exposure
        if channel == "width":
            return float(self.w)
        if channel == "height":
            return float(self.h)
        raise ChannelError("unknown channel %s" % channel)

    def write(self, channel, value):
        self.gate()
        if channel == "exposure":
            if value <= 0:
                raise ChannelError("exposure must be positive")
            self.exposure = value
        elif channel in ("width", "height"):
            n = int(round(value))
            if not 2 <= n <= 4096:
                raise ChannelError("size out of range")
            if channel == "width":
                self.w = n
            else:
                self.h = n
        else:
            raise ChannelError("channel %s not writable" % channel)
