"""Simulated GPIB buses (``SIM::<bus>::<addr>`` addresses), a simulated Prologix
adapter, and fault injection.

A simulated port behaves like a real line-oriented port: the driver writes
commands and reads replies through the normal :class:`Resource` API, so the
whole I/O path (arbitration, timeouts, garbled replies, reconnection) is
exercised.  Several simulated devices sit on one bus, each at its GPIB address
(``SIM::<bus>`` alone is address 0); like on a real GPIB bus, writing to an
address where no device is fails at once (no listener).

:class:`PrologixServer` is a Prologix GPIB-Ethernet adapter on a local TCP port
with the devices of a simulated bus behind it, so ``PROLOGIX::127.0.0.1:<port>::<addr>``
exercises the real Prologix transport.

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
import socket
import threading
import time
from typing import Callable, Dict, List, Optional

from ..common.errors import ResourceError
from ..backend.resources import NoListener, PortBackend, split_gpib


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
        bus, self._addr = split_gpib(address)
        self.bus = get_bus(bus.split("::", 1)[1])
        self._rx: list = []
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
            with self.bus.lock:
                dev = self.bus.devices.get(self._addr)
            if dev is None:                          # the (simulated) GPIB card says so
                raise NoListener("no listener at %s: the instrument is switched off, or "
                                 "the address is wrong" % self.address)
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
            # a list = several reply lines (e.g. a multi-line status listing)
            for r in (reply if isinstance(reply, list) else [reply]):
                if r is not None:
                    if f.hit("garble"):
                        r = f.garble(r)
                    self._rx.append(r)

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
        """A device clear: the input is dropped and the device told (``device_clear``)."""
        self._rx = []
        self._hang_until = 0.0
        with self.bus.lock:
            dev = self.bus.devices.get(self._addr)
        hook = getattr(dev, "device_clear", None)
        if hook is not None:
            hook()


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


# ---------------------------------------------------------------------------
# A simulated Prologix GPIB-Ethernet adapter
# ---------------------------------------------------------------------------
class PrologixServer:
    """A Prologix GPIB-Ethernet adapter on ``127.0.0.1:<port>`` in front of the
    simulated bus *bus*.  It understands ``++addr``, ``++eos``, ``++read eoi``,
    ``++clr``, ``++ver`` and the set-up commands, unescapes data (ESC before CR,
    LF, ESC and ``+``) and hands each data message to the device at the current
    address.  A reply is kept until ``++read`` asks for it; a multi-line reply is
    sent as one message.  ``log`` records everything received, by line."""

    def __init__(self, bus: str):
        self.bus = get_bus(bus)
        self.log: List[str] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._accept, name="sim-prologix", daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        addr, replies = 0, {}                  # GPIB address -> pending reply messages
        esc, line = False, bytearray()
        with conn:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                for b in chunk:
                    if esc:
                        line.append(b)
                        esc = False
                    elif b == 0x1B:
                        esc = True
                    elif b in (0x0A, 0x0D):
                        if b == 0x0A:
                            addr = self._line(conn, bytes(line), addr, replies)
                            line = bytearray()
                    else:
                        line.append(b)

    def _line(self, conn, raw: bytes, addr: int, replies: dict) -> int:
        text = raw.decode("ascii", "replace")
        self.log.append(text)
        if text.startswith("++"):
            head, _, arg = text[2:].partition(" ")
            if head == "addr":
                return int(arg)
            if head == "ver":
                conn.sendall(b"Prologix GPIB-ETHERNET Controller version 01.06.06.00 (sim)\n")
            elif head == "read":
                q = replies.get(addr) or []
                if q:
                    conn.sendall(q.pop(0).encode("ascii") + b"\n")
            elif head == "clr":
                with self.bus.lock:
                    dev = self.bus.devices.get(addr)
                hook = getattr(dev, "device_clear", None)
                if hook is not None:
                    hook()
            return addr
        with self.bus.lock:
            dev = self.bus.devices.get(addr)
        if dev is None:
            return addr                           # nobody there: lost, like on the bus
        reply = dev.handle(text.strip())
        if reply is not None:
            lines = reply if isinstance(reply, list) else [reply]
            replies.setdefault(addr, []).append("\n".join(r for r in lines if r is not None))
        return addr
