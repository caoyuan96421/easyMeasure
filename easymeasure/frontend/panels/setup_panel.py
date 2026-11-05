"""Instruments & channels: instruments, raw channels, aliases, composites,
manual get/set and setup save/load."""

from __future__ import annotations

import json
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ...common.codec import to_jsonable

from ..widgets import FlowLayout, shrinkable_label

STATE_COLORS = {"connected": "#3a3", "error": "#d44", "hung": "#d4d", "disconnected": "#888"}


def _opt_float(text: str) -> Optional[float]:
    t = text.strip()
    if not t:
        return None
    return float(t)


def _fmt_opt(v) -> str:
    return "" if v is None else "%g" % v


class InstrumentDialog(QDialog):
    def __init__(self, drivers: dict, inst: Optional[dict] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Instrument")
        self.drivers = drivers
        self.name = QLineEdit(inst["name"] if inst else "")
        self.name.setEnabled(inst is None)
        self.driver = QComboBox()
        self.driver.addItems(sorted(drivers))
        self.desc = QLabel("")
        self.desc.setWordWrap(True)
        self.desc.setStyleSheet("color: gray")
        self.address = QLineEdit(inst["address"] if inst else "NONE")
        self.address.setPlaceholderText("GPIB0::12::INSTR, COM3, TCP::host::port, SIM::bus, NONE")
        self.options = QPlainTextEdit()
        self.options.setMinimumHeight(160)
        form = QFormLayout(self)
        form.addRow("Name", self.name)
        form.addRow("Driver", self.driver)
        form.addRow("", self.desc)
        form.addRow("Address", self.address)
        form.addRow("Options (JSON)", self.options)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.driver.currentTextChanged.connect(self._driver_changed)
        if inst:
            self.driver.setCurrentText(inst["driver"])
            self.options.setPlainText(json.dumps(to_jsonable(inst.get("options", {})), indent=2))
        else:
            self._driver_changed(self.driver.currentText())
        self.result_value = None

    def _driver_changed(self, name):
        d = self.drivers.get(name, {})
        self.desc.setText(d.get("description", ""))
        if not self.name.isEnabled():
            return
        self.options.setPlainText(json.dumps(to_jsonable(d.get("options", {})), indent=2))

    def _ok(self):
        try:
            opts = json.loads(self.options.toPlainText() or "{}")
            if not isinstance(opts, dict):
                raise ValueError("options must be a JSON object")
        except ValueError as exc:
            QMessageBox.warning(self, "Options", "Invalid JSON: %s" % exc)
            return
        if not self.name.text().strip():
            QMessageBox.warning(self, "Name", "Name required")
            return
        self.result_value = {"name": self.name.text().strip(), "driver": self.driver.currentText(),
                             "address": self.address.text().strip() or "NONE", "options": opts}
        self.accept()


class AliasDialog(QDialog):
    def __init__(self, targets, alias: Optional[dict] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Alias")
        a = alias or {}
        self.name = QLineEdit(a.get("name", ""))
        self.target = QComboBox()
        self.target.setEditable(True)
        self.target.addItems(targets)
        if a.get("target"):
            self.target.setEditText(a["target"])
        self.min = QLineEdit(_fmt_opt(a.get("min")))
        self.max = QLineEdit(_fmt_opt(a.get("max")))
        self.rate = QLineEdit(_fmt_opt(a.get("ramp_rate")))
        self.rate.setPlaceholderText("units/s, empty = jump")
        self.step = QLineEdit(_fmt_opt(a.get("ramp_step")))
        self.step.setPlaceholderText("max step per write, optional")
        self.scale = QLineEdit(_fmt_opt(a.get("scale", 1.0)))
        self.offset = QLineEdit(_fmt_opt(a.get("offset", 0.0)))
        self.unit = QLineEdit(a.get("unit", ""))
        self.allowed = QLineEdit(", ".join(a.get("allowed") or []))
        self.allowed.setPlaceholderText("for text channels: allowed values, comma separated")
        self.read_only = QCheckBox("read only")
        self.read_only.setChecked(bool(a.get("read_only")))
        self.description = QLineEdit(a.get("description", ""))
        form = QFormLayout(self)
        for label, w in (("Name", self.name), ("Target (instrument.channel)", self.target),
                         ("Minimum", self.min), ("Maximum", self.max), ("Ramp rate", self.rate),
                         ("Ramp step", self.step), ("Scale (alias = raw·scale + offset)", self.scale),
                         ("Offset", self.offset), ("Unit", self.unit), ("Allowed values", self.allowed),
                         ("", self.read_only), ("Description", self.description)):
            form.addRow(label, w)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.result_value = None

    def _ok(self):
        try:
            allowed = [s.strip() for s in self.allowed.text().split(",") if s.strip()] or None
            self.result_value = {
                "name": self.name.text().strip(), "target": self.target.currentText().strip(),
                "min": _opt_float(self.min.text()), "max": _opt_float(self.max.text()),
                "ramp_rate": _opt_float(self.rate.text()), "ramp_step": _opt_float(self.step.text()),
                "scale": _opt_float(self.scale.text()) or 1.0,
                "offset": _opt_float(self.offset.text()) or 0.0,
                "unit": self.unit.text().strip(), "allowed": allowed,
                "read_only": self.read_only.isChecked(), "description": self.description.text()}
        except ValueError as exc:
            QMessageBox.warning(self, "Alias", "Invalid number: %s" % exc)
            return
        self.accept()


class CompositeDialog(QDialog):
    def __init__(self, aliases, comp: Optional[dict] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Composite channel")
        c = comp or {}
        self.name = QLineEdit(c.get("name", ""))
        self.targets = QPlainTextEdit("\n".join("%s = %s" % kv for kv in (c.get("targets") or {}).items()))
        self.targets.setPlaceholderText("one per line:  alias = expression of x\n"
                                        "e.g.\nVL = x\nVR = -0.8*x + 0.1\n\navailable aliases: "
                                        + ", ".join(aliases))
        self.read = QLineEdit(c.get("read") or "")
        self.read.setPlaceholderText("optional read-back from targets, e.g. (VL - VR)/2")
        self.min = QLineEdit(_fmt_opt(c.get("min")))
        self.max = QLineEdit(_fmt_opt(c.get("max")))
        self.rate = QLineEdit(_fmt_opt(c.get("ramp_rate")))
        self.unit = QLineEdit(c.get("unit", ""))
        self.description = QLineEdit(c.get("description", ""))
        form = QFormLayout(self)
        for label, w in (("Name", self.name), ("Targets", self.targets), ("Read formula", self.read),
                         ("Minimum", self.min), ("Maximum", self.max), ("Ramp rate", self.rate),
                         ("Unit", self.unit), ("Description", self.description)):
            form.addRow(label, w)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.result_value = None

    def _ok(self):
        targets = {}
        for line in self.targets.toPlainText().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                QMessageBox.warning(self, "Composite", "Line %r needs 'alias = expression'" % line)
                return
            k, v = line.split("=", 1)
            targets[k.strip()] = v.strip()
        try:
            self.result_value = {"name": self.name.text().strip(), "targets": targets,
                                 "read": self.read.text().strip() or None,
                                 "min": _opt_float(self.min.text()), "max": _opt_float(self.max.text()),
                                 "ramp_rate": _opt_float(self.rate.text()),
                                 "unit": self.unit.text().strip(),
                                 "description": self.description.text()}
        except ValueError as exc:
            QMessageBox.warning(self, "Composite", "Invalid number: %s" % exc)
            return
        self.accept()


def _table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setSelectionBehavior(QTableWidget.SelectRows)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().setVisible(False)
    return t


def _same_num(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) <= 1e-9 * max(abs(float(a)), 1e-300)
    except (TypeError, ValueError):
        return False


def _short(v) -> str:
    if v is None:
        return ""
    if isinstance(v, np.ndarray):
        return "array%s" % (v.shape,)
    if isinstance(v, float):
        return "%.6g" % v
    return str(v)


class SetupPanel(QWidget):
    def __init__(self, conn, model, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.model = model
        self.drivers: dict = {}
        # instruments
        self.inst = _table(["Name", "Driver", "Address", "State", "Calls", "Errors", "Timeouts",
                            "Last error"])
        ib = FlowLayout()
        for text, fn in (("Add…", self._add_inst), ("Edit…", self._edit_inst),
                         ("Reconnect", lambda: self._inst_cmd("reconnect_instrument")),
                         ("Disconnect", lambda: self._inst_cmd("disconnect_instrument")),
                         ("Remove", lambda: self._inst_cmd("remove_instrument", confirm=True))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            ib.addWidget(b)
        ib.addStretch(1)
        ibox = QGroupBox("Instruments")
        il = QVBoxLayout(ibox)
        il.addWidget(self.inst)
        il.addLayout(ib)
        # raw channels of selected instrument
        self.raw = _table(["Channel", "Config", "Access", "Type", "Shape", "Unit", "Description"])
        self.raw.setToolTip("config channels (⚙) are settings: set directly, never aliased or swept")
        rb = FlowLayout()
        self.mk_alias = QPushButton("Make alias…")
        self.mk_alias.clicked.connect(self._alias_from_raw)
        self.raw_get = QPushButton("Get")
        self.raw_get.clicked.connect(lambda: self._raw_get())
        self.raw_set = QPushButton("Set…")
        self.raw_set.setToolTip("set a configuration channel")
        self.raw_set.clicked.connect(self._raw_set)
        for b in (self.mk_alias, self.raw_get, self.raw_set):
            rb.addWidget(b)
        self.raw.itemSelectionChanged.connect(self._raw_selection)
        self.act = QListWidget()
        self.act.setToolTip("instrument actions (commands without arguments)")
        self.act.itemDoubleClicked.connect(lambda _: self._run_action())
        self.act_run = QPushButton("▶ Run action")
        self.act_run.clicked.connect(self._run_action)
        rbox = QGroupBox("Instrument channels && actions")
        rl = QVBoxLayout(rbox)
        rl.addWidget(self.raw, 3)
        rl.addLayout(rb)
        al_ = QHBoxLayout()
        al_.addWidget(self.act, 1)
        al_.addWidget(self.act_run)
        rl.addLayout(al_, 1)
        # aliases
        self.alias = _table(["Alias", "Target", "Min", "Max", "Ramp /s", "Step", "Scale",
                             "Offset", "Unit", "Last set"])
        ab = FlowLayout()
        for text, fn in (("Add…", lambda: self._edit_alias(None)), ("Edit…", self._edit_alias_sel),
                         ("Remove", self._remove_alias)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            ab.addWidget(b)
        ab.addStretch(1)
        abox = QGroupBox("Aliases")
        al = QVBoxLayout(abox)
        al.addWidget(self.alias)
        al.addLayout(ab)
        # composites
        self.comp = _table(["Composite", "Targets", "Read", "Min", "Max", "Ramp /s", "Last set"])
        cb = FlowLayout()
        for text, fn in (("Add…", lambda: self._edit_comp(None)), ("Edit…", self._edit_comp_sel),
                         ("Remove", self._remove_comp)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            cb.addWidget(b)
        cb.addStretch(1)
        cbox = QGroupBox("Composites")
        cl = QVBoxLayout(cbox)
        cl.addWidget(self.comp)
        cl.addLayout(cb)
        # manual control
        self.m_chan = QComboBox()
        self.m_chan.setEditable(True)
        self.m_chan.setMinimumContentsLength(10)
        self.m_value = QLineEdit()
        self.m_value.setPlaceholderText("value (number, text, or JSON list)")
        self.m_ramp = QCheckBox("ramp")
        self.m_ramp.setChecked(True)
        get_b = QPushButton("Get")
        set_b = QPushButton("Set")
        get_b.clicked.connect(self._manual_get)
        set_b.clicked.connect(self._manual_set)
        self.m_result = shrinkable_label(QLabel(""))
        self.m_result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.m_value.setMinimumWidth(80)
        mbox = QGroupBox("Manual control")
        mv = QVBoxLayout(mbox)
        ml = FlowLayout()
        for w in (self.m_chan, self.m_value, self.m_ramp, get_b, set_b):
            ml.addWidget(w)
        mv.addLayout(ml)
        mv.addWidget(self.m_result)
        # setup files
        sv = QPushButton("Save setup…")
        ld = QPushButton("Load setup…")
        sv.clicked.connect(self._save_setup)
        ld.clicked.connect(self._load_setup)
        self.msg = shrinkable_label(QLabel(""))
        fb = QHBoxLayout()
        fb.addWidget(sv)
        fb.addWidget(ld)
        fb.addWidget(self.msg, 1)

        top = QSplitter(Qt.Horizontal)
        top.addWidget(ibox)
        top.addWidget(rbox)
        bottom = QSplitter(Qt.Horizontal)
        bottom.addWidget(abox)
        bottom.addWidget(cbox)
        for t in (self.inst, self.raw, self.alias, self.comp):
            t.setMinimumWidth(120)
        split = QSplitter(Qt.Vertical)
        split.addWidget(top)
        split.addWidget(bottom)
        lay = QVBoxLayout(self)
        lay.addLayout(fb)
        lay.addWidget(split, 1)
        lay.addWidget(mbox)
        self.inst.itemSelectionChanged.connect(self._show_raw)
        self.alias.cellDoubleClicked.connect(lambda r, c: self._edit_alias_sel())
        self.comp.cellDoubleClicked.connect(lambda r, c: self._edit_comp_sel())
        self.inst.cellDoubleClicked.connect(lambda r, c: self._edit_inst())
        model.stateChanged.connect(self._state)
        conn.connected.connect(lambda _: self._load_drivers())

    # ---------------------------------------------------------------- helpers --
    def _err(self, prefix):
        return lambda e: self.msg.setText("<span style='color:#d55'>✘ %s: %s</span>" % (prefix, e))

    def _done(self, text):
        def f(_=None):
            self.msg.setText(text)
            self.model.schedule_refresh()
        return f

    def _load_drivers(self):
        self.conn.request("list_drivers", on_ok=lambda d: setattr(self, "drivers", d.get("drivers", {})))

    def _sel_name(self, table):
        r = table.currentRow()
        if r < 0 or table.item(r, 0) is None:
            return None
        return table.item(r, 0).text()

    # ------------------------------------------------------------------ state --
    def _state(self, st):
        insts = st.get("instruments", [])
        sel = self._sel_name(self.inst)
        self.inst.setRowCount(len(insts))
        for r, i in enumerate(insts):
            s = i.get("stats", {})
            vals = (i["name"], i["driver"], i["address"], i["state"], s.get("calls", 0),
                    s.get("errors", 0), s.get("timeouts", 0), i.get("last_error", ""))
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c == 3:
                    it.setForeground(QColor(STATE_COLORS.get(i["state"], "#888")))
                if c == 7:
                    it.setToolTip(str(v))
                self.inst.setItem(r, c, it)
            if i["name"] == sel:
                self.inst.selectRow(r)
        self._show_raw()
        al = self.model.aliases()
        self.alias.setRowCount(len(al))
        for r, a in enumerate(al):
            vals = (a["name"], a["target"], _fmt_opt(a.get("min")), _fmt_opt(a.get("max")),
                    _fmt_opt(a.get("ramp_rate")), _fmt_opt(a.get("ramp_step")),
                    _fmt_opt(a.get("scale")), _fmt_opt(a.get("offset")), a.get("unit", ""),
                    _short(a.get("last")))
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if a.get("error"):
                    it.setForeground(QColor("#d44"))
                    it.setToolTip(a["error"])
                self.alias.setItem(r, c, it)
        cs = self.model.composites()
        self.comp.setRowCount(len(cs))
        for r, c_ in enumerate(cs):
            vals = (c_["name"], "; ".join("%s=%s" % kv for kv in c_["targets"].items()),
                    c_.get("read") or "", _fmt_opt(c_.get("min")), _fmt_opt(c_.get("max")),
                    _fmt_opt(c_.get("ramp_rate")), _short(c_.get("last")))
            for c, v in enumerate(vals):
                self.comp.setItem(r, c, QTableWidgetItem(str(v)))
        names = self.model.readable_channels() + self.model.raw_channels()
        cur = self.m_chan.currentText()
        self.m_chan.blockSignals(True)
        self.m_chan.clear()
        self.m_chan.addItems(names)
        self.m_chan.setEditText(cur)
        self.m_chan.blockSignals(False)

    def _show_raw(self):
        name = self._sel_name(self.inst)
        inst = next((i for i in self.model.instruments() if i["name"] == name), None)
        chans = inst.get("channels", []) if inst else []
        sel = self._sel_name(self.raw)
        self.raw.setRowCount(len(chans))
        for r, c in enumerate(chans):
            shape = c.get("shape")
            vals = (c["name"], "⚙" if c.get("config") else "", c["access"], c["dtype"],
                    "scalar" if shape == [] else ("any" if shape is None else
                                                  "×".join("N" if s == -1 else str(s) for s in shape)),
                    c.get("unit", ""), c.get("description", ""))
            for k, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c.get("allowed"):
                    it.setToolTip("allowed: " + ", ".join(
                        a if isinstance(a, str) else "%g" % a for a in c["allowed"]))
                self.raw.setItem(r, k, it)
            if c["name"] == sel:
                self.raw.selectRow(r)
        acts = inst.get("actions", []) if inst else []
        self.act.clear()
        for a in acts:
            it = QListWidgetItem(a["name"])
            it.setToolTip(a.get("description", ""))
            self.act.addItem(it)
        self.act_run.setEnabled(bool(acts))
        self._raw_selection()

    def _raw_spec(self):
        inst = self._sel_name(self.inst)
        ch = self._sel_name(self.raw)
        i = next((i for i in self.model.instruments() if i["name"] == inst), None)
        c = next((c for c in (i or {}).get("channels", []) if c["name"] == ch), None)
        return inst, c

    def _raw_selection(self):
        _, c = self._raw_spec()
        self.mk_alias.setEnabled(c is not None and not c.get("config"))
        self.raw_get.setEnabled(c is not None and "r" in c.get("access", ""))
        self.raw_set.setEnabled(c is not None and bool(c.get("config")) and "w" in c.get("access", ""))

    def _raw_get(self):
        inst, c = self._raw_spec()
        if c is not None:
            self.m_chan.setEditText("%s.%s" % (inst, c["name"]))
            self._manual_get()

    def _raw_set(self):
        inst, c = self._raw_spec()
        if c is None:
            return
        ref = "%s.%s" % (inst, c["name"])

        def ask(current):
            from PySide6.QtWidgets import QInputDialog
            label = "%s (%s)\nnew value:" % (ref, c.get("description") or c["dtype"])
            if c.get("allowed"):
                items = [a if isinstance(a, str) else "%g" % a for a in c["allowed"]]
                cur = next((i for i, a in enumerate(c["allowed"]) if str(a).casefold() ==
                            str(current).casefold() or (not isinstance(a, str) and
                                                        _same_num(a, current))), 0)
                txt, ok = QInputDialog.getItem(self, "Set %s" % ref, label, items, cur, False)
            else:
                txt, ok = QInputDialog.getText(self, "Set %s" % ref, label, text=_short(current))
            if ok and txt.strip():
                self.m_chan.setEditText(ref)
                self.m_value.setText(txt.strip())
                self._manual_set()
        self.conn.request("read_channel", {"name": ref}, on_ok=ask, on_err=lambda e: ask(""))

    def _run_action(self):
        inst = self._sel_name(self.inst)
        it = self.act.currentItem()
        if not inst or it is None:
            return
        ref = "%s.%s" % (inst, it.text())
        self.msg.setText("running %s…" % ref)
        self.conn.request("run_action", {"ref": ref}, on_ok=self._done("%s done" % ref),
                          on_err=self._err(ref))

    # ------------------------------------------------------------- instruments --
    def _add_inst(self):
        if not self.drivers:
            self._load_drivers()
            self.msg.setText("loading driver list, try again in a moment")
            return
        dlg = InstrumentDialog(self.drivers, parent=self)
        if dlg.exec() and dlg.result_value:
            v = dlg.result_value
            self.msg.setText("connecting %s…" % v["name"])
            self.conn.request("add_instrument", v, on_ok=self._done("instrument %s added" % v["name"]),
                              on_err=self._err("add instrument"))

    def _edit_inst(self):
        name = self._sel_name(self.inst)
        inst = next((i for i in self.model.instruments() if i["name"] == name), None)
        if inst is None:
            return
        dlg = InstrumentDialog(self.drivers or {inst["driver"]: {}}, inst, parent=self)
        if dlg.exec() and dlg.result_value:
            v = dlg.result_value
            self.conn.request("update_instrument", {"name": name, "address": v["address"],
                                                    "options": v["options"], "driver": v["driver"]},
                              on_ok=self._done("instrument %s updated" % name),
                              on_err=self._err("update instrument"))

    def _inst_cmd(self, cmd, confirm=False):
        name = self._sel_name(self.inst)
        if not name:
            return
        if confirm and QMessageBox.question(self, "Remove", "Remove instrument %s?" % name) \
                != QMessageBox.Yes:
            return
        self.msg.setText("%s %s…" % (cmd.replace("_", " "), name))
        self.conn.request(cmd, {"name": name}, on_ok=self._done("%s: done" % name),
                          on_err=self._err(cmd))

    # ----------------------------------------------------------------- aliases --
    def _alias_from_raw(self):
        inst = self._sel_name(self.inst)
        ch = self._sel_name(self.raw)
        if inst and ch:
            self._edit_alias({"name": ch, "target": "%s.%s" % (inst, ch)}, new=True)

    def _edit_alias_sel(self):
        name = self._sel_name(self.alias)
        a = next((a for a in self.model.aliases() if a["name"] == name), None)
        if a:
            self._edit_alias(a)

    def _edit_alias(self, a, new=None):
        is_new = a is None or new
        dlg = AliasDialog(self.model.aliasable_channels(), a, parent=self)
        if dlg.exec() and dlg.result_value:
            v = dlg.result_value
            args = dict(v, replace=not is_new)
            if not is_new and a and a["name"] != v["name"]:
                # rename = remove + add
                self.conn.request("remove_alias", {"name": a["name"]}, on_err=self._err("rename"))
                args["replace"] = False
            self.conn.request("define_alias", args, on_ok=self._done("alias %s saved" % v["name"]),
                              on_err=self._err("alias"))

    def _remove_alias(self):
        name = self._sel_name(self.alias)
        if name:
            self.conn.request("remove_alias", {"name": name},
                              on_ok=self._done("alias %s removed" % name), on_err=self._err("remove"))

    # -------------------------------------------------------------- composites --
    def _edit_comp_sel(self):
        name = self._sel_name(self.comp)
        c = next((c for c in self.model.composites() if c["name"] == name), None)
        if c:
            self._edit_comp(c)

    def _edit_comp(self, c):
        dlg = CompositeDialog([a["name"] for a in self.model.aliases()], c, parent=self)
        if dlg.exec() and dlg.result_value:
            v = dlg.result_value
            if c and c["name"] != v["name"]:
                self.conn.request("remove_composite", {"name": c["name"]})
            self.conn.request("define_composite", dict(v, replace=bool(c and c["name"] == v["name"])),
                              on_ok=self._done("composite %s saved" % v["name"]),
                              on_err=self._err("composite"))

    def _remove_comp(self):
        name = self._sel_name(self.comp)
        if name:
            self.conn.request("remove_composite", {"name": name},
                              on_ok=self._done("composite %s removed" % name),
                              on_err=self._err("remove"))

    # ------------------------------------------------------------------ manual --
    def _manual_get(self):
        name = self.m_chan.currentText().strip()
        if not name:
            return
        self.m_result.setText("reading %s…" % name)
        self.conn.request("read_channel", {"name": name},
                          on_ok=lambda v: self.m_result.setText("%s = %s" % (name, _short(v))),
                          on_err=lambda e: self.m_result.setText("<span style='color:#d55'>✘ %s</span>" % e))

    def _manual_set(self):
        name = self.m_chan.currentText().strip()
        txt = self.m_value.text().strip()
        if not name or not txt:
            return
        try:
            value = json.loads(txt)
        except ValueError:
            value = txt
        if isinstance(value, list):
            value = np.asarray(value, dtype=float)
        self.m_result.setText("setting %s…" % name)
        self.conn.request("write_channel", {"name": name, "value": value, "ramp": self.m_ramp.isChecked()},
                          on_ok=lambda v: (self.m_result.setText("%s ← %s" % (name, _short(v))),
                                           self.model.schedule_refresh()),
                          on_err=lambda e: self.m_result.setText("<span style='color:#d55'>✘ %s</span>" % e))

    # ------------------------------------------------------------------ setups --
    def _save_setup(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save setup", "setup.json", "Setup (*.json)")
        if not path:
            return

        def got(setup):
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(to_jsonable(setup), f, indent=2)
                self.msg.setText("setup saved to %s" % path)
            except OSError as exc:
                self.msg.setText("✘ %s" % exc)
        self.conn.request("get_setup", on_ok=got, on_err=self._err("save setup"))

    def _load_setup(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load setup", "", "Setup (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                setup = json.load(f)
        except Exception as exc:
            QMessageBox.warning(self, "Load setup", str(exc))
            return
        if QMessageBox.question(self, "Load setup",
                                "Replace the current instruments, aliases and composites?") \
                != QMessageBox.Yes:
            return
        self.msg.setText("loading setup (connecting instruments)…")

        def ok(problems):
            self.model.schedule_refresh()
            if problems:
                self.msg.setText("setup loaded with problems:<br>" + "<br>".join(problems[:10]))
            else:
                self.msg.setText("setup %s loaded" % path)
        self.conn.request("load_setup", {"setup": setup}, on_ok=ok, on_err=self._err("load setup"),
                          timeout=600)
