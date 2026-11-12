"""Instruments & channels: instruments, raw channels, aliases, composites, user
actions and setup save/load.  (Setting channels by hand is the Manual panel.)"""

from __future__ import annotations

import json
import time
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QStackedWidget,
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
    """Add / edit an instrument.  The address is an editable list of the ports found
    on the backend computer that the chosen driver accepts (its ``interfaces``),
    devices whose identification matches the driver first (✓), plus the driver's
    suggestions (built-in emulator, NONE).  *Refresh* scans again; *ask *IDN?*
    identifies GPIB / USB / LAN instruments that are not in use."""

    #: the last scan result, shared by all dialogs of this GUI
    scan_cache: Optional[dict] = None

    def __init__(self, drivers: dict, inst: Optional[dict] = None, parent=None,
                 preset: Optional[dict] = None, conn=None):
        super().__init__(parent)
        self.setWindowTitle("Instrument")
        self.drivers = drivers
        self.conn = conn
        self.editing = inst is not None
        self.name = QLineEdit(inst["name"] if inst else "")
        self.name.setEnabled(inst is None)
        self.driver = QComboBox()
        self.driver.addItems(sorted(drivers))
        self.desc = QLabel("")
        self.desc.setWordWrap(True)
        self.desc.setStyleSheet("color: gray")
        self.address = QComboBox()
        self.address.setEditable(True)
        self.address.setInsertPolicy(QComboBox.NoInsert)
        self.address.setCompleter(None)
        self.address.setMinimumContentsLength(28)
        self.address.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.address.lineEdit().setPlaceholderText(
            "GPIB0::12::INSTR, COM3, TCP::host::port, DAQ::Dev1, SIM::bus, NONE")
        self.address.activated.connect(self._picked)
        self.refresh = QPushButton("Refresh")
        self.refresh.setToolTip("scan the backend computer for ports and devices again")
        self.refresh.clicked.connect(self.scan)
        from PySide6.QtCore import QSettings
        self._qs = QSettings("easyMeasure", "gui")
        self.identify = QCheckBox("ask *IDN?")
        self.identify.setToolTip("identify GPIB / USB / LAN instruments while scanning (sends "
                                 "*IDN? to each one not in use; slower)")
        self.identify.setChecked(self._qs.value("scan_identify", True, type=bool))
        self.identify.toggled.connect(lambda on: self._qs.setValue("scan_identify", on))
        arow = QHBoxLayout()
        arow.setContentsMargins(0, 0, 0, 0)
        arow.addWidget(self.address, 1)
        arow.addWidget(self.refresh)
        arow.addWidget(self.identify)
        self.scan_status = QLabel("")
        self.scan_status.setWordWrap(True)
        self.scan_status.setStyleSheet("color: gray")
        self.options = QPlainTextEdit()
        self.options.setMinimumHeight(160)
        form = QFormLayout(self)
        form.addRow("Name", self.name)
        form.addRow("Driver", self.driver)
        form.addRow("", self.desc)
        form.addRow("Address", arow)
        form.addRow("", self.scan_status)
        form.addRow("Options (JSON)", self.options)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self._fixed_address = bool(inst) or bool(preset and preset.get("address"))
        self.driver.currentTextChanged.connect(self._driver_changed)
        if inst:
            self.driver.setCurrentText(inst["driver"])
            self.options.setPlainText(json.dumps(to_jsonable(inst.get("options", {})), indent=2))
            self._driver_changed(inst["driver"])
            self.address.setEditText(inst["address"])
        else:
            if preset and preset.get("driver") in drivers:
                self.driver.setCurrentText(preset["driver"])
            self._driver_changed(self.driver.currentText())
            if preset:
                self.name.setText(preset.get("name", ""))
                if preset.get("address"):
                    self.address.setEditText(preset["address"])
        self.result_value = None
        if InstrumentDialog.scan_cache is None:
            self.scan()

    # -- address list -----------------------------------------------------------------
    def scan(self) -> None:
        if self.conn is None:
            return
        self.refresh.setEnabled(False)
        self.scan_status.setText("scanning ports%s…" % (" and asking *IDN?" if
                                                        self.identify.isChecked() else ""))

        def ok(r):
            InstrumentDialog.scan_cache = r
            self._alive(lambda: (self.refresh.setEnabled(True), self._fill()))

        def err(e):
            self._alive(lambda: (self.refresh.setEnabled(True),
                                 self.scan_status.setText("scan failed: %s" % e)))
        self.conn.request("discover_resources", {"identify": self.identify.isChecked()},
                          on_ok=ok, on_err=err, timeout=120)

    def _alive(self, fn) -> None:
        try:                                    # the dialog may be gone by now
            fn()
        except RuntimeError:
            pass

    @staticmethod
    def _matches(entry: dict, patterns) -> bool:
        idn = str(entry.get("idn") or "").casefold()
        return bool(idn) and any(p.casefold() in idn for p in patterns if p)

    def entries(self) -> list:
        """(address, label, matches, found) offered for the current driver, best first
        (found = from the scan, not a suggestion)."""
        d = self.drivers.get(self.driver.currentText(), {})
        accepted = set(d.get("interfaces") or [])
        from ...backend.resources import interface_of
        out, seen = [], set()
        found = (InstrumentDialog.scan_cache or {}).get("found", [])
        for e in found:
            kind = e.get("interface") or interface_of(e["address"])
            if accepted and kind not in accepted:
                continue
            match = self._matches(e, d.get("idn_match") or [])
            text = e.get("idn") or e.get("description") or ""
            label = e["address"] + ("   — " + text if text else "")
            if match:
                label += "   ✓"
            if e.get("in_use"):
                use = e["in_use"]
                label += "   (in use%s)" % (": " + ", ".join(use) if isinstance(use, list) else "")
            out.append((0 if match else 1, 1 if e.get("in_use") else 0, e["address"], label, match,
                        True))
            seen.add(e["address"].upper())
        out.sort(key=lambda t: t[:3])
        for sg in d.get("suggest") or []:
            if sg["address"].upper() not in seen:
                out.append((2, 0, sg["address"], "%s   — %s" % (sg["address"], sg["description"]),
                            False, False))
        return [(a, label, m, f) for _, _, a, label, m, f in out]

    def _fill(self) -> None:
        keep = self.address.currentText()
        items = self.entries()
        self.address.blockSignals(True)
        self.address.clear()
        for a, label, m, _ in items:
            self.address.addItem(label, a)
        self.address.setCurrentIndex(-1)
        self.address.blockSignals(False)
        fm = self.address.view().fontMetrics()           # the popup shows whole labels
        widest = max([fm.horizontalAdvance(label) for _, label, _, _ in items] or [0])
        self.address.view().setMinimumWidth(min(widest + 40, 1100))
        self.address.view().setTextElideMode(Qt.ElideRight)
        self.address.setEditText(self._addr_of(keep))
        d = self.drivers.get(self.driver.currentText(), {})
        from ...backend.resources import INTERFACE_NAMES, interface_of
        accepted = d.get("interfaces") or []
        if not self._fixed_address:
            best = next((a for a, label, m, _ in items if m and "(in use" not in label), None)
            cur = self.address.currentText().strip()
            if best:
                self.address.setEditText(best)
            elif accepted and interface_of(cur) not in accepted:
                self.address.setEditText("")
        cache = InstrumentDialog.scan_cache
        n_found = sum(1 for *_, f in items if f)
        parts = ["takes: " + ", ".join(INTERFACE_NAMES.get(k, k) for k in accepted)] \
            if accepted else []
        if cache is None:
            parts.append("not scanned yet")
        else:
            hits = sum(1 for _, _, m, _ in items if m)
            parts.append("%d port(s) found for this driver%s" % (
                n_found, (", %d identified ✓" % hits) if hits else ""))
            parts += cache.get("notes", [])
        self.scan_status.setText("\n".join(parts))

    def _picked(self, index: int) -> None:
        a = self.address.itemData(index)
        if a:
            self.address.setEditText(a)

    def _addr_of(self, text: str) -> str:
        """The address for what the box shows (a list label maps to its address)."""
        t = (text or "").strip()
        for i in range(self.address.count()):
            if self.address.itemText(i) == t:
                return self.address.itemData(i)
        return t

    def address_text(self) -> str:
        return self._addr_of(self.address.currentText()) or "NONE"

    # ---------------------------------------------------------------------------------
    def _driver_changed(self, name):
        d = self.drivers.get(name, {})
        self.desc.setText(d.get("description", ""))
        self._fill()
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
        from ...backend.resources import INTERFACE_NAMES, interface_of
        address = self.address_text()
        accepted = self.drivers.get(self.driver.currentText(), {}).get("interfaces") or []
        if accepted and interface_of(address) not in accepted:
            QMessageBox.warning(self, "Address", "%s does not take %s addresses (%s).\nIt takes: %s"
                                % (self.driver.currentText(),
                                   INTERFACE_NAMES.get(interface_of(address)), address,
                                   ", ".join(INTERFACE_NAMES.get(k, k) for k in accepted)))
            return
        self.result_value = {"name": self.name.text().strip(), "driver": self.driver.currentText(),
                             "address": address, "options": opts}
        self.accept()


def _float_or_none(text: str):
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


class ParameterDialog(QDialog):
    """A parameter: a number, or a formula of other parameters."""

    def __init__(self, param: Optional[dict] = None, others=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle("Parameter")
        p = param or {}
        self.name = QLineEdit(p.get("name", ""))
        self.kind = QComboBox()
        self.kind.addItem("Value", "value")
        self.kind.addItem("Formula of other parameters", "formula")
        self.kind.setCurrentIndex(1 if p.get("formula") else 0)
        self.value = QLineEdit(_fmt_opt(p.get("value")) if not p.get("formula") else "")
        self.value.setPlaceholderText("a number, e.g. 0.6 or 1e6")
        self.formula = QLineEdit(p.get("formula", ""))
        others = [o for o in others if o != p.get("name")]
        self.formula.setPlaceholderText("e.g. Ct/Cb" if not others else
                                        "uses " + ", ".join(others[:6]))
        self.unit = QLineEdit(p.get("unit", ""))
        self.description = QLineEdit(p.get("description", ""))
        self.help = QLabel("A constant of the setup. Composite channels and actions may use "
                           "it by its name. A formula may use other parameters only (no "
                           "channels).")
        self.help.setWordWrap(True)
        self.help.setStyleSheet("color: #888")
        form = QFormLayout(self)
        form.addRow("Name", self.name)
        form.addRow("Defined by", self.kind)
        self.value_lab, self.formula_lab = QLabel("Value"), QLabel("Formula")
        form.addRow(self.value_lab, self.value)
        form.addRow(self.formula_lab, self.formula)
        form.addRow("Unit", self.unit)
        form.addRow("Description", self.description)
        form.addRow(self.help)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.kind.currentIndexChanged.connect(self._kind)
        self._kind()
        self.result_value = None

    def _kind(self):
        f = self.kind.currentData() == "formula"
        for w in (self.value, self.value_lab):
            w.setVisible(not f)
        for w in (self.formula, self.formula_lab):
            w.setVisible(f)

    def value_dict(self) -> dict:
        d = {"name": self.name.text().strip(), "unit": self.unit.text().strip(),
             "description": self.description.text().strip()}
        if self.kind.currentData() == "formula":
            d["formula"] = self.formula.text().strip()
        else:
            d["value"] = _float_or_none(self.value.text())
        return d

    def _ok(self):
        d = self.value_dict()
        if not d["name"]:
            QMessageBox.warning(self, "Parameter", "a name is needed")
            return
        if "value" in d and d["value"] is None:
            QMessageBox.warning(self, "Parameter", "the value must be a number")
            return
        if "formula" in d and not d["formula"]:
            QMessageBox.warning(self, "Parameter", "the formula is empty")
            return
        self.result_value = d
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


COMP_TYPES = (("multi", "Multivariate write"), ("read", "Composite read"),
              ("map", "Simple mapping"))
VAR_COLS = ("Name", "Min", "Max", "Unit", "Ramp /s", "Description")
FORMULA_HELP = ("Formulas: + - * / **, abs, sqrt, exp, log, sin …, min/max, "
                "a if cond else b,  piecewise(cond1, v1, cond2, v2, …, default),  "
                "interp(x, xs, ys)")


def _var_cell(v) -> str:
    return "" if v is None else ("%g" % v if isinstance(v, (int, float)) else str(v))


def composite_summary(c: dict) -> str:
    """One line describing a composite definition (for tables)."""
    t = c.get("type", "multi")
    if t == "multi":
        oor = c.get("out_of_range", "skip")
        return "; ".join("%s = %s" % kv for kv in (c.get("targets") or {}).items()) + \
            ("  [out of range: %s]" % oor if oor != "skip" else "")
    if t == "read":
        return c.get("expression", "")
    return "%s = %s  (x = %s)" % (c.get("target"), c.get("forward"), c.get("backward"))


def composite_channels(c: dict):
    t = c.get("type", "multi")
    if t == "multi":
        return [v["name"] for v in c.get("variables") or []]
    return [c["name"]]


#: out-of-range actions of a multivariate composite (key, label)
OUT_OF_RANGE_CHOICES = [("skip", "Skip the point (sweeps)"), ("error", "Error"),
                        ("clamp", "Clamp to the limit")]


class CompositeDialog(QDialog):
    """Define a composite channel: a multivariate write (variables → hardware
    channels), a composite read (a formula of channels) or a one-to-one mapping
    (forward / backward formulas, checked to be each other's inverse)."""

    def __init__(self, model, conn=None, comp: Optional[dict] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Composite channel")
        self.model = model
        self.conn = conn
        c = comp or {}
        kind = c.get("type", "multi")
        self.name = QLineEdit(c.get("name", ""))
        self.type = QComboBox()
        for k, label in COMP_TYPES:
            self.type.addItem(label, k)
        self.type.setCurrentIndex([k for k, _ in COMP_TYPES].index(kind))
        self.description = QLineEdit(c.get("description", ""))
        writable = model.writable_channels(numeric_only=True)
        readable = model.readable_channels()
        own = set(composite_channels(c)) if c else set()

        # -- multivariate write
        self.vars = QTableWidget(0, len(VAR_COLS))
        self.vars.setHorizontalHeaderLabels(VAR_COLS)
        self.vars.verticalHeader().setVisible(False)
        self.vars.horizontalHeader().setStretchLastSection(True)
        self.vars.setMinimumHeight(110)
        for v in (c.get("variables") or []) if kind == "multi" else []:
            self._add_var(v)
        if kind != "multi" or not c:
            self._add_var({"name": "x"})
        vb = QHBoxLayout()
        for text, fn in (("Add variable", lambda: self._add_var({})),
                         ("Remove variable", self._remove_var)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            vb.addWidget(b)
        vb.addStretch(1)
        self.targets = QPlainTextEdit("\n".join("%s = %s" % kv for kv in
                                                (c.get("targets") or {}).items()))
        self.targets.setPlaceholderText(
            "one per line:  channel = formula of the variables\ne.g.\n"
            "VL = U + eps\nVR = U - eps\nVg = piecewise(U < 0, 0, 2*U)\n\n"
            "channels that can be driven: " + ", ".join(n for n in writable if n not in own))
        self.out_of_range = QComboBox()
        for key, label in OUT_OF_RANGE_CHOICES:
            self.out_of_range.addItem(label, key)
        keys = [k for k, _ in OUT_OF_RANGE_CHOICES]
        oor = c.get("out_of_range", "skip") if kind == "multi" else "skip"
        self.out_of_range.setCurrentIndex(keys.index(oor) if oor in keys else 0)
        self.out_of_range.setToolTip(
            "when a combination of the variables would put a target outside its range "
            "(an alias' limits):\nSkip: a sweep skips that point (a direct write is refused)\n"
            "Error: refused everywhere; a sweep with such a point does not start\n"
            "Clamp: the target stops at the end of its range")
        mw = QWidget()
        ml = QFormLayout(mw)
        ml.setContentsMargins(0, 0, 0, 0)
        vbox = QVBoxLayout()
        vbox.addWidget(self.vars)
        vbox.addLayout(vb)
        ml.addRow("Variables", vbox)
        ml.addRow("Targets", self.targets)
        ml.addRow("Out of range", self.out_of_range)

        # -- composite read
        self.expression = QLineEdit(c.get("expression", "") if kind == "read" else "")
        self.expression.setPlaceholderText("e.g. (VL - VR)/2   or   Vg / I")
        self.r_unit = QLineEdit(c.get("unit", "") if kind == "read" else "")
        rw = QWidget()
        rl = QFormLayout(rw)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addRow("Formula", self.expression)
        rl.addRow("Unit", self.r_unit)
        uses = QLabel("may use: " + ", ".join(n for n in readable if n not in own))
        uses.setWordWrap(True)
        rl.addRow(uses)

        # -- mapping
        m = c if kind == "map" else {}
        self.m_target = QComboBox()
        self.m_target.setEditable(True)
        self.m_target.addItems([n for n in writable if n not in own])
        self.m_target.setEditText(m.get("target", ""))
        self.forward = QLineEdit(m.get("forward", ""))
        self.forward.setPlaceholderText("target y from x, e.g.  x + 273.15")
        self.backward = QLineEdit(m.get("backward", ""))
        self.backward.setPlaceholderText("x from the target y, e.g.  y - 273.15")
        self.m_min = QLineEdit(_fmt_opt(m.get("min")))
        self.m_max = QLineEdit(_fmt_opt(m.get("max")))
        self.m_unit = QLineEdit(m.get("unit", ""))
        self.m_rate = QLineEdit(_fmt_opt(m.get("ramp_rate")))
        self.m_rate.setPlaceholderText("units of x per s, optional")
        self.m_tol = QLineEdit(_fmt_opt(m.get("tolerance")))
        self.m_tol.setPlaceholderText("|backward(forward(x)) - x| allowed; default 1e-6 × range")
        pw = QWidget()
        pl = QFormLayout(pw)
        pl.setContentsMargins(0, 0, 0, 0)
        for label, w in (("Target channel", self.m_target), ("Forward  y = f(x)", self.forward),
                         ("Backward  x = g(y)", self.backward), ("Minimum of x", self.m_min),
                         ("Maximum of x", self.m_max), ("Unit", self.m_unit),
                         ("Ramp rate", self.m_rate), ("Inverse tolerance", self.m_tol)):
            pl.addRow(label, w)

        self.pages = QStackedWidget()
        for w in (mw, rw, pw):
            self.pages.addWidget(w)
        self.type.currentIndexChanged.connect(self.pages.setCurrentIndex)
        self.pages.setCurrentIndex(self.type.currentIndex())
        self.info = QLabel(FORMULA_HELP)
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color: gray")
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)

        form = QFormLayout(self)
        form.addRow("Name", self.name)
        form.addRow("Type", self.type)
        form.addRow(self.pages)
        form.addRow("Description", self.description)
        form.addRow(self.info)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.check_btn = bb.addButton("Check", QDialogButtonBox.ActionRole)
        self.check_btn.setToolTip("check the definition with the backend (references, "
                                  "dependency loops, the inverse of a mapping) without saving")
        self.check_btn.clicked.connect(self.check)
        self.check_btn.setEnabled(conn is not None)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.resize(640, 520)
        self.result_value = None
        self.original_name = c.get("name")

    # -- variables table
    def _add_var(self, v: dict) -> None:
        r = self.vars.rowCount()
        self.vars.insertRow(r)
        for col, key in enumerate(("name", "min", "max", "unit", "ramp_rate", "description")):
            self.vars.setItem(r, col, QTableWidgetItem(_var_cell(v.get(key))))

    def _remove_var(self) -> None:
        rows = sorted({i.row() for i in self.vars.selectedIndexes()}, reverse=True) or \
            ([self.vars.rowCount() - 1] if self.vars.rowCount() else [])
        for r in rows:
            self.vars.removeRow(r)

    def _cell(self, r, c) -> str:
        it = self.vars.item(r, c)
        return it.text().strip() if it is not None else ""

    # -- the definition
    def value(self) -> dict:
        """The definition as the backend takes it; raises ValueError on bad input."""
        kind = self.type.currentData()
        d = {"type": kind, "name": self.name.text().strip(),
             "description": self.description.text()}
        if kind == "multi":
            vs = []
            for r in range(self.vars.rowCount()):
                name = self._cell(r, 0)
                if not name:
                    continue
                vs.append({"name": name, "min": _opt_float(self._cell(r, 1)),
                           "max": _opt_float(self._cell(r, 2)), "unit": self._cell(r, 3),
                           "ramp_rate": _opt_float(self._cell(r, 4)),
                           "description": self._cell(r, 5)})
            targets = {}
            for line in self.targets.toPlainText().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    raise ValueError("line %r needs 'channel = formula'" % line)
                k, v = line.split("=", 1)
                targets[k.strip()] = v.strip()
            d.update(variables=vs, targets=targets,
                     out_of_range=self.out_of_range.currentData())
        elif kind == "read":
            d.update(expression=self.expression.text().strip(), unit=self.r_unit.text().strip())
        else:
            d.update(target=self.m_target.currentText().strip(),
                     forward=self.forward.text().strip(), backward=self.backward.text().strip(),
                     min=_opt_float(self.m_min.text()), max=_opt_float(self.m_max.text()),
                     unit=self.m_unit.text().strip(), ramp_rate=_opt_float(self.m_rate.text()),
                     tolerance=_opt_float(self.m_tol.text()))
        return d

    def _show(self, text: str, color: str) -> None:
        self.info.setStyleSheet("color: %s" % color)
        self.info.setText(text)

    def check(self) -> None:
        try:
            d = self.value()
        except ValueError as exc:
            self._show("✘ %s" % exc, "#d44")
            return
        replace = bool(self.original_name) and self.original_name == d["name"]

        def ok(r):
            txt = "✔ defines %s" % ", ".join(r.get("channels") or [])
            for w in r.get("warnings") or []:
                txt += "\n⚠ " + w
            self._show(txt, "#a60" if r.get("warnings") else "#2a2")
        self._show("checking …", "gray")
        self.conn.request("check_composite", dict(d, replace=replace), on_ok=ok,
                          on_err=lambda e: self._show("✘ %s" % e, "#d44"))

    def _ok(self):
        try:
            self.result_value = self.value()
        except ValueError as exc:
            QMessageBox.warning(self, "Composite", str(exc))
            return
        self.accept()


ACTION_HELP = """\
get(name) · set(name, value[, ramp=False]) · set_many({name: value, …}) · set_raw('inst.ch', v)
sleep(s) · wait_until(f, timeout) · action('inst.action') · user_action('other action')
instrument(name).raw_query(…) · print(…) / log(…) · state (a dict kept between runs) · np, math
Limits and ramp rates of aliases and composites apply; Abort stops at the next line,
sleep or ramp step."""


class UserActionDialog(QDialog):
    """Name, description and code of a user action."""

    def __init__(self, act: Optional[dict], channels, existing, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Action" if act is None else "Action %s" % act["name"])
        a = act or {}
        self.original = a.get("name")
        self.existing = set(existing)
        self.name = QLineEdit(a.get("name", ""))
        self.description = QLineEdit(a.get("description", ""))
        self.confirm = QCheckBox("ask before running")
        self.confirm.setChecked(bool(a.get("confirm")))
        from ..display import register_font
        self.code = QPlainTextEdit(a.get("code", ""))
        register_font(self.code, 1.0, monospace=True)
        self.code.setTabStopDistance(4 * self.code.fontMetrics().horizontalAdvance(" "))
        self.code.setPlaceholderText("e.g.\nset_many({'VL': 0, 'VR': 0})\nset('B', 0)  "
                                     "# ramps at the alias rate\nprint('gates and field at 0')")
        self.code.setMinimumHeight(180)
        self.insert = QComboBox()
        self.insert.addItem("insert channel name…")
        self.insert.addItems(list(channels))
        self.insert.activated.connect(self._insert)
        help_ = QLabel(ACTION_HELP)
        help_.setStyleSheet("color: gray")
        help_.setTextInteractionFlags(Qt.TextSelectableByMouse)
        help_.setWordWrap(True)
        form = QFormLayout(self)
        form.addRow("Name", self.name)
        form.addRow("Description", self.description)
        form.addRow("", self.confirm)
        form.addRow("Code", self.code)
        form.addRow("", self.insert)
        form.addRow(help_)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.resize(620, 480)
        self.result_value = None

    def _insert(self, i: int) -> None:
        if i > 0:
            self.code.insertPlainText(repr(self.insert.itemText(i)))
            self.code.setFocus()
        self.insert.setCurrentIndex(0)

    def _ok(self) -> None:
        name = self.name.text().strip()
        code = self.code.toPlainText()
        if not name or not code.strip():
            QMessageBox.warning(self, "Action", "An action needs a name and code.")
            return
        if name != self.original and name in self.existing:
            QMessageBox.warning(self, "Action", "There is already an action %s." % name)
            return
        try:
            compile(code, "<action>", "exec")
        except SyntaxError as exc:
            QMessageBox.warning(self, "Action", "Syntax error in line %s: %s"
                                % (exc.lineno, exc.msg))
            return
        self.result_value = {"name": name, "code": code,
                             "description": self.description.text().strip(),
                             "confirm": self.confirm.isChecked()}
        self.accept()


def _table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setSelectionBehavior(QTableWidget.SelectRows)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().setVisible(False)
    return t


def _last_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, dict) and "shape" in v:
        return "array %s" % "×".join(str(n) for n in v["shape"])
    if isinstance(v, np.ndarray):
        return "array %s" % "×".join(str(n) for n in v.shape)
    if isinstance(v, float):
        return "%.6g" % v
    return _short(v)


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
        self._last_local: dict = {}          # "inst.ch" -> (value, time) of our own get/set
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
        wiz = QPushButton("Driver wizard…")
        wiz.setToolTip("make a driver for a command-based instrument (SCPI or similar) "
                       "without writing code, or edit one made before")
        wiz.clicked.connect(self.driver_wizard)
        ib.addWidget(wiz)
        ib.addStretch(1)
        ibox = QGroupBox("Instruments")
        il = QVBoxLayout(ibox)
        il.addWidget(self.inst)
        il.addLayout(ib)
        # raw channels of selected instrument
        self.raw = _table(["Channel", "Config", "Access", "Type", "Shape", "Unit", "Last value",
                           "Allowed values", "Description"])
        for c, wdt in enumerate((110, 48, 52, 48, 56, 46, 110, 150)):
            self.raw.setColumnWidth(c, wdt)
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
        self.comp = _table(["Composite", "Type", "Definition", "Channels", "Last set"])
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
        # user actions: named snippets of code that control any channel
        self.uact = _table(["Action", "Description", "Last run"])
        self.uact.setToolTip("your own actions: short Python snippets using get / set / "
                             "set_many / sleep … - ▶ Run (or Enter) runs the selected one")
        self.uact_run = QPushButton("▶ Run")
        self.uact_run.setToolTip("run the selected action now")
        self.uact_run.clicked.connect(self._run_uact)
        self.uact_abort = QPushButton("■ Abort")
        self.uact_abort.setToolTip("stop the running action(s) at the next line, sleep or "
                                   "ramp step")
        self.uact_abort.clicked.connect(self._abort_uact)
        ub = FlowLayout()
        ub.addWidget(self.uact_run)
        ub.addWidget(self.uact_abort)
        for text, fn, tip in (("Add…", lambda: self._edit_uact(None), "define a new action"),
                              ("Edit…", self._edit_uact_sel, "change the selected action"),
                              ("Remove", self._remove_uact, "delete the selected action"),
                              ("↑", lambda: self._move_uact(-1), "move up"),
                              ("↓", lambda: self._move_uact(1), "move down")):
            b_ = QPushButton(text)
            b_.setToolTip(tip)
            b_.clicked.connect(fn)
            if text in ("↑", "↓"):
                b_.setFixedWidth(30)
            ub.addWidget(b_)
        ub.addStretch(1)
        ubox = QGroupBox("Actions")
        ul = QVBoxLayout(ubox)
        ul.addWidget(self.uact)
        ul.addLayout(ub)
        self.uact.cellDoubleClicked.connect(lambda r, c: self._edit_uact_sel())
        from PySide6.QtGui import QShortcut, QKeySequence
        for key in (Qt.Key_Return, Qt.Key_Enter):
            sc = QShortcut(QKeySequence(key), self.uact)
            sc.setContext(Qt.WidgetShortcut)
            sc.activated.connect(self._run_uact)
        # parameters: constants of the setup (values or formulas of each other)
        self.param = _table(["Parameter", "Value", "Formula", "Unit", "Used by", "Description"])
        self.param.setToolTip("constants of the setup; composite channels and actions use them "
                              "by name - double-click to edit")
        pb = FlowLayout()
        for text, fn in (("Add…", lambda: self._edit_param(None)), ("Edit…", self._edit_param_sel),
                         ("Remove", self._remove_param)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            pb.addWidget(b)
        pb.addStretch(1)
        pbox = QGroupBox("Parameters")
        pl = QVBoxLayout(pbox)
        pl.addWidget(self.param)
        pl.addLayout(pb)
        self.param.cellDoubleClicked.connect(lambda r, c: self._edit_param_sel())
        # setup files: actions for the main window's Setup menu
        self.act_save = QAction("Save setup…", self)
        self.act_save.setToolTip("write the setup (instruments, parameters, channels, "
                                 "composites, monitor list, actions) to a file")
        self.act_save.triggered.connect(self._save_setup)
        self.act_load = QAction("Load setup…", self)
        self.act_load.setToolTip("replace the current setup by one from a file")
        self.act_load.triggered.connect(self._load_setup)
        self.act_merge = QAction("Merge setup…", self)
        self.act_merge.setToolTip("take instruments, parameters, channels, composites and "
                                  "actions from a setup file into this setup")
        self.act_merge.triggered.connect(lambda: self.merge_setup())
        #: the actions of the Setup menu, in order
        self.setup_actions = [self.act_save, self.act_load, self.act_merge]
        self.msg = shrinkable_label(QLabel(""))
        fb = QHBoxLayout()
        fb.addWidget(self.msg, 1)

        top = QSplitter(Qt.Horizontal)
        top.addWidget(ibox)
        top.addWidget(rbox)
        bottom = QSplitter(Qt.Horizontal)
        bottom.addWidget(abox)
        bottom.addWidget(cbox)
        for t in (self.inst, self.raw, self.alias, self.comp, self.uact, self.param):
            t.setMinimumWidth(120)
        last = QSplitter(Qt.Horizontal)
        last.addWidget(pbox)
        last.addWidget(ubox)
        split = QSplitter(Qt.Vertical)
        split.addWidget(top)
        split.addWidget(bottom)
        split.addWidget(last)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 3)
        split.setStretchFactor(2, 2)
        lay = QVBoxLayout(self)
        lay.addLayout(fb)
        lay.addWidget(split, 1)
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
            last = c_.get("last")
            if isinstance(last, dict):
                last = ", ".join("%s = %s" % (k, "—" if v is None else _short(v))
                                 for k, v in last.items())
            vals = (c_["name"], dict(COMP_TYPES).get(c_.get("type"), c_.get("type")),
                    composite_summary(c_), ", ".join(composite_channels(c_)),
                    "" if last is None else _short(last))
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c_.get("description"):
                    it.setToolTip(c_["description"])
                self.comp.setItem(r, c, it)
        self._show_params()
        self._show_uacts()

    def _show_params(self):
        ps = self.model.parameters()
        sel = self._sel_name(self.param)
        self.param.setRowCount(len(ps))
        for r, p in enumerate(ps):
            vals = (p["name"], _short(p.get("current")), p.get("formula") or "",
                    p.get("unit", ""), ", ".join(p.get("used_by") or []),
                    p.get("description", ""))
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if p.get("description"):
                    it.setToolTip(p["description"])
                if c == 1 and p.get("formula"):
                    it.setForeground(QColor("#888"))
                    it.setToolTip("= %s" % p["formula"])
                self.param.setItem(r, c, it)
            if p["name"] == sel:
                self.param.selectRow(r)

    def _show_raw(self):
        name = self._sel_name(self.inst)
        inst = next((i for i in self.model.instruments() if i["name"] == name), None)
        chans = inst.get("channels", []) if inst else []
        sel = self._sel_name(self.raw)
        self.raw.setRowCount(len(chans))
        for r, c in enumerate(chans):
            shape = c.get("shape")
            allowed = " | ".join(a if isinstance(a, str) else "%g" % a for a in c.get("allowed") or [])
            last, last_t = c.get("last"), c.get("last_t")
            loc = self._last_local.get("%s.%s" % (name, c["name"]))
            if loc is not None and (last_t is None or loc[1] >= last_t):
                last, last_t = loc                       # our own get/set, newer than the state
            vals = (c["name"], "⚙" if c.get("config") else "", c["access"], c["dtype"],
                    "scalar" if shape == [] else ("any" if shape is None else
                                                  "×".join("N" if s == -1 else str(s) for s in shape)),
                    c.get("unit", ""), _last_text(last), allowed, c.get("description", ""))
            for k, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if k == 6 and last_t:
                    it.setToolTip("read or set at %s" % time.strftime("%H:%M:%S", time.localtime(last_t)))
                elif k == 7 and allowed:
                    it.setToolTip("allowed: " + allowed.replace(" | ", ", "))
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
        if c is None:
            return
        ref = "%s.%s" % (inst, c["name"])
        self.msg.setText("reading %s…" % ref)

        def ok(v):
            self.msg.setText("%s = %s" % (ref, _short(v)))
            self._note_value(ref, v)
        self.conn.request("read_channel", {"name": ref}, on_ok=ok, on_err=self._err(ref))

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
                self._write(ref, txt.strip())
        self.conn.request("read_channel", {"name": ref},
                          on_ok=lambda v: (self._note_value(ref, v), ask(v)),
                          on_err=lambda e: ask(""))

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
    def driver_wizard(self):
        from ..driver_wizard import DriverWizard
        wiz = DriverWizard(self.conn, self)
        wiz.exec()
        r = wiz.saved
        if not r:
            return
        self.msg.setText("driver %s saved%s" % (r["driver"], (" - reconnected: " + ", ".join(
            r["reloaded"])) if r.get("reloaded") else ""))
        self.model.schedule_refresh()
        address = wiz.pages[-1].address.text().strip()

        def offer(d):
            self.drivers = d.get("drivers", {})
            if not r.get("reloaded") and QMessageBox.question(
                    self, "Driver saved", "Driver %s is ready.\nAdd an instrument that uses it "
                    "now?" % r["driver"]) == QMessageBox.Yes:
                self._add_inst(preset={"driver": r["driver"], "address": address,
                                       "name": r["driver"].split("_")[-1] or "inst"})
        self.conn.request("list_drivers", on_ok=offer)

    def _add_inst(self, preset: Optional[dict] = None):
        if not self.drivers:
            self._load_drivers()
            self.msg.setText("loading driver list, try again in a moment")
            return
        dlg = InstrumentDialog(self.drivers, parent=self, preset=preset, conn=self.conn)
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
        dlg = InstrumentDialog(self.drivers or {inst["driver"]: {}}, inst, parent=self,
                               conn=self.conn)
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

    # -------------------------------------------------------------- parameters --
    def _edit_param_sel(self):
        name = self._sel_name(self.param)
        p = next((p for p in self.model.parameters() if p["name"] == name), None)
        if p:
            self._edit_param(p)

    def _edit_param(self, p):
        dlg = ParameterDialog(p, [x["name"] for x in self.model.parameters()], parent=self)
        if dlg.exec() and dlg.result_value:
            v = dlg.result_value
            args = dict(v, replace=bool(p) and p["name"] == v["name"])
            if p and p["name"] != v["name"]:
                self.conn.request("remove_parameter", {"name": p["name"]},
                                  on_err=self._err("rename"))
            self.conn.request("define_parameter", args,
                              on_ok=self._done("parameter %s saved" % v["name"]),
                              on_err=self._err("parameter"))

    def _remove_param(self):
        name = self._sel_name(self.param)
        if name:
            self.conn.request("remove_parameter", {"name": name},
                              on_ok=self._done("parameter %s removed" % name),
                              on_err=self._err("remove"))

    # -------------------------------------------------------------- composites --
    def _edit_comp_sel(self):
        name = self._sel_name(self.comp)
        c = next((c for c in self.model.composites() if c["name"] == name), None)
        if c:
            self._edit_comp(c)

    def _edit_comp(self, c):
        dlg = CompositeDialog(self.model, self.conn, c, parent=self)
        if dlg.exec() and dlg.result_value:
            v = dlg.result_value
            replace = bool(c and c["name"] == v["name"])

            def saved(r, name=v["name"]):
                warn = r.get("warnings") if isinstance(r, dict) else None
                self._done("composite %s saved%s" % (name, ("  ⚠ " + "; ".join(warn))
                                                     if warn else ""))()

            def define(_=None):
                self.conn.request("define_composite", dict(v, replace=replace),
                                  on_ok=saved, on_err=self._err("composite"))
            if c and not replace:
                # renamed: only drop the old name once the new definition is known good
                self.conn.request("check_composite", dict(v, replace=False), on_err=self._err(
                    "composite"), on_ok=lambda _: self.conn.request(
                        "remove_composite", {"name": c["name"]}, on_ok=define,
                        on_err=self._err("composite")))
            else:
                define()

    def _remove_comp(self):
        name = self._sel_name(self.comp)
        if name:
            self.conn.request("remove_composite", {"name": name},
                              on_ok=self._done("composite %s removed" % name),
                              on_err=self._err("remove"))

    # ------------------------------------------------------------------ manual --
    def _note_value(self, ref: str, v) -> None:
        """A value we just read or set: show it in the channel table at once."""
        if "." not in ref:
            return
        self._last_local[ref] = (v, time.time())
        inst, ch = ref.split(".", 1)
        if inst == self._sel_name(self.inst):
            for r in range(self.raw.rowCount()):
                if self.raw.item(r, 0) is not None and self.raw.item(r, 0).text() == ch:
                    it = QTableWidgetItem(_last_text(v))
                    it.setToolTip("read or set at %s" % time.strftime("%H:%M:%S"))
                    self.raw.setItem(r, 6, it)

    def _write(self, ref: str, txt: str) -> None:
        """Set a configuration channel from the text the user typed."""
        try:
            value = json.loads(txt)
        except ValueError:
            value = txt
        if isinstance(value, list):
            value = np.asarray(value, dtype=float)
        self.msg.setText("setting %s…" % ref)
        self.conn.request("write_channel", {"name": ref, "value": value},
                          on_ok=lambda v: (self.msg.setText("%s ← %s" % (ref, _short(v))),
                                           self._note_value(ref, v),
                                           self.model.schedule_refresh()),
                          on_err=self._err(ref))

    # ------------------------------------------------------------ user actions --
    def _uact_sel(self) -> Optional[dict]:
        name = self._sel_name(self.uact)
        return next((a for a in self.model.user_actions() if a["name"] == name), None)

    def _show_uacts(self) -> None:
        acts = self.model.user_actions()
        sel = self._sel_name(self.uact)
        self.uact.setRowCount(len(acts))
        for r, a in enumerate(acts):
            last = a.get("last") or {}
            if a.get("running"):
                status, color = "running…", "#e39a1b"
            elif last:
                status = "%s %s (%.3g s)" % ({"finished": "✔", "aborted": "■", "error": "✘"}.get(
                    last.get("status"), ""), time.strftime("%H:%M:%S", time.localtime(
                        last.get("ended") or 0)), last.get("elapsed") or 0)
                color = {"error": "#d44", "aborted": "#c80"}.get(last.get("status"), "")
            else:
                status, color = "", ""
            for c, v in enumerate((a["name"], a.get("description", ""), status)):
                it = QTableWidgetItem(v)
                tip = a.get("description") or a["name"]
                if last.get("error"):
                    tip += "\nlast run: " + last["error"]
                it.setToolTip(tip)
                if c == 2 and color:
                    it.setForeground(QColor(color))
                self.uact.setItem(r, c, it)
            if a["name"] == sel:
                self.uact.selectRow(r)
        if acts and self.uact.currentRow() < 0:
            self.uact.selectRow(0)
        running = any(a.get("running") for a in acts)
        self.uact_abort.setEnabled(running)
        self.uact_run.setEnabled(bool(acts))

    def _run_uact(self, a: Optional[dict] = None) -> None:
        a = a or self._uact_sel()
        if a is None:
            return
        if a.get("confirm") and QMessageBox.question(
                self, "Run action", "Run %s?%s" % (a["name"], ("\n\n" + a["description"])
                                                   if a.get("description") else "")) \
                != QMessageBox.Yes:
            return
        name = a["name"]
        self.msg.setText("running %s…" % name)

        def done(r):
            st = r.get("status") if isinstance(r, dict) else "finished"
            if st == "finished":
                self.msg.setText("✔ %s done (%.3g s)" % (name, r.get("elapsed") or 0))
            else:
                self._err(name)("%s%s" % (st, (": " + r["error"]) if r.get("error") else ""))
            self.model.schedule_refresh()
        self.conn.request("run_user_action", {"name": name}, on_ok=done,
                          on_err=self._err(name), timeout=7 * 24 * 3600)
        self.model.schedule_refresh()

    def _abort_uact(self) -> None:
        a = self._uact_sel()
        name = a["name"] if a is not None and a.get("running") else None
        self.conn.request("abort_user_action", {"name": name} if name else {},
                          on_ok=lambda r: self.msg.setText("aborting %s" % ", ".join(r or [])),
                          on_err=self._err("abort"))

    def _edit_uact_sel(self) -> None:
        a = self._uact_sel()
        if a is not None:
            self._edit_uact(a)

    def _edit_uact(self, a: Optional[dict]) -> None:
        names = self.model.readable_channels() + self.model.config_channels(writable=True)
        dlg = UserActionDialog(a, names, [x["name"] for x in self.model.user_actions()],
                               parent=self)
        if dlg.exec() and dlg.result_value:
            v = dict(dlg.result_value)
            if a is not None:
                v.update(replace=a["name"] == v["name"], rename_from=a["name"])
            self.conn.request("define_user_action", v,
                              on_ok=self._done("action %s saved" % v["name"]),
                              on_err=self._err("action"))

    def _remove_uact(self) -> None:
        a = self._uact_sel()
        if a is None or QMessageBox.question(self, "Remove action", "Remove the action %s?"
                                             % a["name"]) != QMessageBox.Yes:
            return
        self.conn.request("remove_user_action", {"name": a["name"]},
                          on_ok=self._done("action %s removed" % a["name"]),
                          on_err=self._err("remove"))

    def _move_uact(self, step: int) -> None:
        a = self._uact_sel()
        if a is None:
            return
        names = [x["name"] for x in self.model.user_actions()]
        pos = max(0, min(len(names) - 1, names.index(a["name"]) + step))
        self.conn.request("move_user_action", {"name": a["name"], "position": pos},
                          on_ok=self._done(""), on_err=self._err("move"))

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

    def merge_setup(self, path: str = "") -> "MergeSetupDialog":
        """Open the merge wizard (with *path*: that file already chosen)."""
        from ..merge_dialog import MergeSetupDialog

        def done(results):
            bad = [r for r in results if r.startswith("✘")]
            self.msg.setText(("merged %d items" % len(results)) if not bad else
                             "merged with problems: %s" % "; ".join(bad[:5]))
            if bad:
                QMessageBox.warning(self, "Merge setup", "\n".join(results))
        dlg = MergeSetupDialog(self.conn, self.model, self.drivers, parent=self, on_done=done)
        if path:
            dlg.file_page.path.setText(path)
            dlg.file_page.load(path)
        dlg.open()
        return dlg

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
