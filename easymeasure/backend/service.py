"""The backend service: wires all modules together and exposes the command API.

:class:`Backend` is usable in-process (tests, scripts) and is what the IPC
server (:mod:`easymeasure.backend.server`) exposes to frontends.  Every public
command is a method listed in :attr:`Backend.COMMANDS`; it takes keyword
arguments and returns JSON/codec-serialisable data.
"""

from __future__ import annotations

import copy
import os
import shutil
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from ..common import paths
from ..common.errors import (ChannelError, DriverError, EasyMeasureError, RampStopped,
                             SweepDefinitionError)
from . import setup_io
from .channels import Alias, ChannelManager
from .composites import composite_from_dict
from .registry import DriverRegistry
from .instrument import InstrumentManager
from .logsys import get_logger, ring, setup_logging
from .monitor import Monitor
from .resources import ResourceManager, normalize_address
from .runqueue import RunQueue
from .scripting import ScriptRunner
from .storage import DataSet, DataStore, export_hdf5, read_header
from .user_actions import UserAction, UserActions
from .sweep import RunControl, SweepRunner, compute_setpoints, normalize_definition, validate_against

log = get_logger("backend")

DEFAULT_SETTINGS = {
    "data_dir": None,            # default: ~/.easymeasure/data
    "ppt_path": "",              # append slides here when a run ends ("" = off)
    "auto_ppt": True,            # append a slide automatically at the end of every run
    "export_hdf5": True,         # write an .h5 copy next to the journal at the end
    "fsync_interval": 0.5,       # seconds between fsyncs of the data file (data lost on a crash)
    "driver_paths": [],          # extra directories with driver files
    "autosave_setup": True,      # save the setup to <home>/autosave_setup.json on every change
    "ppt_colormap": "viridis",   # colormap of the heat maps on slides
    "remote_access": False,      # start the internet gateway with the backend
    "folders_confirmed": False,  # the GUI asked for the data folder / PowerPoint file once
}


class Backend:
    COMMANDS = (
        "ping", "get_state", "get_settings", "set_settings",
        "list_drivers", "rescan_drivers", "discover_resources", "list_resources",
        "wizard_templates", "wizard_check", "wizard_list", "wizard_load", "wizard_save",
        "wizard_try", "wizard_delete",
        "add_instrument", "remove_instrument", "connect_instrument", "disconnect_instrument",
        "reconnect_instrument", "update_instrument", "list_instruments",
        "read_channel", "write_channel", "query_channel", "raw_query", "raw_write",
        "channel_info",
        "define_alias", "remove_alias", "define_composite", "remove_composite", "check_composite",
        "set_many", "list_channels", "stop_ramps", "ramp_state",
        "define_parameter", "remove_parameter", "set_parameter", "list_parameters",
        "check_parameter", "check_code", "check_user_action",
        "define_user_action", "remove_user_action", "move_user_action", "run_user_action",
        "abort_user_action", "list_user_actions",
        "monitor_add", "monitor_remove", "monitor_list", "monitor_history",
        "validate_sweep", "queue_add", "queue_remove", "queue_move", "queue_clear",
        "queue_start", "queue_stop", "queue_state", "queue_options", "pause", "resume", "abort",
        "abort_all", "current_run", "run_snapshot", "list_runs", "load_run", "read_run_header",
        "save_setup", "load_setup", "get_setup", "make_ppt", "recent_logs", "run_script_now",
        "shutdown", "start_tray", "run_action", "remote_status", "remote_start", "remote_stop",
        "remote_set_user", "remote_remove_user", "remote_cert_request", "remote_cert_use",
    )

    def __init__(self, settings: Optional[dict] = None, home: Optional[Path] = None,
                 log_to_console: bool = True):
        self.home = Path(home) if home else paths.home_dir()
        self.ring = setup_logging(self.home / "logs", console=log_to_console)
        self.settings = dict(DEFAULT_SETTINGS)
        self._settings_file = self.home / "backend_settings.json"
        try:
            if self._settings_file.exists():
                import json
                self.settings.update(json.loads(self._settings_file.read_text()))
        except Exception as exc:
            log.warning("cannot read settings: %s", exc)
        self.settings.update(settings or {})
        if not self.settings.get("data_dir"):
            self.settings["data_dir"] = str(self.home / "data")
        self._listeners: List[Callable[[str, Any], None]] = []
        self._listeners_lock = threading.Lock()

        self.resources = ResourceManager()
        #: drivers made with the Driver Wizard (and any others dropped here)
        self.user_driver_dir = self.home / "drivers"
        self.user_driver_dir.mkdir(parents=True, exist_ok=True)
        self.drivers = DriverRegistry(self._driver_paths())
        self.drivers.scan()
        self.instruments = InstrumentManager(self.resources, self.drivers)
        self.channels = ChannelManager(self.instruments)
        self.instruments.listeners.append(self._instrument_specs_changed)
        self.instruments.fault_listeners.append(self._instrument_fault)
        self.control = RunControl()
        self.monitor = Monitor(self.channels)
        self.monitor.listeners.append(lambda m: self.emit("monitor", m))
        self.channels.ramp_listeners.append(lambda act: self.emit("ramps", {"active": act}))
        self.scripts = ScriptRunner(self.channels, self.instruments, self.control,
                                    run_sweep=self._script_run_sweep)
        self.store = DataStore(Path(self.settings["data_dir"]))
        self.runner: Optional[SweepRunner] = None
        self.last_dataset: Optional[DataSet] = None
        self._run_lock = threading.Lock()
        self.queue = RunQueue(self._execute_sweep_item, self.scripts.run, self.control, self.emit)
        self.user_actions = UserActions(self._action_runner, self.emit)
        self.ring.add_listener(self._on_log)
        self.monitor.start()
        self.started = time.time()
        self._tray = None
        self._remote = None
        self._remote_want = False
        self._remote_args: dict = {}
        self._shutdown_cb: Optional[Callable[[], None]] = None
        self._post_threads: List[threading.Thread] = []
        log.info("backend started (home %s, data %s)", self.home, self.settings["data_dir"])

    # ------------------------------------------------------------------ events --
    def add_listener(self, cb: Callable[[str, Any], None]) -> None:
        with self._listeners_lock:
            self._listeners.append(cb)

    def remove_listener(self, cb) -> None:
        with self._listeners_lock:
            try:
                self._listeners.remove(cb)
            except ValueError:
                pass

    def emit(self, kind: str, payload: Any) -> None:
        with self._listeners_lock:
            ls = list(self._listeners)
        for cb in ls:
            try:
                cb(kind, payload)
            except Exception:
                pass

    def _on_log(self, entry: dict) -> None:
        self.emit("log", entry)

    # ---------------------------------------------------------------- dispatch --
    def dispatch(self, cmd: str, args: Optional[dict] = None) -> Any:
        if cmd not in self.COMMANDS:
            raise ChannelError("unknown command %r" % cmd)
        fn = getattr(self, cmd)
        return fn(**(args or {}))

    # ---------------------------------------------------------------- general --
    def ping(self) -> dict:
        """Liveness check: ``{pong: <server time>, uptime: <s>}``."""
        return {"pong": time.time(), "uptime": time.time() - self.started}

    def get_state(self) -> dict:
        """Everything a front end shows, in one dict: ``uptime``, ``settings``,
        ``instruments``, ``channels`` (aliases, composites, virtual channels), ``monitor``,
        ``actions`` (user actions), ``queue``, ``current_run``, ``resources`` and
        ``ramps`` (ramps of direct writes now running, see ``ramp_state``)."""
        return {
            "uptime": time.time() - self.started,
            "settings": self.get_settings(),
            "instruments": self.instruments.describe(),
            "channels": self.channels.describe(),
            "monitor": self.monitor.status(),
            "actions": self.user_actions.describe(),
            "queue": self.queue.describe(),
            "current_run": self.current_run(),
            "resources": self.resources.list(),
            "ramps": self.channels.active_ramps(),
        }

    def get_settings(self) -> dict:
        """The backend settings (see :data:`DEFAULT_SETTINGS`) plus derived values
        such as the effective data folder."""
        return copy.deepcopy(self.settings)

    def set_settings(self, **kwargs) -> dict:
        """Change backend settings, e.g. ``set_settings(data_dir="D:/data", ppt_path="runs.pptx")``.
        Unknown keys are refused; a data folder that cannot be created is refused.
        Returns the new settings."""
        unknown = [k for k in kwargs if k not in DEFAULT_SETTINGS]
        if unknown:
            raise ChannelError("unknown setting(s): %s" % ", ".join(unknown))
        if kwargs.get("data_dir"):
            try:                                  # usable? (created if missing) - else refuse
                store = DataStore(Path(os.path.expanduser(str(kwargs["data_dir"]))))
            except OSError as exc:
                raise ChannelError("data folder %s cannot be used: %s"
                                   % (kwargs["data_dir"], exc.strerror or exc)) from None
            kwargs["data_dir"] = str(store.root)
            self.store = store
        elif "data_dir" in kwargs:
            kwargs.pop("data_dir")                 # empty: keep the current one
        self.settings.update(kwargs)
        try:
            setup_io.atomic_write_json(self._settings_file, self.settings)
        except Exception as exc:
            log.warning("cannot persist settings: %s", exc)
        self.emit("settings", self.get_settings())
        return self.get_settings()

    def recent_logs(self, n: int = 500, min_level: int = 0) -> list:
        """The last *n* log records at or above *min_level* (logging levels)."""
        return self.ring.recent(int(n), int(min_level))

    # ---------------------------------------------------------------- drivers --
    def list_drivers(self) -> dict:
        """All drivers that could be loaded: ``{drivers: {name: {description, options,
        channels, shared, interfaces, idn_match, suggest}}, errors: {file: message}}``."""
        return self.drivers.describe()

    def _driver_paths(self) -> list:
        return [self.user_driver_dir] + [Path(p) for p in self.settings.get("driver_paths") or []]

    def rescan_drivers(self) -> dict:
        """Load the driver directories again (after adding or changing a driver file);
        returns the same as :meth:`list_drivers`."""
        self.drivers.extra_paths = self._driver_paths()
        self.drivers.scan()
        return self.drivers.describe()

    # ---------------------------------------------------------- driver wizard --
    def _builtin_driver_names(self) -> set:
        import pkgutil
        from .. import drivers as pkg
        return {m.name for m in pkgutil.iter_modules(pkg.__path__)}

    def wizard_templates(self) -> list:
        """The Driver Wizard's starting points (generic SCPI, SMU, DMM, line protocol)."""
        from ..drivers import _command
        return _command.templates()

    def wizard_check(self, definition: dict) -> dict:
        """Problems of a definition, and the Python file it would become."""
        from ..drivers import _command
        errors, warnings = _command.check_definition(definition, self._builtin_driver_names())
        source = _command.render_source(definition) if not errors else ""
        path = self.user_driver_dir / ("%s.py" % definition.get("name", ""))
        if not errors and path.exists() and not _command.is_wizard_file(path):
            errors.append("%s exists and was not made with the wizard" % path)
        elif not errors and path.exists():
            warnings.append("saving replaces the existing driver %s" % definition["name"])
            if _command.edited_by_hand(path):
                warnings.append("%s was edited by hand outside DEFINITION; saving replaces those "
                                "edits (the old file is kept as %s.bak)" % (path.name, path.name))
        return {"errors": errors, "warnings": warnings, "source": source, "path": str(path)}

    def wizard_list(self) -> list:
        """Drivers made with the wizard (they can be reopened)."""
        from ..drivers import _command
        out = []
        for f in sorted(self.user_driver_dir.glob("*.py")):
            if _command.is_wizard_file(f):
                try:
                    d = _command.read_definition(f)
                    out.append({"name": f.stem, "path": str(f),
                                "description": d.get("description", ""),
                                "channels": len(d.get("channels", []))})
                except Exception as exc:
                    out.append({"name": f.stem, "path": str(f), "error": str(exc)})
        return out

    def wizard_load(self, name: str) -> dict:
        """The definition of a driver made with the wizard, to edit it again."""
        from ..drivers import _command
        path = self.user_driver_dir / ("%s.py" % name)
        if not path.exists():
            raise DriverError("no wizard driver %r" % name)
        return _command.read_definition(path)

    def wizard_save(self, definition: dict, overwrite: bool = False) -> dict:
        """Write the driver file, load it, and reconnect instruments that use it."""
        from ..drivers import _command
        chk = self.wizard_check(definition)
        if chk["errors"]:
            raise DriverError("; ".join(chk["errors"]))
        path = Path(chk["path"])
        if path.exists() and not overwrite:
            raise DriverError("driver %s exists already (save again to replace it)"
                              % definition["name"])
        if path.exists() and _command.edited_by_hand(path):
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        tmp = path.with_suffix(".py.tmp")
        tmp.write_text(chk["source"], encoding="utf-8")
        os.replace(tmp, path)
        self.rescan_drivers()
        err = self.drivers.describe()["errors"].get(definition["name"])
        if err:
            raise DriverError("saved %s but it does not load: %s" % (path, err))
        reloaded = []
        if self.runner is None:
            for h in list(self.instruments.handles()):
                if h.driver_name == definition["name"]:
                    try:
                        self.update_instrument(h.name)
                        reloaded.append(h.name)
                    except Exception as exc:
                        log.warning("instrument %s did not reload: %s", h.name, exc)
        log.info("driver %s saved to %s (reloaded: %s)", definition["name"], path,
                 ", ".join(reloaded) or "-")
        self._changed("drivers")
        return {"path": str(path), "driver": definition["name"], "reloaded": reloaded,
                "warnings": chk["warnings"]}

    def wizard_delete(self, name: str) -> None:
        """Delete a driver file made with the wizard (refused while an instrument uses it)."""
        from ..drivers import _command
        path = self.user_driver_dir / ("%s.py" % name)
        if not _command.is_wizard_file(path):
            raise DriverError("no wizard driver %r" % name)
        users = [h.name for h in self.instruments.handles() if h.driver_name == name]
        if users:
            raise DriverError("driver %s is used by %s" % (name, ", ".join(users)))
        path.unlink()
        self.rescan_drivers()
        self._changed("drivers")

    def wizard_try(self, definition: dict, address: str, options: Optional[dict] = None) -> dict:
        """Connect a temporary instrument with this definition and read every readable
        channel once (nothing is saved, nothing written).  Errors are reported per
        channel, not raised."""
        from ..drivers import _command
        errors, warnings = _command.check_definition(definition)
        if errors:
            return {"connected": False, "error": "; ".join(errors), "readings": {}}
        cls = _command.CommandInstrument.from_definition(definition)
        tmp = "wizard_try"
        while tmp in self.instruments.names():
            tmp += "_"
        self.drivers.register("__wizard_try__", cls)
        out = {"connected": False, "error": "", "readings": {}, "idn": ""}
        try:
            h = self.instruments.add(tmp, "__wizard_try__", address or "NONE", options or {},
                                     connect=True)
            out["connected"] = h.state == "connected"
            out["error"] = h.last_error if not out["connected"] else ""
            if out["connected"]:
                out["idn"] = getattr(h.driver, "idn", "")
                for c in h.channels.values():
                    if not c.readable:
                        continue
                    t0 = time.perf_counter()
                    try:
                        v = h.read(c.name)
                        out["readings"][c.name] = {"value": v, "unit": c.unit,
                                                   "ms": round((time.perf_counter() - t0) * 1e3, 1)}
                    except Exception as exc:
                        out["readings"][c.name] = {"error": "%s: %s" % (type(exc).__name__, exc)}
        except Exception as exc:
            out["error"] = "%s: %s" % (type(exc).__name__, exc)
        finally:
            try:
                if tmp in self.instruments.names():
                    self.instruments.remove(tmp)
            finally:
                self.drivers.unregister("__wizard_try__")
        return out

    def discover_resources(self, interfaces: Optional[list] = None, identify: bool = False) -> dict:
        """Ports / devices of this computer by interface (see ResourceManager.discover);
        in-use entries name the instruments and carry their identification."""
        by_addr: dict = {}
        for h in self.instruments.handles():
            by_addr.setdefault(normalize_address(h.address).upper(), []).append(h)
        r = self.resources.discover(interfaces, identify,
                                    in_use={a: [h.name for h in hs] for a, hs in by_addr.items()})
        for e in r["found"]:
            for h in by_addr.get(normalize_address(e["address"]).upper(), []):
                idn = getattr(h.driver, "idn", "") or getattr(h.driver, "product", "")
                if idn and not e.get("idn"):
                    e["idn"] = str(idn)              # what the instrument told its driver
        return r

    def list_resources(self) -> list:
        """The ports in use: one entry per resource (address, interface, owners,
        shared flag)."""
        return self.resources.list()

    # ------------------------------------------------------------ instruments --
    def add_instrument(self, name: str, driver: str, address: str = "NONE",
                       options: Optional[dict] = None, connect: bool = True) -> dict:
        """Add an instrument: a *name* of your choice, a *driver* from
        :meth:`list_drivers`, an *address* (``GPIB0::12::INSTR``, ``COM3``, ``TCPIP0::…``,
        ``DAQ::Dev1``, ``SIM::…``, ``NONE``) and driver *options*.  With *connect* it is
        connected at once; a failed connection is reported in the returned description
        (``state``, ``last_error``) and retried on demand."""
        clash = [c.name for c in self.channels.composites() if c.type == "multi" and
                 c.name == name]
        if clash:
            raise ChannelError("%r is the name of a multivariate composite (its variables are "
                               "%s.…): choose another instrument name" % (name, name))
        h = self.instruments.add(name, driver, address, options or {}, connect=connect)
        self._changed("instruments")
        return h.describe()

    def remove_instrument(self, name: str) -> None:
        """Disconnect and remove an instrument (refused while aliases use it)."""
        used = [a.name for a in self.channels.aliases() if a.target.split(".")[0] == name]
        if used:
            raise ChannelError("instrument %s is used by alias(es) %s" % (name, ", ".join(used)))
        self.instruments.remove(name)
        self._changed("instruments")

    def update_instrument(self, name: str, address: Optional[str] = None,
                          options: Optional[dict] = None, driver: Optional[str] = None) -> dict:
        """Change address/options/driver of an instrument (it is reconnected)."""
        old = self.instruments.get(name)
        if self.runner is not None:
            raise ChannelError("cannot modify instruments while a sweep is running")
        addr = old.address if address is None else address
        opts = old.options if options is None else options
        drv = old.driver_name if driver is None else driver
        self.instruments.check_interface(drv, addr)     # before the old one is gone
        self.instruments.remove(name)
        h = self.instruments.add(name, drv, addr, opts, connect=True)
        self._changed("instruments")
        return h.describe()

    def connect_instrument(self, name: str) -> dict:
        """Connect an instrument that is disconnected; returns its description."""
        h = self.instruments.get(name)
        h.connect()
        self._changed("instruments")
        return h.describe()

    def disconnect_instrument(self, name: str) -> dict:
        """Disconnect an instrument (it stays in the setup); returns its description."""
        h = self.instruments.get(name)
        h.disconnect(release=False)
        self._changed("instruments")
        return h.describe()

    def reconnect_instrument(self, name: str) -> dict:
        """Disconnect and connect again (e.g. after a power cycle); returns its
        description."""
        h = self.instruments.get(name)
        h.reconnect()
        self._changed("instruments")
        return h.describe()

    def _instrument_specs_changed(self, name: str) -> None:
        """A driver changed its channel list (mode switch ...): values cached for its
        aliases are stale, and every front end must reload the state."""
        self.channels.forget_instrument(name)
        self._changed("instruments")

    def _instrument_fault(self, name: str, message: str) -> None:
        """A safety fault (a magnet quench ...): stop everything that runs - the
        running sweep or script, the rest of the queue, the user actions - and tell
        every front end."""
        reason = "%s: %s" % (name, message)
        log.error("SAFETY FAULT %s - stopping the queue and the user actions", reason)
        try:
            was = self.queue.abort_all(reason)
        except Exception as exc:
            was = False
            log.error("aborting the queue failed: %s", exc)
        try:
            self.user_actions.abort()
        except Exception:
            pass
        self.emit("instrument_fault", {"instrument": name, "message": message,
                                       "aborted": bool(was)})
        self._changed("instruments")

    def list_instruments(self) -> list:
        """Every instrument with its driver, address, options, state, last error,
        channels and actions."""
        return self.instruments.describe()

    # --------------------------------------------------------------- channels --
    def read_channel(self, name: str) -> Any:
        """Read an alias, a composite channel or ``instrument.channel``."""
        return self.channels.get(name)

    def write_channel(self, name: str, value: Any, ramp: bool = True) -> Any:
        """Set an alias, composite channel or configuration channel.  Alias limits
        apply and, with *ramp*, the value is approached at the alias' ramp rate.
        Measurement channels of instruments cannot be set raw (use an alias or
        ``set_raw`` in a script).  Returns the value that was set."""
        with self.channels.stoppable_ramps("write_channel %s" % name):
            try:
                return self.channels.set(name, value, ramp=ramp)
            except RampStopped:
                v = self.channels.get(name)
                log.info("ramp of %s stopped at %s", name, v)
                return v

    def stop_ramps(self) -> list:
        """Stop the ramps of direct writes (``write_channel``, ``set_many``: the Manual
        panel, clients) that are running now.  Every channel stays where its ramp had
        got to; the write returns that value.  Sweeps, scripts and user actions are
        not affected (use ``abort``).  Returns the channels that were ramping."""
        stopped = self.channels.stop_ramps()
        if stopped:
            log.info("ramps stopped: %s", ", ".join(stopped))
        return stopped

    def ramp_state(self) -> dict:
        """``{"active": [...]}``: the ramps of direct writes now running (channels that
        move, the values asked for, since when).  The event ``ramps`` sends the same
        whenever it changes."""
        return {"active": self.channels.active_ramps()}

    def query_channel(self, ref: str, value: Any) -> Any:
        """Write + read back in one transaction (limits of aliases on *ref* apply)."""
        return self.channels.query_raw(ref, value)

    def raw_query(self, instrument: str, command: str) -> str:
        """Send a command to an instrument and return its reply (admin only remotely)."""
        return self.instruments.get(instrument).raw_query(command)

    def raw_write(self, instrument: str, command: str) -> None:
        """Send a command to an instrument without reading a reply (admin only remotely)."""
        self.instruments.get(instrument).raw_write(command)

    def run_action(self, ref: str) -> None:
        """Execute an instrument action, e.g. ``run_action(ref="dmm.reset")``."""
        if not isinstance(ref, str) or "." not in ref:
            raise ChannelError("action reference must be 'instrument.action'")
        inst, name = ref.split(".", 1)
        h = self.instruments.get(inst)
        try:
            h.run_action(name)
        finally:
            self.channels.forget_instrument(inst)

    def channel_info(self, name: str) -> dict:
        """Kind, unit, limits, ramp rate, shape and writability of a channel name."""
        return self.channels.info(name)

    def define_alias(self, replace: bool = False, **alias) -> dict:
        """Define an alias: ``name``, ``target`` (``instrument.channel``) and optional
        ``min``, ``max``, ``ramp_rate`` (units/s), ``ramp_step``, ``scale``, ``offset``,
        ``unit``, ``allowed``, ``read_only``, ``description``.  With *replace* an existing
        alias of that name is changed.  Returns the stored definition."""
        a = Alias.from_dict(alias)
        self.instruments.resolve(a.target)  # must exist now
        self.channels.add_alias(a, replace=replace)
        self._changed("channels")
        return a.to_dict()

    def remove_alias(self, name: str) -> None:
        """Remove an alias (refused while a composite channel uses it)."""
        self.monitor.remove(name)
        self.channels.remove_alias(name)
        self._changed("channels")

    @staticmethod
    def _parameter(name, value=None, formula="", definition=None, unit="", description=""):
        from .parameters import Parameter
        if definition is not None and str(definition).strip() != "":
            text = str(definition).strip()
            try:                                  # a number: a value, else a formula
                value, formula = float(text), ""
            except ValueError:
                value, formula = None, text
        return Parameter(name=name, value=value, formula=formula or "", unit=unit,
                         description=description)

    def define_parameter(self, name: str, value=None, formula: str = "", unit: str = "",
                         description: str = "", replace: bool = False,
                         definition: Optional[str] = None) -> dict:
        """Define a parameter: a named constant of the setup, with a ``value`` or a
        ``formula`` of other parameters (no live channels, no circles).  Or give
        *definition*, the text the user typed: a number becomes the value, anything
        else the formula.  Parameters read like channels and may be used in
        composite formulas and actions.  Returns its definition with the present value
        (``current``) and warnings."""
        p = self._parameter(name, value, formula, definition, unit, description)
        warnings = self.channels.add_parameter(p, replace=replace)
        self._changed("channels")
        d = next(x for x in self.channels.describe_parameters() if x["name"] == p.name)
        return dict(d, warnings=warnings)

    def check_parameter(self, name: str, value=None, formula: str = "", unit: str = "",
                        description: str = "", replace: bool = False,
                        definition: Optional[str] = None) -> dict:
        """Check a parameter definition without defining it (the editors' Check
        button): ``{name, value | formula, current, warnings}``; raises the error
        :meth:`define_parameter` would."""
        p = self._parameter(name, value, formula, definition, unit, description)
        v, warnings = self.channels.check_parameter(p, replace=replace)
        return dict(p.to_dict(), current=v, warnings=warnings)

    def check_code(self, code: str, what: str = "code") -> dict:
        """Check Python code (a script, a user action, a point action) without
        running it: ``{ok, errors, warnings}`` - syntax errors, and quoted channels
        that do not exist (``get('nope')``)."""
        from .scripting import check_code
        return check_code(code, self.channels, what)

    def check_user_action(self, name: str, code: str, description: str = "",
                          confirm: bool = False, replace: bool = False) -> dict:
        """Check a user action without defining it: its name and code, like
        :meth:`check_code` (``ok`` is False for a bad name too)."""
        r = self.check_code(code, "action %s" % name)
        try:
            UserAction(name=name, code=code, description=description, confirm=confirm)
        except Exception as exc:
            r["ok"] = False
            if not r["errors"]:
                r["errors"].append(str(exc))
        if not replace and name in {a["name"] for a in self.user_actions.describe()}:
            r["ok"] = False
            r["errors"].append("an action %r already exists" % name)
        return r

    def set_parameter(self, name: str, value: float) -> float:
        """Set the value of a value parameter (the Parameters and Manual panels).
        Parameters computed from a formula follow; nothing is written to any
        instrument.  Refused for a formula parameter, or if a mapping that uses the
        parameter would no longer be invertible.  Returns the value."""
        v = self.channels.set_parameter(name, value)
        self._changed("channels")
        return v

    def remove_parameter(self, name: str) -> None:
        """Remove a parameter (refused while a parameter formula or a composite uses
        it)."""
        self.channels.remove_parameter(name)
        self.monitor.remove(name)
        self._changed("channels")

    def list_parameters(self) -> list:
        """Every parameter: ``{name, value | formula, unit, description, current,
        used_by}``."""
        return self.channels.describe_parameters()

    def define_composite(self, replace: bool = False, **comp) -> dict:
        """Define a composite channel (``type`` multi / read / map, see
        backend/composites.py).  Returns its definition, the channels it provides and
        warnings (e.g. a variable that drives nothing)."""
        c, notes = composite_from_dict(comp)
        old = next((o for o in self.channels.composites() if o.name == c.name), None)
        warnings = notes + self.channels.add_composite(c, replace=replace)
        if old is not None:
            for ch in set(old.channels()) - set(c.channels()):
                self.monitor.remove(ch)
        for w in warnings:
            log.warning(w)
        self._changed("channels")
        return dict(c.to_dict(), channels=c.channels(), warnings=warnings)

    def check_composite(self, replace: bool = True, **comp) -> dict:
        """Check a composite definition without defining it (the editors' Check
        button): the mapping inverse check, references, dependency loops."""
        c, notes = composite_from_dict(comp)
        cand = {o.name: o for o in self.channels.composites()}
        if c.name in cand and not replace:
            raise ChannelError("composite %r already exists" % c.name)
        cand[c.name] = c
        warnings = notes + self.channels._validate(cand, c)
        return dict(c.to_dict(), channels=c.channels(), warnings=warnings)

    def remove_composite(self, name: str) -> None:
        """Remove a composite channel (refused while another composite uses one of its
        channels)."""
        c = next((o for o in self.channels.composites() if o.name == name), None)
        self.channels.remove_composite(name)
        for ch in (c.channels() if c is not None else [name]):
            self.monitor.remove(ch)
        self._changed("channels")

    def set_many(self, values: dict, ramp: bool = True) -> dict:
        """Set several channels together (e.g. all variables of a composite)."""
        with self.channels.stoppable_ramps("set_many %s" % ", ".join(values)):
            try:
                plan = self.channels.set_many(values, ramp=bool(ramp))
            except RampStopped:
                plan = {}
                for n in values:
                    try:
                        plan[n] = self.channels.get(n)
                    except Exception:
                        plan[n] = None
                log.info("ramp of %s stopped", ", ".join(values))
        self._changed("channels")
        return {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in plan.items()}

    def list_channels(self) -> dict:
        """Aliases, composites, the channels composites define (``virtual``), with
        their last values, and the parameters."""
        return self.channels.describe()

    # ---------------------------------------------------------------- monitor --
    def monitor_add(self, channel: str, interval: float = 1.0) -> list:
        """Poll *channel* every *interval* seconds and push its values to the front
        ends (``monitor`` events).  Returns the monitor list."""
        if not self.channels.exists(channel):
            raise ChannelError("unknown channel %r" % channel)
        self.monitor.add(channel, float(interval))
        self._changed("monitor")
        return self.monitor.config()

    def monitor_remove(self, channel: str) -> list:
        """Stop monitoring *channel*; returns the monitor list."""
        self.monitor.remove(channel)
        self._changed("monitor")
        return self.monitor.config()

    def monitor_list(self) -> list:
        """The monitored channels with interval, last value, last error and time."""
        return self.monitor.status()

    def monitor_history(self, channel: Optional[str] = None) -> dict:
        """Recent values of one monitored channel (or of all): ``{channel: [[t, value], …]}``."""
        return self.monitor.history(channel)

    # ----------------------------------------------------------------- sweeps --
    def validate_sweep(self, definition: dict) -> dict:
        """Check a sweep definition against the present channels without running it:
        all setpoints are computed and limit-checked.  Returns ``{ok, warnings, shape,
        total_points, min_duration, definition}`` (the normalised definition) or raises
        with the first problem."""
        d = normalize_definition(definition)
        warnings = validate_against(d, self.channels)
        shape = [dim["points"] for dim in d["dimensions"]]
        total = 1
        for n in shape:
            total *= n
        est = 0.0
        inner = 1
        for dim in d["dimensions"]:
            inner *= dim["points"]
            est += inner * (dim["pre_delay"] + dim["post_delay"])
        return {"ok": True, "warnings": warnings, "shape": shape, "total_points": total,
                "min_duration": est, "definition": d}

    def _execute_sweep_item(self, definition: dict, item: dict) -> dict:
        d = normalize_definition(definition)
        # re-check limits right before running (abortable: a big map takes a moment)
        validate_against(d, self.channels, abort_check=self.control.check)
        with self._run_lock:
            run, path = self.store.new_path(d["name"])
            runner = SweepRunner(d, self.channels, self.control, path, run, self.emit,
                                 script_runner=self.scripts.run,
                                 fsync_interval=float(self.settings.get("fsync_interval", 0.5)),
                                 extra_header={"queue_item": item.get("id")})
            self.runner = runner
        item["run"] = run
        item["path"] = str(path)
        try:
            status = runner.run()
        finally:
            with self._run_lock:
                self.runner = None
                self.last_dataset = runner.dataset
        self._post_run(runner)
        return {"status": status, "run": run, "path": str(path), "error": runner.error}

    def _script_run_sweep(self, definition: dict):
        """``run_sweep()`` from a script: run nested in the script's thread; returns the
        run's :class:`DataSet` (``.status``, ``.data``, ``.setpoints``, ``.path`` …), so
        the script can decide what to measure next from what it just measured."""
        if self.runner is not None:
            raise EasyMeasureError("a sweep is already running")
        res = self._execute_sweep_item(definition, {"id": None})
        ds = self.last_dataset
        if ds is None:                     # the data file could not even be created
            raise EasyMeasureError("sweep %s: %s" % (res["status"], res.get("error")))
        ds.path = res["path"]
        return ds

    def _post_run(self, runner: SweepRunner) -> None:
        ds = runner.dataset
        if ds is None:
            return
        do_h5 = bool(self.settings.get("export_hdf5"))
        ppt = self.settings.get("ppt_path") or ""
        do_ppt = bool(ppt) and bool(self.settings.get("auto_ppt")) and runner.d.get("ppt", True)

        def work():
            if do_h5:
                try:
                    p = export_hdf5(ds, Path(runner.path).with_suffix(".h5"))
                    log.info("run %d exported to %s", runner.run_number, p)
                except Exception as exc:
                    log.warning("HDF5 export of run %d failed: %s", runner.run_number, exc)
            if do_ppt:
                try:
                    self._ppt(ds, ppt)
                except Exception as exc:
                    log.error("PPT generation for run %d failed: %s", runner.run_number, exc)

        t = threading.Thread(target=work, name="post-run-%d" % runner.run_number, daemon=True)
        t.start()
        self._post_threads = [x for x in self._post_threads if x.is_alive()] + [t]

    def _ppt(self, ds: DataSet, ppt_path: str) -> str:
        from ..report.pptx_report import append_run_slide
        ds.header.setdefault("file", str(ds.header.get("file") or ""))
        out = append_run_slide(ppt_path, ds, cmap=self.settings.get("ppt_colormap", "viridis"))
        log.info("slide for run %s appended to %s", ds.header.get("run"), out)
        self.emit("ppt", {"run": ds.header.get("run"), "path": str(out)})
        return str(out)

    def make_ppt(self, path: Optional[str] = None, ppt_path: Optional[str] = None) -> str:
        """Append a slide for a run file (or the current/last run) immediately."""
        ppt_path = ppt_path or self.settings.get("ppt_path")
        if not ppt_path:
            raise ChannelError("no PowerPoint file configured")
        if path:
            ds = DataSet.load(path)
        else:
            with self._run_lock:
                ds = self.runner.dataset if self.runner is not None else self.last_dataset
            if ds is None:
                raise ChannelError("no run available")
            if ds.header.get("file") is None and self.runner is not None:
                ds.header["file"] = str(self.runner.path)
        return self._ppt(ds, ppt_path)

    def current_run(self) -> Optional[dict]:
        """The running sweep: ``{run, name, path, status, progress, definition,
        paused}``, or None."""
        with self._run_lock:
            r = self.runner
            if r is None:
                return None
            return {"run": r.run_number, "name": r.d["name"], "path": str(r.path),
                    "status": r.status, "progress": r.progress(), "definition": r.d,
                    "paused": self.control.paused}

    def run_snapshot(self, last: bool = True) -> Optional[dict]:
        """Full dataset of the running sweep (or the last one when idle)."""
        with self._run_lock:
            ds = self.runner.dataset if self.runner is not None else (self.last_dataset if last else None)
            running = self.runner is not None
        if ds is None:
            return None
        snap = ds.snapshot()
        snap["running"] = running
        return snap

    def list_runs(self, limit: int = 200) -> list:
        """The newest data files in the data folder (path, name, size, modification time)."""
        return self.store.list_runs(int(limit))

    def load_run(self, path: str) -> dict:
        """A stored run (complete or partial ``.emj`` file) as a snapshot: header,
        setpoints, data and status."""
        return DataSet.load(path).snapshot()

    def read_run_header(self, path: str) -> dict:
        """Only the header of a stored run (its definition and metadata)."""
        return read_header(path)

    # ------------------------------------------------------------------ queue --
    def queue_add(self, item: dict, start: bool = False, position: Optional[int] = None) -> int:
        """Add ``{kind: "sweep", definition}`` or ``{kind: "script", code, name}`` to the
        queue (at *position*, default the end); with *start* the queue starts.  Returns
        the item id."""
        if item.get("kind") == "sweep":
            validate_against(normalize_definition(item.get("definition") or {}), self.channels)
        return self.queue.add(item, start=start, position=position)

    def queue_remove(self, item_id: int) -> None:
        """Remove a waiting queue item."""
        self.queue.remove(int(item_id))

    def queue_move(self, item_id: int, position: int) -> None:
        """Move a waiting queue item to *position*."""
        self.queue.move(int(item_id), int(position))

    def queue_clear(self) -> None:
        """Remove every waiting queue item."""
        self.queue.clear()

    def queue_start(self) -> bool:
        """Start processing the queue; False (and the queue stays idle) if it is empty."""
        return self.queue.start()

    def queue_stop(self) -> None:
        """Stop after the current item (the rest stays queued)."""
        self.queue.stop()

    def queue_state(self) -> dict:
        """``{state, can, processing, stop_on_error, current, queued, history}``; ``state`` is idle /
        running / paused / aborting and ``can`` lists the controls that apply now."""
        return self.queue.describe()

    def queue_options(self, stop_on_error: Optional[bool] = None) -> dict:
        """Set queue options (``stop_on_error``: stop the queue when an item fails);
        returns the queue state."""
        if stop_on_error is not None:
            self.queue.stop_on_error = bool(stop_on_error)
        return self.queue.describe()

    def pause(self) -> None:
        """Pause the running item at the next point (sweeps) or line (scripts)."""
        self.queue.pause()

    def resume(self) -> None:
        """Resume a paused item."""
        self.queue.resume()

    def abort(self) -> None:
        """Abort the running item; the sweep's post phase still runs (by default)."""
        self.queue.abort()

    def abort_all(self) -> None:
        """Abort the running item, clear the queue and abort running user actions."""
        self.queue.abort_all()
        self.user_actions.abort()

    # ----------------------------------------------------------- user actions --
    def _action_runner(self, control) -> ScriptRunner:
        r = ScriptRunner(self.channels, self.instruments, control)   # no run_sweep: queue it
        r.state = self.scripts.state            # the same ``state`` dict as queued scripts
        return r

    def list_user_actions(self) -> list:
        """The user actions with their code, ``running`` flag and ``last`` run result."""
        return self.user_actions.describe()

    def define_user_action(self, name: str, code: str, description: str = "",
                           confirm: bool = False, replace: bool = False,
                           rename_from: Optional[str] = None) -> dict:
        """Define (or with *replace* change; with *rename_from* rename) a user action:
        a named Python snippet using the script API (see backend/user_actions.py)."""
        a = UserAction(name, code, description, confirm)
        self.user_actions.define(a, replace=replace, rename_from=rename_from)
        self._changed("actions")
        return a.to_dict()

    def remove_user_action(self, name: str) -> None:
        """Delete a user action (refused while it runs)."""
        self.user_actions.remove(name)
        self._changed("actions")

    def move_user_action(self, name: str, position: int) -> None:
        """Move a user action to *position* (0 = first) in the list."""
        self.user_actions.move(name, position)
        self._changed("actions")

    def run_user_action(self, name: str) -> dict:
        """Run a user action now (not through the queue) and wait for its end:
        {name, status: finished / aborted / error, error, elapsed}."""
        return self.user_actions.run(name)

    def abort_user_action(self, name: Optional[str] = None) -> list:
        """Abort the running user action *name* (or every running one); returns the
        names asked to stop."""
        return self.user_actions.abort(name)

    def run_script_now(self, code: str, name: str = "script") -> int:
        """Queue a script at the front and start the queue."""
        return self.queue.add({"kind": "script", "code": code, "name": name}, start=True,
                              position=0)

    # ------------------------------------------------------------------ setup --
    def get_setup(self) -> dict:
        """The present setup as a dict (see :meth:`save_setup`)."""
        return setup_io.capture(self.instruments, self.channels, self.monitor, self.user_actions)

    def save_setup(self, path: Optional[str] = None) -> dict:
        """The setup (instruments, aliases, composites, monitor list, user actions) as a
        dict; with *path* also written there as JSON."""
        s = self.get_setup()
        if path:
            setup_io.save(path, s)
        return s

    def load_setup(self, setup: Optional[dict] = None, path: Optional[str] = None,
                   replace: bool = True, connect: bool = True) -> list:
        """Load a setup from a dict or a JSON file.  *replace* (default) removes what is
        there first; *connect* connects the instruments.  Returns the problems found
        (an empty list when everything loaded)."""
        if self.runner is not None:
            raise ChannelError("cannot load a setup while a sweep is running")
        if setup is None:
            if not path:
                raise ChannelError("give a setup dict or a path")
            setup = setup_io.read(path)
        problems = setup_io.apply(setup, self.instruments, self.channels, self.monitor,
                                  replace=replace, connect=connect,
                                  user_actions=self.user_actions)
        self._changed("setup")
        return problems

    def _changed(self, what: str) -> None:
        if self.settings.get("autosave_setup"):
            try:
                setup_io.atomic_write_json(self.home / "autosave_setup.json", self.get_setup())
            except Exception as exc:
                log.warning("setup autosave failed: %s", exc)
        self.emit("changed", {"what": what})

    def restore_autosave(self) -> list:
        p = self.home / "autosave_setup.json"
        if not p.exists():
            return []
        log.info("restoring autosaved setup %s", p)
        try:
            return self.load_setup(path=str(p))
        except Exception as exc:
            log.error("cannot restore autosave: %s", exc)
            return [str(exc)]

    # --------------------------------------------------------------- shutdown --
    def set_shutdown_callback(self, cb: Callable[[], None]) -> None:
        self._shutdown_cb = cb

    # ------------------------------------------------------------------- tray --
    def tray_running(self) -> bool:
        return self._tray is not None and self._tray.poll() is None

    def start_tray(self) -> dict:
        """Start the system-tray helper process (no-op if it is already running)."""
        if self.tray_running():
            return {"running": True, "pid": self._tray.pid}
        from ..tray import spawn
        log_path = self.home / "logs" / "tray.log"
        proc, reason = spawn(log_path=log_path)
        self._tray = proc
        if proc is None:
            log.info("tray icon not started: %s", reason)
            return {"running": False, "reason": reason}
        log.info("tray helper started (pid %d, log %s)", proc.pid, log_path)

        def watch(p=proc):
            code = p.wait()
            if not getattr(self, "_closing", False):
                log.warning("tray helper exited with code %s (see %s)", code, log_path)
        threading.Thread(target=watch, name="tray-watch", daemon=True).start()
        return {"running": True, "pid": proc.pid}

    # ----------------------------------------------------------------- remote --
    def _remote_proc_alive(self) -> bool:
        return self._remote is not None and self._remote.poll() is None

    def remote_status(self) -> dict:
        """State of the internet gateway (see :mod:`easymeasure.remote`)."""
        import json
        from ..remote import config as rconfig
        from ..remote.users import UserStore
        st = {}
        try:
            st = json.loads(rconfig.status_path(self.home).read_text())
        except Exception:
            pass
        # the status file must come from *this* gateway process: after a crash the old
        # process's file is still fresh while the restarted one is not listening yet
        # (the pid cannot tell: a venv's python.exe on Windows is a launcher)
        t = float(st.get("t", 0))
        running = self._remote_proc_alive() and t >= getattr(self, "_remote_spawned", 0) and \
            time.time() - t < 10
        cfg = rconfig.load(self.home)
        users = UserStore(rconfig.users_path(self.home)).list()
        port = st.get("port") or cfg["port"]
        urls = []
        try:
            import socket
            hosts = [socket.getfqdn() or socket.gethostname()]
            hosts += sorted({i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None)
                             if ":" not in i[4][0] and not i[4][0].startswith("127.")})
            if cfg["host"] in ("127.0.0.1", "localhost"):
                hosts = ["127.0.0.1"]
            urls = ["https://%s:%s" % (h, port) for h in dict.fromkeys(hosts) if h]
        except Exception:
            pass
        if cfg.get("acme") and cfg.get("certfile"):
            d = cfg["acme"]["domain"]
            urls = ["https://%s%s" % (d, "" if int(port) == 443 else ":%s" % port)] + \
                [u for u in urls if d not in u]
        return {"enabled": self._remote_want, "running": running, "urls": urls,
                "pid": self._remote.pid if self._remote_proc_alive() else None,
                "port": port, "host": cfg["host"],
                "fingerprint": st.get("fingerprint"), "self_signed": st.get("self_signed"),
                "cert": self._remote_cert_info(cfg, st if running else {}),
                "connections": st.get("connections", []) if running else [],
                "users": users, "config": cfg, "log": str(self.home / "logs" / "remote.log")}

    def _remote_cert_info(self, cfg: dict, st: dict) -> dict:
        """Which certificate the gateway uses (or will use) and the Let's Encrypt state."""
        from ..remote import acme
        from ..remote import config as rconfig
        from ..remote.tlsutil import cert_info
        mode = "letsencrypt" if cfg.get("acme") and cfg.get("certfile") else \
            ("files" if cfg.get("certfile") else "self-signed")
        path = cfg.get("certfile") or str(rconfig.tls_dir(self.home) / "cert.pem")
        out = {"mode": mode, "certfile": cfg.get("certfile"), "keyfile": cfg.get("keyfile"),
               "acme": cfg.get("acme"), "in_use": st.get("cert")}
        try:
            info = cert_info(Path(path))
            out.update(names=info["names"], expires=info["expires"], issuer=info["issuer"])
        except Exception as exc:
            out["error"] = "" if mode == "self-signed" else str(exc)
        a = acme.read_status(self.home)
        proc = getattr(self, "_acme_proc", None)
        if a.get("state") == "running" and not (proc is not None and proc.poll() is None) and \
                time.time() - float(a.get("started", 0)) > 600:
            a["state"] = "error"                  # the process died without a word
        out["request"] = a
        return out

    def remote_cert_request(self, domain: str, email: str = "", agree_tos: bool = False,
                            staging: bool = False, http_port: int = 80) -> dict:
        """Get a Let's Encrypt certificate for *domain* in a separate process (progress
        in ``remote_status()["cert"]["request"]``); the gateway switches to it when done."""
        import subprocess
        import sys
        from ..remote import acme
        if not agree_tos:
            raise ChannelError("the Let's Encrypt Subscriber Agreement must be agreed to")
        try:
            domain = acme.check_domain(domain)
        except acme.AcmeError as exc:
            raise ChannelError(str(exc)) from None
        proc = getattr(self, "_acme_proc", None)
        if proc is not None and proc.poll() is None:
            raise ChannelError("a certificate request is already running")
        args = [sys.executable, "-m", "easymeasure.remote", "--home", str(self.home),
                "letsencrypt", domain, "--agree-tos", "--http-port", str(int(http_port))]
        if email:
            args += ["--email", str(email)]
        if staging:
            args.append("--staging")
        args += getattr(self, "_acme_extra_args", [])          # tests: another ACME server
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + \
            env.get("PYTHONPATH", "")
        (self.home / "logs").mkdir(parents=True, exist_ok=True)
        acme._write_status(self.home, {"state": "running", "domain": domain, "staging": staging,
                                       "started": time.time(), "log": [],
                                       "message": "starting…"})
        out = open(self.home / "logs" / "acme.out", "ab")
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env)
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self._acme_proc = subprocess.Popen(args, **kwargs)
        out.close()
        log.info("Let's Encrypt certificate requested for %s%s", domain,
                 " (staging)" if staging else "")
        return self.remote_status()

    def remote_cert_use(self, mode: str, certfile: Optional[str] = None,
                        keyfile: Optional[str] = None) -> dict:
        """Use the self-signed certificate (``mode="self-signed"``) or certificate files
        (``"files"``, PEM chain + key); a running gateway switches within seconds."""
        from ..remote import config as rconfig
        from ..remote.tlsutil import server_context
        cfg = rconfig.load(self.home)
        if mode == "self-signed":
            cfg.update(certfile=None, keyfile=None, acme=None)
        elif mode == "files":
            if not certfile or not keyfile:
                raise ChannelError("give the certificate (chain) file and its key file")
            try:
                server_context(Path(certfile), Path(keyfile))
            except Exception as exc:
                raise ChannelError("these files cannot be used: %s" % exc) from None
            cfg.update(certfile=str(certfile), keyfile=str(keyfile), acme=None)
        else:
            raise ChannelError("mode is self-signed or files")
        rconfig.save(cfg, self.home)
        log.info("remote access certificate: %s", certfile if mode == "files" else mode)
        return self.remote_status()

    def remote_start(self, port: Optional[int] = None, host: Optional[str] = None,
                     persist: bool = True) -> dict:
        """Start the internet gateway in its own process and keep it running."""
        if port:
            self._remote_args["port"] = int(port)
            from ..remote import config as rconfig
            cfg = rconfig.load(self.home)
            if int(cfg.get("port") or 0) != int(port):
                cfg["port"] = int(port)                 # remembered for the next start
                rconfig.save(cfg, self.home)
        if host:
            self._remote_args["host"] = str(host)
        self._remote_want = True
        if persist and not self.settings.get("remote_access"):
            self.set_settings(remote_access=True)
        if not self._remote_proc_alive():
            self._spawn_remote()
        return self.remote_status()

    def remote_set_user(self, name: str, password: Optional[str] = None,
                        role: Optional[str] = None) -> list:
        """Add a remote user or change password / role (local GUI only)."""
        from ..remote import config as rconfig
        from ..remote.users import UserStore
        users = UserStore(rconfig.users_path(self.home))
        try:
            users.set_user(name, password or None, role or None)
        except ValueError as exc:
            raise ChannelError(str(exc)) from None
        log.info("remote user %s %s", name, "updated" if password is None else "password set")
        return users.list()

    def remote_remove_user(self, name: str) -> list:
        """Delete a remote account (local GUI only); returns the remaining accounts."""
        from ..remote import config as rconfig
        from ..remote.users import UserStore
        users = UserStore(rconfig.users_path(self.home))
        users.remove(name)
        log.info("remote user %s removed", name)
        return users.list()

    def remote_stop(self, persist: bool = True) -> dict:
        """Stop the internet gateway; with *persist* remote access stays off after a
        restart.  Returns :meth:`remote_status`."""
        self._remote_want = False
        if persist and self.settings.get("remote_access"):
            self.set_settings(remote_access=False)
        proc, self._remote = self._remote, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except Exception:
                proc.kill()
            log.info("remote gateway stopped")
        return self.remote_status()

    def _spawn_remote(self) -> None:
        import subprocess
        import sys
        args = [sys.executable, "-m", "easymeasure.remote", "--home", str(self.home), "serve",
                "--quiet"]
        if self._remote_args.get("port"):
            args += ["--port", str(self._remote_args["port"])]
        if self._remote_args.get("host"):
            args += ["--host", self._remote_args["host"]]
        env = dict(os.environ)
        pkg_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
        # glibc: give large (message-sized) buffers back to the OS when they are freed
        # instead of keeping them in the heap / per-thread arenas
        env.setdefault("MALLOC_MMAP_THRESHOLD_", "262144")
        env.setdefault("MALLOC_TRIM_THRESHOLD_", "1048576")
        env.setdefault("MALLOC_ARENA_MAX", "2")
        (self.home / "logs").mkdir(parents=True, exist_ok=True)
        out = open(self.home / "logs" / "remote-gateway.out", "ab")
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env)
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self._remote_spawned = time.time()
        proc = subprocess.Popen(args, **kwargs)
        out.close()
        self._remote = proc
        log.info("remote gateway started (pid %d)", proc.pid)

        def watch(p=proc):
            delay = 1.0
            while True:
                t0 = time.time()
                code = p.wait()
                if getattr(self, "_closing", False) or not self._remote_want or self._remote is not p:
                    return
                if time.time() - t0 > 60:
                    delay = 1.0
                log.warning("remote gateway exited (code %s); restarting in %.0f s (see %s)",
                            code, delay, self.home / "logs" / "remote-gateway.out")
                time.sleep(delay)
                delay = min(60.0, delay * 2)
                if getattr(self, "_closing", False) or not self._remote_want:
                    return
                try:
                    self._spawn_remote()
                except Exception as exc:
                    log.error("cannot restart the remote gateway: %s", exc)
                return
        threading.Thread(target=watch, name="remote-watch", daemon=True).start()

    def shutdown(self, force: bool = False) -> str:
        """Shut the backend down (refused while a sweep runs, unless *force*)."""
        if self.runner is not None and not force:
            raise ChannelError("a sweep is running; abort it first or use force")
        log.warning("backend shutdown requested")
        cb = self._shutdown_cb
        if cb is not None:
            threading.Timer(0.2, cb).start()
        return "shutting down"

    def close(self) -> None:
        """Stop everything (used by the server on exit and by tests)."""
        self._closing = True
        self._remote_want = False
        remote, self._remote = self._remote, None
        if remote is not None and remote.poll() is None:
            try:
                remote.terminate()
            except Exception:
                pass
        tray, self._tray = self._tray, None
        if tray is not None and tray.poll() is None:
            try:
                tray.terminate()
            except Exception:
                pass
        try:
            self.queue.abort_all()
            self.queue.shutdown()
        except Exception:
            pass
        for t in self._post_threads:
            t.join(10.0)
        try:
            self.monitor.stop()
        except Exception:
            pass
        try:
            self.instruments.shutdown()
        except Exception:
            pass
        try:
            self.resources.close_all()
        except Exception:
            pass
        try:
            self.ring.remove_listener(self._on_log)
        except Exception:
            pass
        log.info("backend closed")
