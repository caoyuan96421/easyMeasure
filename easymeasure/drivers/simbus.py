"""Simulated byte ports (``SIM::<bus>`` addresses) and fault injection.

A simulated port behaves like a real line-oriented port: the driver writes
commands and reads replies through the normal :class:`Resource` API, so the
whole I/O path (arbitration, timeouts, garbled replies, reconnection) is
exercised.  Several simulated devices may sit on one bus; like a Prologix GPIB
adapter, ``++addr N`` selects which one receives subsequent commands.

Faults are injected by :class:`FaultInjector`, configured through driver
options::

    error_rate      probability that a command is silently dropped (-> read timeout)
    garble_rate     probability that a reply is corrupted
    hang_rate       probability that the port blocks for ``hang_time`` seconds
    hang_time       how long a "hang" lasts (default 30 s, i.e. longer than any deadline)
    disconnect_rate probability that the port "drops" and must be reopened
    exception_rate  probability that the driver raises a random Python exception
    nan_rate        probability that a numeric reading is NaN
    latency         artificial delay per command (seconds)
"""

from __future__ import annotations

import random
import threading
import time
from typing import Callable, Dict, Optional

from ..common.errors import ResourceError
from ..backend.resources import PortBackend


class FaultInjector:
    KEYS = ("error_rate", "garble_rate", "hang_rate", "disconnect_rate", "exception_rate",
            "nan_rate")

    def __init__(self, options: Optional[dict] = None, seed: Optional[int] = None):
        o = options or {}
        self.error_rate = float(o.get("error_rate", 0.0))
        self.garble_rate = float(o.get("garble_rate", 0.0))
        self.hang_rate = float(o.get("hang_rate", 0.0))
        self.hang_time = float(o.get("hang_time", 30.0))
        self.disconnect_rate = float(o.get("disconnect_rate", 0.0))
        self.exception_rate = float(o.get("exception_rate", 0.0))
        self.nan_rate = float(o.get("nan_rate", 0.0))
        self.latency = float(o.get("latency", 0.0))
        self.rng = random.Random(seed if seed is not None else o.get("seed"))
        self.counts = {k: 0 for k in ("error", "garble", "hang", "disconnect", "exception", "nan")}

    @property
    def active(self) -> bool:
        return any(getattr(self, k) > 0 for k in self.KEYS)

    def hit(self, kind: str) -> bool:
        rate = getattr(self, kind + "_rate", 0.0)
        if rate > 0 and self.rng.random() < rate:
            self.counts[kind] += 1
            return True
        return False

    def garble(self, text: str) -> str:
        choice = self.rng.randrange(4)
        if choice == 0:
            return ""  # empty reply
        if choice == 1:
            return text[: max(0, len(text) // 2)] + "#"  # truncated
        if choice == 2:
            return "".join(self.rng.choice("!@#$%^&*?xyz") for _ in range(max(3, len(text))))
        return "ERR -113,\"Undefined header\""

    def random_exception(self) -> BaseException:
        excs = [ZeroDivisionError("simulated division by zero in driver"),
                KeyError("simulated missing key"),
                IndexError("simulated index error"),
                OSError("simulated OS error"),
                RuntimeError("simulated runtime error"),
                AttributeError("simulated attribute error"),
                TypeError("simulated type error"),
                MemoryError("simulated memory error"),
                SystemExit(3)]  # buggy drivers sometimes call sys.exit()!
        self.counts["exception"] += 1
        return self.rng.choice(excs)


class SimDevice:
    """Base class of a device living on a simulated bus."""

    def __init__(self, faults: Optional[FaultInjector] = None):
        self.faults = faults or FaultInjector()

    def handle(self, command: str) -> Optional[str]:
        """Process one command; return a reply line for queries."""
        raise NotImplementedError


class _Bus:
    def __init__(self, name: str):
        self.name = name
        self.devices: Dict[int, SimDevice] = {}
        self.lock = threading.Lock()


_buses: Dict[str, _Bus] = {}
_buses_lock = threading.Lock()


def get_bus(name: str) -> _Bus:
    with _buses_lock:
        b = _buses.get(name.upper())
        if b is None:
            b = _Bus(name.upper())
            _buses[name.upper()] = b
        return b


def attach_device(bus: str, addr: int, device: SimDevice) -> None:
    b = get_bus(bus)
    with b.lock:
        b.devices[int(addr)] = device


def detach_device(bus: str, addr: int) -> None:
    b = get_bus(bus)
    with b.lock:
        b.devices.pop(int(addr), None)


def reset_buses() -> None:
    with _buses_lock:
        _buses.clear()


class SimPortBackend(PortBackend):
    """PortBackend implementation for ``SIM::<bus>`` addresses."""

    def __init__(self, address, options):
        super().__init__(address, options)
        self.bus = get_bus(address.split("::", 1)[1])
        self._rx: list = []
        self._addr = 0
        self._open = False
        self._hang_until = 0.0
        self.timeout = float(options.get("timeout", 0.5))

    def open(self):
        self._open = True
        self._rx = []

    def close(self):
        self._open = False
        self._rx = []

    def _check(self):
        if not self._open:
            raise ResourceError("simulated port %s is closed" % self.address)

    def write_raw(self, data: bytes) -> None:
        self._check()
        text = data.decode(self.encoding, errors="replace")
        term = self.write_termination or "\n"
        for cmd in [c.strip() for c in text.split(term)]:
            if not cmd:
                continue
            if cmd.startswith("++addr"):
                try:
                    self._addr = int(cmd.split()[1])
                except (IndexError, ValueError):
                    pass
                continue
            with self.bus.lock:
                dev = self.bus.devices.get(self._addr)
            if dev is None:
                continue  # nobody listening -> the read will time out
            f = dev.faults
            if f.latency:
                time.sleep(f.latency)
            if f.hit("disconnect"):
                self._open = False
                raise ResourceError("simulated port %s disconnected" % self.address)
            if f.hit("hang"):
                self._hang_until = time.monotonic() + f.hang_time
            if f.hit("error"):
                continue  # command lost
            reply = dev.handle(cmd)
            if reply is not None:
                if f.hit("garble"):
                    reply = f.garble(reply)
                self._rx.append(reply)

    def read_raw(self, size=None) -> bytes:
        self._check()
        if self._hang_until > time.monotonic():
            # simulate a device/port that blocks the calling thread
            while time.monotonic() < self._hang_until and self._open:
                time.sleep(0.05)
            self._hang_until = 0.0
            raise ResourceError("simulated port %s was hung" % self.address)
        if not self._rx:
            time.sleep(min(self.timeout, 0.2))
            raise ResourceError("read timeout on %s" % self.address)
        line = self._rx.pop(0)
        return (line + (self.read_termination or "\n")).encode(self.encoding)

    def clear(self):
        self._rx = []
        self._hang_until = 0.0


class SCPIDevice(SimDevice):
    """Helper: dispatch ``HEAD:SUB value`` / ``HEAD:SUB?`` commands to callables."""

    def __init__(self, faults=None, idn="easyMeasure,SimDevice,0,1.0"):
        super().__init__(faults)
        self.idn = idn
        self.setters: Dict[str, Callable[[str], None]] = {}
        self.getters: Dict[str, Callable[[], str]] = {}

    def handle(self, command: str) -> Optional[str]:
        cmd = command.strip()
        up = cmd.upper()
        if up == "*IDN?":
            return self.idn
        if up in ("*RST", "*CLS"):
            return None
        if up.endswith("?"):
            key = up[:-1]
            fn = self.getters.get(key)
            if fn is None:
                return "ERR -113,\"Undefined header\""
            return fn()
        head, _, arg = cmd.partition(" ")
        fn = self.setters.get(head.upper())
        if fn is not None:
            try:
                fn(arg.strip())
            except Exception:
                pass  # a real device would set an error flag
        return None
