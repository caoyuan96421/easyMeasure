"""Saving and loading of setups (instruments, aliases, composites, monitor list,
user actions)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from ..common.codec import to_jsonable
from ..common.errors import EasyMeasureError
from .channels import Alias
from .composites import composite_from_dict
from .logsys import get_logger

log = get_logger("setup")

FORMAT = "easyMeasure-setup-1"


def capture(instruments, channels, monitor, user_actions=None) -> dict:
    out = {
        "format": FORMAT,
        "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
        "instruments": [
            {"name": h.name, "driver": h.driver_name, "address": h.address,
             "options": dict(h.options)}
            for h in instruments.handles()
        ],
        "aliases": [a.to_dict() for a in channels.aliases()],
        "composites": [c.to_dict() for c in channels.composites()],
        "monitor": monitor.config(),
    }
    if user_actions is not None:
        out["actions"] = [a.to_dict() for a in user_actions.actions()]
    return out


def atomic_write_json(path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(to_jsonable(obj), f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save(path, setup: dict) -> None:
    atomic_write_json(path, setup)
    log.info("setup saved to %s", path)


def read(path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict) or d.get("format") != FORMAT:
        raise EasyMeasureError("%s is not an easyMeasure setup file" % path)
    return d


def apply(setup: dict, instruments, channels, monitor, replace: bool = True,
          connect: bool = True, user_actions=None) -> List[str]:
    """Apply a setup.  Returns a list of problems (never raises for single items)."""
    if not isinstance(setup, dict) or setup.get("format") != FORMAT:
        raise EasyMeasureError("not an easyMeasure setup")
    problems: List[str] = []
    if replace:
        monitor.clear()
        channels.clear()
        if user_actions is not None:
            user_actions.clear()
        for name in instruments.names():
            try:
                instruments.remove(name)
            except Exception as exc:
                problems.append("removing %s: %s" % (name, exc))
    for inst in setup.get("instruments", []):
        try:
            h = instruments.add(inst["name"], inst["driver"], inst.get("address", "NONE"),
                                inst.get("options", {}), connect=connect)
            if connect and h.state != "connected":
                problems.append("instrument %s: %s" % (inst["name"], h.last_error or h.state))
        except Exception as exc:
            problems.append("instrument %s: %s" % (inst.get("name"), exc))
    for a in setup.get("aliases", []):
        try:
            channels.add_alias(Alias.from_dict(a), replace=True)
        except Exception as exc:
            problems.append("alias %s: %s" % (a.get("name"), exc))
    # composites may use each other: define in passes until nothing more can be added
    todo = list(setup.get("composites", []))
    last_err = {}
    while todo:
        left = []
        for c in todo:
            try:
                comp, notes = composite_from_dict(c)
                channels.add_composite(comp, replace=True)
                problems.extend(notes)
            except Exception as exc:
                last_err[c.get("name")] = exc
                left.append(c)
        if len(left) == len(todo):
            for c in left:
                problems.append("composite %s: %s" % (c.get("name"), last_err[c.get("name")]))
            break
        todo = left
    for m in setup.get("monitor", []):
        try:
            monitor.add(m["channel"], float(m.get("interval", 1.0)))
        except Exception as exc:
            problems.append("monitor %s: %s" % (m.get("channel"), exc))
    if user_actions is not None:
        from .user_actions import UserAction
        for d in setup.get("actions", []):
            try:
                user_actions.define(UserAction.from_dict(d), replace=True)
            except Exception as exc:
                problems.append("action %s: %s" % (d.get("name") if isinstance(d, dict) else d,
                                                   exc))
    for p in problems:
        log.warning("setup: %s", p)
    return problems
