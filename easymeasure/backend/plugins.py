"""The plugin manager: finds plugins, runs each on its own thread, and ends them.

* **Where plugins come from.** The plugins that come with easyMeasure
  (:mod:`easymeasure.plugins.builtin`); installed packages that name them under the
  entry point group ``easymeasure.plugins`` (``pip install …``); ``<home>/plugins``
  and the folders of the ``plugin_paths`` setting: a ``.py`` file or a folder with
  ``__init__.py`` each, defining one :class:`~easymeasure.plugins.Plugin` subclass.
* **Versions.** A plugin declares the plugin API it was written for (``API``); one
  this easyMeasure cannot run is not loaded, and the Plugins dialog says why.
* **Its own thread.** Everything of a plugin - ``start``, ``stop``, timers, widget
  callbacks, backend events, the reads and writes of its channels - runs on the
  plugin's thread.  An exception there is logged and shown in the Plugins dialog;
  the backend goes on.  A ``start`` that fails leaves the plugin off.
* **Clean disabling, at any time.** Disabling revokes the plugin's access to the
  backend at once, removes its panels, channels and timers and asks the plugin to
  ``stop``; a plugin that does not return in time (stuck in a loop) is left
  behind, cut off from everything.
* **The enabled plugins and their options** are kept in the backend settings and
  enabled again when the backend starts.
"""

from __future__ import annotations

import heapq
import importlib
import importlib.metadata
import importlib.util
import inspect
import itertools
import logging
import pkgutil
import queue
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..common.errors import ChannelError, EasyMeasureError, InstrumentError
from ..plugins.api import (API_VERSION, Action, BackendAPI, Channel, Plugin, PluginStopped,
                          Timer, api_problem)
from ..plugins.ui import PluginUI, UIError
from .instrument import Instrument
from .logsys import get_logger

log = get_logger("plugins")

#: how long enabling waits for ``start``, and disabling for ``stop``
START_TIMEOUT = 10.0
STOP_TIMEOUT = 3.0
#: a channel read / write / action of the plugin waits this long for its thread
CALL_TIMEOUT = 10.0
#: widget changes are sent to the front ends at most this often
UI_FLUSH = 0.05
#: backend events waiting for a slow plugin beyond this many are dropped
MAX_PENDING_EVENTS = 500
#: the entry point group of installed plugins
ENTRY_POINTS = "easymeasure.plugins"


class PluginInfo:
    """A plugin that can be enabled: its class and where it came from (*kind*:
    ``builtin``, ``package`` (an installed one) or ``file``)."""

    def __init__(self, name: str, cls: type, source: str, kind: str):
        self.name, self.cls, self.source, self.kind = name, cls, source, kind

    @property
    def builtin(self) -> bool:
        return self.kind == "builtin"

    @property
    def title(self) -> str:
        return self.cls.TITLE or self.name

    @property
    def description(self) -> str:
        return (self.cls.DESCRIPTION or (self.cls.__doc__ or "")).strip()


class _Task:
    __slots__ = ("fn", "args", "done", "result", "error", "what")

    def __init__(self, fn, args, what, wait: bool):
        self.fn, self.args, self.what = fn, args, what
        self.done = threading.Event() if wait else None
        self.result = None
        self.error: Optional[BaseException] = None


class PluginHost:
    """One running plugin: its thread, its queue of work, its timers."""

    def __init__(self, manager: "PluginManager", info: PluginInfo, options: dict):
        self.manager = manager
        self.info = info
        self.name = info.name
        self.options = dict(options)
        self.state = "starting"            # starting | running | stopping | stopped | error
        self.last_error = ""
        self.errors = 0
        self.recent_errors: "deque[str]" = deque(maxlen=10)
        self.dropped_events = 0
        self._tasks: "queue.Queue[Optional[_Task]]" = queue.Queue()
        self._pending_events = 0
        self._ev_lock = threading.Lock()
        self._timers: list = []
        self._seq = itertools.count()
        self._timer_lock = threading.Lock()
        self._stop = threading.Event()
        self._ui_pending: Dict[str, dict] = {}
        self._ui_lock = threading.Lock()
        self._ui_last = 0.0
        self._blocked = 0                  # the plugin thread waits in a backend call
        self.instrument: Optional[str] = None
        self.channels: Dict[str, Channel] = {}
        self.actions: Dict[str, Action] = {}
        self._in_channel = 0               # running a read / write / action of its own
        self.backend = BackendAPI(manager.backend.dispatch, self.name, self._blocking,
                                  self._guard)
        self.ui = PluginUI(self._panels_changed, self._widget_changed)
        self.plugin: Optional[Plugin] = None
        self.thread = threading.Thread(target=self._loop, name="plugin-%s" % self.name,
                                       daemon=True)
        self.log = logging.getLogger("easymeasure.plugin.%s" % self.name)

    # -- lifecycle -------------------------------------------------------------------
    def start(self) -> None:
        """Make the plugin and run its ``start`` on its thread; raise if that fails."""
        plugin = self.info.cls()
        plugin.name = self.name
        plugin.options = dict(self.options)
        plugin.backend = self.backend
        plugin.ui = self.ui
        plugin.log = self.log
        plugin._host = self
        self.plugin = plugin
        self.thread.start()
        task = self.submit(plugin.start, what="start", wait=True)
        if not task.done.wait(START_TIMEOUT):
            self.last_error = "start did not return within %.0f s" % START_TIMEOUT
            self.end(error=True)
            raise EasyMeasureError("plugin %s: %s" % (self.name, self.last_error))
        if task.error is not None:
            self.last_error = _describe(task.error)
            self.end(error=True)
            raise EasyMeasureError("plugin %s did not start: %s" % (self.name, self.last_error))
        if self.state == "starting":
            self.state = "running"

    def end(self, error: bool = False) -> None:
        """Disable: cut the plugin off at once, then let it ``stop`` (if it can)."""
        if self.state in ("stopped", "error") and self._stop.is_set():
            return
        self.state = "stopping"
        self.backend.revoke()
        with self._timer_lock:
            for _, _, t in self._timers:
                t.cancel()
            self._timers = []
        self._remove_instrument()
        stopped = None
        if self.plugin is not None and self.thread.is_alive():
            stopped = self.submit(self._safe_stop, what="stop", wait=True)
        self._tasks.put(None)                 # the thread ends after stop()
        if stopped is not None and not stopped.done.wait(STOP_TIMEOUT):
            self.log.error("plugin %s did not stop within %.0f s: left behind, cut off from "
                           "the backend", self.name, STOP_TIMEOUT)
        self._stop.set()
        self.ui._panels = []                  # gone from every front end, stuck or not
        self.ui._reindex()
        self.state = "error" if error else "stopped"
        self.manager._emit_plugins()

    def _safe_stop(self) -> None:
        try:
            self.plugin.stop()
        except Exception as exc:
            self.log.warning("plugin %s: stop failed: %s", self.name, exc)

    # -- work on the plugin's thread ----------------------------------------------------
    def submit(self, fn: Callable, *args, what: str = "", wait: bool = False) -> _Task:
        t = _Task(fn, args, what or getattr(fn, "__name__", "call"), wait)
        self._tasks.put(t)
        return t

    def run_sync(self, fn: Callable, *args, timeout: Optional[float] = None) -> Any:
        """Run *fn* on the plugin's thread and return its result (channel reads ...)."""
        timeout = CALL_TIMEOUT if timeout is None else timeout
        if self._stop.is_set() or self.state in ("stopping", "stopped", "error"):
            raise InstrumentError("plugin %s is disabled" % self.name)
        if threading.current_thread() is self.thread or self._blocked:
            # the plugin itself is asking (or waits in a backend call that ends here):
            # running it here instead of on its queue avoids waiting for ourselves
            return fn(*args)
        task = self.submit(self._as_channel, fn, *args, wait=True)
        if not task.done.wait(timeout):
            raise InstrumentError("plugin %s did not answer within %.0f s" % (self.name, timeout))
        if task.error is not None:
            raise task.error
        return task.result

    def _as_channel(self, fn, *args):
        self._in_channel += 1
        try:
            return fn(*args)
        finally:
            self._in_channel -= 1

    def _guard(self, cmd: str, args: dict) -> None:
        """While one of its channels is being read, the plugin's instrument is busy
        with that: a backend call back to it would wait for itself."""
        if not self._in_channel or self.instrument is None:
            return
        own = self.instrument + "."

        def mine(v):
            return isinstance(v, str) and (v.startswith(own) or v == self.instrument)
        vals = list(args.values())
        vals += [x for v in args.values() if isinstance(v, (list, tuple)) for x in v]
        vals += [k for v in args.values() if isinstance(v, dict) for k in v]
        if any(mine(v) for v in vals):
            raise EasyMeasureError(
                "plugin %s: a channel of the plugin uses %s through the backend, which "
                "waits for that very channel - use the plugin's own state instead"
                % (self.name, self.instrument))

    def _blocking(self, on: bool) -> None:
        if threading.current_thread() is self.thread:
            self._blocked += 1 if on else -1

    def add_timer(self, timer: Timer) -> Timer:
        with self._timer_lock:
            if self._stop.is_set():
                timer.cancel()
                return timer
            heapq.heappush(self._timers, (time.monotonic() + timer.interval, next(self._seq),
                                          timer))
        self._tasks.put(_Task(lambda: None, (), "wake", False))      # re-plan the wait
        return timer

    def backend_event(self, kind: str, data: Any) -> None:
        with self._ev_lock:
            if self._pending_events >= MAX_PENDING_EVENTS:
                self.dropped_events += 1
                return
            self._pending_events += 1
        self.submit(self._deliver, kind, data, what="on_backend_event")

    def _deliver(self, kind, data) -> None:
        with self._ev_lock:
            self._pending_events -= 1
        self.plugin.on_backend_event(kind, data)

    def ui_event(self, widget: str, event: str, value: Any) -> None:
        if not self.ui.accepts(widget, event):
            raise ChannelError("plugin %s has no widget %r with a %r event"
                               % (self.name, widget, event))
        self.submit(self._ui_dispatch, widget, event, value, what="%s.%s" % (widget, event))

    def _ui_dispatch(self, widget: str, event: str, value: Any) -> None:
        fn = self.ui.handler(widget, event)
        if fn is None:
            self.plugin.on_event(widget, event, value)
        elif event == "click":
            fn()
        else:
            fn(value)

    def _loop(self) -> None:
        while not self._stop.is_set():
            wait = 0.5
            with self._timer_lock:
                if self._timers:
                    wait = max(0.0, min(wait, self._timers[0][0] - time.monotonic()))
            if self._ui_pending:
                wait = min(wait, max(0.0, self._ui_last + UI_FLUSH - time.monotonic()))
            try:
                task = self._tasks.get(timeout=wait)
            except queue.Empty:
                task = None
            else:
                if task is None:
                    self._flush_ui(force=True)
                    return
                self._run(task)
            self._run_timers()
            self._flush_ui()

    def _run(self, task: _Task) -> None:
        try:
            task.result = task.fn(*task.args)
        except PluginStopped:
            pass                                       # disabled meanwhile: nothing to say
        except BaseException as exc:  # noqa: B902 - nothing of a plugin may escape
            task.error = exc if isinstance(exc, Exception) else EasyMeasureError(
                "%s: %s" % (type(exc).__name__, exc))
            if task.done is None or task.what in ("start",):
                self.errors += 1
                self.last_error = "%s: %s" % (task.what, _describe(exc))
                self.recent_errors.append("%s  %s" % (time.strftime("%H:%M:%S"), self.last_error))
                self.log.error("plugin %s: %s failed: %s", self.name, task.what,
                               _describe(exc, tb=True))
                if task.what != "start":
                    self.manager._emit_plugins()
        finally:
            if task.done is not None:
                task.done.set()

    def _run_timers(self) -> None:
        now = time.monotonic()
        while True:
            with self._timer_lock:
                if not self._timers or self._timers[0][0] > now or self._stop.is_set():
                    return
                _, _, t = heapq.heappop(self._timers)
            if t.cancelled:
                continue
            self._run(_Task(t.fn, (), "timer %s" % getattr(t.fn, "__name__", ""), False))
            if t.repeat and not t.cancelled:
                with self._timer_lock:
                    if not self._stop.is_set():
                        heapq.heappush(self._timers, (time.monotonic() + t.interval,
                                                      next(self._seq), t))

    # -- the panels -----------------------------------------------------------------
    def _panels_changed(self) -> None:
        if self.state != "stopping":
            self.manager._emit_plugins()

    def _widget_changed(self, widget: str, props: dict) -> None:
        with self._ui_lock:
            self._ui_pending.setdefault(widget, {}).update(props)
        if threading.current_thread() is not self.thread:
            self._tasks.put(_Task(lambda: None, (), "wake", False))

    def _flush_ui(self, force: bool = False) -> None:
        now = time.monotonic()
        if not self._ui_pending or (not force and now - self._ui_last < UI_FLUSH):
            return
        with self._ui_lock:
            changes, self._ui_pending = self._ui_pending, {}
        self._ui_last = now
        if not self.backend.revoked:
            self.manager.backend.emit("plugin_ui", {"plugin": self.name, "set": changes})

    # -- channels and actions -----------------------------------------------------------
    def provide(self, channels: List[Channel], actions: List[Action]) -> None:
        for c in channels:
            if not isinstance(c, Channel):
                raise TypeError("provide(channels=…) takes Channel objects")
            if "r" in c.access and c.read is None:
                raise ValueError("channel %s can be read, but has no read()" % c.name)
            if "w" in c.access and c.write is None:
                raise ValueError("channel %s can be written, but has no write()" % c.name)
        names = [c.name for c in channels] + [a.name for a in actions]
        if len(set(names)) != len(names):
            raise ValueError("channel and action names must be unique")
        self.channels = {c.name: c for c in channels}
        self.actions = {a.name: a for a in actions}
        self._remove_instrument()
        if channels or actions:
            self.manager._add_instrument(self)

    def _remove_instrument(self) -> None:
        if self.instrument is not None:
            self.manager._remove_instrument(self)
            self.instrument = None

    def describe(self) -> dict:
        return {"instrument": self.instrument, "state": self.state, "last_error": self.last_error,
                "errors": self.errors, "recent_errors": list(self.recent_errors),
                "dropped_events": self.dropped_events,
                "panels": self.ui.describe() if self.state in ("starting", "running") else []}


def plugin_driver(host: PluginHost) -> type:
    """The driver class of a plugin's instrument: it hands every call to the plugin's
    thread (and refuses once the plugin is disabled)."""

    class PluginInstrument(Instrument):
        DESCRIPTION = "channels of plugin %s" % host.name
        INTERFACES = ("none",)

        def connect(self):
            if host.state not in ("starting", "running"):
                raise InstrumentError("plugin %s is disabled" % host.name)

        def get_channels(self):
            return [c.spec() for c in host.channels.values()]

        def get_actions(self):
            return [a.spec() for a in host.actions.values()]

        def read(self, channel):
            c = host.channels.get(channel)
            if c is None or c.read is None:
                raise ChannelError("%s.%s cannot be read" % (host.name, channel))
            return host.run_sync(c.read)

        def write(self, channel, value):
            c = host.channels.get(channel)
            if c is None or c.write is None:
                raise ChannelError("%s.%s cannot be written" % (host.name, channel))
            host.run_sync(c.write, value)

        def action(self, name):
            a = host.actions.get(name)
            if a is None:
                raise ChannelError("plugin %s has no action %s" % (host.name, name))
            return host.run_sync(a.run, timeout=a.timeout)

    PluginInstrument.__name__ = "Plugin_%s" % host.name
    return PluginInstrument


def _describe(exc: BaseException, tb: bool = False) -> str:
    text = "%s: %s" % (type(exc).__name__, exc)
    if tb:
        text += "\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__,
                                                          limit=6))
    return text


class PluginManager:
    def __init__(self, backend):
        self.backend = backend
        self.available: Dict[str, PluginInfo] = {}
        self.errors: Dict[str, str] = {}          # file -> why it did not load
        self.hosts: Dict[str, PluginHost] = {}
        self._lock = threading.RLock()
        self._gen = itertools.count(1)
        backend.add_listener(self._backend_event)

    # -- settings ---------------------------------------------------------------------
    def _settings(self) -> dict:
        return self.backend.settings

    def enabled_names(self) -> List[str]:
        return list(self._settings().get("plugins_enabled") or [])

    def options_of(self, name: str) -> dict:
        info = self.available.get(name)
        opts = dict(getattr(info.cls, "OPTIONS", {}) or {}) if info else {}
        opts.update((self._settings().get("plugin_options") or {}).get(name) or {})
        return opts

    def _remember(self, name: str, enabled: Optional[bool] = None,
                  options: Optional[dict] = None) -> None:
        st = self._settings()
        on = [n for n in (st.get("plugins_enabled") or []) if n != name]
        if enabled or (enabled is None and name in (st.get("plugins_enabled") or [])):
            on.append(name)
        st["plugins_enabled"] = on
        if options is not None:
            st.setdefault("plugin_options", {})[name] = dict(options)
        self.backend._save_settings()

    # -- discovery ----------------------------------------------------------------------
    def folders(self) -> List[Path]:
        return [self.backend.home / "plugins"] + [
            Path(p) for p in self._settings().get("plugin_paths") or []]

    def scan(self) -> None:
        found: Dict[str, PluginInfo] = {}
        errors: Dict[str, str] = {}
        from ..plugins import builtin
        for m in pkgutil.iter_modules(builtin.__path__):
            if m.name.startswith("_"):
                continue
            modname = builtin.__name__ + "." + m.name
            try:
                module = importlib.import_module(modname)
                self._collect(module, m.name, modname, "builtin", found, errors)
            except Exception:
                errors[modname] = traceback.format_exc(limit=5)
        self._scan_packages(found, errors)
        for folder in self.folders():
            try:
                folder.mkdir(parents=True, exist_ok=True)
                entries = sorted(folder.iterdir())
            except OSError as exc:
                errors[str(folder)] = str(exc)
                continue
            for p in entries:
                if p.name.startswith(("_", ".")):
                    continue
                if p.is_dir() and (p / "__init__.py").exists():
                    src = p / "__init__.py"
                elif p.suffix == ".py":
                    src = p
                else:
                    continue
                try:
                    module = self._load_file(p.stem if p.is_file() else p.name, src)
                    self._collect(module, p.stem if p.is_file() else p.name, str(p), "file",
                                  found, errors)
                except Exception:
                    errors[str(p)] = traceback.format_exc(limit=5)
        with self._lock:
            self.available, self.errors = found, errors
        for f, e in errors.items():
            log.error("plugin %s failed to load:\n%s", f, e)
        log.info("plugins: %d found, %d failed", len(found), len(errors))

    def _scan_packages(self, found: dict, errors: dict) -> None:
        """Installed packages naming plugins in their entry points::

            [project.entry-points."easymeasure.plugins"]
            leakage = "easymeasure_leakage:Leakage"     # a Plugin class, or a module

        An upgraded package is loaded anew when the backend starts again."""
        try:
            eps = importlib.metadata.entry_points(group=ENTRY_POINTS)
        except Exception:
            errors[ENTRY_POINTS] = traceback.format_exc(limit=3)
            return
        for ep in sorted(eps, key=lambda e: e.name):
            dist = getattr(ep, "dist", None)
            where = "package %s %s (%s)" % (dist.name, dist.version, ep.value) if dist \
                else "package (%s)" % ep.value
            try:
                obj = ep.load()
                if inspect.ismodule(obj):
                    self._collect(obj, ep.name, where, "package", found, errors)
                elif inspect.isclass(obj) and issubclass(obj, Plugin):
                    self._add(obj, ep.name, where, "package", found, errors)
                else:
                    errors[where] = "the entry point %s is neither a Plugin class nor a module" \
                        % ep.name
            except Exception:
                errors[where] = traceback.format_exc(limit=5)

    def _load_file(self, stem: str, src: Path):
        # a fresh module every scan, so an edited plugin is loaded anew
        modname = "easymeasure_user_plugins.%s_%d" % (stem, next(self._gen))
        spec = importlib.util.spec_from_file_location(
            modname, str(src), submodule_search_locations=[str(src.parent)]
            if src.name == "__init__.py" else None)
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        spec.loader.exec_module(module)       # type: ignore[union-attr]
        return module

    @classmethod
    def _collect(cls_, module, stem: str, source: str, kind: str, found: dict, errors: dict):
        cls = getattr(module, "PLUGIN", None)
        if cls is None:
            cands = [c for _, c in inspect.getmembers(module, inspect.isclass)
                     if issubclass(c, Plugin) and c is not Plugin
                     and c.__module__ == module.__name__]
            if not cands:
                return
            if len(cands) > 1:
                errors[source] = "it defines several plugins; set PLUGIN = <class>"
                return
            cls = cands[0]
        if not (inspect.isclass(cls) and issubclass(cls, Plugin)):
            errors[source] = "PLUGIN is not a Plugin subclass"
            return
        cls_._add(cls, stem, source, kind, found, errors)

    @staticmethod
    def _add(cls, stem: str, source: str, kind: str, found: dict, errors: dict) -> None:
        name = cls.NAME or stem
        if not name.replace("_", "").isalnum():
            errors[source] = "plugin name %r: letters, digits and _ only" % name
            return
        problem = api_problem(getattr(cls, "API", ""))
        if problem:
            errors[source] = "plugin %s is not loaded: %s" % (name, problem)
            return
        if name in found:
            errors[source] = "a plugin named %s exists already (%s)" % (name, found[name].source)
            return
        found[name] = PluginInfo(name, cls, source, kind)

    # -- enable / disable ------------------------------------------------------------------
    def enable(self, name: str, remember: bool = True) -> dict:
        with self._lock:
            info = self.available.get(name)
            if info is None:
                raise ChannelError("no plugin %r (see list_plugins)" % name)
            h = self.hosts.get(name)
            if h is not None and h.state in ("starting", "running"):
                return self.describe_one(name)
            host = PluginHost(self, info, self.options_of(name))
            self.hosts[name] = host
        if remember:
            self._remember(name, enabled=True)
        log.info("starting plugin %s", name)
        try:
            host.start()
        except Exception:
            log.error("plugin %s: %s", name, host.last_error)
            raise
        finally:
            self._emit_plugins()
        return self.describe_one(name)

    def disable(self, name: str, remember: bool = True) -> dict:
        with self._lock:
            if name not in self.available and name not in self.hosts:
                raise ChannelError("no plugin %r" % name)
            h = self.hosts.get(name)
        if remember:
            self._remember(name, enabled=False)
        if h is not None:
            log.info("stopping plugin %s", name)
            h.end()
        self._emit_plugins()
        return self.describe_one(name)

    def set_options(self, name: str, options: dict) -> dict:
        info = self.available.get(name)
        if info is None:
            raise ChannelError("no plugin %r" % name)
        defaults = dict(getattr(info.cls, "OPTIONS", {}) or {})
        bad = [k for k in options if k not in defaults]
        if bad:
            raise ChannelError("plugin %s has no option(s) %s" % (name, ", ".join(bad)))
        self._remember(name, options=options)
        h = self.hosts.get(name)
        if h is not None and h.state in ("starting", "running"):
            self.disable(name, remember=False)          # options apply on a fresh start
            self.enable(name, remember=False)
        return self.describe_one(name)

    def start_enabled(self) -> None:
        for name in self.enabled_names():
            if name not in self.available:
                log.warning("plugin %s is enabled but not found", name)
                continue
            try:
                self.enable(name, remember=False)
            except Exception as exc:
                log.error("plugin %s: %s", name, exc)

    def stop_all(self) -> None:
        for name in list(self.hosts):
            try:
                h = self.hosts[name]
                if h.state in ("starting", "running"):
                    h.end()
            except Exception as exc:
                log.warning("stopping plugin %s: %s", name, exc)

    # -- routing -------------------------------------------------------------------------
    def ui_event(self, plugin: str, widget: str, event: str, value: Any = None) -> None:
        h = self.hosts.get(plugin)
        if h is None or h.state not in ("starting", "running"):
            raise ChannelError("plugin %s is not enabled" % plugin)
        h.ui_event(widget, event, value)

    def _backend_event(self, kind: str, data: Any) -> None:
        if kind == "plugin_ui":
            return
        for h in list(self.hosts.values()):
            if h.state != "running" or h.plugin is None:
                continue
            ev = h.plugin.EVENTS
            # log records only when asked by name: a plugin logging in its handler
            # would otherwise feed itself
            if kind in ev or (kind != "log" and (ev == "*" or "*" in ev)):
                h.backend_event(kind, data)

    # -- the plugin's instrument -----------------------------------------------------------
    def _add_instrument(self, host: PluginHost) -> None:
        name = host.plugin.instrument
        self.backend.channels.claim(name, "instrument")
        self.backend.instruments.add(name, "plugin:%s" % host.name, "NONE", {},
                                     connect=True, cls=plugin_driver(host), owner=host.name)
        host.instrument = name
        self.backend._changed("instruments")

    def _remove_instrument(self, host: PluginHost) -> None:
        try:
            self.backend.instruments.remove(host.instrument)
        except ChannelError:
            pass
        self.backend._changed("instruments")

    # -- describing --------------------------------------------------------------------
    def describe_one(self, name: str) -> dict:
        info = self.available.get(name)
        h = self.hosts.get(name)
        d = {"name": name, "title": info.title if info else name,
             "description": info.description if info else "",
             "version": getattr(info.cls, "VERSION", "") if info else "",
             "api": getattr(info.cls, "API", "") if info else "",
             "source": info.source if info else "", "builtin": bool(info and info.builtin),
             "kind": info.kind if info else "",
             "available": info is not None, "enabled": name in self.enabled_names(),
             "options": self.options_of(name),
             "defaults": dict(getattr(info.cls, "OPTIONS", {}) or {}) if info else {},
             "state": "off", "last_error": "", "errors": 0, "recent_errors": [], "panels": [],
             "instrument": None}
        if h is not None:
            d.update(h.describe())
            if h.state == "stopped":
                d["state"] = "off"
        return d

    def describe(self) -> dict:
        names = sorted(set(self.available) | set(self.hosts))
        return {"plugins": [self.describe_one(n) for n in names], "errors": dict(self.errors),
                "folders": [str(p) for p in self.folders()], "api": API_VERSION}

    def _emit_plugins(self) -> None:
        try:
            self.backend.emit("plugins", self.describe())
        except Exception as exc:
            log.warning("plugins event: %s", exc)


__all__ = ["PluginManager", "PluginHost", "PluginInfo", "UIError"]
