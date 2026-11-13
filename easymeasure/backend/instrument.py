"""Instrument abstraction layer.

Two classes matter:

:class:`Instrument`
    Base class for drivers.  A driver declares its channels (``CHANNELS``) and
    implements ``read(channel)`` / ``write(channel, value)`` (or per-channel
    ``_read_<name>`` / ``_write_<name>`` methods).  Drivers are written in the
    obvious, naive way - all the paranoia lives in :class:`InstrumentHandle`.

:class:`InstrumentHandle`
    The guarded wrapper the rest of the backend uses.  Every driver call:

    * runs on a dedicated worker thread with a hard deadline - a driver that hangs
      forever is abandoned, its port invalidated and a fresh worker started;
    * runs inside a resource transaction (port arbitration);
    * has every exception (including ``SystemExit`` / ``KeyboardInterrupt`` raised
      by buggy driver code) caught and converted to :class:`InstrumentError`;
    * is retried with recovery / reconnection according to the instrument options;
    * has its result validated and coerced to the declared type and shape.

Channel values are either numeric (stored as ``float`` or ``numpy.ndarray`` of
float64 of any dimension) or strings.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..common.errors import (ChannelError, EasyMeasureError, InstrumentError, InstrumentFault,
                             InstrumentNotConnected, InstrumentTimeout, ResourceError)
from .logsys import RateLimitedLogger, get_logger
from .resources import (INTERFACE_NAMES, INTERFACES, PRIO_MEASUREMENT, FairLock, Resource,
                        ResourceManager, abort_check, interface_of,
                        set_thread_priority, thread_priority)

log = get_logger("instrument")


# ---------------------------------------------------------------------------
# Channel specification
# ---------------------------------------------------------------------------
@dataclass
class ChannelSpec:
    """Static description of an instrument channel.

    ``shape``: ``()`` for scalars, otherwise a tuple whose entries are fixed sizes
    or ``-1`` for "any size".  ``None`` means "unknown / anything".

    ``allowed``: optional list of the accepted values (strings for ``str`` channels,
    numbers for scalar ``float`` channels).  Writes are checked against it (text
    ignoring case and blanks, numbers to 1e-9 relative) and normalised to the listed
    spelling; front ends show a combo box instead of a text field.

    A driver may return a *different* channel list from :meth:`Instrument.get_channels`
    after a configuration channel was written (e.g. a mode switch renames the
    reading channel or changes its unit): the backend asks again after every
    configuration write and action, and tells the front ends when it changed.
    """

    name: str
    access: str = "rw"            # 'r', 'w' or 'rw'
    dtype: str = "float"          # 'float' or 'str'
    shape: Optional[Tuple[int, ...]] = ()
    unit: str = ""
    description: str = ""
    timeout: Optional[float] = None   # per-channel call deadline override
    #: configuration channel (sensitivity, range, mode, ...): readable/writable on the
    #: instrument, but never aliased or swept, which keeps channel lists short
    config: bool = False
    #: accepted values (see class doc); None = anything of the right type
    allowed: Optional[List[Any]] = None
    #: array channels: for each value axis, the name of a channel of the same instrument
    #: holding its coordinates (e.g. ``axes=("frequencies",)`` for a VNA trace), or
    #: None for a plain index.  A sweep reads them once, after its pre phase, and the
    #: plots use them as coordinates and labels.
    axes: Optional[Tuple[Optional[str], ...]] = None

    def __post_init__(self):
        if self.access not in ("r", "w", "rw"):
            raise ValueError("access must be r, w or rw")
        if self.dtype not in ("float", "str"):
            raise ValueError("dtype must be 'float' or 'str'")
        if self.shape is not None:
            self.shape = tuple(int(s) for s in self.shape)
        if self.allowed is not None:
            vals = list(self.allowed)
            if not vals:
                raise ValueError("allowed must list at least one value")
            if self.dtype == "str":
                self.allowed = [str(v) for v in vals]
            else:
                if self.shape != ():
                    raise ValueError("allowed values need a scalar channel")
                self.allowed = [float(v) for v in vals]
        if self.axes is not None:
            self.axes = tuple(None if a in (None, "") else str(a) for a in self.axes)

    @property
    def readable(self) -> bool:
        return "r" in self.access

    @property
    def writable(self) -> bool:
        return "w" in self.access

    @property
    def is_scalar(self) -> bool:
        return self.shape == ()

    def match_allowed(self, value: Any) -> Any:
        """The listed spelling of *value*, or None if it is not an allowed value."""
        if self.allowed is None:
            return value
        if self.dtype == "str":
            key = _text_key(value)
            for a in self.allowed:
                if _text_key(a) == key:
                    return a
            return None
        try:
            x = float(value)
        except (TypeError, ValueError):
            return None
        for a in self.allowed:
            if x == a or abs(x - a) <= 1e-9 * max(abs(a), abs(x)):
                return a
        return None

    def to_dict(self) -> dict:
        return {
            "name": self.name, "access": self.access, "dtype": self.dtype,
            "shape": None if self.shape is None else list(self.shape),
            "unit": self.unit, "description": self.description, "config": self.config,
            "allowed": None if self.allowed is None else list(self.allowed),
            "axes": None if self.axes is None else list(self.axes),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ChannelSpec":
        shape = d.get("shape", ())
        return cls(name=d["name"], access=d.get("access", "rw"), dtype=d.get("dtype", "float"),
                   shape=None if shape is None else tuple(shape), unit=d.get("unit", ""),
                   description=d.get("description", ""), timeout=d.get("timeout"),
                   config=bool(d.get("config", False)), allowed=d.get("allowed"),
                   axes=d.get("axes"))


@dataclass
class ActionSpec:
    """An instrument action: a command without arguments (trigger, reset, auto-range,
    auto-phase ...).  Implemented by ``_action_<name>()`` or ``Instrument.action()``."""

    name: str
    description: str = ""
    timeout: Optional[float] = None

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description}

    @classmethod
    def from_dict(cls, d) -> "ActionSpec":
        if isinstance(d, str):
            return cls(d)
        return cls(d["name"], d.get("description", ""), d.get("timeout"))


def _text_key(v) -> str:
    """Comparison key for allowed text values: case, blanks and the hard-to-type
    µ / Ω do not matter ("500uV" == "500 µV", "I (1 MOhm)" == "I (1 MΩ)")."""
    t = "".join(str(v).split()).casefold()
    return t.replace("µ", "u").replace("μ", "u").replace("ω", "ohm")


def coerce_value(value: Any, spec: ChannelSpec, direction: str = "read") -> Any:
    """Convert *value* to the channel's canonical representation.

    Numeric scalars become ``float``; numeric arrays become float64 ndarrays;
    strings stay ``str``.  Raises :class:`ChannelError` for incompatible data.
    Channels with ``allowed`` values: a written value must be one of them and is
    replaced by the listed spelling; a read value is normalised when it matches.
    """
    v = _coerce(value, spec)
    if spec.allowed is not None:
        m = spec.match_allowed(v)
        if m is not None:
            return m
        if direction == "write":
            raise ChannelError("%s must be one of %s, got %r" % (
                spec.name, ", ".join(_fmt_allowed(a) for a in spec.allowed), value))
    return v


def _fmt_allowed(a) -> str:
    return a if isinstance(a, str) else "%g" % a


def _coerce(value: Any, spec: ChannelSpec) -> Any:
    if spec.dtype == "str":
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, np.ndarray) and value.ndim == 0:
            value = value.item()
        if isinstance(value, (str, int, float, np.generic)) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, bool):
            return str(value)
        raise ChannelError("channel %s expects a string, got %s" % (spec.name, type(value).__name__))

    # numeric
    if value is None:
        raise ChannelError("channel %s: got no value (None)" % spec.name)
    if isinstance(value, (dict, set)):
        raise ChannelError("channel %s: got a %s" % (spec.name, type(value).__name__))
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("ascii", errors="replace")
    if isinstance(value, str):
        txt = value.strip()
        if "," in txt and spec.shape != ():
            try:
                value = np.array([float(x) for x in txt.split(",") if x.strip()], dtype=float)
            except ValueError as exc:
                raise ChannelError("channel %s: cannot parse %r" % (spec.name, txt[:60])) from exc
        else:
            try:
                value = float(txt)
            except ValueError as exc:
                raise ChannelError("channel %s: cannot parse %r as number"
                                   % (spec.name, txt[:60])) from exc
    if isinstance(value, bool):
        value = float(value)
    try:
        arr = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ChannelError("channel %s: value of type %s is not numeric"
                           % (spec.name, type(value).__name__)) from exc
    if arr.dtype == object:  # pragma: no cover - asarray(dtype=float) prevents this
        raise ChannelError("channel %s: ragged data" % spec.name)
    shape = spec.shape
    if shape is not None:
        if shape == ():
            if arr.size != 1:
                raise ChannelError("channel %s is scalar but got shape %s" % (spec.name, arr.shape))
            return float(arr.reshape(()))
        if arr.ndim != len(shape) or any(s != -1 and s != a for s, a in zip(shape, arr.shape)):
            raise ChannelError("channel %s expects shape %s, got %s"
                               % (spec.name, shape, arr.shape))
    if arr.ndim == 0:
        return float(arr)
    return np.array(arr, dtype=float, copy=True)


# ---------------------------------------------------------------------------
# Driver base class
# ---------------------------------------------------------------------------
class Instrument:
    """Base class for instrument drivers.

    Subclasses set ``CHANNELS`` (a list of :class:`ChannelSpec`) and implement
    either ``read``/``write`` or ``_read_<channel>`` / ``_write_<channel>`` methods.
    Inside driver methods use ``self.resource`` for I/O - the handle has already
    opened a transaction on it.
    """

    DESCRIPTION = ""
    CHANNELS: List[ChannelSpec] = []
    ACTIONS: List[ActionSpec] = []
    DEFAULT_OPTIONS: Dict[str, Any] = {}
    #: interfaces this driver accepts (``resources.INTERFACES``: gpib, usb, tcpip,
    #: tcp, serial, daq, sim, none, other); an address of another interface is refused
    INTERFACES: Sequence[str] = INTERFACES
    #: substrings of the identification (``*IDN?``, NI-DAQmx product type) that mark a
    #: device as one for this driver (used to suggest addresses; case-insensitive)
    IDN_MATCH: Sequence[str] = ()
    #: whether this driver's port may be shared with other instruments by default
    SHARED_RESOURCE = False
    #: seconds between calls of :meth:`poll` by the backend's watchdog (0: never);
    #: option ``poll_interval`` overrides it
    POLL_INTERVAL = 0.0
    #: a safety fault the driver has detected and not seen cleared yet (a message),
    #: see :class:`~easymeasure.common.errors.InstrumentFault`
    fault: Optional[str] = None

    def __init__(self, name: str, address: str, options: Optional[dict] = None):
        self.name = name
        self.address = address
        opts = dict(self.DEFAULT_OPTIONS)
        opts.update(options or {})
        self.options = opts
        self.resource: Optional[Resource] = None  # set by the handle

    @classmethod
    def suggested_addresses(cls, driver_name: str) -> List[dict]:
        """Addresses worth offering without a scan ({address, description}): a
        built-in emulator, ``NONE`` for drivers without a port."""
        return [{"address": "NONE", "description": "no port"}] if "none" in cls.INTERFACES else []

    # -- override in drivers ----------------------------------------------------
    def connect(self) -> None:
        """Called once after the port is available (inside a transaction)."""

    def disconnect(self) -> None:
        """Called before the port is released (inside a transaction)."""

    def get_channels(self) -> List[ChannelSpec]:
        """Return the channel list (may depend on options / connected hardware).

        Called after :meth:`connect` and again after every write to a configuration
        channel and every action, so the list may follow the instrument state (a mode
        switch may rename a channel, change its unit or make it read-only).  Keep it
        cheap: no I/O, just the state the driver already knows."""
        return list(self.CHANNELS)

    def get_actions(self) -> List[ActionSpec]:
        """Return the action list (may depend on options / connected hardware)."""
        return list(self.ACTIONS)

    def action(self, name: str) -> None:
        fn = getattr(self, "_action_" + name, None)
        if fn is None:
            raise ChannelError("%s has no action %s" % (self.name, name))
        fn()

    def read(self, channel: str) -> Any:
        fn = getattr(self, "_read_" + channel, None)
        if fn is None:
            raise ChannelError("%s: channel %s is not readable" % (self.name, channel))
        return fn()

    def write(self, channel: str, value: Any) -> None:
        fn = getattr(self, "_write_" + channel, None)
        if fn is None:
            raise ChannelError("%s: channel %s is not writable" % (self.name, channel))
        fn(value)

    def poll(self) -> None:
        """Called by the backend's watchdog every ``POLL_INTERVAL`` seconds while
        connected (and nothing else is talking to the instrument): check the
        instrument's health and raise :class:`InstrumentFault` for a safety fault
        (a quench).  May also move along a procedure the driver runs step by step."""

    def poll_interval(self) -> float:
        try:
            return max(0.0, float(self.options.get("poll_interval", self.POLL_INTERVAL) or 0))
        except (TypeError, ValueError):
            return float(self.POLL_INTERVAL or 0)

    def recover(self) -> None:
        """Light-weight recovery after a failed call (e.g. clear the port)."""
        if self.resource is not None:
            self.resource.clear()

    def select(self, resource: Resource) -> None:
        """For shared ports: make this instrument the active device on the bus."""

    def raw_query(self, command: str) -> str:
        """Pass-through query, used by scripts for things not exposed as channels."""
        if self.resource is None:
            raise InstrumentError("%s has no port" % self.name)
        return self.resource.query(command)

    def raw_write(self, command: str) -> None:
        if self.resource is None:
            raise InstrumentError("%s has no port" % self.name)
        self.resource.write(command)


# ---------------------------------------------------------------------------
# Guarded worker
# ---------------------------------------------------------------------------
class _Job:
    __slots__ = ("fn", "done", "result", "error", "abandoned", "priority")
    # (abort checks stay in the calling thread: drivers are never interrupted)

    def __init__(self, fn: Callable[[], Any]):
        self.fn = fn
        self.priority = thread_priority()   # inherit the caller's arbitration priority
        self.done = threading.Event()
        self.result = None
        self.error: Optional[BaseException] = None
        self.abandoned = False


class _Worker(threading.Thread):
    """Executes driver calls one at a time.  Can be abandoned if it hangs."""

    def __init__(self, name: str):
        super().__init__(name=name, daemon=True)
        self.jobs: "queue.Queue[Optional[_Job]]" = queue.Queue()
        self.abandoned = False
        self.current: Optional[_Job] = None

    def run(self):
        while True:
            job = self.jobs.get()
            if job is None or self.abandoned:
                return
            self.current = job
            set_thread_priority(job.priority)
            try:
                job.result = job.fn()
            except BaseException as exc:  # noqa: B902 - deliberately catch everything
                job.error = exc
            finally:
                self.current = None
                job.done.set()


class InstrumentHandle:
    """Guarded, thread-safe wrapper around a driver instance."""

    def __init__(self, name: str, driver_name: str, driver_cls: type, address: str,
                 options: dict, resources: ResourceManager):
        self.name = name
        self.driver_name = driver_name
        self.address = address or "NONE"
        self.options = dict(options or {})
        self._resources = resources
        self._driver_cls = driver_cls
        self.driver: Optional[Instrument] = None
        self.resource: Optional[Resource] = None
        self.state = "disconnected"   # disconnected | connected | error | hung
        self.last_error = ""
        #: called (outside the call lock) when the driver's channel/action list changed
        self.on_specs_changed: Optional[Callable[["InstrumentHandle"], None]] = None
        #: a safety fault reported by the driver (InstrumentFault) and not cleared yet
        self.fault: Optional[str] = None
        #: called with (handle, message) when a new fault is reported
        self.on_fault: Optional[Callable[["InstrumentHandle", str], None]] = None
        self._next_poll = 0.0
        self._specs_dirty = False
        #: channel -> (value, unix time) of the last successful read or write
        self.last_values: Dict[str, Tuple[Any, float]] = {}
        self._channels: Dict[str, ChannelSpec] = {}
        self._actions: Dict[str, ActionSpec] = {}
        self._worker: Optional[_Worker] = None
        self._worker_count = 0
        # serialises calls through the handle; FIFO so that a client polling in a
        # tight loop can never starve a running sweep
        self._call_lock = FairLock()
        self._needs_reconnect = False
        self._consecutive_failures = 0
        self.stats = {"calls": 0, "errors": 0, "timeouts": 0, "reconnects": 0, "retries": 0}
        self._rl = RateLimitedLogger(log, 5.0)
        o = self.options
        self.call_timeout = float(o.get("call_timeout", 10.0))
        self.retries = int(o.get("retries", 2))
        self.retry_delay = float(o.get("retry_delay", 0.05))
        self.reconnect_after = int(o.get("reconnect_after", 2))
        self.shared = bool(o.get("shared", getattr(driver_cls, "SHARED_RESOURCE", False)))
        # a hang is expensive: retry at most this many times after a timeout
        self.timeout_retries = int(o.get("timeout_retries", 1))
        # circuit breaker: a call (e.g. "read hang") that times out this many times in a
        # row fails fast for breaker_cooldown seconds, so one pathological channel
        # cannot monopolise the instrument for all the others
        self.breaker_threshold = int(o.get("breaker_threshold", 2))
        self.breaker_cooldown = float(o.get("breaker_cooldown", 10.0))
        self._breaker: Dict[str, list] = {}
        # how long a caller waits for another call on this instrument to finish;
        # bounded so a hung instrument can never pile up blocked threads forever
        self.lock_wait = float(o.get("lock_wait", max(30.0, self.call_timeout * (self.retries + 2))))
        self.max_waiters = int(o.get("max_waiters", 4))

    def _locked(self):
        handle = self

        class _Ctx:
            def __enter__(self_):
                lk = handle._call_lock
                if lk.locked and not lk.held_by_me() and thread_priority() > PRIO_MEASUREMENT \
                        and lk.waiters >= handle.max_waiters:
                    # enough callers are already queued behind a slow/hung call: fail
                    # fast instead of tying up yet another server thread
                    raise InstrumentTimeout("%s is busy (%d requests already waiting)"
                                            % (handle.name, lk.waiters))
                if not lk.acquire(handle.lock_wait, tag=handle.name):
                    raise InstrumentTimeout("%s is busy (another call has been running for more "
                                            "than %.0f s)" % (handle.name, handle.lock_wait))

            def __exit__(self_, *exc):
                try:
                    handle._call_lock.release()
                except RuntimeError:
                    pass
                return False
        return _Ctx()

    # -- worker management ------------------------------------------------------
    def _get_worker(self) -> _Worker:
        w = self._worker
        if w is None or not w.is_alive() or w.abandoned:
            self._worker_count += 1
            w = _Worker("drv-%s-%d" % (self.name, self._worker_count))
            w.start()
            self._worker = w
        return w

    def _abandon_worker(self, reason: str) -> None:
        w = self._worker
        self._worker = None
        if w is None:
            return
        w.abandoned = True
        try:
            w.jobs.put_nowait(None)
        except Exception:
            pass
        res = self.resource
        if res is not None:
            # The hung thread may still hold the port lock: break it and drop the
            # handle so the stuck thread can't corrupt future traffic.  If it does
            # NOT hold the lock (hung outside any I/O), another instrument may be in
            # the middle of a transaction on a shared bus: leave the port alone.
            holds = res.lock.locked and res.lock.owner_tag == self.name
            if holds:
                res.lock.force_release()
            if holds or not res.shared:
                res.invalidate(reason)
        # The abandoned thread still references the old driver object; start
        # fresh so its late side effects cannot leak into the new session.
        self.driver = None
        self._needs_reconnect = True

    def _run(self, fn: Callable[[], Any], timeout: float, transaction: bool = True) -> Any:
        """Run *fn* on the worker within a resource transaction and a deadline."""
        res = self.resource

        def job_fn():
            if transaction and res is not None:
                with res.transaction(self.name, timeout=max(timeout, 1.0)):
                    return fn()
            return fn()

        job = _Job(job_fn)
        worker = self._get_worker()
        worker.jobs.put(job)
        if not job.done.wait(timeout):
            job.abandoned = True
            self.stats["timeouts"] += 1
            self.state = "hung"
            self._abandon_worker("call timed out after %.1fs" % timeout)
            raise InstrumentTimeout("%s: call timed out after %.1f s" % (self.name, timeout))
        if job.error is not None:
            err = job.error
            if isinstance(err, EasyMeasureError):
                raise err
            raise InstrumentError("%s: driver raised %s: %s" % (self.name, type(err).__name__, err)) \
                from (err if isinstance(err, Exception) else None)
        return job.result

    # -- connection ---------------------------------------------------------
    def connect(self) -> None:
        with self._locked():
            self._connect_locked()
        self._fire_specs_changed()

    def _connect_locked(self) -> None:
        if self.driver is not None and self.state == "connected" and not self._needs_reconnect:
            return
        if self.resource is None:
            self.resource = self._resources.acquire(self.address, self.name, self.shared,
                                                    self.options)
        try:
            timeout = float(self.options.get("connect_timeout", max(self.call_timeout, 10.0)))
            if self.driver is None:
                # even the constructor of a buggy driver may hang: run it guarded
                cls, name, addr, opts = self._driver_cls, self.name, self.address, self.options
                self.driver = self._run(lambda: cls(name, addr, opts), timeout, transaction=False)
            drv = self.driver
            drv.resource = self.resource
            if self.resource.shared:
                self.resource.set_selector(self.name, self._make_selector(drv))
            self._run(drv.connect, timeout)
            chans = self._run(drv.get_channels, timeout, transaction=False)
            acts = self._run(drv.get_actions, timeout, transaction=False)
            self._apply_specs(chans, acts)
            self.state = "connected"
            self.last_error = ""
            self._needs_reconnect = False
            self._consecutive_failures = 0
            log.info("instrument %s (%s @ %s) connected, %d channels", self.name,
                     self.driver_name, self.address, len(self._channels))
        except BaseException as exc:
            self.state = "error"
            self.last_error = str(exc)
            # make sure the next call tries to connect again instead of using a
            # half-initialised driver
            self._needs_reconnect = True
            log.error("instrument %s failed to connect: %s", self.name, exc)
            if isinstance(exc, EasyMeasureError):
                raise
            raise InstrumentError("%s: connect failed: %s" % (self.name, exc)) from None

    def _apply_specs(self, chans, acts) -> bool:
        """Install a (new) channel/action list from the driver; True if it changed."""
        new_c: Dict[str, ChannelSpec] = {}
        for c in chans or []:
            if not isinstance(c, ChannelSpec):
                c = ChannelSpec.from_dict(c)
            if c.name in new_c:
                raise ChannelError("%s: duplicate channel name %r" % (self.name, c.name))
            new_c[c.name] = c
        new_a: Dict[str, ActionSpec] = {}
        for a in acts or []:
            if not isinstance(a, ActionSpec):
                a = ActionSpec.from_dict(a)
            if a.name in new_c:
                raise ChannelError("%s: %r is both a channel and an action (one name, one "
                                   "thing: %s.%s)" % (self.name, a.name, self.name, a.name))
            new_a[a.name] = a
        # compare with what clients saw so far (the static class lists before the
        # first connect)
        changed = new_c != self.channels or new_a != self.actions
        self._channels, self._actions = new_c, new_a
        if changed:
            self._specs_dirty = True
        return changed

    def _refresh_specs(self, drv: Instrument) -> None:
        """Re-ask the driver for its channel list (on the worker, under the call lock)."""
        try:
            self._apply_specs(drv.get_channels(), drv.get_actions())
        except Exception as exc:          # a broken get_channels must not fail the write
            log.warning("%s: get_channels() failed: %s", self.name, exc)

    def refresh_specs(self) -> bool:
        """Ask the driver again for its channel list; True if it changed."""
        with self._locked():
            drv = self.driver
            if drv is None or self.state != "connected":
                return False
            spec = ChannelSpec("__specs__", "r", "str")
            self._specs_dirty = False
            self._run(lambda: self._refresh_specs(drv), spec.timeout or self.call_timeout,
                      transaction=False)
            changed = self._specs_dirty
        self._fire_specs_changed()
        return changed

    def _fire_specs_changed(self) -> None:
        if not self._specs_dirty:
            return
        self._specs_dirty = False
        log.info("%s: channel list changed: %s", self.name,
                 ", ".join("%s[%s]" % (c.name, c.unit) if c.unit else c.name
                           for c in self._channels.values()))
        cb = self.on_specs_changed
        if cb is not None:
            try:
                cb(self)
            except Exception as exc:
                log.warning("%s: specs-changed listener failed: %s", self.name, exc)

    def _make_selector(self, drv: Instrument):
        def _sel(res: Resource):
            drv.select(res)
        return _sel

    def disconnect(self, release: bool = True) -> None:
        with self._locked():
            drv = self.driver
            if drv is not None and self.state in ("connected", "error"):
                try:
                    self._run(drv.disconnect, min(self.call_timeout, 5.0))
                except Exception as exc:
                    log.warning("instrument %s: error during disconnect: %s", self.name, exc)
            self.state = "disconnected"
            if release and self.resource is not None:
                self._resources.release(self.resource, self.name)
                self.resource = None
            w, self._worker = self._worker, None
            if w is not None:
                w.jobs.put(None)
            log.info("instrument %s disconnected", self.name)

    def reconnect(self) -> None:
        with self._locked():
            self._reconnect_locked()
        self._fire_specs_changed()

    def _reconnect_locked(self) -> None:
        self.stats["reconnects"] += 1
        log.warning("instrument %s: reconnecting", self.name)
        drv = self.driver
        if drv is not None and self.state != "hung":
            try:
                self._run(drv.disconnect, min(self.call_timeout, 3.0))
            except Exception:
                pass
        if self.resource is not None:
            self.resource.invalidate("reconnect")
        self._needs_reconnect = True
        self.state = "disconnected"
        self._connect_locked()

    # -- channel info -------------------------------------------------------
    @property
    def channels(self) -> Dict[str, ChannelSpec]:
        if not self._channels:
            # never connected successfully: describe the static channel list of the
            # driver class (dynamic drivers may refine it once connected)
            try:
                return {c.name: c for c in getattr(self._driver_cls, "CHANNELS", [])}
            except Exception:
                return {}
        return dict(self._channels)

    @property
    def actions(self) -> Dict[str, ActionSpec]:
        if not self._actions and not self._channels:
            try:
                return {a.name: a for a in getattr(self._driver_cls, "ACTIONS", [])}
            except Exception:
                return {}
        return dict(self._actions)

    def run_action(self, name: str) -> None:
        """Execute an action (guarded like any other call: deadline, retries)."""
        act = self.actions.get(name)
        if act is None and not self._channels and self.state != "connected":
            try:
                self.connect()
            except Exception:
                pass
            act = self.actions.get(name)
        if act is None:
            raise ChannelError("instrument %s has no action %r" % (self.name, name))
        spec = ChannelSpec("action:" + name, "w", "str", (), timeout=act.timeout)
        # never retried automatically: an action (trigger, reset) is not idempotent

        def do(drv: Instrument):
            drv.action(name)
            self._refresh_specs(drv)        # e.g. a reset may change the mode

        self._guarded("action %s" % name, spec, do, retries=0)
        log.info("%s: action %s executed", self.name, name)

    def spec(self, channel: str) -> ChannelSpec:
        """Pure lookup (never does I/O, so it is safe for status queries)."""
        spec = self.channels.get(channel)
        if spec is None:
            raise self._no_channel(channel)
        return spec

    def _no_channel(self, channel: str) -> ChannelError:
        names = list(self.channels)
        return ChannelError("instrument %s has no channel %r%s" % (
            self.name, channel, (" (channels now: %s)" % ", ".join(names)) if names else ""))

    def _io_spec(self, channel: str) -> ChannelSpec:
        """Lookup for an I/O call: may (re)connect to learn a dynamic channel list."""
        spec = self.channels.get(channel)
        if spec is None and not self._channels and self.state != "connected":
            # dynamic channel list not known yet (initial connect failed): try again
            try:
                self.connect()
            except Exception:
                pass
            spec = self.channels.get(channel)
        if spec is None:
            raise self._no_channel(channel)
        return spec

    # -- guarded operations -------------------------------------------------
    def _breaker_check(self, what: str) -> None:
        b = self._breaker.get(what)
        if b is not None and b[1] > time.monotonic():
            raise InstrumentTimeout("%s %s quarantined for %.0f s after %d consecutive timeouts"
                                    % (self.name, what, b[1] - time.monotonic(), b[0]))

    def _breaker_update(self, what: str, timed_out: bool) -> None:
        if not timed_out:
            self._breaker.pop(what, None)
            return
        b = self._breaker.setdefault(what, [0, 0.0, 0])   # [timeouts, open_until, trips]
        b[0] += 1
        if b[0] >= self.breaker_threshold:
            # exponential back-off: a channel that keeps hanging is retried less and less
            cooldown = min(self.breaker_cooldown * (2 ** b[2]), 300.0)
            b[2] += 1
            b[1] = time.monotonic() + cooldown
            log.warning("%s: '%s' timed out %d times in a row; failing fast for %.0f s",
                        self.name, what, b[0], cooldown)

    def breaker_state(self) -> Dict[str, float]:
        now = time.monotonic()
        return {k: round(v[1] - now, 1) for k, v in self._breaker.items() if v[1] > now}

    def _guarded(self, what: str, spec: ChannelSpec, fn: Callable[[Instrument], Any],
                 retries: Optional[int] = None) -> Any:
        timeout = spec.timeout or self.call_timeout
        self._breaker_check(what)
        try:
            result = self._guarded_inner(what, spec, fn, timeout, retries)
        except InstrumentTimeout:
            self._breaker_update(what, True)
            raise
        finally:
            self._fire_specs_changed()     # a (re)connect or config write changed them
        self._breaker_update(what, False)
        return result

    def _guarded_inner(self, what, spec, fn, timeout, retries=None) -> Any:
        last_exc: Optional[BaseException] = None
        attempts = 1 + max(0, self.retries if retries is None else retries)
        b = self._breaker.get(what)
        if b is not None and b[0] >= self.breaker_threshold:
            attempts = 1          # half-open trial after a cooldown: a single attempt
        timeouts = 0
        with self._locked():
            for attempt in range(attempts):
                try:
                    if self.driver is None or self._needs_reconnect or self.state == "disconnected":
                        if self.state == "disconnected" and not self._needs_reconnect and \
                                not self.options.get("auto_connect", True):
                            raise InstrumentNotConnected("%s is not connected" % self.name)
                        self._connect_locked()
                    drv = self.driver
                    self.stats["calls"] += 1
                    result = self._run(lambda: fn(drv), timeout)
                    self._consecutive_failures = 0
                    if self.state != "connected":
                        self.state = "connected"
                    self._fault_cleared(drv)
                    return result
                except InstrumentFault as exc:
                    # a safety fault: never retried, reported to the backend once
                    self._report_fault(exc)
                    raise
                except ChannelError as exc:
                    # wrong channel / wrong value type: retrying will not help.
                    # (but a ChannelError from coercing a *garbled reply* is retryable;
                    # those are raised as _GarbledReply below)
                    raise
                except BaseException as exc:  # noqa: B902
                    last_exc = exc
                    self.stats["errors"] += 1
                    self._consecutive_failures += 1
                    self.last_error = "%s: %s" % (type(exc).__name__, exc)
                    if self.state != "hung":
                        self.state = "error"
                    self._rl.log(30, "%s.%s" % (self.name, what),
                                 "%s %s attempt %d/%d failed: %s", self.name, what,
                                 attempt + 1, attempts, self.last_error)
                    if isinstance(exc, InstrumentTimeout):
                        timeouts += 1
                    if attempt + 1 >= attempts or timeouts > self.timeout_retries:
                        break
                    abort_check()   # an abort is honoured between attempts
                    self.stats["retries"] += 1
                    if isinstance(exc, InstrumentTimeout) or \
                            self._consecutive_failures >= self.reconnect_after:
                        try:
                            self._reconnect_locked()
                        except Exception as rexc:
                            self.last_error = "reconnect failed: %s" % rexc
                    else:
                        try:
                            drv = self.driver
                            if drv is not None:
                                self._run(drv.recover, min(timeout, 5.0))
                        except Exception:
                            pass
                    time.sleep(self.retry_delay * (attempt + 1))
        if isinstance(last_exc, InstrumentError):
            raise type(last_exc)("%s (after %d attempts)" % (last_exc, attempt + 1))
        raise InstrumentError("%s %s failed after %d attempts: %s"
                              % (self.name, what, attempts, last_exc))

    # -- safety faults and the watchdog ------------------------------------------------
    def _report_fault(self, exc: BaseException) -> None:
        msg = str(exc)
        self.last_error = "FAULT: %s" % msg
        new = self.fault is None
        self.fault = msg
        if new:
            log.error("%s: FAULT: %s", self.name, msg)
            cb = self.on_fault
            if cb is not None:
                try:
                    cb(self, msg)
                except Exception as cexc:
                    log.error("%s: fault handler failed: %s", self.name, cexc)

    def _fault_cleared(self, drv) -> None:
        if self.fault is not None and drv is not None and getattr(drv, "fault", None) is None:
            log.warning("%s: fault cleared (%s)", self.name, self.fault)
            self.fault = None
            if self.last_error.startswith("FAULT"):
                self.last_error = ""

    def poll_due(self, now: float) -> bool:
        drv = self.driver
        if drv is None or self.state != "connected":
            return False
        iv = drv.poll_interval()
        return iv > 0 and now >= self._next_poll

    def poll(self) -> None:
        """One watchdog call of the driver's ``poll`` - skipped when another call is
        using the instrument (that call checks the instrument itself)."""
        drv = self.driver
        if drv is None:
            return
        self._next_poll = time.monotonic() + max(0.02, drv.poll_interval())
        lk = self._call_lock
        if not lk.acquire(0.02, tag=self.name):
            return
        try:
            if self.driver is not drv or self.state != "connected":
                return
            self._run(drv.poll, min(self.call_timeout, 10.0))
            self._fault_cleared(drv)
        except InstrumentFault as exc:
            self._report_fault(exc)
        except Exception as exc:
            self._rl.log(30, "%s.poll" % self.name, "%s: watchdog poll failed: %s",
                         self.name, exc)
        finally:
            try:
                lk.release()
            except RuntimeError:
                pass
            self._fire_specs_changed()

    def read(self, channel: str) -> Any:
        spec = self._io_spec(channel)
        if not spec.readable:
            raise ChannelError("%s.%s is write-only" % (self.name, channel))

        def do(drv: Instrument):
            raw = drv.read(channel)
            try:
                return coerce_value(raw, spec, "read")
            except ChannelError as exc:
                raise _GarbledReply(str(exc)) from None

        v = self._guarded("read %s" % channel, spec, do)
        self.last_values[channel] = (v, time.time())
        return v

    def write(self, channel: str, value: Any) -> None:
        spec = self._io_spec(channel)
        if not spec.writable:
            raise ChannelError("%s.%s is read-only" % (self.name, channel))
        value = coerce_value(value, spec, "write")  # bad user value -> ChannelError, no retry

        def do(drv: Instrument):
            drv.write(channel, value)
            if spec.config:
                self._refresh_specs(drv)    # a mode switch may change the channel list

        self._guarded("write %s" % channel, spec, do)
        self.last_values[channel] = (value, time.time())

    def query(self, channel: str, value: Any) -> Any:
        """Write *value* to the channel and read it back in one transaction."""
        spec = self._io_spec(channel)
        if not (spec.readable and spec.writable):
            raise ChannelError("%s.%s must be read/write for query" % (self.name, channel))
        value = coerce_value(value, spec, "write")

        def do(drv: Instrument):
            drv.write(channel, value)
            if spec.config:
                self._refresh_specs(drv)
            raw = drv.read(channel)
            try:
                return coerce_value(raw, spec, "read")
            except ChannelError as exc:
                raise _GarbledReply(str(exc)) from None

        v = self._guarded("query %s" % channel, spec, do)
        self.last_values[channel] = (v, time.time())
        return v

    def raw_query(self, command: str) -> str:
        spec = ChannelSpec("__raw__", "rw", "str")
        return self._guarded("raw query", spec, lambda drv: str(drv.raw_query(command)))

    def raw_write(self, command: str) -> None:
        spec = ChannelSpec("__raw__", "rw", "str")
        self._guarded("raw write", spec, lambda drv: drv.raw_write(command))

    def call(self, method: str, *args, **kwargs) -> Any:
        """Call an arbitrary public driver method under the same guards."""
        if method.startswith("_"):
            raise ChannelError("private driver methods cannot be called")
        spec = ChannelSpec("__call__", "rw", "str")

        def do(drv: Instrument):
            fn = getattr(drv, method, None)
            if fn is None or not callable(fn):
                raise ChannelError("%s has no method %s" % (self.name, method))
            return fn(*args, **kwargs)

        return self._guarded("call %s" % method, spec, do)

    def describe(self) -> dict:
        return {
            "name": self.name,
            "driver": self.driver_name,
            "address": self.address,
            "options": self.options,
            "state": self.state,
            "last_error": self.last_error,
            "fault": self.fault,
            "stats": dict(self.stats),
            "quarantined": self.breaker_state(),
            "channels": [dict(c.to_dict(), **self._last_of(c.name)) for c in self.channels.values()],
            "actions": [a.to_dict() for a in self.actions.values()],
        }


    def _last_of(self, channel: str) -> dict:
        """Last value for status displays: scalars and text as they are, arrays only
        as their shape (a state snapshot must stay small)."""
        lv = self.last_values.get(channel)
        if lv is None:
            return {"last": None, "last_t": None}
        v, t = lv
        if isinstance(v, np.ndarray):
            v = {"shape": list(v.shape)}
        elif isinstance(v, str) and len(v) > 200:
            v = v[:200] + "…"
        return {"last": v, "last_t": t}


class _GarbledReply(InstrumentError):
    """Reply could not be coerced to the channel type: retryable I/O problem."""


# ---------------------------------------------------------------------------
# Instrument manager
# ---------------------------------------------------------------------------
class InstrumentManager:
    """Owns all instrument handles."""

    def __init__(self, resources: ResourceManager, driver_registry):
        self.resources = resources
        self.drivers = driver_registry
        self._handles: Dict[str, InstrumentHandle] = {}
        self._lock = threading.RLock()
        #: callbacks ``f(instrument_name)`` run when an instrument's channel list changed
        self.listeners: List[Callable[[str], None]] = []
        #: callbacks ``f(instrument_name, message)`` run when an instrument reports a
        #: new safety fault (:class:`InstrumentFault`, e.g. a magnet quench)
        self.fault_listeners: List[Callable[[str, str], None]] = []
        # the watchdog: calls ``poll`` of the drivers that ask for it (POLL_INTERVAL)
        self._stop = threading.Event()
        self._watchdog = threading.Thread(target=self._watch, name="instrument-watchdog",
                                          daemon=True)
        self._watchdog.start()

    def _watch(self) -> None:
        while not self._stop.wait(0.02):
            now = time.monotonic()
            for h in self.handles():
                try:
                    if h.poll_due(now):
                        h.poll()
                except Exception as exc:          # never let the watchdog die
                    log.warning("watchdog: %s: %s", h.name, exc)

    def _fault(self, h: InstrumentHandle, message: str) -> None:
        if self._handles.get(h.name) is not h:
            return
        for cb in list(self.fault_listeners):
            try:
                cb(h.name, message)
            except Exception as exc:
                log.error("fault listener failed: %s", exc)

    def add(self, name: str, driver: str, address: str = "NONE", options: Optional[dict] = None,
            connect: bool = True) -> InstrumentHandle:
        if not name or not name.replace("_", "").isalnum():
            raise ChannelError("instrument name must be alphanumeric/underscore, got %r" % name)
        cls = self.drivers.get(driver)
        self.check_interface(driver, address, cls)
        with self._lock:
            if name in self._handles:
                raise ChannelError("instrument %r already exists" % name)
            h = InstrumentHandle(name, driver, cls, address, options or {}, self.resources)
            h.on_specs_changed = self._specs_changed
            h.on_fault = self._fault
            self._handles[name] = h
        if connect:
            try:
                h.connect()
            except Exception:
                # keep it registered (state=error) so the user can fix & reconnect
                pass
        return h

    def check_interface(self, driver: str, address: str, cls=None) -> None:
        """Refuse an address of an interface the driver does not accept."""
        cls = cls or self.drivers.get(driver)
        accepted = tuple(getattr(cls, "INTERFACES", INTERFACES))
        kind = interface_of(address)
        if kind not in accepted:
            raise ChannelError("%s does not take %s addresses (%r); it takes: %s" % (
                driver, INTERFACE_NAMES.get(kind, kind), address or "NONE",
                ", ".join(INTERFACE_NAMES.get(k, k) for k in accepted)))

    def _specs_changed(self, h: InstrumentHandle) -> None:
        if self._handles.get(h.name) is not h:
            return                         # removed / replaced meanwhile
        for cb in list(self.listeners):
            try:
                cb(h.name)
            except Exception as exc:
                log.warning("instrument listener failed: %s", exc)

    def remove(self, name: str) -> None:
        with self._lock:
            h = self._handles.pop(name, None)
        if h is None:
            raise ChannelError("no instrument %r" % name)
        try:
            h.disconnect()
        except Exception as exc:
            log.warning("error removing %s: %s", name, exc)

    def get(self, name: str) -> InstrumentHandle:
        with self._lock:
            h = self._handles.get(name)
        if h is None:
            raise ChannelError("no instrument %r" % name)
        return h

    def names(self) -> List[str]:
        with self._lock:
            return list(self._handles)

    def handles(self) -> List[InstrumentHandle]:
        with self._lock:
            return list(self._handles.values())

    def resolve(self, ref: str) -> Tuple[InstrumentHandle, ChannelSpec]:
        """Resolve ``"instrument.channel"``."""
        if not isinstance(ref, str) or "." not in ref:
            raise ChannelError("channel reference must be 'instrument.channel', got %r" % (ref,))
        inst, ch = ref.split(".", 1)
        h = self.get(inst)
        return h, h.spec(ch)

    def describe(self) -> List[dict]:
        return [h.describe() for h in self.handles()]

    def shutdown(self) -> None:
        self._stop.set()
        for h in self.handles():
            try:
                h.disconnect()
            except Exception:
                pass
        with self._lock:
            self._handles.clear()
