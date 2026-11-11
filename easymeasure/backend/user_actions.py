"""User-defined actions: short named Python snippets (e.g. "all gates to zero",
"ramp the magnet down") that control any channel with the script API.

An action runs at once when asked (like a write from the Manual panel), in the
requesting thread, with the same namespace as queued scripts (``get``, ``set``,
``set_many``, ``set_raw``, ``sleep``, ``instrument``, ``action`` ...) plus
``user_action(name)`` to run another user action.  Each run has its own abort
control: ``abort_user_action`` (or abort all) stops it at the next line, sleep
or ramp step; pausing a sweep does not pause it.  The definitions travel with
the setup (save / load / autosave).
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..common.errors import AbortRequested, ChannelError
from .logsys import get_logger
from .scripting import ScriptRunner
from .sweep import RunControl

log = get_logger("user_actions")

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_ .\-]{0,63}$")
MAX_DEPTH = 8


@dataclass
class UserAction:
    name: str
    code: str
    description: str = ""
    confirm: bool = False           # front ends ask before running it

    def __post_init__(self):
        if not isinstance(self.name, str) or not NAME_RE.match(self.name.strip()):
            raise ChannelError("action name %r: use letters, digits, space, _ . - "
                               "(starting with a letter or _)" % (self.name,))
        self.name = self.name.strip()
        if not isinstance(self.code, str) or not self.code.strip():
            raise ChannelError("action %s has no code" % self.name)
        try:
            compile(self.code, "<action %s>" % self.name, "exec")
        except SyntaxError as exc:
            raise ChannelError("action %s: syntax error line %s: %s"
                               % (self.name, exc.lineno, exc.msg)) from None
        self.description = str(self.description or "")
        self.confirm = bool(self.confirm)

    def to_dict(self) -> dict:
        return {"name": self.name, "code": self.code, "description": self.description,
                "confirm": self.confirm}

    @classmethod
    def from_dict(cls, d: dict) -> "UserAction":
        if not isinstance(d, dict):
            raise ChannelError("an action is {name, code, description, confirm}")
        return cls(d.get("name"), d.get("code"), d.get("description", ""),
                   bool(d.get("confirm", False)))


@dataclass
class _Run:
    control: RunControl
    started: float
    thread: int
    depth: int = 0
    log: List[str] = field(default_factory=list)


class UserActions:
    def __init__(self, make_runner: Callable[[RunControl], ScriptRunner],
                 emit: Callable[[str, dict], None]):
        self._make_runner = make_runner
        self._emit = emit
        self._lock = threading.Lock()
        self._actions: Dict[str, UserAction] = {}
        self._running: Dict[str, _Run] = {}
        self._last: Dict[str, dict] = {}
        self._local = threading.local()

    # -- definitions ---------------------------------------------------------------
    def define(self, a: UserAction, replace: bool = False, rename_from: Optional[str] = None
               ) -> None:
        with self._lock:
            if a.name in self._actions and not replace and a.name != rename_from:
                raise ChannelError("action %r already exists" % a.name)
            if rename_from and rename_from != a.name:
                if rename_from in self._running:
                    raise ChannelError("action %s is running" % rename_from)
                items = [(a.name, a) if k == rename_from else (k, v)
                         for k, v in self._actions.items()]
                self._actions = dict(items)
                self._last.pop(rename_from, None)
            else:
                self._actions[a.name] = a

    def remove(self, name: str) -> None:
        with self._lock:
            if name not in self._actions:
                raise ChannelError("no action %r" % name)
            if name in self._running:
                raise ChannelError("action %s is running" % name)
            del self._actions[name]
            self._last.pop(name, None)

    def move(self, name: str, position: int) -> None:
        with self._lock:
            if name not in self._actions:
                raise ChannelError("no action %r" % name)
            items = [(k, v) for k, v in self._actions.items() if k != name]
            items.insert(max(0, min(int(position), len(items))), (name, self._actions[name]))
            self._actions = dict(items)

    def clear(self) -> None:
        with self._lock:
            self._actions.clear()
            self._last.clear()

    def get(self, name: str) -> UserAction:
        a = self._actions.get(name)
        if a is None:
            raise ChannelError("no action %r" % name)
        return a

    def actions(self) -> List[UserAction]:
        return list(self._actions.values())

    def describe(self) -> List[dict]:
        with self._lock:
            out = []
            for a in self._actions.values():
                d = a.to_dict()
                r = self._running.get(a.name)
                d["running"] = r is not None
                d["since"] = r.started if r else None
                d["last"] = dict(self._last[a.name]) if a.name in self._last else None
                out.append(d)
            return out

    # -- running -------------------------------------------------------------------
    def run(self, name: str) -> dict:
        """Run an action to its end in the calling thread; returns {status, elapsed,
        error}.  A failure or an abort is reported, not raised, for a top-level run;
        a nested ``user_action()`` raises so the outer action stops too."""
        a = self.get(name)
        depth = getattr(self._local, "depth", 0)
        nested = depth > 0
        if depth >= MAX_DEPTH:
            raise ChannelError("user actions call each other more than %d deep" % MAX_DEPTH)
        stack = getattr(self._local, "stack", [])
        if name in stack:
            raise ChannelError("action %s calls itself (%s)" % (name, " → ".join(stack + [name])))
        with self._lock:
            if name in self._running and not nested:
                raise ChannelError("action %s is already running" % name)
            parent = self._running.get(stack[0]) if stack else None
            control = parent.control if parent else RunControl()
            if not nested:
                self._running[name] = _Run(control, time.time(), threading.get_ident())
        self._local.depth = depth + 1
        self._local.stack = stack + [name]
        if not nested:
            self._emit("user_action", {"name": name, "status": "running"})
            log.info("action %s started", name)
        t0 = time.time()
        res = {"name": name, "status": "finished", "error": None}
        try:
            runner = self._make_runner(control)
            runner.run(a.code, name="action " + name, extra={"user_action": self.run_nested})
        except Exception as exc:
            if nested:
                raise
            if isinstance(exc, AbortRequested):
                res.update(status="aborted", error=str(exc))
                log.warning("action %s aborted", name)
            else:
                res.update(status="error", error=str(exc))
                log.error("action %s failed: %s", name, exc)
        finally:
            self._local.depth = depth
            self._local.stack = stack
            if not nested:
                res["elapsed"] = time.time() - t0
                res["ended"] = time.time()
                with self._lock:
                    self._running.pop(name, None)
                    self._last[name] = dict(res)
        if not nested:
            if res["status"] == "finished":
                log.info("action %s finished (%.2f s)", name, res["elapsed"])
            self._emit("user_action", dict(res))
        return res

    def run_nested(self, name: str) -> None:
        """``user_action(name)`` inside an action's code."""
        if getattr(self._local, "depth", 0) == 0:
            raise ChannelError("user_action() only works inside an action")
        self.run(name)

    def abort(self, name: Optional[str] = None) -> List[str]:
        """Abort one running action (or all of them); returns the ones asked to stop."""
        with self._lock:
            names = [n for n in self._running if name is None or n == name]
            for n in names:
                self._running[n].control.abort()
        if name is not None and not names:
            raise ChannelError("action %s is not running" % name)
        return names

    def running(self) -> List[str]:
        with self._lock:
            return list(self._running)
