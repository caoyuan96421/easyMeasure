"""Client-side cache of the backend state, shared by all panels."""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from .connection import BackendConnection


class BackendModel(QObject):
    """Holds the latest ``get_state`` result and refreshes it on change events."""

    stateChanged = Signal(dict)
    queueChanged = Signal(dict)
    rampsChanged = Signal(list)       # ramps of direct writes now running (stop_ramps)
    message = Signal(str, str)        # (level, text) for the status bar
    fault = Signal(str, str, bool)    # (instrument, message, a run was aborted): a quench ...

    def __init__(self, conn: BackendConnection, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.state: dict = {}
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(150)
        self._refresh_timer.timeout.connect(self.refresh)
        conn.connected.connect(lambda _: self.refresh())
        conn.event.connect(self._on_event)
        conn.request_failed.connect(lambda cmd, err: self.message.emit("error", "%s: %s" % (cmd, err)))

    def refresh(self) -> None:
        self.conn.request("get_state", on_ok=self._got_state,
                          on_err=lambda e: self.message.emit("error", "get_state: %s" % e))

    def schedule_refresh(self) -> None:
        self._refresh_timer.start()

    def _got_state(self, st) -> None:
        if not isinstance(st, dict):
            return
        self.state = st
        self.stateChanged.emit(st)
        if "queue" in st:
            self.queueChanged.emit(st["queue"])
        self.rampsChanged.emit(list(st.get("ramps") or []))

    def _on_event(self, kind: str, data) -> None:
        if kind in ("changed", "settings", "user_action"):
            self.schedule_refresh()
        elif kind == "queue" and isinstance(data, dict):
            self.state["queue"] = data
            self.queueChanged.emit(data)
        elif kind == "ramps" and isinstance(data, dict):
            self.state["ramps"] = list(data.get("active") or [])
            self.rampsChanged.emit(self.state["ramps"])
        elif kind == "sweep_warning":
            self.message.emit("warning", str(data.get("message")))
        elif kind == "instrument_fault" and isinstance(data, dict):
            self.fault.emit(str(data.get("instrument")), str(data.get("message")),
                            bool(data.get("aborted")))
            self.schedule_refresh()
        elif kind == "ppt":
            self.message.emit("info", "slide added to %s" % data.get("path"))

    # ---- convenience accessors -------------------------------------------------
    def data_dir(self) -> str:
        """The backend's data directory ('' before the first state arrives)."""
        return str((self.state.get("settings") or {}).get("data_dir") or "")

    def data_dialog_dir(self) -> str:
        """Where a dialog for data files opens: the folder last used inside the data
        directory, else the data directory itself (never the working directory)."""
        import os
        from PySide6.QtCore import QSettings
        root = self.data_dir()
        last = str(QSettings("easyMeasure", "gui").value("last_data_dir", "") or "")
        if root and os.path.isdir(root):
            if last and os.path.isdir(last):
                try:
                    if os.path.commonpath([os.path.abspath(last), os.path.abspath(root)]) == \
                            os.path.abspath(root):
                        return last
                except ValueError:                  # other drive (Windows)
                    pass
            return root
        if last and os.path.isdir(last):
            return last
        return os.path.expanduser("~")

    @staticmethod
    def remember_data_dir(path: str) -> None:
        import os
        from PySide6.QtCore import QSettings
        QSettings("easyMeasure", "gui").setValue("last_data_dir", os.path.dirname(path))

    def aliases(self) -> List[dict]:
        return list((self.state.get("channels") or {}).get("aliases", []))

    def composites(self) -> List[dict]:
        return list((self.state.get("channels") or {}).get("composites", []))

    def virtual_channels(self) -> List[dict]:
        """Channels the composites define: variables of multivariate composites
        (``kind`` "variable"), mappings ("map") and composite reads ("read")."""
        return list((self.state.get("channels") or {}).get("virtual", []))

    def parameters(self) -> List[dict]:
        """The setup's parameters: {name, value | formula, unit, description, current,
        used_by}."""
        return list((self.state.get("channels") or {}).get("parameters", []))

    def instruments(self) -> List[dict]:
        return list(self.state.get("instruments", []))

    def user_actions(self) -> List[dict]:
        """The user-defined actions (name, code, description, confirm, running, last)."""
        return list(self.state.get("actions", []))

    def writable_channels(self, numeric_only: bool = False) -> List[str]:
        out = []
        for a in self.aliases():
            if a.get("writable") and not (numeric_only and a.get("dtype") == "str"):
                out.append(a["name"])
        out += [v["name"] for v in self.virtual_channels() if v.get("writable")]
        return out

    def readable_channels(self) -> List[str]:
        out = [a["name"] for a in self.aliases() if a.get("readable", True)]
        out += [v["name"] for v in self.virtual_channels()]
        out += [p["name"] for p in self.parameters()]
        return out

    def raw_channels(self) -> List[str]:
        out = []
        for inst in self.instruments():
            for c in inst.get("channels", []):
                out.append("%s.%s" % (inst["name"], c["name"]))
        return out

    def aliasable_channels(self) -> List[str]:
        """Instrument channels that may be aliased (everything but config channels)."""
        return ["%s.%s" % (i["name"], c["name"]) for i in self.instruments()
                for c in i.get("channels", []) if not c.get("config")]

    def config_channels(self, writable: bool = False) -> List[str]:
        """Configuration channels (``instrument.channel``), set directly, never swept."""
        return ["%s.%s" % (i["name"], c["name"]) for i in self.instruments()
                for c in i.get("channels", []) if c.get("config")
                and ("w" in c.get("access", "rw") if writable else "r" in c.get("access", "rw"))]

    def channel_choices(self) -> dict:
        """``name -> list of allowed values`` for aliases and configuration channels
        that only accept certain values (front ends offer a combo box)."""
        out = {}
        for a in self.aliases():
            if a.get("choices"):
                out[a["name"]] = list(a["choices"])
        for i in self.instruments():
            for c in i.get("channels", []):
                if c.get("allowed"):
                    out["%s.%s" % (i["name"], c["name"])] = list(c["allowed"])
        return out

    def actions(self) -> List[str]:
        """All instrument actions as ``instrument.action``."""
        return ["%s.%s" % (i["name"], a["name"]) for i in self.instruments()
                for a in i.get("actions", [])]

    def channel_meta(self, name: str) -> Optional[dict]:
        for a in self.aliases():
            if a["name"] == name:
                return a
        for v in self.virtual_channels():
            if v["name"] == name:
                return v
        return None

    def composite_groups(self) -> List[dict]:
        """Multivariate composites with more than one variable: ``{name, variables}``,
        the variables by their channel names (``nD.n``); front ends offer setting them
        together."""
        return [{"name": c["name"],
                 "variables": ["%s.%s" % (c["name"], v["name"]) for v in c["variables"]]}
                for c in self.composites() if c.get("type") == "multi"
                and len(c.get("variables") or []) > 1]
