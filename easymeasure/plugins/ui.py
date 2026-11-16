"""The widgets a plugin builds its panels and data tabs from.

A plugin never draws anything itself: it describes its panels as a tree of the
widgets below - plain data, so every front end (the desktop GUI, the web page)
renders the same panel, also one connected later or over the internet.  It then
changes the widgets' properties (:meth:`PluginUI.set`) and hears of what the user
does with them (``on_click=…``, ``on_change=…``, ``on_turn=…`` callbacks, or
:meth:`~easymeasure.plugins.Plugin.on_event`).

Every widget is a dict ``{"type", "id", "props", "children", "events"}``.  An
``id`` is needed for widgets that change or that the user operates; it is unique
within the plugin.  Properties that change are set by id::

    self.ui.set("ch1", value="-1.2345")
    self.ui.set("sens", selected=[2, 0, 1])

Layout
    :func:`row`, :func:`column`, :func:`group` (a framed box with a title),
    :func:`spacer`
Display
    :func:`label`, :func:`seg7` (seven-segment digits), :func:`led`,
    :func:`ledlist` (one LED per option: which one is lit shows a setting),
    :func:`bar` (bar graph), :func:`plot` (curves)
Controls
    :func:`button`, :func:`knob` (turned by the wheel, dragging or the arrow keys),
    :func:`select` (a list of choices), :func:`number` (a number field)

Colours are names (``red``, ``green``, ``amber``, ``blue``, ``white``) or
``#rrggbb``.
"""

from __future__ import annotations

import math
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Union

#: widget type -> (events it raises, properties a plugin may set)
WIDGETS: Dict[str, tuple] = {
    "row": ((), {"gap", "align", "visible"}),
    "column": ((), {"gap", "align", "visible"}),
    "group": ((), {"title", "gap", "visible", "dark"}),
    "spacer": ((), {"size"}),
    "label": ((), {"text", "size", "bold", "color", "align", "visible"}),
    "seg7": ((), {"value", "digits", "color", "size", "label", "unit", "visible"}),
    "led": ((), {"on", "color", "label", "visible"}),
    "ledlist": ((), {"options", "selected", "color", "orient", "label", "visible"}),
    "bar": ((), {"value", "min", "max", "color", "label", "visible"}),
    "plot": ((), {"title", "xlabel", "ylabel", "series", "visible"}),
    "button": (("click",), {"text", "label", "enabled", "color", "visible", "lit"}),
    "knob": (("turn",), {"label", "enabled", "visible"}),
    "select": (("change",), {"options", "value", "label", "enabled", "visible"}),
    "number": (("change",), {"value", "label", "unit", "min", "max", "step", "decimals",
                             "enabled", "visible"}),
}
CONTAINERS = {"row", "column", "group"}
Handler = Callable[..., Any]
Widget = Dict[str, Any]


def _w(kind: str, id: Optional[str], props: dict, children=(), handlers=None) -> Widget:
    w: Widget = {"type": kind, "id": id, "props": {k: v for k, v in props.items()
                                                    if v is not None}}
    if children:
        w["children"] = [c for c in children if c is not None]
    if handlers:
        w["_handlers"] = {e: f for e, f in handlers.items() if f is not None}
    return w


# ----------------------------------------------------------------- layout --
def row(*children: Widget, id: Optional[str] = None, gap: int = 8,
        align: str = "start") -> Widget:
    """Children side by side (*align*: ``start``, ``center``, ``end``, ``stretch``)."""
    return _w("row", id, {"gap": gap, "align": align}, children)


def column(*children: Widget, id: Optional[str] = None, gap: int = 6,
           align: str = "start") -> Widget:
    """Children one below the other."""
    return _w("column", id, {"gap": gap, "align": align}, children)


def group(title: str, *children: Widget, id: Optional[str] = None, gap: int = 6,
          dark: bool = False) -> Widget:
    """A framed box with a *title*; its children one below the other.  *dark*: the
    dark face of an instrument front panel."""
    return _w("group", id, {"title": title, "gap": gap, "dark": dark}, children)


def spacer(size: int = 0) -> Widget:
    """Empty space: *size* pixels, or 0 to take up what is left."""
    return _w("spacer", None, {"size": size})


# ---------------------------------------------------------------- display --
def label(text: str, id: Optional[str] = None, size: str = "normal", bold: bool = False,
          color: Optional[str] = None, align: str = "left") -> Widget:
    """Text (*size*: ``small``, ``normal``, ``large``)."""
    return _w("label", id, {"text": text, "size": size, "bold": bold, "color": color,
                            "align": align})


def seg7(id: str, value: str = "", digits: int = 6, color: str = "red", size: int = 32,
         label: Optional[str] = None, unit: Optional[str] = None) -> Widget:
    """A seven-segment display of *digits* digits, *size* pixels high.  *value* is
    text: digits, ``-``, ``.``, blanks and the letters a seven-segment display can
    show (``E``, ``r``, ``o``, ``L``, ``H``, ``P`` …); it is right-aligned.  *unit*
    is shown beside it, *label* below."""
    return _w("seg7", id, {"value": value, "digits": int(digits), "color": color,
                           "size": int(size), "label": label, "unit": unit})


def led(id: str, label: str = "", on: bool = False, color: str = "red") -> Widget:
    """One LED with a *label* beside it."""
    return _w("led", id, {"label": label, "on": bool(on), "color": color})


def ledlist(id: str, options: Sequence[str], selected: Union[int, Sequence[int], None] = None,
            color: str = "red", orient: str = "vertical", label: Optional[str] = None) -> Widget:
    """One LED per option, the *selected* one(s) lit (an index, a list of indices,
    or None).  Several ledlists side by side show a value in parts, as an
    instrument's front panel does: mantissa, decade, unit."""
    return _w("ledlist", id, {"options": [str(o) for o in options], "selected": selected,
                              "color": color, "orient": orient, "label": label})


def bar(id: str, value: float = 0.0, min: float = 0.0, max: float = 1.0, color: str = "red",
        label: Optional[str] = None) -> Widget:
    """A bar graph: filled from 0 (from the centre when *min* < 0) to *value*."""
    return _w("bar", id, {"value": float(value), "min": float(min), "max": float(max),
                          "color": color, "label": label})


def plot(id: str, title: str = "", xlabel: str = "", ylabel: str = "",
         series: Optional[List[dict]] = None) -> Widget:
    """Curves: *series* is a list of ``{"name", "x": [...], "y": [...]}``; set it again
    to update the plot."""
    return _w("plot", id, {"title": title, "xlabel": xlabel, "ylabel": ylabel,
                           "series": list(series or [])})


# --------------------------------------------------------------- controls --
def button(id: str, text: str, on_click: Optional[Handler] = None, label: Optional[str] = None,
           color: Optional[str] = None, enabled: bool = True) -> Widget:
    """A push button; *label* is a small caption above it (as printed on a panel).
    ``on_click()`` runs on the plugin's thread."""
    return _w("button", id, {"text": text, "label": label, "color": color, "enabled": enabled},
              handlers={"click": on_click})


def knob(id: str, label: Optional[str] = None, on_turn: Optional[Handler] = None,
         enabled: bool = True) -> Widget:
    """A rotary knob without end stops: ``on_turn(steps)`` with +1 / -1 per click
    (the mouse wheel, dragging, the arrow keys)."""
    return _w("knob", id, {"label": label, "enabled": enabled}, handlers={"turn": on_turn})


def select(id: str, options: Sequence[str], value: Optional[str] = None,
           label: Optional[str] = None, on_change: Optional[Handler] = None,
           enabled: bool = True) -> Widget:
    """A list of choices: ``on_change(value)``."""
    return _w("select", id, {"options": [str(o) for o in options], "value": value,
                             "label": label, "enabled": enabled},
              handlers={"change": on_change})


def number(id: str, value: float = 0.0, label: Optional[str] = None, unit: Optional[str] = None,
           min: Optional[float] = None, max: Optional[float] = None, step: float = 1.0,
           decimals: int = 3, on_change: Optional[Handler] = None,
           enabled: bool = True) -> Widget:
    """A number field: ``on_change(value)`` when the user enters a value."""
    return _w("number", id, {"value": value, "label": label, "unit": unit, "min": min,
                             "max": max, "step": step, "decimals": int(decimals),
                             "enabled": enabled}, handlers={"change": on_change})


# ------------------------------------------------------------ the plugin UI --
class UIError(ValueError):
    pass


def _clean(value: Any) -> Any:
    """Plain data a front end can show (numbers that are not finite become None)."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int,)):
        return int(value)
    if isinstance(value, float) or hasattr(value, "__float__") and not hasattr(value, "__len__"):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)) or hasattr(value, "tolist"):
        seq = value.tolist() if hasattr(value, "tolist") else value
        return [_clean(v) for v in seq]
    return str(value)


class PluginUI:
    """A plugin's panels and data tabs (``self.ui`` of a plugin).

    ``add_panel(id, title, root)`` adds a tab beside Instruments, Sweep editor and
    Queue; ``add_tab(id, title, root)`` one beside Live data.  Both can be removed
    again.  ``set(widget_id, **props)`` changes a widget's properties; the front ends
    get the changes together, a few times a second."""

    def __init__(self, changed: Callable[[], None], updated: Callable[[str, dict], None]):
        self._panels: List[dict] = []
        self._widgets: Dict[str, Widget] = {}
        self._handlers: Dict[str, Dict[str, Handler]] = {}
        self._changed = changed          # the panels changed (layout)
        self._updated = updated          # properties of one widget changed
        self._lock = threading.RLock()   # front ends read it from other threads

    # -- panels --------------------------------------------------------------
    def add_panel(self, id: str, title: str, root: Widget) -> None:
        """A panel beside Instruments, Sweep editor and Queue."""
        self._add(id, title, root, "control")

    def add_tab(self, id: str, title: str, root: Widget) -> None:
        """A tab beside Live data (plots, tables of the plugin's own results)."""
        self._add(id, title, root, "data")

    def remove(self, id: str) -> None:
        """Remove a panel or a tab."""
        with self._lock:
            keep = [p for p in self._panels if p["id"] != id]
            if len(keep) == len(self._panels):
                raise UIError("no panel %r" % id)
            self._panels = keep
            self._reindex()
        self._changed()

    def clear(self) -> None:
        with self._lock:
            self._panels = []
            self._reindex()
        self._changed()

    def _add(self, id: str, title: str, root: Widget, where: str) -> None:
        if not isinstance(id, str) or not id:
            raise UIError("a panel needs an id")
        if any(p["id"] == id for p in self._panels):
            raise UIError("panel %r exists already" % id)
        panel = {"id": id, "title": str(title), "where": where, "root": root}
        with self._lock:
            old = self._panels
            self._panels = old + [panel]
            try:
                self._reindex()
            except Exception:
                self._panels = old
                self._reindex()
                raise
        self._changed()

    def _reindex(self) -> None:
        widgets: Dict[str, Widget] = {}
        handlers: Dict[str, Dict[str, Handler]] = {}

        def walk(w, path):
            if not isinstance(w, dict) or w.get("type") not in WIDGETS:
                raise UIError("%s: not a widget (%r)" % (path, w.get("type") if isinstance(w, dict)
                                                        else type(w).__name__))
            kind = w["type"]
            wid = w.get("id")
            events = WIDGETS[kind][0]
            if wid is not None:
                if not isinstance(wid, str) or not wid:
                    raise UIError("%s: the id must be a non-empty string" % path)
                if wid in widgets:
                    raise UIError("widget id %r is used twice" % wid)
                widgets[wid] = w
            elif events:
                raise UIError("%s: a %s needs an id" % (path, kind))
            hs = w.get("_handlers") or {}
            for ev, fn in hs.items():
                if ev not in events:
                    raise UIError("%s: a %s has no %r event" % (path, kind, ev))
                if not callable(fn):
                    raise UIError("%s: the %s handler is not callable" % (path, ev))
            if hs:
                handlers[wid] = dict(hs)
            kids = w.get("children") or []
            if kids and kind not in CONTAINERS:
                raise UIError("%s: a %s has no children" % (path, kind))
            for i, c in enumerate(kids):
                walk(c, "%s/%s[%d]" % (path, kind, i))

        for p in self._panels:
            walk(p["root"], p["id"])
        self._widgets, self._handlers = widgets, handlers

    # -- properties ------------------------------------------------------------
    def set(self, widget_id: str, **props) -> None:
        """Change properties of a widget, e.g. ``set("ch1", value="1.234")``."""
        with self._lock:
            w = self._widgets.get(widget_id)
            if w is None:
                raise UIError("no widget %r" % widget_id)
            allowed = WIDGETS[w["type"]][1]
            bad = [k for k in props if k not in allowed]
            if bad:
                raise UIError("a %s has no propert%s %s" % (
                    w["type"], "ies" if len(bad) > 1 else "y", ", ".join(sorted(bad))))
            clean = {k: _clean(v) for k, v in props.items()}
            changed = {k: v for k, v in clean.items() if w["props"].get(k, object()) != v}
            if not changed:
                return
            w["props"].update(changed)
        self._updated(widget_id, changed)

    def get(self, widget_id: str, prop: str) -> Any:
        w = self._widgets.get(widget_id)
        if w is None:
            raise UIError("no widget %r" % widget_id)
        return w["props"].get(prop)

    def handler(self, widget_id: str, event: str) -> Optional[Handler]:
        return (self._handlers.get(widget_id) or {}).get(event)

    def accepts(self, widget_id: str, event: str) -> bool:
        w = self._widgets.get(widget_id)
        return w is not None and event in WIDGETS[w["type"]][0]

    def describe(self) -> List[dict]:
        """The panels as plain data (with the present properties): what front ends
        render."""
        def out(w):
            d = {"type": w["type"], "id": w.get("id"), "props": _clean(w["props"]),
                 "events": list(WIDGETS[w["type"]][0])}
            if w.get("children"):
                d["children"] = [out(c) for c in w["children"]]
            return d
        with self._lock:
            return [{"id": p["id"], "title": p["title"], "where": p["where"],
                     "root": out(p["root"])} for p in self._panels]


def walk(widgets: Iterable[Widget]):
    """Every widget of the trees, depth first."""
    for w in widgets:
        yield w
        yield from walk(w.get("children") or [])
