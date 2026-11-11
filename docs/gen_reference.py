"""Generate the reference pages that come straight from the code (run by the
mkdocs ``gen-files`` plugin at every build, so they never go stale):

* ``instruments.md``            - every supported instrument (one row per driver)
* ``reference/drivers/*.md``    - one page per driver: interfaces, options, channels
                                  (as reported by the driver connected to its emulator),
                                  actions, notes and the class API
* ``reference/commands.md``     - every backend command: signature, role, description
* ``reference/expressions.md``  - the functions and constants formulas may use
* ``reference/cli.md``          - ``--help`` of the command-line programs
* ``reference/api/*.md``        - one page per Python module (mkdocstrings)

``python docs/gen_reference.py --check`` builds the pages into memory and reports
problems without mkdocs (used by the tests).
"""

from __future__ import annotations

import inspect
import io
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
sys.path.insert(0, str(ROOT))

IN_MKDOCS = __name__ == "<run_path>"          # mkdocs-gen-files runs us with runpy
PAGES = {}
if IN_MKDOCS:
    import mkdocs_gen_files
    _open = mkdocs_gen_files.open
    _set_edit = mkdocs_gen_files.set_edit_path
else:                                         # --check / tests: collect in memory

    class _Mem(io.StringIO):
        def __init__(self, name):
            super().__init__()
            self.name = name

        def __exit__(self, *a):
            PAGES[self.name] = self.getvalue()
            return super().__exit__(*a)

    def _open(name, mode="w"):
        return _Mem(name)

    def _set_edit(*a):
        pass


INTERFACE_NAMES = {
    "gpib": "GPIB", "usb": "USB-TMC", "tcpip": "VISA LAN", "tcp": "TCP socket",
    "serial": "serial", "daq": "NI-DAQmx", "sim": "simulated bus", "none": "no port",
    "other": "other VISA",
}
ROLE = {0: "viewer", 1: "operator", 2: "admin", None: "local only"}

HARDWARE_ORDER = ["keithley2400", "keysight_b2901", "keysight_b2902", "sr830", "hp34401a",
                  "yokogawa7651", "hp4145", "ni6211", "ni63xx", "cygnus16430", "scpi_generic"]

KIND = {
    "keithley2400": "Source-measure unit", "keysight_b2901": "Source-measure unit",
    "keysight_b2902": "Source-measure unit (2 outputs)", "sr830": "Lock-in amplifier",
    "hp34401a": "Digital multimeter", "yokogawa7651": "DC voltage / current source",
    "hp4145": "Semiconductor parameter analyzer", "ni6211": "Multifunction DAQ",
    "ni63xx": "Multifunction DAQ (X Series)", "cygnus16430": "8-channel supply",
    "scpi_generic": "Any SCPI instrument (configured by options)",
}

SHORT_NAME = {"keithley2400": "smu", "keysight_b2901": "smu", "keysight_b2902": "smu2",
              "sr830": "lockin", "hp34401a": "dmm", "yokogawa7651": "src", "hp4145": "spa",
              "ni6211": "daq", "ni63xx": "daq", "cygnus16430": "cyg", "scpi_generic": "inst"}

COMMAND_GROUPS = [
    ("Connection and state", ["ping", "get_state", "get_settings", "set_settings", "shutdown",
                              "start_tray", "recent_logs"]),
    ("Drivers and ports", ["list_drivers", "rescan_drivers", "discover_resources",
                           "list_resources"]),
    ("Driver Wizard", ["wizard_templates", "wizard_check", "wizard_list", "wizard_load",
                       "wizard_save", "wizard_try", "wizard_delete"]),
    ("Instruments", ["add_instrument", "update_instrument", "remove_instrument",
                     "connect_instrument", "disconnect_instrument", "reconnect_instrument",
                     "list_instruments", "run_action", "raw_query", "raw_write"]),
    ("Channels", ["read_channel", "write_channel", "set_many", "query_channel",
                  "channel_info", "list_channels"]),
    ("Aliases and composites", ["define_alias", "remove_alias", "define_composite",
                                "check_composite", "remove_composite"]),
    ("User actions", ["define_user_action", "remove_user_action", "move_user_action",
                      "run_user_action", "abort_user_action", "list_user_actions"]),
    ("Monitor", ["monitor_add", "monitor_remove", "monitor_list", "monitor_history"]),
    ("Sweeps and queue", ["validate_sweep", "queue_add", "queue_remove", "queue_move",
                          "queue_clear", "queue_start", "queue_stop", "queue_state",
                          "queue_options", "pause", "resume", "abort", "abort_all",
                          "run_script_now"]),
    ("Runs and data", ["current_run", "run_snapshot", "list_runs", "load_run",
                       "read_run_header", "make_ppt"]),
    ("Setups", ["save_setup", "load_setup", "get_setup"]),
    ("Remote access", ["remote_status", "remote_start", "remote_stop", "remote_set_user",
                       "remote_remove_user", "remote_cert_request", "remote_cert_use"]),
]


def md_escape(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ").strip()


def fmt_value(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return "%g" % v
    if isinstance(v, str):
        return "`%s`" % v.replace("\n", "\\n").replace("\r", "\\r") if v else '`""`'
    return "`%s`" % md_escape(v)


def doc_md(text: str) -> str:
    """Docstring text (reST-ish) → Markdown good enough for the tables and pages."""
    import re
    text = inspect.cleandoc(text or "")
    text = re.sub(r":(?:meth|class|func|mod|data|attr):`~?([^`]+)`", r"`\1`", text)
    return text


# -- drivers ---------------------------------------------------------------------------
def collect_drivers():
    """{name: info} with the channels and actions as the driver reports them when
    connected to its emulator (or the static lists when it has none)."""
    import logging
    from easymeasure.backend.service import Backend
    from easymeasure.backend.registry import DriverRegistry
    logging.disable(logging.CRITICAL)
    home = Path(tempfile.mkdtemp(prefix="emdocs"))
    be = Backend(home=home / "home", log_to_console=False,
                 settings={"folders_confirmed": True, "autosave_setup": False,
                           "data_dir": str(home / "data")})
    out = {}
    try:
        reg = be.drivers
        desc = be.list_drivers()["drivers"]
        for name, info in desc.items():
            cls = reg.get(name)
            d = dict(info)
            d["cls"] = cls
            d["module"] = cls.__module__
            d["doc"] = doc_md(inspect.getdoc(cls) or "")
            d["actions"] = [a.to_dict() for a in getattr(cls, "ACTIONS", [])]
            d["live"] = None
            sims = [s["address"] for s in info.get("suggest") or []
                    if s["address"].startswith(("SIM::", "DAQ::SIM")) or s["address"] == "NONE"]
            if sims:
                try:
                    h = be.add_instrument("doc_" + name, name, sims[0], {})
                    if h.get("state") == "connected":
                        inst = be.instruments.get("doc_" + name)
                        d["channels"] = [c.to_dict() for c in inst.channels.values()]
                        d["actions"] = [a.to_dict() for a in inst.actions.values()]
                        d["live"] = sims[0]
                except Exception as exc:          # static lists then
                    d["live_error"] = str(exc)
                finally:
                    try:
                        be.remove_instrument("doc_" + name)
                    except Exception:
                        pass
            out[name] = d
    finally:
        be.close()
        logging.disable(logging.NOTSET)
    return out


def channel_table(chans, config: bool) -> str:
    rows = [c for c in chans if bool(c.get("config")) == config]
    if not rows:
        return "_none_\n"
    lines = ["| channel | access | type | unit | allowed values | description |",
             "|---|---|---|---|---|---|"]
    for c in rows:
        shape = c.get("shape")
        typ = c["dtype"]
        if shape:
            typ += "[%s]" % "×".join("any" if n in (-1, None) else str(n) for n in shape)
        elif shape is None:
            typ += " (any shape)"
        allowed = ", ".join(fmt_value(a) for a in c["allowed"]) if c.get("allowed") else ""
        lines.append("| `%s` | %s | %s | %s | %s | %s |" % (
            c["name"], c["access"], typ, md_escape(c.get("unit") or ""), allowed,
            md_escape(c.get("description") or "")))
    return "\n".join(lines) + "\n"


def driver_page(name: str, d: dict) -> str:
    notes = DOCS / "_driver_notes" / ("%s.md" % name)
    parts = ["# `%s` – %s\n" % (name, d["description"] or name)]
    parts.append('<span class="em-tag">%s</span>\n' % ("simulated" if name.startswith("sim_")
                                                       else KIND.get(name, "instrument")))
    parts.append("| property | value |\n|---|---|")
    parts.append("| driver name | `%s` |" % name)
    parts.append("| module | `%s` |" % d["module"])
    parts.append("| interfaces | %s |" % (", ".join(
        "%s (`%s`)" % (INTERFACE_NAMES.get(i, i), i) for i in d["interfaces"]) or "–"))
    if d.get("idn_match"):
        parts.append("| recognised by `*IDN?` | %s |" % ", ".join("`%s`" % m for m in d["idn_match"]))
    sugg = [s for s in d.get("suggest") or []]
    if sugg:
        parts.append("| suggested addresses | %s |" % ", ".join(
            "`%s` (%s)" % (s["address"], md_escape(s.get("description", ""))) for s in sugg))
    parts.append("| shared port by default | %s |" % ("yes" if d.get("shared") else "no"))
    parts.append("")
    if notes.exists():
        parts.append(notes.read_text(encoding="utf-8").rstrip() + "\n")
    elif d["doc"]:
        parts.append(d["doc"] + "\n")
    parts.append("## Add it\n")
    addr = d.get("live") or (sugg[0]["address"] if sugg else "GPIB0::12::INSTR")
    parts.append("```python\nc.call(\"add_instrument\", name=\"%s\", driver=\"%s\", address=\"%s\")\n```\n"
                 % (SHORT_NAME.get(name, name[4:] if name.startswith("sim_") else name), name, addr))
    if d.get("live"):
        parts.append("(`%s` is the driver's built-in emulator; use the instrument's real "
                     "address for hardware.)\n" % addr)
    parts.append("## Options\n")
    if d["options"]:
        parts.append("| option | default |\n|---|---|")
        for k, v in d["options"].items():
            parts.append("| `%s` | %s |" % (k, fmt_value(v)))
        parts.append("\nPlus the guard options every instrument has (`call_timeout`, `retries`, "
                     "…, see [Robustness](../../guide/robustness.md#guard-options)).\n")
    else:
        parts.append("Only the guard options every instrument has (see "
                     "[Robustness](../../guide/robustness.md#guard-options)).\n")
    src = ("as the driver reports them connected to its emulator (`%s`) in its default "
           "configuration" % d["live"]) if d.get("live") else "as declared by the driver"
    parts.append("## Channels\n")
    parts.append("Measurement / source channels, %s. Mode and model settings can rename "
                 "channels or change units; the front ends follow.\n" % src)
    parts.append(channel_table(d["channels"], False))
    parts.append("## Configuration channels\n")
    parts.append("Settings: read and set directly (`write_channel(\"inst.<name>\", value)`, "
                 "sweep pre/post constants), never aliased or swept.\n")
    parts.append(channel_table(d["channels"], True))
    parts.append("## Actions\n")
    if d["actions"]:
        parts.append("| action | description |\n|---|---|")
        for a in d["actions"]:
            parts.append("| `%s` | %s |" % (a["name"], md_escape(a.get("description", ""))))
        parts.append("\nRun with `run_action(ref=\"<instrument>.<action>\")` or "
                     "`action('<instrument>.<action>')` in scripts.\n")
    else:
        parts.append("_none_\n")
    parts.append("## Python API\n")
    mod_page = "../api/%s.md" % d["module"].replace(".", "/")
    parts.append("Class [`%s`](%s#%s.%s) in module [`%s`](%s)." % (
        d["cls"].__name__, mod_page, d["module"], d["cls"].__name__, d["module"], mod_page))
    if notes.exists() and d["doc"]:
        parts.append("\n" + d["doc"])
    return "\n".join(parts) + "\n"


def instruments_page(drivers: dict) -> str:
    hw = [n for n in HARDWARE_ORDER if n in drivers]
    hw += sorted(n for n in drivers if n not in hw and not n.startswith("sim_"))
    sim = sorted(n for n in drivers if n.startswith("sim_"))
    out = ["# Supported instruments\n",
           "Every driver that comes with easyMeasure. Each hardware driver is tested against "
           "an emulator built from the instrument's programming manual, so the commands it "
           "sends are known to exist (see [Testing](guide/testing.md)). This page is "
           "generated from the drivers themselves at every documentation build.\n",
           "Instruments without a driver: build one in minutes with the "
           "[Driver Wizard](guide/driver-wizard.md) (most SCPI and other text-command "
           "instruments), configure [`scpi_generic`](reference/drivers/scpi_generic.md) with "
           "options, or [write a driver](scripting/writing-drivers.md).\n",
           "## Hardware\n",
           "| instrument | kind | driver | interfaces | channels | actions |",
           "|---|---|---|---|---|---|"]
    for n in hw:
        d = drivers[n]
        nch = len([c for c in d["channels"] if not c.get("config")])
        ncfg = len([c for c in d["channels"] if c.get("config")])
        out.append("| %s | %s | [`%s`](reference/drivers/%s.md) | %s | %s | %s |" % (
            md_escape(d["description"]), KIND.get(n, ""), n, n,
            ", ".join(INTERFACE_NAMES.get(i, i) for i in d["interfaces"] if i != "sim"),
            ("%d + %d settings" % (nch, ncfg)) if (nch or ncfg) else "from options",
            len(d["actions"]) or "–"))
    out += ["", "## Simulated instruments\n",
            "For trying easyMeasure without hardware and for testing. Every simulated driver "
            "takes fault-injection options (see [Simulated instruments]"
            "(guide/simulation.md)).\n",
            "| driver | simulates | channels |", "|---|---|---|"]
    for n in sim:
        d = drivers[n]
        out.append("| [`%s`](reference/drivers/%s.md) | %s | %s |" % (
            n, n, md_escape(d["description"]),
            ", ".join("`%s`" % c["name"] for c in d["channels"] if not c.get("config"))))
    out += ["", "## Emulated hardware\n",
            "Give a hardware driver the address `SIM::<anything>` (NI DAQs: `DAQ::SIM`) and "
            "it talks to a built-in emulator of the instrument instead of a port - useful to "
            "rehearse a measurement before the instrument is connected.\n"]
    return "\n".join(out) + "\n"


# -- commands --------------------------------------------------------------------------
def commands_page() -> str:
    from easymeasure.backend.service import Backend
    from easymeasure.backend.server import CONTROL_COMMANDS
    from easymeasure.remote import policy
    cmds = list(Backend.COMMANDS)
    grouped = [c for _, g in COMMAND_GROUPS for c in g]
    missing = [c for c in cmds if c not in grouped]
    groups = COMMAND_GROUPS + ([("Other", missing)] if missing else [])
    out = ["# Backend commands\n",
           "Everything the front ends do goes through these commands, so a script can do "
           "the same. Call them with keyword arguments:\n",
           "```python\nfrom easymeasure.client import BackendClient\nc = BackendClient.connect()\n"
           "c.call(\"write_channel\", name=\"Vg\", value=0.2)\n```\n",
           "or in-process on a `Backend` object (`be.write_channel(name=\"Vg\", value=0.2)`). "
           "**Role** is the minimum role a [remote user](../guide/remote.md#roles) needs "
           "(*local only*: refused through the internet gateway). Commands marked ⚡ are served "
           "by a separate thread pool, so they answer even while every instrument is busy.\n",
           "| group | commands |", "|---|---|"]
    for title, names in groups:
        out.append("| %s | %s |" % (title, ", ".join("[`%s`](#%s)" % (n, n) for n in names
                                                      if n in cmds)))
    out.append("")
    for title, names in groups:
        out.append("## %s\n" % title)
        for n in names:
            if n not in cmds:
                continue
            f = getattr(Backend, n)
            sig = str(inspect.signature(f)).replace("(self, ", "(").replace("(self)", "()")
            lvl = policy.required_level(n)
            if n in policy.LOCAL_ONLY:
                lvl = None
            role = ROLE.get(lvl, "local only")
            out.append('### `%s` {#%s}\n' % (n, n))
            out.append("```python\n%s%s\n```\n" % (n, sig))
            out.append("**Role:** %s%s\n" % (role, "  ⚡" if n in CONTROL_COMMANDS else ""))
            doc = doc_md(inspect.getdoc(f) or "")
            out.append((doc or "_(no description)_") + "\n")
    return "\n".join(out) + "\n"


# -- expressions -----------------------------------------------------------------------
def expressions_page() -> str:
    from easymeasure.common import exprs
    funcs = sorted(exprs._NP_FUNCS)
    consts = sorted(exprs._NP_CONSTS)
    math_funcs = sorted(n for n, v in exprs._MATH_FUNCS.items() if callable(v))
    builtins = sorted(exprs._BUILTINS)
    out = [
        "# Expressions\n",
        "Formulas appear in several places: sweep `mapping`s, composite channel targets, "
        "reads and mappings, *Set from expression…* in the Manual panel. They are "
        "evaluated by a sandboxed evaluator (`easymeasure.common.exprs`): only the names "
        "below, arithmetic, comparisons, `and` / `or` / `not`, `a if c else b`, indexing and "
        "list / tuple literals. No attribute access (except `np.<function>` from the list), "
        "no imports, no files; array sizes and powers are bounded.\n",
        "## Limits\n",
        "| limit | value |\n|---|---|",
        "| source length | %d characters |" % exprs.MAX_SOURCE,
        "| largest array | %s elements |" % format(exprs.MAX_ELEMENTS, ","),
        "| largest integer from `**` / `<<` | %d bits |" % exprs.MAX_INT_BITS,
        "",
        "## Functions\n",
        "Array functions (also as `np.<name>`):\n",
        ", ".join("`%s`" % f for f in funcs) + "\n",
        "Scalar math (as `math.<name>`): " + ", ".join("`%s`" % f for f in math_funcs) + "\n",
        "Built-ins: " + ", ".join("`%s`" % b for b in builtins) + "\n",
        "Constants: " + ", ".join("`%s`" % c for c in consts) + " (also `math.tau`), "
        "`True`, `False`, `None`.\n",
        "## Piecewise functions\n",
        "```python\npiecewise(x < 0, -x, x < 1, x**2, 1)   # cond1, value1, cond2, value2, …, default\n"
        "piecewise(x > 0, sqrt(x))                # no default: NaN where no condition holds\n"
        "-x if x < 0 else 2*x                      # scalar\nwhere(x < 0, -x, 2*x)                     "
        "# elementwise\ninterp(x, [0, 1, 2], [0, 10, 0])          # table, linear in between\n```\n",
        "`piecewise` works on scalars and arrays (elementwise, with broadcasting). Invalid "
        "operations (`log(-1)`, `0/0`) give NaN / inf without warnings.\n",
        "## Names per context\n",
        "| where | names |\n|---|---|",
        "| sweep `mapping` | `x` (linear value from `start` to `stop`), `i` (index), `N` "
        "(points), `t = i/(N-1)`, `start`, `stop` |",
        "| composite *multivariate write* target | the composite's variables |",
        "| composite *read* | any channel: aliases, variables, mappings, other reads |",
        "| composite *mapping* | `forward`: `x`; `backward`: `y` |",
        "| *Set from expression…* | `x` (the present value) |",
        "",
    ]
    return "\n".join(out) + "\n"


# -- settings --------------------------------------------------------------------------
def _commented_dict(path: Path, var: str):
    """``[(key, default_source, comment)]`` of a module-level dict literal, with the
    comments written next to each entry (continuation comment lines are joined)."""
    import re
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(var + " = {"))
    out = []
    for l in lines[start + 1:]:
        if l.startswith("}"):
            break
        m = re.match(r'\s*"(\w+)":\s*(.*?),?\s*(?:#\s*(.*))?$', l)
        if m:
            out.append([m.group(1), m.group(2).rstrip(","), (m.group(3) or "").strip()])
        else:
            c = re.match(r"\s*#\s*(.*)$", l)
            if c and out:
                out[-1][2] = (out[-1][2] + " " + c.group(1).strip()).strip()
    return out


def settings_page() -> str:
    from easymeasure.backend.service import DEFAULT_SETTINGS
    from easymeasure.remote.config import DEFAULTS
    be = _commented_dict(ROOT / "easymeasure/backend/service.py", "DEFAULT_SETTINGS")
    gw = _commented_dict(ROOT / "easymeasure/remote/config.py", "DEFAULTS")
    out = ["# Settings and files\n",
           "## Backend settings\n",
           "Kept in `<home>/backend_settings.json`. Change them in the GUI (*File → "
           "Settings*) or with [`set_settings`](commands.md#set_settings):\n",
           "```python\nc.call(\"set_settings\", data_dir=\"D:/data\", fsync_interval=0.5)\n```\n",
           "| setting | default | meaning |", "|---|---|---|"]
    for k, v, cmt in be:
        if k in DEFAULT_SETTINGS:
            out.append("| `%s` | %s | %s |" % (k, fmt_value(DEFAULT_SETTINGS[k]), md_escape(cmt)))
    out += ["", "## Remote gateway configuration\n",
            "Kept in `<home>/remote/config.json`; see [Remote access](../guide/remote.md). "
            "Show or change it with `python -m easymeasure.remote config [key=value …]` "
            "(JSON values).\n",
            "| key | default | meaning |", "|---|---|---|"]
    for k, v, cmt in gw:
        if k in DEFAULTS:
            out.append("| `%s` | %s | %s |" % (k, fmt_value(DEFAULTS[k]), md_escape(cmt)))
    out += ["", "## Files\n",
            "`<home>` is `~/.easymeasure` (override with the environment variable "
            "`EASYMEASURE_HOME`, or `--home` on the command line).\n",
            "| file | contents |", "|---|---|",
            "| `<home>/backend.json` | port and access token of the running backend (clients read it) |",
            "| `<home>/backend_settings.json` | backend settings |",
            "| `<home>/autosave_setup.json` | the setup, saved on every change (`--restore`) |",
            "| `<home>/data/<date>/<run>_<name>.emj` / `.h5` | data, unless `data_dir` is set |",
            "| `<home>/drivers/` | drivers made with the Driver Wizard (and your own) |",
            "| `<home>/logs/backend.log` | backend log (rotating) |",
            "| `<home>/logs/remote.log` | remote access audit log |",
            "| `<home>/remote/config.json`, `users.json` | gateway configuration, accounts (hashed passwords) |",
            "| `<home>/remote/tls/` | gateway certificates (self-signed, Let's Encrypt `acme/<domain>/`) |",
            "",
            "## Ports\n",
            "| port | used by |", "|---|---|",
            "| 47811 (localhost only) | backend ↔ GUIs and `BackendClient` (`--port`) |",
            "| 8443 (configurable, e.g. 443) | remote gateway, HTTPS / WSS |",
            "| 80 (a few seconds) | Let's Encrypt HTTP-01 challenge |", ""]
    return "\n".join(out) + "\n"


# -- command line ----------------------------------------------------------------------
CLI = [("Backend", "easymeasure.backend", ["--help"], "easymeasure-backend"),
       ("Desktop GUI", "easymeasure.frontend", ["--help"], "easymeasure"),
       ("Remote access", "easymeasure.remote", ["--help"], "easymeasure-remote"),
       ("Tray icon", "easymeasure.tray", ["--help"], None)]
REMOTE_SUB = ["serve", "adduser", "passwd", "role", "deluser", "users", "cert", "letsencrypt",
              "status", "config"]


def _help(module, args):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", COLUMNS="100")
    try:
        r = subprocess.run([sys.executable, "-m", module] + args, capture_output=True,
                           text=True, timeout=60, cwd=str(ROOT), env=env)
        return (r.stdout or r.stderr).rstrip()
    except Exception as exc:
        return "(could not run: %s)" % exc


def cli_page() -> str:
    out = ["# Command line\n",
           "Installed with easyMeasure: `easymeasure-backend`, `easymeasure` (the GUI) and "
           "`easymeasure-remote`; every program also runs as `python -m <module>`. The help "
           "texts below are generated from the programs.\n"]
    for title, mod, args, script in CLI:
        out.append("## %s\n" % title)
        out.append("`python -m %s`%s\n" % (mod, (" or `%s`" % script) if script else ""))
        out.append("```text\n%s\n```\n" % _help(mod, args))
        if mod == "easymeasure.remote":
            for sub in REMOTE_SUB:
                out.append("### `%s`\n" % sub)
                out.append("```text\n%s\n```\n" % _help(mod, [sub, "--help"]))
    return "\n".join(out) + "\n"


# -- Python API ------------------------------------------------------------------------
API_SKIP = ("__main__",)
API_INTRO = {
    "easymeasure.backend": "The backend process: instruments, channels, sweeps, queue, storage.",
    "easymeasure.client": "Blocking clients for scripts and notebooks.",
    "easymeasure.common": "Shared helpers: codec, safe expressions, N-D slicing, errors.",
    "easymeasure.drivers": "Instrument drivers, simulated instruments and emulators.",
    "easymeasure.frontend": "The PySide6 desktop GUI.",
    "easymeasure.remote": "The internet gateway, accounts and access policy.",
    "easymeasure.report": "Figures and PowerPoint slides.",
}


def api_pages():
    pkg = ROOT / "easymeasure"
    mods = []
    for path in sorted(pkg.rglob("*.py")):
        rel = path.relative_to(ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] in API_SKIP or "__pycache__" in parts:
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        mods.append((".".join(parts), path))
    nav = ["* [Overview](index.md)"]
    index = ["# Python API\n",
             "Every module of easyMeasure, generated from the docstrings. Start with "
             "[`easymeasure.backend.service`](easymeasure/backend/service.md) (the `Backend` "
             "class and its commands), [`easymeasure.client`](easymeasure/client.md) and "
             "[`easymeasure.backend.instrument`](easymeasure/backend/instrument.md) (the "
             "driver base class).\n"]
    last_pkg = None
    for name, path in mods:
        parts = name.split(".")
        doc_path = Path(*parts).with_suffix(".md")
        top = ".".join(parts[:2]) if len(parts) > 1 else name
        if top != last_pkg and top in API_INTRO:
            index.append("\n## `%s`\n\n%s\n" % (top, API_INTRO[top]))
            last_pkg = top
        index.append("* [`%s`](%s)" % (name, doc_path.as_posix()))
        indent = "    " * (len(parts) - 1)
        nav.append("%s* [%s](%s)" % (indent, parts[-1], doc_path.as_posix()))
        with _open(("reference/api" / doc_path).as_posix(), "w") as f:
            f.write("# `%s`\n\n::: %s\n    options:\n      show_root_heading: false\n"
                    "      show_submodules: false\n" % (name, name))
        _set_edit(("reference/api" / doc_path).as_posix(), path.relative_to(ROOT).as_posix())
    with _open("reference/api/index.md", "w") as f:
        f.write("\n".join(index) + "\n")
    with _open("reference/api/SUMMARY.md", "w") as f:
        f.write("\n".join(nav) + "\n")
    return [m for m, _ in mods]


# -- main ------------------------------------------------------------------------------
def generate():
    drivers = collect_drivers()
    with _open("instruments.md", "w") as f:
        f.write(instruments_page(drivers))
    hw = [n for n in HARDWARE_ORDER if n in drivers]
    hw += sorted(n for n in drivers if n not in hw and not n.startswith("sim_"))
    sim = sorted(n for n in drivers if n.startswith("sim_"))
    nav = ["* [Overview](index.md)", "* Hardware"]
    nav += ["    * [%s](%s.md)" % (n, n) for n in hw]
    nav += ["* Simulated"] + ["    * [%s](%s.md)" % (n, n) for n in sim]
    for n in hw + sim:
        with _open("reference/drivers/%s.md" % n, "w") as f:
            f.write(driver_page(n, drivers[n]))
        _set_edit("reference/drivers/%s.md" % n,
                  "easymeasure/drivers/%s.py" % drivers[n]["module"].split(".")[-1])
    with _open("reference/drivers/index.md", "w") as f:
        f.write("# Instrument drivers\n\nOne page per driver: interfaces, options, every "
                "channel and action, notes and the Python class. The overview with all "
                "instruments is [Supported instruments](../../instruments.md).\n\n"
                + "\n".join("* [`%s`](%s.md) – %s" % (n, n, md_escape(drivers[n]["description"]))
                            for n in hw + sim) + "\n")
    with _open("reference/drivers/SUMMARY.md", "w") as f:
        f.write("\n".join(nav) + "\n")
    with _open("reference/commands.md", "w") as f:
        f.write(commands_page())
    with _open("reference/expressions.md", "w") as f:
        f.write(expressions_page())
    with _open("reference/cli.md", "w") as f:
        f.write(cli_page())
    with _open("reference/settings.md", "w") as f:
        f.write(settings_page())
    api_pages()
    return drivers


if IN_MKDOCS:
    generate()
elif __name__ == "__main__":
    generate()
    for k in sorted(PAGES):
        print("%-48s %6d bytes" % (k, len(PAGES[k])))
