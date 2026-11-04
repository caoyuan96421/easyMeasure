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
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from ..common.errors import (ChannelError, EasyMeasureError, InstrumentError,
                             InstrumentNotConnected, InstrumentTimeout, ResourceError)
from .logsys import RateLimitedLogger, get_logger
from .resources import (PRIO_MEASUREMENT, FairLock, Resource, ResourceManager, abort_check,
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

    def __post_init__(self):
        if self.access not in ("r", "w", "rw"):
            raise ValueError("access must be r, w or rw")
        if self.dtype not in ("float", "str"):
            raise ValueError("dtype must be 'float' or 'str'")
        if self.shape is not None:
            self.shape = tuple(int(s) for s in self.shape)

    @property
    def readable(self) -> bool:
        return "r" in self.access

    @property
    def writable(self) -> bool:
        return "w" in self.access

    @property
    def is_scalar(self) -> bool:
        return self.shape == ()

    def to_dict(self) -> dict:
        return {
            "name": self.name, "access": self.access, "dtype": self.dtype,
            "shape": None if self.shape is None else list(self.shape),
            "unit": self.unit, "description": self.description, "config": self.config,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ChannelSpec":
        shape = d.get("shape", ())
        return cls(name=d["name"], access=d.get("access", "rw"), dtype=d.get("dtype", "float"),
                   shape=None if shape is None else tuple(shape), unit=d.get("unit", ""),
                   description=d.get("description", ""), timeout=d.get("timeout"),
                   config=bool(d.get("config", False)))


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


def coerce_value(value: Any, spec: ChannelSpec, direction: str = "read") -> Any:
    """Convert *value* to the channel's canonical representation.

    Numeric scalars become ``float``; numeric arrays become float64 ndarrays;
    strings stay ``str``.  Raises :class:`ChannelError` for incompatible data.
    """
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
    #: whether this driver's port may be shared with other instruments by default
    SHARED_RESOURCE = False

    def __init__(self, name: str, address: str, options: Optional[dict] = None):
        self.name = name
        self.address = address
        opts = dict(self.DEFAULT_OPTIONS)
        opts.update(options or {})
        self.options = opts
        self.resource: Optional[Resource] = None  # set by the handle

    # -- override in drivers ----------------------------------------------------
    def connect(self) -> None:
        """Called once after the port is available (inside a transaction)."""

    def disconnect(self) -> None:
        """Called before the port is released (inside a transaction)."""

    def get_channels(self) -> List[ChannelSpec]:
        """Return the channel list (may depend on options / connected hardware)."""
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
            self._channels = {}
            for c in chans:
                if not isinstance(c, ChannelSpec):
                    c = ChannelSpec.from_dict(c)
                self._channels[c.name] = c
            acts = self._run(drv.get_actions, timeout, transaction=False)
            self._actions = {}
            for a in acts or []:
                if not isinstance(a, ActionSpec):
                    a = ActionSpec.from_dict(a)
                self._actions[a.name] = a
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
        self._guarded("action %s" % name, spec, lambda drv: drv.action(name), retries=0)
        log.info("%s: action %s executed", self.name, name)

    def spec(self, channel: str) -> ChannelSpec:
        """Pure lookup (never does I/O, so it is safe for status queries)."""
        spec = self.channels.get(channel)
        if spec is None:
            raise ChannelError("instrument %s has no channel %r" % (self.name, channel))
        return spec

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
            raise ChannelError("instrument %s has no channel %r" % (self.name, channel))
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
                    return result
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

        return self._guarded("read %s" % channel, spec, do)

    def write(self, channel: str, value: Any) -> None:
        spec = self._io_spec(channel)
        if not spec.writable:
            raise ChannelError("%s.%s is read-only" % (self.name, channel))
        value = coerce_value(value, spec, "write")  # bad user value -> ChannelError, no retry

        def do(drv: Instrument):
            drv.write(channel, value)

        self._guarded("write %s" % channel, spec, do)

    def query(self, channel: str, value: Any) -> Any:
        """Write *value* to the channel and read it back in one transaction."""
        spec = self._io_spec(channel)
        if not (spec.readable and spec.writable):
            raise ChannelError("%s.%s must be read/write for query" % (self.name, channel))
        value = coerce_value(value, spec, "write")

        def do(drv: Instrument):
            drv.write(channel, value)
            raw = drv.read(channel)
            try:
                return coerce_value(raw, spec, "read")
            except ChannelError as exc:
                raise _GarbledReply(str(exc)) from None

        return self._guarded("query %s" % channel, spec, do)

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
            "stats": dict(self.stats),
            "quarantined": self.breaker_state(),
            "channels": [c.to_dict() for c in self.channels.values()],
            "actions": [a.to_dict() for a in self.actions.values()],
        }


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

    def add(self, name: str, driver: str, address: str = "NONE", options: Optional[dict] = None,
            connect: bool = True) -> InstrumentHandle:
        if not name or not name.replace("_", "").isalnum():
            raise ChannelError("instrument name must be alphanumeric/underscore, got %r" % name)
        cls = self.drivers.get(driver)
        with self._lock:
            if name in self._handles:
                raise ChannelError("instrument %r already exists" % name)
            h = InstrumentHandle(name, driver, cls, address, options or {}, self.resources)
            self._handles[name] = h
        if connect:
            try:
                h.connect()
            except Exception:
                # keep it registered (state=error) so the user can fix & reconnect
                pass
        return h

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
        for h in self.handles():
            try:
                h.disconnect()
            except Exception:
                pass
        with self._lock:
            self._handles.clear()
