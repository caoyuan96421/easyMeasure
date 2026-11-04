"""Driver registry.

Every instrument driver lives in its own Python source file.  A driver module
must define a subclass of :class:`easymeasure.backend.instrument.Instrument`; if it
defines several, the one named in a module-level ``DRIVER`` variable is used.
The driver is identified by its module name (e.g. ``sim_dmm``).

Drivers are discovered in this package and in any additional directories given
to :class:`DriverRegistry` (``driver_paths`` in the backend settings), so users can
keep their own drivers outside the library.  Import errors are captured and
reported instead of crashing the backend.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import pkgutil
import sys
import threading
import traceback
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ...common.errors import DriverError
from ..instrument import Instrument
from ..logsys import get_logger

log = get_logger("drivers")

_SKIP = {"simbus", "simworld"}


class DriverRegistry:
    def __init__(self, extra_paths: Optional[Iterable[str]] = None):
        self.extra_paths: List[Path] = [Path(p) for p in (extra_paths or [])]
        self._classes: Dict[str, type] = {}
        self._errors: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._scanned = False

    # -- discovery -------------------------------------------------------------
    def scan(self) -> None:
        with self._lock:
            self._classes.clear()
            self._errors.clear()
            pkg_path = Path(__file__).parent
            for mod in pkgutil.iter_modules([str(pkg_path)]):
                if mod.name.startswith("_") or mod.name in _SKIP:
                    continue
                self._load_builtin(mod.name)
            for d in self.extra_paths:
                if not d.is_dir():
                    log.warning("driver path %s does not exist", d)
                    continue
                for f in sorted(d.glob("*.py")):
                    if not f.name.startswith("_"):
                        self._load_file(f)
            self._scanned = True
        log.info("driver registry: %d drivers, %d failed", len(self._classes), len(self._errors))

    def _register_module(self, name: str, module) -> None:
        cls = getattr(module, "DRIVER", None)
        if cls is None:
            candidates = [c for _, c in inspect.getmembers(module, inspect.isclass)
                          if issubclass(c, Instrument) and c is not Instrument
                          and c.__module__ == module.__name__]
            if len(candidates) == 1:
                cls = candidates[0]
            elif not candidates:
                return  # helper module, not a driver
            else:
                self._errors[name] = "module defines several drivers; set DRIVER = <class>"
                return
        if not (inspect.isclass(cls) and issubclass(cls, Instrument)):
            self._errors[name] = "DRIVER is not an Instrument subclass"
            return
        self._classes[name] = cls

    def _load_builtin(self, name: str) -> None:
        try:
            module = importlib.import_module(__name__ + "." + name)
            self._register_module(name, module)
        except Exception:
            self._errors[name] = traceback.format_exc(limit=5)
            log.error("failed to import driver %s:\n%s", name, self._errors[name])

    def _load_file(self, path: Path) -> None:
        name = path.stem
        modname = "easymeasure_user_drivers." + name
        try:
            spec = importlib.util.spec_from_file_location(modname, str(path))
            module = importlib.util.module_from_spec(spec)
            sys.modules[modname] = module
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            self._register_module(name, module)
        except Exception:
            self._errors[name] = traceback.format_exc(limit=5)
            log.error("failed to import user driver %s:\n%s", path, self._errors[name])

    # -- access ----------------------------------------------------------------
    def get(self, name: str) -> type:
        if not self._scanned:
            self.scan()
        cls = self._classes.get(name)
        if cls is None:
            if name in self._errors:
                raise DriverError("driver %r failed to load: %s" % (name, self._errors[name]))
            raise DriverError("unknown driver %r" % name)
        return cls

    def register(self, name: str, cls: type) -> None:
        """Register a driver class programmatically (useful for tests)."""
        if not (inspect.isclass(cls) and issubclass(cls, Instrument)):
            raise DriverError("not an Instrument subclass")
        if not self._scanned:
            self.scan()
        self._classes[name] = cls

    def describe(self) -> dict:
        if not self._scanned:
            self.scan()
        out = {}
        for name, cls in sorted(self._classes.items()):
            try:
                chans = [c.to_dict() for c in getattr(cls, "CHANNELS", [])]
            except Exception:
                chans = []
            out[name] = {
                "description": (cls.DESCRIPTION or (cls.__doc__ or "")).strip().split("\n")[0],
                "options": dict(getattr(cls, "DEFAULT_OPTIONS", {})),
                "channels": chans,
                "shared": bool(getattr(cls, "SHARED_RESOURCE", False)),
            }
        return {"drivers": out, "errors": dict(self._errors)}
