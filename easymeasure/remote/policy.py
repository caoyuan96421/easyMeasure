"""What remote users may do.

Three roles, each including the rights of the one before:

``viewer``
    Watch everything: state, monitor, live and stored data, logs, queue.  Reads
    channels (an instrument query) but changes nothing.
``operator``
    Run measurements within the configured setup: set channels (alias limits and
    ramp rates are always enforced by the backend), run instrument actions,
    monitor channels, queue / start / pause / abort sweeps, add PPT slides,
    reconnect instruments, run the user actions an admin defined.  Cannot run Python code (scripts, sweep hooks), change
    instruments, aliases or their limits, send raw instrument commands, change
    backend settings or touch files outside the data directory.
``admin``
    Everything a local user can do, including scripts and hook code - which means
    full control of the backend computer.  Give this role sparingly.

Commands that only make sense on the backend computer itself (tray icon, remote
access control) are never available remotely.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

LEVEL = {"viewer": 0, "operator": 1, "admin": 2}

VIEW = {
    "ping", "get_state", "get_settings", "list_drivers", "list_resources", "list_instruments",
    "read_channel", "channel_info", "list_channels", "monitor_list", "monitor_history",
    "validate_sweep", "queue_state", "current_run", "run_snapshot", "list_runs", "load_run",
    "read_run_header", "get_setup", "save_setup", "recent_logs", "list_clients",
    "list_user_actions", "ramp_state", "list_parameters",
}
OPERATE = {
    "write_channel", "set_many", "stop_ramps", "set_parameter", "query_channel", "run_action", "monitor_add", "monitor_remove",
    "queue_add", "queue_remove", "queue_move", "queue_clear", "queue_start", "queue_stop",
    "queue_options", "pause", "resume", "abort", "abort_all", "make_ppt",
    "connect_instrument", "disconnect_instrument", "reconnect_instrument",
    # user actions: running one runs code an admin wrote, defining one is writing code
    "run_user_action", "abort_user_action",
}
ADMIN = {
    "set_settings", "rescan_drivers", "discover_resources", "add_instrument",
    "remove_instrument", "update_instrument", "raw_query", "raw_write", "define_alias",
    "remove_alias", "define_parameter", "remove_parameter", "define_composite", "check_composite", "remove_composite", "load_setup", "run_script_now",
    "define_user_action", "remove_user_action", "move_user_action",
    "shutdown",
    # the Driver Wizard writes driver code (init scripts are Python)
    "wizard_templates", "wizard_check", "wizard_list", "wizard_load", "wizard_save",
    "wizard_try", "wizard_delete",
}
#: never over the network, whatever the role
LOCAL_ONLY = {"start_tray", "remote_status", "remote_start", "remote_stop", "remote_set_user",
              "remote_remove_user", "remote_cert_request", "remote_cert_use"}

MIN_MONITOR_INTERVAL = 0.1


class Denied(Exception):
    pass


def required_level(cmd: str) -> Optional[int]:
    if cmd in VIEW:
        return 0
    if cmd in OPERATE:
        return 1
    if cmd in ADMIN:
        return 2
    return None


def allowed_commands(role: str) -> list:
    lvl = LEVEL.get(role, -1)
    out = set()
    if lvl >= 0:
        out |= VIEW
    if lvl >= 1:
        out |= OPERATE
    if lvl >= 2:
        out |= ADMIN
    return sorted(out)


def _inside(path: str, roots: Iterable[str]) -> bool:
    try:
        p = Path(os.path.realpath(path))
    except (OSError, ValueError, TypeError):
        return False
    for r in roots:
        if not r:
            continue
        try:
            root = Path(os.path.realpath(r))
        except (OSError, ValueError):
            continue
        a, b = os.path.normcase(str(p)), os.path.normcase(str(root))
        if a == b or a.startswith(b.rstrip("\\/") + os.sep):
            return True
    return False


def _has_code(definition) -> bool:
    if not isinstance(definition, dict):
        return False
    for ph in ("pre", "post"):
        p = definition.get(ph)
        if isinstance(p, dict) and str(p.get("hook") or "").strip():
            return True
    # per-point hooks of the dimensions: any code counts, enabled or not (the
    # backend's rules for "enabled" need not be repeated here to be safe)
    dims = definition.get("dimensions")
    for dim in dims if isinstance(dims, list) else []:
        hooks = dim.get("hooks") if isinstance(dim, dict) else None
        if hooks is None:
            continue
        if not isinstance(hooks, dict):
            return True
        for h in hooks.values():
            code = h.get("code") if isinstance(h, dict) else h
            if str(code or "").strip():
                return True
    return False


def check(role: str, cmd: str, args: dict, data_roots: Iterable[str]) -> dict:
    """Raise :class:`Denied` or return the (possibly adjusted) arguments."""
    lvl = LEVEL.get(role, -1)
    if cmd in LOCAL_ONLY:
        raise Denied("%s is only available on the backend computer" % cmd)
    need = required_level(cmd)
    if need is None:
        raise Denied("unknown command %r" % cmd)
    if lvl < need:
        raise Denied("%s requires the %s role (you are %s)"
                     % (cmd, ("viewer", "operator", "admin")[need], role))
    if cmd == "set_settings" and "remote_access" in args:
        raise Denied("remote access can only be switched on the backend computer")
    if lvl >= 2:
        return args                     # admins can run code anyway: no further limits
    roots = list(data_roots)

    def path_ok(key: str, required: bool = False):
        p = args.get(key)
        if p is None or p == "":
            if required:
                raise Denied("%s needs %r" % (cmd, key))
            return
        if not isinstance(p, str):
            raise Denied("%s must be a string" % key)
        if not roots:
            raise Denied("data directory unknown; try again in a moment")
        if not _inside(p, roots):
            raise Denied("%s: only files inside the data directory are accessible remotely" % cmd)

    if cmd in ("load_run", "read_run_header"):
        path_ok("path", required=True)
    elif cmd == "save_setup":
        if args.get("path"):
            raise Denied("saving setup files on the backend computer requires the admin role")
    elif cmd == "make_ppt":
        if args.get("ppt_path"):
            raise Denied("choosing the PowerPoint file requires the admin role")
        path_ok("path")
    elif cmd == "queue_add":
        item = args.get("item")
        if not isinstance(item, dict):
            raise Denied("queue_add needs an item")
        kind = item.get("kind")
        if kind == "script":
            raise Denied("scripts run Python code on the backend: admin role required")
        if kind != "sweep":
            raise Denied("unknown queue item kind %r" % kind)
        if _has_code(item.get("definition")):
            raise Denied("sweep hooks run Python code on the backend: admin role required "
                         "(clear the pre/post and point hook code)")
    elif cmd == "monitor_add":
        try:
            iv = float(args.get("interval", 1.0))
        except (TypeError, ValueError):
            raise Denied("interval must be a number") from None
        if not iv >= MIN_MONITOR_INTERVAL:
            args = dict(args, interval=MIN_MONITOR_INTERVAL)
    return args
