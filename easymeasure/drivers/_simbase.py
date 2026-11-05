"""Helpers shared by the simulated drivers (not a driver itself)."""

from __future__ import annotations

import threading
import time
from typing import Dict, Tuple

import numpy as np

from ..backend.instrument import Instrument
from .simbus import FaultInjector, SimDevice, attach_device

#: devices persist across driver re-creation (like real hardware keeps its state)
_DEVICES: Dict[Tuple[str, int], SimDevice] = {}
_DEV_LOCK = threading.Lock()

FAULT_OPTION_DOC = {
    "error_rate": 0.0, "garble_rate": 0.0, "hang_rate": 0.0, "hang_time": 30.0,
    "disconnect_rate": 0.0, "exception_rate": 0.0, "nan_rate": 0.0, "latency": 0.0,
}


def get_or_create_device(bus: str, addr: int, factory) -> SimDevice:
    key = (bus.upper(), int(addr))
    with _DEV_LOCK:
        dev = _DEVICES.get(key)
        if dev is None:
            dev = factory()
            _DEVICES[key] = dev
    attach_device(bus, addr, dev)
    return dev


def reset_devices() -> None:
    with _DEV_LOCK:
        _DEVICES.clear()


class SimBusInstrument(Instrument):
    """Base for simulated instruments that talk SCPI over a ``SIM::`` port."""

    SHARED_RESOURCE = True

    def bus_name(self) -> str:
        if not self.address.upper().startswith("SIM::"):
            raise ValueError("%s needs a SIM::<bus> address" % type(self).__name__)
        return self.address.split("::", 1)[1]

    @property
    def gpib_addr(self) -> int:
        return int(self.options.get("gpib_addr", 0))

    def faults(self) -> FaultInjector:
        return FaultInjector(self.options)

    def select(self, resource):
        resource.write("++addr %d" % self.gpib_addr)

    def connect(self):
        self.resource.write("++addr %d" % self.gpib_addr)
        idn = self.resource.query("*IDN?")
        if not idn or "," not in idn:
            raise IOError("unexpected *IDN? reply %r" % idn)
        self.idn = idn

    def maybe_nan(self, value: float) -> float:
        dev = self.device
        if dev is not None and dev.faults.hit("nan"):
            return float("nan")
        return value

    def maybe_raise(self) -> None:
        dev = self.device
        if dev is not None and dev.faults.hit("exception"):
            raise dev.faults.random_exception()

    device = None


class SimDirectInstrument(Instrument):
    """Base for simulated instruments that do not use a port.

    Fault injection happens directly in the driver (exceptions, hangs, NaNs,
    latency).  ``garble_rate`` makes reads return values of the wrong type.
    """

    def __init__(self, name, address, options=None):
        super().__init__(name, address, options)
        self.fi = FaultInjector(self.options)
        self.rng = np.random.default_rng()

    def gate(self) -> None:
        """Call at the start of every driver operation."""
        f = self.fi
        if f.latency:
            time.sleep(f.latency)
        if f.hit("hang"):
            time.sleep(f.hang_time)
        if f.hit("error"):
            raise IOError("simulated communication error")
        if f.hit("disconnect"):
            raise ConnectionError("simulated cable unplugged")
        if f.rng.random() < f.exception_rate:
            raise f.random_exception()

    def finish(self, value):
        """Post-process a reading (NaN / garbage injection)."""
        f = self.fi
        if f.hit("garble"):
            return f.rng.choice([None, "garbage", {"a": 1}, [1, [2, 3]], b"\xff\xfe"])
        if f.hit("nan"):
            if isinstance(value, np.ndarray):
                v = value.copy()
                v.flat[self.rng.integers(v.size)] = np.nan
                return v
            return float("nan")
        return value
