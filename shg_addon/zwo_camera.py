"""ZWO ASI USB camera and basic spectrometer, using the native Windows ASI SDK.

Based on the local SpecialMeasure smiZWOSpec/smcZWOSpec acquisition workflow.
Address NONE; camera_index selects the enumerated camera (not its CameraID).
ROI coordinates are MATLAB-style 1-based inclusive, applied in software.
"""
from __future__ import annotations

import ctypes as ct
import math
import os
import time
from pathlib import Path

import numpy as np

from easymeasure.backend.instrument import ActionSpec, ChannelSpec, Instrument
from easymeasure.common.errors import ChannelError


class CameraInfo(ct.Structure):
    _fields_ = [("Name", ct.c_char * 64), ("CameraID", ct.c_int),
                ("MaxHeight", ct.c_long), ("MaxWidth", ct.c_long),
                ("IsColorCam", ct.c_int), ("BayerPattern", ct.c_int),
                ("SupportedBins", ct.c_int * 16),
                ("SupportedVideoFormat", ct.c_int * 8), ("PixelSize", ct.c_double),
                ("MechanicalShutter", ct.c_int), ("ST4Port", ct.c_int),
                ("IsCoolerCam", ct.c_int), ("IsUSB3Host", ct.c_int),
                ("IsUSB3Camera", ct.c_int), ("ElecPerADU", ct.c_float),
                ("BitDepth", ct.c_int), ("IsTriggerCam", ct.c_int),
                ("Unused", ct.c_char * 16)]


class ControlCaps(ct.Structure):
    _fields_ = [("Name", ct.c_char * 64), ("Description", ct.c_char * 128),
                ("MaxValue", ct.c_long), ("MinValue", ct.c_long),
                ("DefaultValue", ct.c_long), ("IsAutoSupported", ct.c_int),
                ("IsWritable", ct.c_int), ("ControlType", ct.c_int),
                ("Unused", ct.c_char * 32)]


def load_sdk(path=""):
    if os.name != "nt":
        raise OSError("This driver requires Windows and the Windows ASICamera2.dll")
    candidates = [Path(path)] if path else [
        Path(__file__).resolve().parents[1] / "vendor" / "zwo" / "ASICamera2.dll",
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "ASIStudio" / "ASICamera2.dll",
    ]
    dll = next((p for p in candidates if p.is_file()), None)
    if dll is None:
        raise FileNotFoundError("ASICamera2.dll not found; set the sdk_path instrument option")
    sdk = ct.CDLL(str(dll.resolve()))
    i, lng, ptr = ct.c_int, ct.c_long, ct.POINTER
    signatures = {
        "ASIGetNumOfConnectedCameras": [], "ASIGetCameraProperty": [ptr(CameraInfo), i],
        "ASIOpenCamera": [i], "ASIInitCamera": [i], "ASICloseCamera": [i],
        "ASIGetNumOfControls": [i, ptr(i)], "ASIGetControlCaps": [i, i, ptr(ControlCaps)],
        "ASIGetControlValue": [i, i, ptr(lng), ptr(i)],
        "ASISetControlValue": [i, i, lng, i], "ASISetROIFormat": [i, i, i, i, i],
        "ASISetStartPos": [i, i, i], "ASISetCameraMode": [i, i],
        "ASIStartExposure": [i, i], "ASIStopExposure": [i],
        "ASIGetExpStatus": [i, ptr(i)],
        "ASIGetDataAfterExp": [i, ptr(ct.c_ubyte), lng],
    }
    for name, args in signatures.items():
        fn = getattr(sdk, name)
        fn.argtypes, fn.restype = args, i
    return sdk


class ZWOCamera(Instrument):
    DESCRIPTION = "ZWO ASI camera: RAW16 images, integrated signal and spectrum (native SDK)"
    INTERFACES = ("none",)
    DEFAULT_OPTIONS = dict(sdk_path="", camera_index=0, binning=1,
                           max_exposure=60.0, exposure_margin=10.0,
                           background_frames=5, call_timeout=90.0, retries=0)
    CHANNELS = [
        ChannelSpec("image", "r", "float", (-1, -1), "counts", "background-corrected full frame"),
        ChannelSpec("spectrum", "r", "float", (-1,), "counts", "sum rows IntB:IntT, all columns"),
        ChannelSpec("signal", "r", "float", (), "counts", "sum rectangular integration region"),
        ChannelSpec("exposure", "rw", unit="s", config=True),
        ChannelSpec("gain", "rw", config=True),
        ChannelSpec("temperature", "r", unit="degC", config=True),
        ChannelSpec("target_temperature", "rw", unit="degC", config=True),
        ChannelSpec("cooler", "rw", "str", config=True, allowed=["ON", "OFF"]),
        *[ChannelSpec(n, "rw", unit="px", config=True) for n in ("IntB", "IntT", "IntL", "IntR")],
        ChannelSpec("width", "r", unit="px", config=True),
        ChannelSpec("height", "r", unit="px", config=True),
    ]
    ACTIONS = [ActionSpec("capture_background", "Cover/block the camera first; average dark frames including bias", timeout=3600),
               ActionSpec("clear_background", "Discard the saved background")]
    CONTROLS = {"exposure": 1, "gain": 0, "temperature": 8,
                "target_temperature": 16, "cooler": 17}

    def _check(self, name, *args):
        code = getattr(self.sdk, name)(*args)
        if code != 0:
            raise IOError("%s failed (ASI error %s)" % (name, code))

    def connect(self):
        self.opened = False
        self.background = None
        self.sdk = load_sdk(self.options["sdk_path"])
        index = self._integer(self.options["camera_index"], "camera_index")
        count = self.sdk.ASIGetNumOfConnectedCameras()
        if not 0 <= index < count:
            raise IOError("ZWO camera index %d not available (%d connected); close other camera apps" % (index, count))
        info = CameraInfo()
        self._check("ASIGetCameraProperty", ct.byref(info), index)
        self.camera_id = info.CameraID
        self.camera_name = info.Name.decode(errors="replace")
        self._check("ASIOpenCamera", self.camera_id)
        self.opened = True
        try:
            self._check("ASIInitCamera", self.camera_id)
            self.caps = {}
            n = ct.c_int()
            self._check("ASIGetNumOfControls", self.camera_id, ct.byref(n))
            for k in range(n.value):
                cap = ControlCaps()
                self._check("ASIGetControlCaps", self.camera_id, k, ct.byref(cap))
                self.caps[cap.ControlType] = cap
            binning = self._integer(self.options["binning"], "binning")
            if binning <= 0 or binning not in info.SupportedBins:
                raise ChannelError("Unsupported binning")
            formats = list(info.SupportedVideoFormat)
            if 2 not in formats[:formats.index(-1) if -1 in formats else len(formats)]:
                raise ChannelError("Camera does not support RAW16")
            self.width = (info.MaxWidth // binning) // 8 * 8
            self.height = (info.MaxHeight // binning) // 2 * 2
            if self.width <= 0 or self.height <= 0:
                raise ChannelError("Invalid sensor dimensions")
            maximum = float(self.options["max_exposure"])
            margin = float(self.options["exposure_margin"])
            if not math.isfinite(maximum) or maximum <= 0 or not math.isfinite(margin) or margin <= 0:
                raise ChannelError("max_exposure and exposure_margin must be finite and positive")
            if maximum + margin + 5 >= float(self.options["call_timeout"]):
                raise ChannelError("call_timeout must exceed max_exposure + exposure_margin + 5 seconds")
            self._check("ASISetROIFormat", self.camera_id, self.width, self.height, binning, 2)
            self._check("ASISetStartPos", self.camera_id, 0, 0)
            self._check("ASISetCameraMode", self.camera_id, 0)
            self.roi = dict(IntB=1, IntT=self.height, IntL=1, IntR=self.width)
        except BaseException:
            self.disconnect()
            raise

    def disconnect(self):
        if getattr(self, "opened", False):
            try:
                self._check("ASICloseCamera", self.camera_id)
            finally:
                self.opened = False

    @staticmethod
    def _integer(value, name):
        x = float(value)
        if not math.isfinite(x) or x != int(x):
            raise ChannelError("%s must be an integer" % name)
        return int(x)

    def get_channels(self):
        caps = getattr(self, "caps", None)
        return [c for c in self.CHANNELS if caps is None or c.name not in self.CONTROLS
                or (self.CONTROLS[c.name] in caps and
                    (not c.writable or caps[self.CONTROLS[c.name]].IsWritable))]

    def _control(self, kind):
        value, auto = ct.c_long(), ct.c_int()
        self._check("ASIGetControlValue", self.camera_id, kind, ct.byref(value), ct.byref(auto))
        return value.value

    def _set_control(self, kind, value):
        cap = self.caps.get(kind)
        if cap is None or not cap.IsWritable:
            raise ChannelError("Camera does not support writing control %d" % kind)
        if not cap.MinValue <= value <= cap.MaxValue:
            raise ChannelError("Control %d must be between %d and %d" % (kind, cap.MinValue, cap.MaxValue))
        self._check("ASISetControlValue", self.camera_id, kind, value, 0)

    def _frame(self):
        exposure = self._control(1) / 1e6
        if exposure > float(self.options["max_exposure"]):
            raise ChannelError("Exposure exceeds max_exposure; reduce exposure before acquiring")
        deadline = time.monotonic() + exposure + float(self.options["exposure_margin"])
        self._check("ASIStartExposure", self.camera_id, 0)
        try:
            while True:
                status = ct.c_int()
                self._check("ASIGetExpStatus", self.camera_id, ct.byref(status))
                if status.value == 2:
                    break
                if status.value != 1:
                    raise IOError("ASI exposure failed or became idle (status %d)" % status.value)
                if time.monotonic() >= deadline:
                    raise TimeoutError("ZWO exposure exceeded its deadline")
                time.sleep(0.01)
            frame = np.empty((self.height, self.width), dtype=np.uint16)
            self._check("ASIGetDataAfterExp", self.camera_id,
                        frame.ctypes.data_as(ct.POINTER(ct.c_ubyte)), frame.nbytes)
            return frame.astype(np.float64)
        except BaseException:
            self.sdk.ASIStopExposure(self.camera_id)
            raise

    def read(self, channel):
        if channel in self.CONTROLS:
            value = self._control(self.CONTROLS[channel])
            if channel == "exposure":
                return value / 1e6
            if channel == "temperature":
                return value / 10.0
            if channel == "cooler":
                return "ON" if value else "OFF"
            return float(value)
        if channel in ("width", "height"):
            return float(getattr(self, channel))
        if channel in self.roi:
            return float(self.roi[channel])
        if channel in ("image", "spectrum", "signal"):
            if self.background is not None and self.background_key != self._background_key():
                self.background = None
            frame = self._frame()
            if self.background is not None:
                frame -= self.background
            if channel == "image":
                return frame
            rows = frame[self.roi["IntB"] - 1:self.roi["IntT"], :]
            if channel == "spectrum":
                return rows.sum(axis=0)
            return float(rows[:, self.roi["IntL"] - 1:self.roi["IntR"]].sum())
        raise ChannelError("Unknown channel %s" % channel)

    def write(self, channel, value):
        if channel in self.CONTROLS and channel != "temperature":
            if channel == "cooler":
                if value not in ("ON", "OFF"):
                    raise ChannelError("cooler must be ON or OFF")
                raw = int(value == "ON")
            elif channel == "exposure":
                seconds = float(value)
                if not math.isfinite(seconds) or not 0 < seconds <= float(self.options["max_exposure"]):
                    raise ChannelError("exposure must be positive and no greater than max_exposure")
                raw = round(seconds * 1e6)
            else:
                raw = self._integer(value, channel)
            self._set_control(self.CONTROLS[channel], raw)
            self.background = None
            return
        if channel in self.roi:
            roi = dict(self.roi, **{channel: self._integer(value, channel)})
            if not (1 <= roi["IntB"] <= roi["IntT"] <= self.height and
                    1 <= roi["IntL"] <= roi["IntR"] <= self.width):
                raise ChannelError("ROI must satisfy 1 <= bottom <= top <= height and 1 <= left <= right <= width")
            self.roi = roi
            return
        raise ChannelError("Channel %s is not writable" % channel)

    def _background_key(self):
        return tuple(self._control(k) for k in (1, 0, 16, 17) if k in self.caps)

    def _action_clear_background(self):
        self.background = None

    def _action_capture_background(self):
        n = self._integer(self.options["background_frames"], "background_frames")
        if not 1 <= n <= 50:
            raise ChannelError("background_frames must be 1..50")
        if n * (float(self.options["max_exposure"]) + float(self.options["exposure_margin"]) + 1) >= 3590:
            raise ChannelError("Background acquisition would exceed action timeout; reduce frames or max_exposure")
        key = self._background_key()
        total = np.zeros((self.height, self.width), dtype=np.float64)
        for _ in range(n):
            total += self._frame()
        self.background, self.background_key = total / n, key
