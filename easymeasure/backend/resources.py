"""Resource management: low-level ports (COM / GPIB / USB / Ethernet / simulated).

Responsibilities
----------------
* Each physical port is represented by exactly one :class:`Resource` object, so
  two instruments can never open the same port independently and fight over it.
* By default a resource is *exclusive*: a second instrument asking for it gets a
  :class:`ResourceBusyError`.
* Shared ports (a GPIB bus behind a Prologix adapter, an RS-485 chain, a
  multi-channel controller ...) can be requested with ``shared=True``.  Access is
  then arbitrated: every I/O sequence must run inside
  ``with resource.transaction(owner):`` which grants a fair, FIFO, re-entrant
  lock.  When ownership switches from one instrument to another an optional
  *selector* callback of the new owner is run (e.g. send ``++addr 12``).
* Resources can be *invalidated* (e.g. after a hung call).  The next transaction
  transparently reopens the port; stale handles held by abandoned threads are
  closed and ignored.

Address formats
---------------
=================================  ===========================================
``COM3`` / ``ASRL3::INSTR``         serial port (pyserial, or pyvisa for ASRL)
``/dev/ttyUSB0``                    serial port (pyserial)
``GPIB0::12::INSTR``                pyvisa
``USB0::0x1234::...::INSTR``        pyvisa
``TCPIP0::1.2.3.4::inst0::INSTR``   pyvisa
``TCP::192.168.1.5::5025``          raw TCP socket (built-in, no pyvisa needed)
``SIM::<name>``                     simulated byte port (see drivers/simbus.py)
``DAQ::<device>``                   NI-DAQmx device (``DAQ::Dev1``); no byte port, but
                                    one owner, so two drivers cannot share a DAQ card
``NONE`` or empty                   instrument without a port (DLL/software)
=================================  ===========================================

Each address belongs to one *interface* (:func:`interface_of`): ``gpib``,
``usb``, ``tcpip`` (VISA LAN), ``tcp`` (raw socket), ``serial``, ``daq``, ``sim``,
``none`` or ``other`` (any other VISA type).  Drivers list the interfaces they
accept (``Instrument.INTERFACES``); :meth:`ResourceManager.discover` finds the
ports of this computer by interface, optionally with their ``*IDN?``.
"""

from __future__ import annotations

import socket
import threading
import time
from collections import deque
from typing import Callable, Dict, Optional, Set, Union

from ..common.errors import ResourceBusyError, ResourceError
from .logsys import get_logger

log = get_logger("resources")

Data = Union[str, bytes]


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------
INTERFACES = ("gpib", "usb", "tcpip", "tcp", "serial", "daq", "sim", "none", "other")
#: everything that carries bytes (SCPI and other command-based instruments)
BYTE_PORTS = ("gpib", "usb", "tcpip", "tcp", "serial", "other", "sim")
#: GPIB instruments: GPIB, or a Prologix-style GPIB-USB (serial) / GPIB-Ethernet
#: (TCP) adapter, or RS-232 where the instrument has it
GPIB_LIKE = ("gpib", "serial", "tcp", "sim")
INTERFACE_NAMES = {"gpib": "GPIB", "usb": "USB (USBTMC)", "tcpip": "LAN (VISA)",
                   "tcp": "TCP socket", "serial": "serial / COM", "daq": "NI-DAQmx",
                   "sim": "simulated", "none": "no port", "other": "other VISA"}


def interface_of(address: Optional[str]) -> str:
    """The interface an address belongs to (see :data:`INTERFACES`)."""
    a = (address or "").strip()
    up = a.upper()
    if not a or up == "NONE":
        return "none"
    if up.startswith("SIM::"):
        return "sim"
    if up.startswith("DAQ::"):
        return "daq"
    if up.startswith("TCP::"):
        return "tcp"
    if up.startswith(("COM", "ASRL")) or a.startswith("/dev/"):
        return "serial"
    for prefix, kind in (("GPIB", "gpib"), ("USB", "usb"), ("TCPIP", "tcpip")):
        if up.startswith(prefix):
            return kind
    return "other"


# ---------------------------------------------------------------------------
# Fair re-entrant lock
# ---------------------------------------------------------------------------
_prio = threading.local()

#: priority levels for lock arbitration (lower = served first)
PRIO_MEASUREMENT = 0     # the run-queue thread (sweeps, scripts)
PRIO_NORMAL = 1          # monitor, GUI requests, anything else


def set_thread_priority(p: int) -> None:
    _prio.value = int(p)


def thread_priority() -> int:
    return getattr(_prio, "value", PRIO_NORMAL)


def set_abort_check(fn) -> None:
    """Register a non-blocking 'raise if aborted' callable for this thread.

    Instrument retries / reconnects consult it, so an abort is honoured between
    attempts instead of only after all retries of a misbehaving instrument."""
    _prio.abort_check = fn


def abort_check() -> None:
    fn = getattr(_prio, "abort_check", None)
    if fn is not None:
        fn()


class FairLock:
    """Re-entrant, FIFO-within-priority lock with timeout.

    Plain ``threading.RLock`` is not fair: a fast loop (e.g. the monitor) can starve
    a sweep.  Here waiters are served in arrival order, except that waiters with a
    better priority (the measurement thread) go first, so monitoring and GUI
    traffic never slow a running sweep down.
    """

    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        self._owner: Optional[int] = None
        self._count = 0
        self._queue: list = []          # [(priority, seq, ticket)]
        self._seq = 0
        self.owner_tag: Optional[str] = None  # who holds it (for diagnostics)

    def acquire(self, timeout: Optional[float] = None, tag: Optional[str] = None,
                priority: Optional[int] = None) -> bool:
        me = threading.get_ident()
        deadline = None if timeout is None else time.monotonic() + timeout
        prio = thread_priority() if priority is None else priority
        with self._cond:
            if self._owner == me:
                self._count += 1
                return True
            self._seq += 1
            entry = (prio, self._seq, object())
            self._queue.append(entry)
            try:
                while not (self._owner is None and min(self._queue) is entry):
                    if deadline is None:
                        self._cond.wait()
                    else:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return False
                        self._cond.wait(remaining)
            finally:
                # remove our ticket whether we got the lock or timed out
                try:
                    self._queue.remove(entry)
                except ValueError:
                    pass
                self._cond.notify_all()
            self._owner = me
            self._count = 1
            self.owner_tag = tag
            return True

    def release(self) -> None:
        with self._cond:
            if self._owner != threading.get_ident():
                raise RuntimeError("release of un-acquired FairLock")
            self._count -= 1
            if self._count == 0:
                self._owner = None
                self.owner_tag = None
                self._cond.notify_all()

    def force_release(self) -> None:
        """Release the lock regardless of owner (used after abandoning a hung thread)."""
        with self._cond:
            self._owner = None
            self._count = 0
            self.owner_tag = None
            self._cond.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    @property
    def waiters(self) -> int:
        return len(self._queue)

    def held_by_me(self) -> bool:
        return self._owner == threading.get_ident()

    @property
    def locked(self) -> bool:
        return self._owner is not None


# ---------------------------------------------------------------------------
# Port backends (the thing that actually moves bytes)
# ---------------------------------------------------------------------------
class PortBackend:
    """Minimal byte-oriented interface implemented per port type."""

    def __init__(self, address: str, options: dict):
        self.address = address
        self.options = options
        self.timeout = float(options.get("timeout", 2.0))
        self.read_termination = options.get("read_termination", "\n")
        self.write_termination = options.get("write_termination", "\n")
        self.encoding = options.get("encoding", "ascii")

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def write_raw(self, data: bytes) -> None:
        raise NotImplementedError

    def read_raw(self, size: Optional[int] = None) -> bytes:
        """Read *size* bytes, or up to the read termination when size is None."""
        raise NotImplementedError

    def clear(self) -> None:
        """Flush input buffers (best effort)."""

    # convenience used by Resource
    def write(self, data: Data) -> None:
        if isinstance(data, str):
            data = (data + (self.write_termination or "")).encode(self.encoding)
        self.write_raw(data)

    def read(self) -> str:
        raw = self.read_raw(None)
        text = raw.decode(self.encoding, errors="replace")
        term = self.read_termination or ""
        if term and text.endswith(term):
            text = text[: -len(term)]
        return text.rstrip("\r\n")


class NullBackend(PortBackend):
    """For instruments that have no port of their own."""

    def open(self):
        pass

    def close(self):
        pass

    def write_raw(self, data):
        raise ResourceError("resource %r has no port" % self.address)

    def read_raw(self, size=None):
        raise ResourceError("resource %r has no port" % self.address)


class SocketBackend(PortBackend):
    """Raw TCP socket: ``TCP::host::port``."""

    def __init__(self, address, options):
        super().__init__(address, options)
        parts = address.split("::")
        if len(parts) != 3:
            raise ResourceError("TCP address must be TCP::host::port, got %r" % address)
        self.host, self.port = parts[1], int(parts[2])
        self._sock: Optional[socket.socket] = None
        self._rx = bytearray()

    def open(self):
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        self._sock = s
        self._rx.clear()

    def close(self):
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def _need(self) -> socket.socket:
        if self._sock is None:
            raise ResourceError("socket %s not open" % self.address)
        return self._sock

    def write_raw(self, data):
        try:
            self._need().sendall(data)
        except OSError as exc:
            raise ResourceError("write to %s failed: %s" % (self.address, exc)) from exc

    def read_raw(self, size=None):
        sock = self._need()
        term = (self.read_termination or "").encode(self.encoding)
        deadline = time.monotonic() + self.timeout
        while True:
            if size is not None and len(self._rx) >= size:
                out = bytes(self._rx[:size])
                del self._rx[:size]
                return out
            if size is None and term and term in self._rx:
                i = self._rx.index(term) + len(term)
                out = bytes(self._rx[:i])
                del self._rx[:i]
                return out
            if time.monotonic() > deadline:
                raise ResourceError("read timeout on %s" % self.address)
            try:
                chunk = sock.recv(65536)
            except socket.timeout as exc:
                raise ResourceError("read timeout on %s" % self.address) from exc
            except OSError as exc:
                raise ResourceError("read from %s failed: %s" % (self.address, exc)) from exc
            if not chunk:
                raise ResourceError("connection %s closed by peer" % self.address)
            self._rx.extend(chunk)

    def clear(self):
        self._rx.clear()


class SerialBackend(PortBackend):
    """Serial port through pyserial."""

    def __init__(self, address, options):
        super().__init__(address, options)
        self._ser = None
        port = address
        if address.upper().startswith("ASRL"):
            num = address[4:].split("::")[0]
            port = "COM" + num if num.isdigit() else num
        self.port = port

    def open(self):
        try:
            import serial  # type: ignore
        except ImportError as exc:
            raise ResourceError("pyserial is required for %s" % self.address) from exc
        o = self.options
        try:
            self._ser = serial.Serial(
                self.port,
                baudrate=int(o.get("baudrate", 9600)),
                bytesize=int(o.get("bytesize", 8)),
                parity=o.get("parity", "N"),
                stopbits=float(o.get("stopbits", 1)),
                timeout=self.timeout,
                write_timeout=self.timeout,
                rtscts=bool(o.get("rtscts", False)),
                xonxoff=bool(o.get("xonxoff", False)),
            )
        except Exception as exc:
            raise ResourceError("cannot open serial port %s: %s" % (self.port, exc)) from exc

    def close(self):
        s, self._ser = self._ser, None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    def write_raw(self, data):
        if self._ser is None:
            raise ResourceError("serial port %s not open" % self.port)
        try:
            self._ser.write(data)
            self._ser.flush()
        except Exception as exc:
            raise ResourceError("serial write failed on %s: %s" % (self.port, exc)) from exc

    def read_raw(self, size=None):
        if self._ser is None:
            raise ResourceError("serial port %s not open" % self.port)
        try:
            if size is not None:
                data = self._ser.read(size)
                if len(data) < size:
                    raise ResourceError("serial read timeout on %s" % self.port)
                return data
            term = (self.read_termination or "\n").encode(self.encoding)
            data = self._ser.read_until(term)
            if not data.endswith(term):
                raise ResourceError("serial read timeout on %s" % self.port)
            return data
        except ResourceError:
            raise
        except Exception as exc:
            raise ResourceError("serial read failed on %s: %s" % (self.port, exc)) from exc

    def clear(self):
        try:
            if self._ser is not None:
                self._ser.reset_input_buffer()
        except Exception:
            pass


class VisaBackend(PortBackend):
    """GPIB / USBTMC / VXI-11 / HiSLIP through pyvisa."""

    _rm = None
    _rm_lock = threading.Lock()

    def __init__(self, address, options):
        super().__init__(address, options)
        self._inst = None

    @classmethod
    def _resource_manager(cls, backend: str = ""):
        with cls._rm_lock:
            if cls._rm is None:
                try:
                    import pyvisa  # type: ignore
                except ImportError as exc:
                    raise ResourceError("pyvisa is required for VISA addresses") from exc
                cls._rm = pyvisa.ResourceManager(backend) if backend else pyvisa.ResourceManager()
            return cls._rm

    def open(self):
        rm = self._resource_manager(self.options.get("visa_backend", ""))
        try:
            inst = rm.open_resource(self.address)
            inst.timeout = int(self.timeout * 1000)
            if self.read_termination is not None:
                inst.read_termination = self.read_termination
            if self.write_termination is not None:
                inst.write_termination = ""  # we append ourselves
            self._inst = inst
        except Exception as exc:
            raise ResourceError("cannot open VISA resource %s: %s" % (self.address, exc)) from exc

    def close(self):
        inst, self._inst = self._inst, None
        if inst is not None:
            try:
                inst.close()
            except Exception:
                pass

    def write_raw(self, data):
        if self._inst is None:
            raise ResourceError("VISA resource %s not open" % self.address)
        try:
            self._inst.write_raw(data)
        except Exception as exc:
            raise ResourceError("VISA write failed on %s: %s" % (self.address, exc)) from exc

    def read_raw(self, size=None):
        if self._inst is None:
            raise ResourceError("VISA resource %s not open" % self.address)
        try:
            if size is not None:
                return self._inst.read_bytes(size)
            return self._inst.read_raw()
        except Exception as exc:
            raise ResourceError("VISA read failed on %s: %s" % (self.address, exc)) from exc

    def clear(self):
        try:
            if self._inst is not None:
                self._inst.clear()
        except Exception:
            pass


def _backend_for(address: str, options: dict) -> PortBackend:
    a = address.strip()
    up = a.upper()
    if not a or up == "NONE":
        return NullBackend("NONE", options)
    if up.startswith("SIM::"):
        from ..drivers.simbus import SimPortBackend  # local import avoids a cycle
        return SimPortBackend(a, options)
    if up.startswith("DAQ::"):
        return NullBackend(a, options)           # the driver talks to NI-DAQmx itself
    if up.startswith("TCP::"):
        return SocketBackend(a, options)
    if up.startswith("COM") or a.startswith("/dev/"):
        return SerialBackend(a, options)
    if up.startswith("ASRL") and not options.get("use_visa", False):
        return SerialBackend(a, options)
    if "::" in a:
        return VisaBackend(a, options)
    raise ResourceError("unrecognised resource address %r" % address)


def normalize_address(address: Optional[str]) -> str:
    a = (address or "").strip()
    if not a or a.upper() == "NONE":
        return "NONE"
    up = a.upper()
    if up.startswith("COM"):
        return up
    if up.startswith("DAQ::"):
        return "DAQ::" + a[5:]                   # device names are case sensitive
    if up.startswith(("GPIB", "USB", "TCPIP", "ASRL", "TCP::", "SIM::")):
        # VISA addresses are case-insensitive in the prefix; keep the remainder
        head, sep, rest = a.partition("::")
        return head.upper() + sep + rest
    return a


# ---------------------------------------------------------------------------
# Resource object
# ---------------------------------------------------------------------------
class Resource:
    """One physical port, shared by one or more owners under arbitration."""

    def __init__(self, address: str, shared: bool, options: dict):
        self.address = address
        self.shared = shared
        self.options = dict(options)
        self.owners: Set[str] = set()
        self.lock = FairLock()
        self._backend: Optional[PortBackend] = None
        self._generation = 0
        self._state_lock = threading.Lock()
        self._selectors: Dict[str, Callable[["Resource"], None]] = {}
        self._last_owner: Optional[str] = None
        self.lock_timeout = float(options.get("lock_timeout", 30.0))
        self.stats = {"opens": 0, "errors": 0, "invalidations": 0}

    # -- lifecycle ------------------------------------------------------------
    def _ensure_open(self) -> PortBackend:
        # The port is opened WITHOUT holding _state_lock: a driver/OS call that
        # hangs in open() must never block close()/invalidate() of other threads.
        with self._state_lock:
            if self._backend is not None:
                return self._backend
            gen = self._generation
        be = _backend_for(self.address, self.options)
        be.open()
        with self._state_lock:
            if self._generation != gen or self._backend is not None:
                stale = True
            else:
                stale = False
                self._backend = be
                self._generation += 1
                self.stats["opens"] += 1
                self._last_owner = None  # force re-select after reopen
        if stale:
            _close_in_background(be, self.address)
            raise ResourceError("port %s was invalidated while it was being opened" % self.address)
        log.debug("opened resource %s (generation %d)", self.address, self._generation)
        # if the port was reopened in the middle of a transaction on a shared bus,
        # re-select the current owner so traffic is not misdirected
        owner = self.lock.owner_tag
        if self.shared and owner is not None and self.lock.held_by_me():
            sel = self._selectors.get(owner)
            if sel is not None:
                self._last_owner = owner
                sel(self)
        return be

    def close(self) -> None:
        with self._state_lock:
            be, self._backend = self._backend, None
            self._generation += 1
        if be is not None:
            _close_in_background(be, self.address)

    def invalidate(self, reason: str = "") -> None:
        """Drop the current handle; the next transaction reopens the port."""
        self.stats["invalidations"] += 1
        log.warning("invalidating resource %s%s", self.address, (": " + reason) if reason else "")
        self.close()

    @property
    def is_open(self) -> bool:
        return self._backend is not None

    # -- arbitration ----------------------------------------------------------
    def set_selector(self, owner: str, selector: Optional[Callable[["Resource"], None]]) -> None:
        """Register a callback run whenever *owner* takes over the shared port."""
        if selector is None:
            self._selectors.pop(owner, None)
        else:
            self._selectors[owner] = selector

    def transaction(self, owner: str, timeout: Optional[float] = None) -> "_Transaction":
        return _Transaction(self, owner, self.lock_timeout if timeout is None else timeout)

    # -- I/O (must be called inside a transaction) -----------------------------
    def _io(self) -> PortBackend:
        if not self.lock.held_by_me():
            raise ResourceError("I/O on %s outside of a transaction" % self.address)
        return self._ensure_open()

    def write(self, data: Data) -> None:
        try:
            self._io().write(data)
        except ResourceError:
            self.stats["errors"] += 1
            raise
        except Exception as exc:
            self.stats["errors"] += 1
            raise ResourceError("write on %s failed: %s" % (self.address, exc)) from exc

    def read(self) -> str:
        try:
            return self._io().read()
        except ResourceError:
            self.stats["errors"] += 1
            raise
        except Exception as exc:
            self.stats["errors"] += 1
            raise ResourceError("read on %s failed: %s" % (self.address, exc)) from exc

    def read_raw(self, size: Optional[int] = None) -> bytes:
        try:
            return self._io().read_raw(size)
        except ResourceError:
            self.stats["errors"] += 1
            raise
        except Exception as exc:
            self.stats["errors"] += 1
            raise ResourceError("read on %s failed: %s" % (self.address, exc)) from exc

    def query(self, cmd: Data, delay: float = 0.0) -> str:
        self.write(cmd)
        if delay:
            time.sleep(delay)
        return self.read()

    def clear(self) -> None:
        try:
            self._io().clear()
        except Exception:
            pass

    @property
    def backend(self) -> PortBackend:
        """Direct access to the port object (inside a transaction) for exotic drivers."""
        return self._io()

    def describe(self) -> dict:
        return {
            "address": self.address,
            "shared": self.shared,
            "owners": sorted(self.owners),
            "open": self.is_open,
            "locked_by": self.lock.owner_tag,
            "stats": dict(self.stats),
        }


class _Transaction:
    def __init__(self, res: Resource, owner: str, timeout: float):
        self.res = res
        self.owner = owner
        self.timeout = timeout

    def __enter__(self) -> Resource:
        if not self.res.lock.acquire(self.timeout, tag=self.owner):
            raise ResourceBusyError("timeout waiting for resource %s (held by %s)"
                                    % (self.res.address, self.res.lock.owner_tag))
        try:
            if self.res.shared and self.res._last_owner != self.owner:
                sel = self.res._selectors.get(self.owner)
                if sel is not None:
                    self.res._ensure_open()        # may already re-select on reopen
                    if self.res._last_owner != self.owner:
                        sel(self.res)
                self.res._last_owner = self.owner
        except BaseException:
            self.res.lock.release()
            raise
        return self.res

    def __exit__(self, exc_type, exc, tb):
        try:
            self.res.lock.release()
        except RuntimeError:
            # lock was force-released after a hang; nothing to do
            pass
        return False


def _close_in_background(be: PortBackend, address: str) -> None:
    """Close a port without risking that a hung driver blocks the caller."""

    def _do():
        try:
            be.close()
        except Exception as exc:  # pragma: no cover - best effort
            log.debug("error closing %s: %s", address, exc)

    t = threading.Thread(target=_do, name="close-%s" % address, daemon=True)
    t.start()
    t.join(2.0)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
class ResourceManager:
    """Registry of all open resources."""

    def __init__(self):
        self._resources: Dict[str, Resource] = {}
        self._lock = threading.Lock()

    def acquire(self, address: Optional[str], owner: str, shared: bool = False,
                options: Optional[dict] = None) -> Resource:
        addr = normalize_address(address)
        options = options or {}
        with self._lock:
            res = self._resources.get(addr)
            if addr == "NONE":
                # every port-less instrument gets its own null resource
                key = "NONE#" + owner
                res = self._resources.get(key)
                if res is None:
                    res = Resource("NONE", False, options)
                    self._resources[key] = res
                res.owners.add(owner)
                return res
            if res is not None:
                if owner in res.owners:
                    return res
                if not (res.shared and shared):
                    raise ResourceBusyError(
                        "resource %s is already used by %s%s" % (
                            addr, ", ".join(sorted(res.owners)),
                            "" if res.shared else " (exclusive)"))
                res.owners.add(owner)
                log.info("%s joined shared resource %s (owners: %s)", owner, addr,
                         ", ".join(sorted(res.owners)))
                return res
            res = Resource(addr, shared, options)
            res.owners.add(owner)
            self._resources[addr] = res
            log.info("%s acquired resource %s (%s)", owner, addr, "shared" if shared else "exclusive")
            return res

    def release(self, res: Resource, owner: str) -> None:
        with self._lock:
            res.owners.discard(owner)
            res.set_selector(owner, None)
            if res.owners:
                return
            for k, v in list(self._resources.items()):
                if v is res:
                    del self._resources[k]
        res.close()
        log.info("released resource %s", res.address)

    def list(self) -> list:
        with self._lock:
            return [r.describe() for r in self._resources.values()]

    def close_all(self) -> None:
        with self._lock:
            items = list(self._resources.values())
            self._resources.clear()
        for r in items:
            r.close()

    def discover(self, interfaces=None, identify: bool = False, timeout: float = 0.8,
                 in_use: Optional[Dict[str, list]] = None) -> dict:
        """Ports on this computer: ``{"found": [{address, interface, description,
        [idn], [in_use]}], "notes": [why something could not be scanned]}``.

        *interfaces* limits the scan; *identify* asks VISA instruments (GPIB / USB /
        LAN) ``*IDN?`` - not those open here or listed in *in_use* ({address: [users]},
        the configured instruments), which are left alone."""
        want = set(interfaces or INTERFACES)
        found, notes = [], []
        with self._lock:
            busy = {k.upper(): r for k, r in self._resources.items()}
        serial_names = set()
        if "serial" in want:
            try:
                from serial.tools import list_ports  # type: ignore
                for p in sorted(list_ports.comports(), key=lambda p: p.device):
                    desc = p.description if p.description not in (None, "n/a") else ""
                    if p.manufacturer and p.manufacturer not in desc:
                        desc = ("%s (%s)" % (desc, p.manufacturer)).strip()
                    found.append({"address": p.device, "interface": "serial",
                                  "description": desc or p.device})
                    serial_names.add(p.device.upper())
            except ImportError:
                notes.append("serial ports: pyserial is not installed (pip install pyserial)")
            except Exception as exc:
                notes.append("serial ports: %s" % exc)
        visa_kinds = {"gpib", "usb", "tcpip", "other", "serial"} & want
        if visa_kinds:
            try:
                rm = VisaBackend._resource_manager()
                info = rm.list_resources_info("?*::INSTR")
                for addr, ri in sorted(info.items()):
                    kind = interface_of(addr)
                    if kind not in visa_kinds:
                        continue
                    if kind == "serial":
                        num = addr.upper()[4:].split("::")[0]
                        if ("COM" + num) in serial_names or num in serial_names:
                            continue                    # pyserial listed it already
                    alias = getattr(ri, "alias", None) or ""
                    found.append({"address": addr, "interface": kind,
                                  "description": ("VISA alias %s" % alias) if alias else ""})
            except ResourceError as exc:
                if visa_kinds - {"serial"}:
                    notes.append("GPIB / USB / LAN: %s (pip install pyvisa, and a VISA library "
                                 "such as NI-VISA or pip install pyvisa-py)" % exc)
            except Exception as exc:
                notes.append("VISA: %s" % exc)
        if "daq" in want:
            try:
                import nidaqmx.system  # type: ignore
                for d in nidaqmx.system.System.local().devices:
                    found.append({"address": "DAQ::" + d.name, "interface": "daq",
                                  "description": str(d.product_type), "idn": str(d.product_type)})
            except ImportError:
                notes.append("NI-DAQmx: the nidaqmx package is not installed")
            except Exception as exc:
                text = str(exc).splitlines()[0]
                if "could not find an installation" in text.lower():
                    text = "the NI-DAQmx driver runtime is not installed (ni.com)"
                notes.append("NI-DAQmx: %s" % text)
        users = {normalize_address(k).upper(): v for k, v in (in_use or {}).items()}
        for e in found:
            key = normalize_address(e["address"]).upper()
            r = busy.get(e["address"].upper())
            if key in users:
                e["in_use"] = sorted(users[key])
            elif r is not None:
                e["in_use"] = sorted(r.owners)
        if identify:
            for e in found:
                if e["interface"] in ("gpib", "usb", "tcpip", "other") and not e.get("in_use"):
                    e["idn"] = self._identify(e["address"], timeout)
        return {"found": found, "notes": notes}

    @staticmethod
    def _identify(address: str, timeout: float) -> str:
        """``*IDN?`` of a VISA instrument nobody here has open ('' if it does not answer)."""
        try:
            rm = VisaBackend._resource_manager()
            inst = rm.open_resource(address, open_timeout=int(timeout * 1000))
        except Exception:
            return ""
        try:
            inst.timeout = int(timeout * 1000)
            return str(inst.query("*IDN?")).strip()
        except Exception:
            return ""
        finally:
            try:
                inst.close()
            except Exception:
                pass
