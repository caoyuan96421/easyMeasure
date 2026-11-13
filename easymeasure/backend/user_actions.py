"""User-defined actions: short named Python snippets (``zero_gates``,
``magnet_down``) that control any channel with the script API.

A user action is a write-only channel with a plain name: writing it runs it
(``write_channel(name="zero_gates")``, ``set('zero_gates')`` in a script or another
action, a pre-sweep constant, a Manual-panel button).  It runs in the writing
thread, with the same namespace as queued scripts (``get``, ``set``, ``set_many``,
``sleep``, ``instrument`` ...).  Each run has its own abort control:
``abort_user_action`` (or abort all) stops it at the next line, sleep or ramp step;
run from a script or a sweep, aborting those stops it too.  The definitions travel
with the setup (save / load / autosave).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..common.errors import AbortRequested, ChannelError
from .logsys import get_logger
from .composites import ok_name
from .scripting import ScriptRunner
from .sweep import RunControl

log = get_logger("user_actions")

MAX_DEPTH = 8


class _Linked(RunControl):
    """The control of a user action run from a script or a sweep: their abort (and
    pause) stops the action too."""

    def __init__(self, parent_check):
        super().__init__()
        self._parent = parent_check

    def check(self, on_pause=None) -> None:
        self._parent()
        super().check(on_pause)

    def check_abort(self) -> None:
        self._parent()
        super().check_abort()

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while True:
            self._parent()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            super().sleep(min(remaining, 0.05))


@dataclass
class UserAction:
    name: str
    code: str
    description: str = ""
    confirm: bool = False           # front ends ask before running it

    def __post_init__(self):
        if not isinstance(self.name, str) or not ok_name(self.name.strip()):
            raise ChannelError("user action name %r: use letters, digits and _ (it is a "
                               "channel name, e.g. zero_gates)" % (self.name,))
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
        #: ``claim(name, replace)``: refuses a name taken in the channel namespace
        self.claim: Optional[Callable[[str, bool], None]] = None

    def has(self, name: str) -> bool:
        return name in self._actions

    # -- definitions ---------------------------------------------------------------
    def define(self, a: UserAction, replace: bool = False, rename_from: Optional[str] = None
               ) -> None:
        if self.claim is not None:
            self.claim(a.name, bool(replace) or a.name == rename_from)
        with self._lock:
            if a.name in self._actions and not replace and a.name != rename_from:
                raise ChannelError("user action %r already exists" % a.name)
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
    def run(self, name: str, abort_check: Optional[Callable[[], None]] = None) -> dict:
        """Run an action to its end in the calling thread; returns {status, elapsed,
        error}.  A failure or an abort is reported, not raised, for a top-level run;
        one run by another action raises so the outer action stops too.
        *abort_check* (of the script or sweep that runs it) stops it as well."""
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
            control = parent.control if parent else \
                (_Linked(abort_check) if abort_check is not None else RunControl())
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
            runner.run(a.code, name="action " + name)
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
