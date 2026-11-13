"""Merging part of a saved setup into the current one (no Qt, no backend calls).

A :class:`SetupMerge` holds the items of a setup file (instruments, parameters,
aliases, composite channels, user actions) next to the current setup (as the
backend describes it in ``get_state``).  The user selects items, may give them
new names, and :meth:`SetupMerge.validate` says, per item, whether merging it
works:

* **names**: one namespace for everything (instruments, aliases, composites and
  their channels, parameters, user actions): a name already used in the current
  setup is a conflict, unless the item there is identical (then it is simply
  skipped); two merged items may not get the same name either;
* **dependencies**: an alias needs its instrument, a formula the channels and
  parameters it uses - selected too, or already in the current setup;
* **consistency**: everything is checked by the backend's own rules, run here
  offline on a copy of the current setup (``ChannelManager`` with stand-in
  instruments): writable targets, formulas, circular references between
  composites or parameters, mappings that must be each other's inverse.

New names are carried through: renaming the instrument ``lockin`` to ``lockin2``
rewrites the targets of the merged aliases (``lockin.X`` -> ``lockin2.X``),
renaming an alias, composite channel or parameter rewrites the formulas that
use it and the names quoted in user actions (``get('Vxx')``; parameters also as
bare names in action code).  :meth:`SetupMerge.commands` lists the backend
commands that perform the merge, in an order that works.

Nothing here talks to the backend; the dialog (``merge_dialog.py``) sends the
commands.
"""

from __future__ import annotations

import copy
import io
import json
import re
import tokenize
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..backend.channels import Alias, ChannelManager
from ..backend.composites import composite_from_dict
from ..backend.instrument import ChannelSpec
from ..backend.parameters import Parameter, ordered
from ..backend.user_actions import UserAction
from ..common.errors import ChannelError, EasyMeasureError
from ..common.exprs import Expression, rename

FORMAT = "easyMeasure-setup-1"
#: the order of the selection pages (and of the merge)
KINDS = ("instruments", "parameters", "aliases", "composites", "actions")
TITLE = {"instruments": "Instruments", "parameters": "Parameters", "aliases": "Channels",
         "composites": "Composite channels", "actions": "Actions"}
SINGULAR = {"instruments": "instrument", "parameters": "parameter", "aliases": "alias",
            "composites": "composite", "actions": "action"}
_INSTRUMENT_NAME = re.compile(r"^[A-Za-z0-9_]+$")

# statuses of an item
UNSELECTED, NEW, SAME, WARNING, ERROR = "unselected", "new", "same", "warning", "error"


class MergeFileError(EasyMeasureError):
    """The file is not a setup that can be merged."""


@dataclass
class Item:
    """One item of the setup file."""
    kind: str
    name: str                       # its name in the file
    data: dict                      # its definition in the file
    selected: bool = False
    new_name: str = ""              # "" or the name it gets in the current setup
    status: str = UNSELECTED
    messages: List[str] = field(default_factory=list)
    #: (kind, name) of the items of the file it needs (filled by validate)
    needs: List[Tuple[str, str]] = field(default_factory=list)
    #: its definition as it will be merged (renames applied; filled by validate)
    merged: Optional[dict] = None

    @property
    def target(self) -> str:
        """The name it gets in the current setup."""
        return (self.new_name or "").strip() or self.name

    @property
    def renamed(self) -> bool:
        return self.target != self.name

    def summary(self) -> str:
        d = self.data
        if self.kind == "instruments":
            return "%s at %s" % (d.get("driver", "?"), d.get("address", "NONE"))
        if self.kind == "parameters":
            return ("= %s" % d["formula"]) if d.get("formula") else \
                ("%s %s" % (d.get("value"), d.get("unit") or "")).strip()
        if self.kind == "aliases":
            return "→ %s" % d.get("target", "?")
        if self.kind == "composites":
            t = d.get("type") or "multi"
            if t == "multi":
                vs = ", ".join(v.get("name", "?") if isinstance(v, dict) else str(v)
                               for v in d.get("variables") or [])
                return "multivariate (%s) → %s" % (vs, ", ".join(d.get("targets") or {}))
            if t == "read":
                return "read = %s" % d.get("expression", "")
            return "mapping of %s" % d.get("target", "?")
        if self.kind == "actions":
            return d.get("description") or (d.get("code") or "").strip().splitlines()[0][:60] \
                if (d.get("code") or "").strip() else ""
        return ""


# ---------------------------------------------------------------------------
# stand-in instruments for the offline check
# ---------------------------------------------------------------------------
class _OfflineHandle:
    """An instrument as the channel manager sees it, without I/O."""

    def __init__(self, name: str, channels: Dict[str, ChannelSpec], actions: Dict[str, object]):
        self.name = name
        self.channels = channels
        self.actions = actions

    def _last_of(self, channel: str) -> dict:
        return {"last": None, "last_t": None}


class _OfflineInstruments:
    """What :class:`ChannelManager` needs of the instruments: ``resolve``.  An
    instrument whose channels are not known yet (a dynamic driver that was never
    connected) resolves nothing, which the channel manager treats like an
    instrument that is offline: checked again when it is used."""

    def __init__(self):
        self.chans: Dict[str, Optional[Dict[str, ChannelSpec]]] = {}
        self.acts: Dict[str, Dict[str, object]] = {}

    def names(self) -> List[str]:
        return list(self.chans)

    def get(self, name: str) -> _OfflineHandle:
        if name not in self.chans:
            raise ChannelError("no instrument %r" % name)
        return _OfflineHandle(name, dict(self.chans[name] or {}), dict(self.acts.get(name, {})))

    def handles(self) -> List[_OfflineHandle]:
        return [self.get(n) for n in self.chans]

    def add(self, name: str, channels: Optional[Iterable[dict]],
            actions: Optional[Iterable[dict]] = None) -> None:
        from ..backend.instrument import ActionSpec
        self.acts[name] = {}
        for a in actions or []:
            try:
                sp = ActionSpec.from_dict(a)
                self.acts[name][sp.name] = sp
            except Exception:
                continue
        if channels is None:
            self.chans[name] = None
            return
        out = {}
        for c in channels:
            try:
                s = c if isinstance(c, ChannelSpec) else ChannelSpec.from_dict(c)
                out[s.name] = s
            except Exception:
                continue
        self.chans[name] = out or None

    def resolve(self, ref):
        if not isinstance(ref, str) or "." not in ref:
            raise ChannelError("channel reference must be 'instrument.channel', got %r" % (ref,))
        inst, ch = ref.split(".", 1)
        if inst not in self.chans:
            raise ChannelError("no instrument %r" % inst)
        chans = self.chans[inst]
        if chans is None or ch not in chans:
            raise ChannelError("instrument %s has no channel %r" % (inst, ch))
        return None, chans[ch]


# ---------------------------------------------------------------------------
# renaming inside definitions
# ---------------------------------------------------------------------------
def rename_formula(src: str, mapping: Dict[str, str], keep: Iterable[str] = ()) -> str:
    """*src* with the names in *mapping* replaced (the formula is left as written
    when none of them occurs).  Names in *keep* (a composite's own variables,
    ``x``/``y`` of a mapping) are never renamed."""
    m = {k: v for k, v in mapping.items() if k != v and k not in set(keep)}
    if not m or not str(src or "").strip():
        return src
    try:
        names = set(Expression(str(src)).names)
    except Exception:
        return src                    # invalid anyway: the check reports it
    if not names & set(m):
        return src
    return rename(str(src), {k: v for k, v in m.items() if k in names})


def rename_target(target: str, inst_map: Dict[str, str]) -> str:
    """``inst.channel`` with the instrument renamed."""
    if not isinstance(target, str) or "." not in target:
        return target
    inst, ch = target.split(".", 1)
    return "%s.%s" % (inst_map.get(inst, inst), ch)


def _string_literals(code: str) -> List[Tuple[str, Tuple[int, int], Tuple[int, int], str]]:
    """``(value, start, end, token text)`` of the plain string literals of *code*."""
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return out
    for t in toks:
        if t.type != tokenize.STRING:
            continue
        m = re.match(r"^([rRuU]?)('''|\"\"\"|'|\")(.*)\2$", t.string, re.S)
        if not m or "\\" in m.group(3):
            continue
        out.append((m.group(3), t.start, t.end, t.string))
    return out


def rename_code(code: str, chan_map: Dict[str, str], inst_map: Dict[str, str],
                param_map: Dict[str, str], action_map: Dict[str, str]) -> str:
    """Action code with the renamed things renamed, as far as they can be told
    apart from other text: string literals that are exactly a channel name
    (``'Vxx'``), ``'instrument.channel'``, an instrument name or an action name;
    bare names that are parameters (actions see the parameters as variables)."""
    if not (chan_map or inst_map or param_map or action_map) or not code:
        return code
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return code
    edits: List[Tuple[Tuple[int, int], Tuple[int, int], str]] = []
    strings = {(s, e): v for v, s, e, _ in _string_literals(code)}
    for t in toks:
        new = None
        if t.type == tokenize.STRING and (t.start, t.end) in strings:
            v = strings[(t.start, t.end)]
            if v in chan_map:
                nv = chan_map[v]
            elif "." in v and v.split(".", 1)[0] in inst_map:
                nv = rename_target(v, inst_map)
            elif v in inst_map:
                nv = inst_map[v]
            elif v in action_map:
                nv = action_map[v]
            else:
                nv = None
            if nv is not None and nv != v:
                new = t.string.replace(v, nv, 1) if t.string.count(v) == 1 else None
                if new is None:
                    q = t.string[-1]
                    new = q + nv + q
        elif t.type == tokenize.NAME and t.string in param_map:
            new = param_map[t.string]
        if new is not None and new != t.string:
            edits.append((t.start, t.end, new))
    if not edits:
        return code
    lines = code.splitlines(keepends=True)
    for (sr, sc), (er, ec), new in sorted(edits, reverse=True):
        if sr != er:
            continue                       # (multi-line strings are left alone)
        ln = lines[sr - 1]
        lines[sr - 1] = ln[:sc] + new + ln[ec:]
    return "".join(lines)


# ---------------------------------------------------------------------------
# normalised definitions (for "identical?")
# ---------------------------------------------------------------------------
def _comp_dict(c) -> dict:
    """A composite's definition without what is computed (a mapping's image, which
    needs the parameters' values)."""
    if c.type == "map":
        return {"type": "map", "name": c.name, "target": c.target, "forward": c.forward,
                "backward": c.backward, "min": c.min, "max": c.max, "unit": c.unit,
                "ramp_rate": c.ramp_rate, "tolerance": c.tolerance,
                "description": c.description}
    return c.to_dict()


def _norm(kind: str, d: dict):
    if kind == "instruments":
        return {"driver": d.get("driver"), "address": str(d.get("address") or "NONE"),
                "options": dict(d.get("options") or {})}
    if kind == "parameters":
        return Parameter.from_dict(d).to_dict()
    if kind == "aliases":
        return Alias.from_dict(d).to_dict()
    if kind == "composites":
        return _comp_dict(composite_from_dict(d)[0])
    if kind == "actions":
        return UserAction.from_dict(d).to_dict()
    raise KeyError(kind)


def _same(kind: str, a: dict, b: dict) -> bool:
    try:
        return _norm(kind, a) == _norm(kind, b)
    except Exception:
        return False


def _composite_provides(d: dict) -> List[str]:
    """The channel names a composite definition provides (best effort)."""
    try:
        return composite_from_dict(d)[0].channels()
    except Exception:
        t = d.get("type") or "multi"
        if t == "multi":
            return ["%s.%s" % (d.get("name", ""), v.get("name") if isinstance(v, dict) else v)
                    for v in d.get("variables") or [] if v]
        return [d.get("name", "")]


# ---------------------------------------------------------------------------
# the merge
# ---------------------------------------------------------------------------
class SetupMerge:
    """The items of a setup file, what is selected, and whether merging works.

    *current*: the current setup ``{instruments: [{name, driver, address, options,
    channels}], parameters, aliases, composites, actions}`` (see :meth:`from_state`).
    *drivers*: the backend's drivers (``list_drivers()["drivers"]``), to check
    that a merged instrument's driver exists and to know its channels; ``None``:
    not checked.
    """

    def __init__(self, source: dict, current: dict, drivers: Optional[dict] = None):
        if not isinstance(source, dict) or source.get("format") != FORMAT:
            raise MergeFileError("this is not an easyMeasure setup file")
        self.source = copy.deepcopy(source)
        self.current = {k: list((current or {}).get(k) or []) for k in KINDS}
        self.drivers = drivers
        self.items: Dict[str, List[Item]] = {k: [] for k in KINDS}
        self.file_problems: List[str] = []
        for k in KINDS:
            seen = set()
            for d in self.source.get(k) or []:
                if not isinstance(d, dict) or not isinstance(d.get("name"), str):
                    self.file_problems.append("%s: an entry without a name was left out"
                                              % TITLE[k])
                    continue
                if d["name"] in seen:
                    self.file_problems.append("%s: %s appears twice; the second one was left "
                                              "out" % (TITLE[k], d["name"]))
                    continue
                seen.add(d["name"])
                self.items[k].append(Item(k, d["name"], copy.deepcopy(d)))
        self.warnings: List[str] = []
        self.errors: List[str] = []

    # -- construction from what the front end has --------------------------------
    @classmethod
    def from_state(cls, source: dict, state: dict, drivers: Optional[dict] = None
                   ) -> "SetupMerge":
        """*state*: the backend's ``get_state`` (instruments, channels, actions)."""
        ch = state.get("channels") or {}
        cur = {
            "instruments": [{"name": i.get("name"), "driver": i.get("driver"),
                             "address": i.get("address"), "options": i.get("options") or {},
                             "channels": i.get("channels")}
                            for i in state.get("instruments") or []],
            "parameters": [{k: p.get(k) for k in ("name", "value", "formula", "unit",
                                                  "description") if p.get(k) is not None}
                           for p in ch.get("parameters") or []],
            "aliases": list(ch.get("aliases") or []),
            "composites": list(ch.get("composites") or []),
            "actions": [{k: a.get(k) for k in ("name", "code", "description", "confirm")}
                        for a in state.get("actions") or []],
        }
        return cls(source, cur, drivers)

    @classmethod
    def from_file(cls, path, state: dict, drivers: Optional[dict] = None) -> "SetupMerge":
        try:
            with open(path, "r", encoding="utf-8") as f:
                src = json.load(f)
        except (OSError, ValueError) as exc:
            raise MergeFileError("cannot read %s: %s" % (path, exc)) from None
        return cls.from_state(src, state, drivers)

    # -- access ---------------------------------------------------------------------
    def item(self, kind: str, name: str) -> Item:
        for it in self.items[kind]:
            if it.name == name:
                return it
        raise KeyError("%s %s" % (kind, name))

    def selected(self, kind: Optional[str] = None) -> List[Item]:
        kinds = [kind] if kind else KINDS
        return [it for k in kinds for it in self.items[k] if it.selected]

    def select(self, kind: str, name: str, on: bool = True) -> None:
        self.item(kind, name).selected = bool(on)

    def select_with_needs(self, kind: str, name: str) -> List[Tuple[str, str]]:
        """Select an item and, recursively, the items of the file it needs (unless the
        current setup has them already).  Returns what was selected in addition."""
        added: List[Tuple[str, str]] = []
        todo = [(kind, name)]
        while todo:
            k, n = todo.pop()
            it = self.item(k, n)
            if not it.selected:
                it.selected = True
                if (k, n) != (kind, name):
                    added.append((k, n))
            for nk, nn in self._needs(it):
                dep = self.item(nk, nn)
                if not dep.selected and not self._present_identical(dep):
                    todo.append((nk, nn))
        return added

    def needed_by(self, kind: str, name: str) -> List[str]:
        """The selected items that use the item *name* of *kind* (an unselected item
        they need is only fine if this setup has one of that name)."""
        return ["%s %s" % (SINGULAR[it.kind], it.name) for k in KINDS for it in self.items[k]
                if it.selected and (kind, name) in it.needs]

    def rename(self, kind: str, name: str, new_name: str) -> None:
        self.item(kind, name).new_name = (new_name or "").strip()

    # -- name maps ----------------------------------------------------------------------
    def _maps(self):
        inst = {it.name: it.target for it in self.items["instruments"] if it.selected}
        params = {it.name: it.target for it in self.items["parameters"] if it.selected}
        chans = dict(params)
        chans.update({it.name: it.target for it in self.items["aliases"] if it.selected})
        comps = {}
        for it in self.items["composites"]:
            if it.selected:
                comps[it.name] = it.target
                if (it.data.get("type") or "multi") != "multi":
                    chans[it.name] = it.target     # read / map: the composite is the channel
                elif it.renamed:                   # its variables: name.variable
                    for v in it.data.get("variables") or []:
                        short = v.get("name") if isinstance(v, dict) else str(v)
                        chans["%s.%s" % (it.name, short)] = "%s.%s" % (it.target, short)
        acts = {it.name: it.target for it in self.items["actions"] if it.selected}
        return inst, params, chans, comps, acts

    def merged_definition(self, it: Item) -> dict:
        """*it*'s definition with the new names of everything selected applied."""
        inst, params, chans, comps, acts = self._maps()
        d = copy.deepcopy(it.data)
        d["name"] = it.target
        if it.kind == "instruments":
            return {k: d.get(k) for k in ("name", "driver", "address", "options")}
        if it.kind == "parameters":
            if d.get("formula"):
                d["formula"] = rename_formula(d["formula"], params)
            return d
        if it.kind == "aliases":
            d["target"] = rename_target(d.get("target", ""), inst)
            return d
        if it.kind == "composites":
            t = d.get("type")
            if t == "multi":
                own = [v.get("name") if isinstance(v, dict) else str(v)
                       for v in d.get("variables") or []]
                targets = {}
                for tg, f in (d.get("targets") or {}).items():
                    new = chans.get(tg, tg)
                    if new in targets:
                        raise ChannelError("two of its targets would both be %s after the new "
                                           "names" % new)
                    targets[new] = rename_formula(f, chans, keep=own)
                d["targets"] = targets
            elif t == "read":
                d["expression"] = rename_formula(d.get("expression", ""), chans)
            elif t == "map":
                d["target"] = chans.get(d.get("target"), d.get("target"))
                d["forward"] = rename_formula(d.get("forward", ""), params, keep=["x"])
                d["backward"] = rename_formula(d.get("backward", ""), params, keep=["y"])
                d.pop("image", None)
            return d
        if it.kind == "actions":
            d["code"] = rename_code(d.get("code") or "", chans, inst, params, acts)
            return {k: d.get(k) for k in ("name", "code", "description", "confirm")}
        return d

    # -- what exists ------------------------------------------------------------------
    def _current_names(self) -> Dict[str, Dict[str, str]]:
        """Per namespace: name -> what it is, in the current setup."""
        insts = {i["name"]: "the instrument %s (%s)" % (i["name"], i.get("driver"))
                 for i in self.current["instruments"] if i.get("name")}
        chans: Dict[str, str] = {}
        for p in self.current["parameters"]:
            chans[p["name"]] = "the parameter %s" % p["name"]
        for a in self.current["aliases"]:
            chans[a["name"]] = "the alias %s (→ %s)" % (a["name"], a.get("target"))
        for c in self.current["composites"]:
            for ch in _composite_provides(c):
                chans.setdefault(ch, "a channel of the composite %s" % c.get("name"))
            chans.setdefault(c.get("name"), "the composite %s" % c.get("name"))
        acts = {a["name"]: "the user action %s" % a["name"] for a in self.current["actions"]
                if a.get("name")}
        return {"instruments": insts, "channels": chans, "actions": acts}

    def _current_item(self, kind: str, name: str) -> Optional[dict]:
        for d in self.current[kind]:
            if d.get("name") == name:
                return d
        return None

    def _present(self, kind: str, name: str) -> bool:
        """Is there something called *name* the item of *kind* would provide?"""
        names = self._current_names()
        if kind == "instruments":
            return name in names["instruments"]
        if kind == "actions":
            return name in names["actions"]
        return name in names["channels"]

    def _present_identical(self, it: Item) -> bool:
        """Does the current setup have *it* (under its name in the file) unchanged?"""
        old = self._current_item(it.kind, it.name)
        return old is not None and _same(it.kind, old, it.data)

    # -- dependencies within the file ---------------------------------------------------
    def _file_providers(self) -> Dict[str, Tuple[str, str]]:
        """Channel name in the file -> (kind, item name) of the item providing it."""
        out: Dict[str, Tuple[str, str]] = {}
        for it in self.items["parameters"]:
            out[it.name] = ("parameters", it.name)
        for it in self.items["aliases"]:
            out.setdefault(it.name, ("aliases", it.name))
        for it in self.items["composites"]:
            for ch in _composite_provides(it.data):
                out.setdefault(ch, ("composites", it.name))
        return out

    def _needs(self, it: Item) -> List[Tuple[str, str]]:
        """The items of the file *it* uses (by their names in the file)."""
        prov = self._file_providers()
        insts = {i.name for i in self.items["instruments"]}
        out: List[Tuple[str, str]] = []

        def add(name):
            p = prov.get(name)
            if p is not None and p != (it.kind, it.name) and p not in out:
                out.append(p)

        d = it.data
        try:
            if it.kind == "aliases":
                inst = str(d.get("target", "")).split(".", 1)[0]
                if inst in insts:
                    out.append(("instruments", inst))
            elif it.kind == "parameters" and d.get("formula"):
                for n in Expression(d["formula"]).free_names:
                    add(n)
            elif it.kind == "composites":
                c = composite_from_dict(d)[0]
                names = set(c.references())
                if c.type == "read":
                    names |= set(c.expr.free_names)
                for n in sorted(names):
                    add(n)
            elif it.kind == "actions":
                for v, _, _, _ in _string_literals(d.get("code") or ""):
                    if v in prov:
                        add(v)
                    elif "." in v and v.split(".", 1)[0] in insts:
                        p = ("instruments", v.split(".", 1)[0])
                        if p not in out:
                            out.append(p)
        except Exception:
            pass                               # an invalid definition: reported elsewhere
        return out

    # -- validation -------------------------------------------------------------------
    def validate(self) -> bool:
        """Check the selection; fills every item's ``status`` and ``messages`` (and
        ``merged``).  True when the selected items can be merged."""
        self.errors, self.warnings = [], []
        for k in KINDS:
            for it in self.items[k]:
                it.status, it.messages, it.merged = UNSELECTED, [], None
                it.needs = self._needs(it)
        cur_names = self._current_names()
        prov = self._file_providers()

        def err(it: Item, msg: str) -> None:
            it.status = ERROR
            if msg not in it.messages:
                it.messages.append(msg)

        def warn(it: Item, msg: str) -> None:
            if it.status != ERROR:
                it.status = WARNING
            if msg not in it.messages:
                it.messages.append(msg)

        # 1. the definitions as they will be merged; identical items; name conflicts
        skip: Set[Tuple[str, str]] = set()       # selected, but already there identically
        taken: Dict[Tuple[str, str], Item] = {}  # (namespace, new name) -> merged item
        # one namespace: instruments, channels, composites, parameters, user actions
        cur_names["all"] = {**cur_names["instruments"], **cur_names["actions"],
                            **cur_names["channels"]}
        for k in KINDS:
            ns = "all"
            for it in self.items[k]:
                if not it.selected:
                    continue
                it.status = NEW
                try:
                    it.merged = self.merged_definition(it)
                    _norm(k, it.merged)            # valid on its own?
                except Exception as exc:
                    err(it, "invalid: %s" % exc)
                    continue
                if k == "instruments" and not _INSTRUMENT_NAME.match(it.target):
                    err(it, "invalid name %r: use letters, digits and _" % it.target)
                    continue
                old = self._current_item(k, it.target)
                if old is not None and _same(k, old, it.merged):
                    it.status = SAME
                    it.messages.append("already in this setup, identical: nothing to merge")
                    skip.add((k, it.name))
                    continue
                names = [it.target]
                if k == "composites":
                    names += [n for n in _composite_provides(it.merged) if n != it.target]
                for n in names:
                    if n in cur_names[ns]:
                        what = cur_names[ns][n]
                        err(it, "%s is already %s in this setup: give it another name, or "
                                "leave it out to use the existing one" % (n, what)
                            if n == it.target else
                            "its channel %s is already %s in this setup" % (n, what))
                    other = taken.get((ns, n))
                    if other is not None and other is not it:
                        err(it, "%s would also be the name of the merged %s %s"
                            % (n, SINGULAR[other.kind], other.name))
                    taken.setdefault((ns, n), it)

        def ok(it: Item) -> bool:
            return it.selected and it.status != ERROR and (it.kind, it.name) not in skip

        # 2. dependencies: needed items of the file that are not selected and not here
        _, _, chans, _, _ = self._maps()
        for k in KINDS:
            for it in self.items[k]:
                if not it.selected or (k, it.name) in skip:
                    continue
                for nk, nn in it.needs:
                    dep = self.item(nk, nn)
                    if dep.selected:
                        continue
                    # not selected: the current setup must have it under the same name
                    if self._present(nk, nn) or (nk == "composites" and any(
                            self._present(nk, ch) for ch in _composite_provides(dep.data))):
                        if not self._present_identical(dep):
                            old = self._current_names()[
                                "instruments" if nk == "instruments" else "channels"].get(nn)
                            warn(it, "uses %s, which here is %s - not the same as in the file "
                                     "(select the file's one, under another name, if that is "
                                     "the one meant)" % (nn, old or "something else"))
                        continue
                    msg = "needs the %s %s, which is not selected (%s page) and not in this " \
                          "setup" % (SINGULAR[nk], nn, TITLE[nk])
                    if k == "actions":
                        warn(it, msg)
                    else:
                        err(it, msg)

        # 3. the backend's own rules, offline on a copy of the current setup
        inst = _OfflineInstruments()
        for i in self.current["instruments"]:
            inst.add(i["name"], i.get("channels"), i.get("actions"))
        cm = ChannelManager(inst)
        cur_params = []
        for p in self.current["parameters"]:
            try:
                cur_params.append(Parameter.from_dict(p))
            except Exception:
                pass
        try:
            cm.load_parameters(cur_params)
        except Exception as exc:
            self.warnings.append("the current parameters do not check out: %s" % exc)
        for a in self.current["aliases"]:
            try:
                cm.add_alias(Alias.from_dict(a), replace=True)
            except Exception as exc:
                self.warnings.append("current alias %s: %s" % (a.get("name"), exc))
        todo = list(self.current["composites"])
        while todo:
            left = []
            for c in todo:
                try:
                    cm.add_composite(composite_from_dict(c)[0], replace=True)
                except Exception:
                    left.append(c)
            if len(left) == len(todo):
                for c in left:
                    self.warnings.append("current composite %s does not check out"
                                         % c.get("name"))
                break
            todo = left

        # instruments
        for it in self.items["instruments"]:
            if not ok(it):
                continue
            drv = it.merged.get("driver")
            if self.drivers is not None and drv not in self.drivers:
                err(it, "the driver %s is not available on this backend" % drv)
                continue
            chans_known = None
            if self.drivers is not None:
                chans_known = (self.drivers.get(drv) or {}).get("channels") or None
            inst.add(it.target, chans_known)
            addr = str(it.merged.get("address") or "NONE")
            if addr.upper() not in ("NONE", "") and not addr.upper().startswith("SIM::"):
                users = [i["name"] for i in self.current["instruments"]
                         if str(i.get("address") or "").upper() == addr.upper()]
                if users:
                    warn(it, "the address %s is already used by %s: two instruments on one "
                             "port only work if it can be shared" % (addr, ", ".join(users)))
        merged_insts = {it.target for it in self.items["instruments"] if ok(it)} | \
            {i["name"] for i in self.current["instruments"]}
        # instruments merged identically are there already

        # parameters: all at once (formulas in any order), a bad one is left out
        params = [it for it in self.items["parameters"] if ok(it)]
        objs = {}
        for it in params:
            try:
                objs[it.target] = (it, Parameter.from_dict(it.merged))
            except Exception as exc:
                err(it, str(exc))
        while True:
            try:
                cm.load_parameters(cur_params + [p for _, p in objs.values()])
                break
            except Exception as exc:
                bad = getattr(exc, "parameter", None)
                if bad in objs:
                    err(objs.pop(bad)[0], str(exc))
                    continue
                for it, _ in objs.values():
                    err(it, str(exc))
                objs = {}
                try:
                    cm.load_parameters(cur_params)
                except Exception:
                    pass
                break

        # aliases
        for it in self.items["aliases"]:
            if not ok(it):
                continue
            a = Alias.from_dict(it.merged)
            iname = a.target.split(".", 1)[0]
            if iname not in merged_insts:
                if it.status != ERROR:
                    err(it, "its instrument %s is neither selected nor in this setup" % iname)
                continue
            chans_known = inst.chans.get(iname)
            if chans_known is not None and a.target.split(".", 1)[1] not in chans_known:
                err(it, "the instrument %s has no channel %s (it has %s)" % (
                    iname, a.target.split(".", 1)[1], ", ".join(sorted(chans_known))))
                continue
            if chans_known is None:
                warn(it, "the channels of %s are known once it is connected: %s is checked "
                         "then" % (iname, a.target))
            try:
                cm.add_alias(a)
            except Exception as exc:
                err(it, str(exc))

        # composites: in passes (they may use each other)
        todo_c = [it for it in self.items["composites"] if ok(it)]
        last_err: Dict[str, str] = {}
        while todo_c:
            left = []
            for it in todo_c:
                try:
                    c, notes = composite_from_dict(it.merged)
                    w = cm.add_composite(c)
                    for m in list(notes) + list(w or []):
                        warn(it, m)
                except Exception as exc:
                    last_err[it.name] = str(exc)
                    left.append(it)
            if len(left) == len(todo_c):
                for it in left:
                    err(it, last_err[it.name])
                break
            todo_c = left

        # actions: valid code; names it quotes that will not exist
        all_chans = {p.name for p in cm.parameters()} | {a.name for a in cm.aliases()} | \
            {ch for c in cm.composites() for ch in c.channels()}
        file_chans = set(prov)
        file_insts = {i.name for i in self.items["instruments"]}
        for it in self.items["actions"]:
            if not ok(it):
                continue
            missing = []
            for v, _, _, _ in _string_literals(it.merged.get("code") or ""):
                if v in all_chans:
                    continue
                if v in file_chans or v in set(chans.values()):
                    missing.append(v)
                elif "." in v and (v.split(".", 1)[0] in file_insts or
                                   v.split(".", 1)[0] in merged_insts):
                    if v.split(".", 1)[0] not in merged_insts:
                        missing.append(v)
            for v in sorted(set(missing)):
                warn(it, "its code uses %r, which will not exist after the merge" % v)

        # dependents of failed items say why
        failed = {(it.kind, it.name) for k in KINDS for it in self.items[k]
                  if it.selected and it.status == ERROR}
        for k in KINDS:
            for it in self.items[k]:
                if not it.selected or it.status == SAME:
                    continue
                bad = [n for n in it.needs if n in failed]
                for nk, nn in bad:
                    msg = "uses the %s %s, which cannot be merged as it is" % (SINGULAR[nk], nn)
                    if k == "actions":
                        warn(it, msg)
                    elif it.status != ERROR:
                        err(it, msg)
        self._cm = cm
        for k in KINDS:
            for it in self.items[k]:
                if it.selected and it.status == ERROR:
                    self.errors.extend("%s %s: %s" % (SINGULAR[k], it.name, m)
                                       for m in it.messages)
                elif it.selected and it.status == WARNING:
                    self.warnings.extend("%s %s: %s" % (SINGULAR[k], it.name, m)
                                         for m in it.messages)
        return not self.errors

    @property
    def can_merge(self) -> bool:
        return not self.errors and any(it.status in (NEW, WARNING) for it in self.selected())

    # -- the commands -----------------------------------------------------------------
    def commands(self) -> List[Tuple[str, dict]]:
        """The backend commands that merge the selection (call :meth:`validate`
        first; raises if it found errors)."""
        if self.errors:
            raise MergeFileError("the selection has problems: %s" % "; ".join(self.errors))
        out: List[Tuple[str, dict]] = []
        todo = {k: [it for it in self.items[k] if it.selected and it.status in (NEW, WARNING)]
                for k in KINDS}
        for it in todo["instruments"]:
            m = it.merged
            out.append(("add_instrument", {"name": m["name"], "driver": m["driver"],
                                           "address": m.get("address") or "NONE",
                                           "options": dict(m.get("options") or {}),
                                           "connect": True}))
        byname = {it.target: it for it in todo["parameters"]}
        if byname:
            for p in ordered([Parameter.from_dict(it.merged) for it in byname.values()]):
                d = p.to_dict()
                out.append(("define_parameter", {"name": d["name"], "value": d.get("value"),
                                                 "formula": d.get("formula", ""),
                                                 "unit": d.get("unit", ""),
                                                 "description": d.get("description", "")}))
        for it in todo["aliases"]:
            out.append(("define_alias", dict(Alias.from_dict(it.merged).to_dict())))
        # composites in an order where each one's channels exist before it
        cm = getattr(self, "_cm", None)
        order = [c.name for c in cm.composites()] if cm is not None else []
        comps = sorted(todo["composites"],
                       key=lambda it: order.index(it.target) if it.target in order else 1e9)
        for it in comps:
            out.append(("define_composite", _comp_dict(composite_from_dict(it.merged)[0])))
        for it in todo["actions"]:
            a = UserAction.from_dict(it.merged).to_dict()
            out.append(("define_user_action", a))
        return out

    def report(self) -> str:
        """A readable summary of the checked selection."""
        lines = []
        for k in KINDS:
            sel = [it for it in self.items[k] if it.selected]
            if not sel:
                continue
            lines.append("%s:" % TITLE[k])
            for it in sel:
                head = it.name if not it.renamed else "%s → %s" % (it.name, it.target)
                mark = {NEW: "✔", SAME: "=", WARNING: "⚠", ERROR: "✘"}.get(it.status, " ")
                lines.append("  %s %s%s" % (mark, head, ("  -  " + "; ".join(it.messages))
                                            if it.messages else ""))
        for p in self.file_problems:
            lines.append("⚠ file: %s" % p)
        for w in self.warnings:
            if not any(w.endswith(m) for k in KINDS for it in self.items[k] for m in it.messages):
                lines.append("⚠ %s" % w)
        return "\n".join(lines) if lines else "nothing selected"
