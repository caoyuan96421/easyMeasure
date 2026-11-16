"""What a plugin is made of: :class:`Plugin`, :class:`Channel`, :class:`Action`.

A plugin is an add-on to the measurement engine that brings its own panels and
processing.  It runs on its own thread, next to the backend; whatever it does
wrong (an exception, a crash, a loop that never ends) stays with the plugin.  It
talks to the backend through the same commands a front end uses
(:attr:`Plugin.backend`), and it never talks to hardware: instruments are reached
through their drivers, with the backend's limits and locks.

A plugin can

* add panels beside Instruments, Sweep editor and Queue, and tabs beside Live
  data (:attr:`Plugin.ui`, see :mod:`easymeasure.plugins.ui`),
* provide channels and actions of its own (:meth:`Plugin.provide`): they appear
  as an instrument named after the plugin, to monitor, set by hand, alias and
  sweep like any other,
* do work at intervals (:meth:`Plugin.every`, :meth:`Plugin.after`) and react to
  backend events (``EVENTS``, :meth:`Plugin.on_backend_event`).

Everything a plugin's code runs - ``start``, ``stop``, timers, widget callbacks,
backend events, the reads and writes of its channels - runs on the plugin's
thread, one thing at a time, so a plugin needs no locks of its own.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..common.errors import EasyMeasureError
from .ui import PluginUI


class PluginStopped(EasyMeasureError):
    """The plugin was disabled: it can no longer use the backend."""


@dataclass
class Channel:
    """A channel the plugin provides: ``read()`` returns its value, ``write(value)``
    sets it.  *access* ``r``, ``w`` or ``rw``; *dtype* ``float`` or ``str``;
    *allowed*: the accepted values (front ends show a list); *config*: a setting
    (never aliased or swept)."""

    name: str
    access: str = "r"
    read: Optional[Callable[[], Any]] = None
    write: Optional[Callable[[Any], None]] = None
    dtype: str = "float"
    unit: str = ""
    description: str = ""
    allowed: Optional[List[Any]] = None
    config: bool = False
    shape: Optional[Tuple[int, ...]] = ()

    def spec(self):
        from ..backend.instrument import ChannelSpec
        return ChannelSpec(self.name, self.access, self.dtype, self.shape, self.unit,
                           self.description, config=self.config,
                           allowed=list(self.allowed) if self.allowed is not None else None)


@dataclass
class Action:
    """An action the plugin provides (a button in the Manual panel, a user action
    can run it, a point action of a sweep too): ``run()``."""

    name: str
    run: Callable[[], Any]
    description: str = ""
    timeout: Optional[float] = None

    def spec(self):
        from ..backend.instrument import ActionSpec
        return ActionSpec(self.name, self.description, self.timeout)


#: plugins do not manage plugins, nor stop the backend
REFUSED = {"enable_plugin", "disable_plugin", "rescan_plugins", "set_plugin_options",
           "plugin_event", "shutdown", "start_tray", "remote_start", "remote_stop",
           "remote_set_user", "remote_remove_user", "remote_cert_request", "remote_cert_use"}


class BackendAPI:
    """The backend, as a plugin sees it: the same commands a front end sends
    (:meth:`call`), and a few short-hands.  After the plugin was disabled every call
    raises :class:`PluginStopped`."""

    def __init__(self, dispatch: Callable[[str, dict], Any], plugin: str,
                 blocking: Optional[Callable[[bool], None]] = None,
                 guard: Optional[Callable[[str, dict], None]] = None):
        self._dispatch = dispatch
        self._plugin = plugin
        self._revoked = threading.Event()
        self._blocking = blocking or (lambda on: None)
        self._guard = guard or (lambda cmd, args: None)

    def revoke(self) -> None:
        self._revoked.set()

    @property
    def revoked(self) -> bool:
        return self._revoked.is_set()

    def call(self, cmd: str, **args) -> Any:
        """Run a backend command, e.g. ``call("read_channel", name="li.X")``: the list
        is ``Backend.COMMANDS`` (the reference of the commands).  Plugins cannot
        manage plugins nor shut the backend down."""
        if self._revoked.is_set():
            raise PluginStopped("plugin %s is disabled" % self._plugin)
        if cmd in REFUSED:
            raise EasyMeasureError("a plugin cannot use %s" % cmd)
        self._guard(cmd, args)
        self._blocking(True)
        try:
            return self._dispatch(cmd, args)
        finally:
            self._blocking(False)

    def read(self, name: str) -> Any:
        """Read a channel (an alias, ``instrument.channel``, a composite …)."""
        return self.call("read_channel", name=name)

    def write(self, name: str, value: Any, ramp: bool = True) -> Any:
        """Set a channel (limits and ramp rates of aliases apply)."""
        return self.call("write_channel", name=name, value=value, ramp=ramp)

    def action(self, name: str) -> Any:
        """Run an action, e.g. ``action("li.auto_phase")``."""
        return self.call("write_channel", name=name)

    def state(self) -> dict:
        """What a front end shows (``get_state``)."""
        return self.call("get_state")

    def instruments(self, driver: Optional[str] = None) -> List[dict]:
        """The instruments (``list_instruments``), only those of *driver* if given."""
        out = self.call("list_instruments")
        return [d for d in out if driver is None or d.get("driver") == driver]


class Timer:
    """A timer of :meth:`Plugin.every` / :meth:`Plugin.after`: ``cancel()`` it."""

    def __init__(self, interval: float, fn: Callable[[], Any], repeat: bool):
        self.interval = max(0.01, float(interval))
        self.fn = fn
        self.repeat = repeat
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class Plugin:
    """Base class of a plugin.  A plugin file (in ``<home>/plugins`` or a plugin
    folder) defines one subclass; set the class attributes and override what you
    need.  Everything below runs on the plugin's own thread."""

    #: identifier (default: the file's name); also the name of its instrument
    NAME = ""
    #: shown in the Plugins menu
    TITLE = ""
    DESCRIPTION = ""
    VERSION = "1.0"
    #: options (name -> default), changed in the Plugins dialog; the plugin is
    #: restarted when they change
    OPTIONS: Dict[str, Any] = {}
    #: backend events to receive in :meth:`on_backend_event` (``"monitor"``,
    #: ``"sweep_point"``, ``"changed"``, ``"instrument_state"`` … or ``"*"``, which is
    #: everything but ``"log"``: log records only when named)
    EVENTS: Sequence[str] = ()

    # set by the framework before start()
    name: str
    options: Dict[str, Any]
    backend: BackendAPI
    ui: PluginUI
    log: logging.Logger

    # -- override ----------------------------------------------------------------
    def start(self) -> None:
        """Build the panels (``self.ui.add_panel``), provide channels, start timers."""

    def stop(self) -> None:
        """Clean up.  (Panels, channels and timers are removed by the framework, also
        when this is never reached because the plugin is stuck.)"""

    def on_event(self, widget: str, event: str, value: Any) -> None:
        """A widget without its own callback was operated (``click``, ``turn`` with
        the steps, ``change`` with the new value)."""

    def on_backend_event(self, kind: str, data: Any) -> None:
        """A backend event of a kind in ``EVENTS``."""

    # -- use ---------------------------------------------------------------------
    def every(self, seconds: float, fn: Callable[[], Any]) -> Timer:
        """Run *fn* every *seconds* (on the plugin's thread; the next run is planned
        when this one has finished, so a slow *fn* never piles up)."""
        return self._host.add_timer(Timer(seconds, fn, True))

    def after(self, seconds: float, fn: Callable[[], Any]) -> Timer:
        """Run *fn* once, *seconds* from now."""
        return self._host.add_timer(Timer(seconds, fn, False))

    def provide(self, channels: Sequence[Channel] = (), actions: Sequence[Action] = ()) -> None:
        """Provide channels and actions, as an instrument named like the plugin
        (``<name>.<channel>``).  Called again, the new lists replace the old."""
        self._host.provide(list(channels), list(actions))

    @property
    def instrument(self) -> str:
        """The name of the plugin's instrument (its channels are ``<instrument>.<ch>``)."""
        return self.name

    _host: Any = None
