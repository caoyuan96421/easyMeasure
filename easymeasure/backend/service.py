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

from ..common import paths
from ..common.errors import ChannelError, DriverError, EasyMeasureError, SweepDefinitionError
from . import setup_io
from .channels import Alias, ChannelManager, Composite
from .registry import DriverRegistry
from .instrument import InstrumentManager
from .logsys import get_logger, ring, setup_logging
from .monitor import Monitor
from .resources import ResourceManager, normalize_address
from .runqueue import RunQueue
from .scripting import ScriptRunner
from .storage import DataSet, DataStore, export_hdf5, read_header
from .sweep import RunControl, SweepRunner, compute_setpoints, normalize_definition, validate_against

log = get_logger("backend")

DEFAULT_SETTINGS = {
    "data_dir": None,            # default: ~/.easymeasure/data
    "ppt_path": "",              # append slides here when a run ends ("" = off)
    "auto_ppt": True,
    "export_hdf5": True,         # write an .h5 copy next to the journal at the end
    "fsync_interval": 0.5,
    "driver_paths": [],
    "autosave_setup": True,
    "ppt_colormap": "viridis",
    "remote_access": False,      # start the internet gateway with the backend
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
        "define_alias", "remove_alias", "define_composite", "remove_composite", "list_channels",
        "monitor_add", "monitor_remove", "monitor_list", "monitor_history",
        "validate_sweep", "queue_add", "queue_remove", "queue_move", "queue_clear",
        "queue_start", "queue_stop", "queue_state", "queue_options", "pause", "resume", "abort",
        "abort_all", "current_run", "run_snapshot", "list_runs", "load_run", "read_run_header",
        "save_setup", "load_setup", "get_setup", "make_ppt", "recent_logs", "run_script_now",
        "shutdown", "start_tray", "run_action", "remote_status", "remote_start", "remote_stop",
        "remote_set_user", "remote_remove_user",
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
        self.control = RunControl()
        self.monitor = Monitor(self.channels)
        self.monitor.listeners.append(lambda m: self.emit("monitor", m))
        self.scripts = ScriptRunner(self.channels, self.instruments, self.control,
                                    run_sweep=self._script_run_sweep)
        self.store = DataStore(Path(self.settings["data_dir"]))
        self.runner: Optional[SweepRunner] = None
        self.last_dataset: Optional[DataSet] = None
        self._run_lock = threading.Lock()
        self.queue = RunQueue(self._execute_sweep_item, self.scripts.run, self.control, self.emit)
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
        return {"pong": time.time(), "uptime": time.time() - self.started}

    def get_state(self) -> dict:
        return {
            "uptime": time.time() - self.started,
            "settings": self.get_settings(),
            "instruments": self.instruments.describe(),
            "channels": self.channels.describe(),
            "monitor": self.monitor.status(),
            "queue": self.queue.describe(),
            "current_run": self.current_run(),
            "resources": self.resources.list(),
        }

    def get_settings(self) -> dict:
        return copy.deepcopy(self.settings)

    def set_settings(self, **kwargs) -> dict:
        unknown = [k for k in kwargs if k not in DEFAULT_SETTINGS]
        if unknown:
            raise ChannelError("unknown setting(s): %s" % ", ".join(unknown))
        self.settings.update(kwargs)
        if "data_dir" in kwargs and kwargs["data_dir"]:
            self.store = DataStore(Path(kwargs["data_dir"]))
        try:
            setup_io.atomic_write_json(self._settings_file, self.settings)
        except Exception as exc:
            log.warning("cannot persist settings: %s", exc)
        self.emit("settings", self.get_settings())
        return self.get_settings()

    def recent_logs(self, n: int = 500, min_level: int = 0) -> list:
        return self.ring.recent(int(n), int(min_level))

    # ---------------------------------------------------------------- drivers --
    def list_drivers(self) -> dict:
        return self.drivers.describe()

    def _driver_paths(self) -> list:
        return [self.user_driver_dir] + [Path(p) for p in self.settings.get("driver_paths") or []]

    def rescan_drivers(self) -> dict:
        self.drivers.extra_paths = self._driver_paths()
        self.drivers.scan()
        return self.drivers.describe()

    # ---------------------------------------------------------- driver wizard --
    def _builtin_driver_names(self) -> set:
        import pkgutil
        from .. import drivers as pkg
        return {m.name for m in pkgutil.iter_modules(pkg.__path__)}

    def wizard_templates(self) -> list:
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
        return self.resources.list()

    # ------------------------------------------------------------ instruments --
    def add_instrument(self, name: str, driver: str, address: str = "NONE",
                       options: Optional[dict] = None, connect: bool = True) -> dict:
        h = self.instruments.add(name, driver, address, options or {}, connect=connect)
        self._changed("instruments")
        return h.describe()

    def remove_instrument(self, name: str) -> None:
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
        h = self.instruments.get(name)
        h.connect()
        self._changed("instruments")
        return h.describe()

    def disconnect_instrument(self, name: str) -> dict:
        h = self.instruments.get(name)
        h.disconnect(release=False)
        self._changed("instruments")
        return h.describe()

    def reconnect_instrument(self, name: str) -> dict:
        h = self.instruments.get(name)
        h.reconnect()
        self._changed("instruments")
        return h.describe()

    def _instrument_specs_changed(self, name: str) -> None:
        """A driver changed its channel list (mode switch ...): values cached for its
        aliases are stale, and every front end must reload the state."""
        self.channels.forget_instrument(name)
        self._changed("instruments")

    def list_instruments(self) -> list:
        return self.instruments.describe()

    # --------------------------------------------------------------- channels --
    def read_channel(self, name: str) -> Any:
        return self.channels.get(name)

    def write_channel(self, name: str, value: Any, ramp: bool = True) -> Any:
        return self.channels.set(name, value, ramp=ramp)

    def query_channel(self, ref: str, value: Any) -> Any:
        """Write + read back in one transaction (limits of aliases on *ref* apply)."""
        return self.channels.query_raw(ref, value)

    def raw_query(self, instrument: str, command: str) -> str:
        return self.instruments.get(instrument).raw_query(command)

    def raw_write(self, instrument: str, command: str) -> None:
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
        return self.channels.info(name)

    def define_alias(self, replace: bool = False, **alias) -> dict:
        a = Alias.from_dict(alias)
        self.instruments.resolve(a.target)  # must exist now
        self.channels.add_alias(a, replace=replace)
        self._changed("channels")
        return a.to_dict()

    def remove_alias(self, name: str) -> None:
        self.monitor.remove(name)
        self.channels.remove_alias(name)
        self._changed("channels")

    def define_composite(self, replace: bool = False, **comp) -> dict:
        c = Composite.from_dict(comp)
        self.channels.add_composite(c, replace=replace)
        self._changed("channels")
        return c.to_dict()

    def remove_composite(self, name: str) -> None:
        self.monitor.remove(name)
        self.channels.remove_composite(name)
        self._changed("channels")

    def list_channels(self) -> dict:
        return self.channels.describe()

    # ---------------------------------------------------------------- monitor --
    def monitor_add(self, channel: str, interval: float = 1.0) -> list:
        if not self.channels.exists(channel):
            raise ChannelError("unknown channel %r" % channel)
        self.monitor.add(channel, float(interval))
        self._changed("monitor")
        return self.monitor.config()

    def monitor_remove(self, channel: str) -> list:
        self.monitor.remove(channel)
        self._changed("monitor")
        return self.monitor.config()

    def monitor_list(self) -> list:
        return self.monitor.status()

    def monitor_history(self, channel: Optional[str] = None) -> dict:
        return self.monitor.history(channel)

    # ----------------------------------------------------------------- sweeps --
    def validate_sweep(self, definition: dict) -> dict:
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
        validate_against(d, self.channels)  # re-check limits right before running
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

    def _script_run_sweep(self, definition: dict) -> str:
        """``run_sweep()`` from a script: run nested in the script's thread."""
        if self.runner is not None:
            raise EasyMeasureError("a sweep is already running")
        res = self._execute_sweep_item(definition, {"id": None})
        return res["status"]

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
        return self.store.list_runs(int(limit))

    def load_run(self, path: str) -> dict:
        return DataSet.load(path).snapshot()

    def read_run_header(self, path: str) -> dict:
        return read_header(path)

    # ------------------------------------------------------------------ queue --
    def queue_add(self, item: dict, start: bool = False, position: Optional[int] = None) -> int:
        if item.get("kind") == "sweep":
            validate_against(normalize_definition(item.get("definition") or {}), self.channels)
        return self.queue.add(item, start=start, position=position)

    def queue_remove(self, item_id: int) -> None:
        self.queue.remove(int(item_id))

    def queue_move(self, item_id: int, position: int) -> None:
        self.queue.move(int(item_id), int(position))

    def queue_clear(self) -> None:
        self.queue.clear()

    def queue_start(self) -> bool:
        """Start processing the queue; False (and the queue stays idle) if it is empty."""
        return self.queue.start()

    def queue_stop(self) -> None:
        self.queue.stop()

    def queue_state(self) -> dict:
        return self.queue.describe()

    def queue_options(self, stop_on_error: Optional[bool] = None) -> dict:
        if stop_on_error is not None:
            self.queue.stop_on_error = bool(stop_on_error)
        return self.queue.describe()

    def pause(self) -> None:
        self.queue.pause()

    def resume(self) -> None:
        self.queue.resume()

    def abort(self) -> None:
        self.queue.abort()

    def abort_all(self) -> None:
        self.queue.abort_all()

    def run_script_now(self, code: str, name: str = "script") -> int:
        """Queue a script at the front and start the queue."""
        return self.queue.add({"kind": "script", "code": code, "name": name}, start=True,
                              position=0)

    # ------------------------------------------------------------------ setup --
    def get_setup(self) -> dict:
        return setup_io.capture(self.instruments, self.channels, self.monitor)

    def save_setup(self, path: Optional[str] = None) -> dict:
        s = self.get_setup()
        if path:
            setup_io.save(path, s)
        return s

    def load_setup(self, setup: Optional[dict] = None, path: Optional[str] = None,
                   replace: bool = True, connect: bool = True) -> list:
        if self.runner is not None:
            raise ChannelError("cannot load a setup while a sweep is running")
        if setup is None:
            if not path:
                raise ChannelError("give a setup dict or a path")
            setup = setup_io.read(path)
        problems = setup_io.apply(setup, self.instruments, self.channels, self.monitor,
                                  replace=replace, connect=connect)
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
        return {"enabled": self._remote_want, "running": running, "urls": urls,
                "pid": self._remote.pid if self._remote_proc_alive() else None,
                "port": port, "host": cfg["host"],
                "fingerprint": st.get("fingerprint"), "self_signed": st.get("self_signed"),
                "connections": st.get("connections", []) if running else [],
                "users": users, "config": cfg, "log": str(self.home / "logs" / "remote.log")}

    def remote_start(self, port: Optional[int] = None, host: Optional[str] = None,
                     persist: bool = True) -> dict:
        """Start the internet gateway in its own process and keep it running."""
        if port:
            self._remote_args["port"] = int(port)
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
        from ..remote import config as rconfig
        from ..remote.users import UserStore
        users = UserStore(rconfig.users_path(self.home))
        users.remove(name)
        log.info("remote user %s removed", name)
        return users.list()

    def remote_stop(self, persist: bool = True) -> dict:
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
