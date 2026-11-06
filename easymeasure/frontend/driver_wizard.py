"""Driver Wizard: build a driver for a command-based instrument without writing code.

Pages: template → identity & connection → channels → actions → initialization
→ check, try on the instrument, save.  The result is a driver file in the
backend's ``<home>/drivers`` (see ``easymeasure/drivers/_command.py``); saving
loads it at once, and the wizard can reopen it later.
"""

from __future__ import annotations

import copy
import json
import time
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtCore import QRegularExpression
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWizard, QWizardPage)

from .widgets import shrinkable_label

TERMINATIONS = [("\\n  (LF)", "\n"), ("\\r\\n  (CR LF)", "\r\n"), ("\\r  (CR)", "\r"), ("none", "")]
CH_COLS = ["Name", "Kind", "Access", "Type", "Unit", "Get (query)", "Set ({value})",
           "Allowed values", "Map (value=word)", "Scale", "Description"]
CH_KEYS = ["name", "config", "access", "dtype", "unit", "get", "set", "allowed", "map",
           "scale", "description"]
CH_HELP = ("<b>Data</b> channels are measured or swept (alias them in the setup); "
           "<b>config</b> channels are settings (range, mode, output …). "
           "<b>Get</b>: the query whose reply is the value (numbers: the first number in "
           "the reply). <b>Set</b>: the command, <code>{value}</code> marks where the value "
           "goes (<code>{value:.3f}</code> for a fixed format). <b>Allowed values</b>: "
           "<code>a | b | c</code> — a drop-down in the GUI. <b>Map</b>: "
           "<code>Voltage=VOLT | Current=CURR</code> translates values into the instrument's "
           "words (replies in SCPI short/long form and 1/0 for ON/OFF are recognised). "
           "<b>Scale</b>: reading × scale (instrument in mA, channel in A: 0.001). "
           "<b>vector</b>: a reply of comma-separated numbers.")


def _cell(text="") -> QTableWidgetItem:
    return QTableWidgetItem("" if text is None else str(text))


def _combo(items, current) -> QComboBox:
    cb = QComboBox()
    cb.addItems(items)
    cb.setCurrentText(current)
    return cb


def _fmt_list(v) -> str:
    if not v:
        return ""
    return " | ".join(x if isinstance(x, str) else "%g" % x for x in v)


def _fmt_map(m) -> str:
    return " | ".join("%s=%s" % (k, v) for k, v in (m or {}).items())


def _parse_list(text: str, numeric: bool):
    parts = [p.strip() for p in text.split("|") if p.strip()]
    if not parts:
        return None
    if numeric:
        try:
            return [float(p) for p in parts]
        except ValueError:
            return parts                     # the check reports it
    return parts


def _parse_map(text: str):
    out = {}
    for p in [p.strip() for p in text.split("|") if p.strip()]:
        k, sep, v = p.partition("=")
        out[k.strip()] = v.strip() if sep else k.strip()
    return out or None


class _Page(QWizardPage):
    @property
    def d(self) -> dict:
        return self.wizard().definition


class TemplatePage(QWizardPage):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.setTitle("Start from")
        self.setSubTitle("Pick a template for a new driver, or reopen a driver made with the "
                         "wizard.  Everything can be changed on the next pages.")
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.itemDoubleClicked.connect(lambda _: self.wizard().next())
        self.list.currentItemChanged.connect(lambda *_: self.completeChanged.emit())
        self.status = shrinkable_label(QLabel(""))
        lay = QVBoxLayout(self)
        lay.addWidget(self.list)
        lay.addWidget(self.status)
        self.templates = []
        self.existing = []

    def initializePage(self):                               # noqa: N802 - Qt API
        self.status.setText("loading templates…")
        self.conn.request("wizard_templates", on_ok=self._got_templates, on_err=self._retry)

    def _retry(self, e):
        """Backend not reachable (yet): say so and ask again shortly."""
        self.status.setText("✘ %s - retrying…" % e)
        from PySide6.QtCore import QTimer
        QTimer.singleShot(1500, lambda: self.isVisible() and not self.list.count()
                          and self.initializePage())

    def _got_templates(self, t):
        self.templates = t
        self.conn.request("wizard_list", on_ok=self._got_list,
                          on_err=lambda e: self._got_list([]))

    def _got_list(self, existing):
        self.existing = [x for x in existing if not x.get("error")]
        self.list.clear()
        for t in self.templates:
            it = QListWidgetItem("%s\n    %s" % (t["title"], t["summary"]))
            it.setData(Qt.UserRole, ("template", t))
            self.list.addItem(it)
        for x in self.existing:
            it = QListWidgetItem("✎ Edit %s\n    %s (%d channels)" % (
                x["name"], x.get("description") or "", x.get("channels", 0)))
            it.setData(Qt.UserRole, ("existing", x))
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)
        self.status.setText("")

    def isComplete(self):                                    # noqa: N802
        return self.list.currentItem() is not None

    def validatePage(self):                                  # noqa: N802
        if self.list.currentItem() is None:
            return False
        kind, x = self.list.currentItem().data(Qt.UserRole)
        wiz = self.wizard()
        if kind == "template":
            wiz.definition = copy.deepcopy(x["definition"])
            wiz.editing = None
            return True
        # reopen: fetch synchronously enough - the wizard waits for the reply
        self.status.setText("loading %s…" % x["name"])
        done = {}
        self.conn.request("wizard_load", {"name": x["name"]},
                          on_ok=lambda d: done.setdefault("d", d),
                          on_err=lambda e: done.setdefault("e", str(e)))
        from PySide6.QtCore import QCoreApplication, QElapsedTimer
        t = QElapsedTimer()
        t.start()
        while not done and t.elapsed() < 10000:
            QCoreApplication.processEvents()
            time.sleep(0.005)
        if "d" not in done:
            self.status.setText("✘ cannot load %s: %s" % (x["name"], done.get("e", "timeout")))
            return False
        wiz.definition = done["d"]
        wiz.editing = x["name"]
        return True


class BasicsPage(_Page):
    def __init__(self):
        super().__init__()
        self.setTitle("Identity and connection")
        self.setSubTitle("The driver name is what setups refer to (\"driver\": \"my_dmm\").")
        self.name = QLineEdit()
        self.name.setValidator(QRegularExpressionValidator(QRegularExpression(r"[a-z][a-z0-9_]*")))
        self.name.textChanged.connect(lambda _: self.completeChanged.emit())
        self.desc = QLineEdit()
        self.idn_query = QLineEdit()
        self.idn_query.setPlaceholderText("*IDN?  (empty: do not identify)")
        self.idn_match = QLineEdit()
        self.idn_match.setPlaceholderText("e.g. 34461  (several: 34461 | 34465; empty: accept any)")
        self.err_query = QLineEdit()
        self.err_query.setPlaceholderText("SYST:ERR?  (empty: the instrument has no error queue)")
        self.rterm = QComboBox()
        self.wterm = QComboBox()
        for label, _ in TERMINATIONS:
            self.rterm.addItem(label)
            self.wterm.addItem(label)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0.1, 600)
        self.timeout.setSuffix(" s")
        self.query_delay = QDoubleSpinBox()
        self.query_delay.setRange(0, 10)
        self.query_delay.setDecimals(3)
        self.query_delay.setSuffix(" s")
        self.query_delay.setToolTip("wait between sending a query and reading its reply "
                                    "(slow instruments)")
        form = QFormLayout(self)
        form.addRow("Driver name", self.name)
        form.addRow("Description", self.desc)
        form.addRow("Identification query", self.idn_query)
        form.addRow("Expected in the reply", self.idn_match)
        form.addRow("Error query", self.err_query)
        hint = shrinkable_label(QLabel("With an error query the driver reads the instrument's "
                                       "error queue after every setting, so a value the "
                                       "instrument refused is reported with its own message."))
        hint.setStyleSheet("color: gray")
        form.addRow(hint)
        form.addRow("Read termination", self.rterm)
        form.addRow("Write termination", self.wterm)
        form.addRow("Timeout", self.timeout)
        form.addRow("Query delay", self.query_delay)

    def initializePage(self):                               # noqa: N802
        d, o = self.d, self.d.get("options", {})
        self.name.setText(d.get("name", ""))
        self.name.setReadOnly(self.wizard().editing is not None)
        self.desc.setText(d.get("description", ""))
        self.idn_query.setText(d.get("idn_query", ""))
        self.idn_match.setText(d.get("idn_match", ""))
        self.err_query.setText(d.get("error_query", ""))
        terms = [t for _, t in TERMINATIONS]
        self.rterm.setCurrentIndex(terms.index(o.get("read_termination", "\n"))
                                   if o.get("read_termination", "\n") in terms else 0)
        self.wterm.setCurrentIndex(terms.index(o.get("write_termination", "\n"))
                                   if o.get("write_termination", "\n") in terms else 0)
        self.timeout.setValue(float(o.get("timeout", 5.0)))
        self.query_delay.setValue(float(o.get("query_delay", 0.0)))

    def isComplete(self):                                    # noqa: N802
        return bool(self.name.text())

    def validatePage(self):                                  # noqa: N802
        d = self.d
        d["name"] = self.name.text().strip()
        d["description"] = self.desc.text().strip()
        d["idn_query"] = self.idn_query.text().strip()
        d["idn_match"] = self.idn_match.text().strip()
        d["error_query"] = self.err_query.text().strip()
        o = dict(d.get("options", {}))
        o["read_termination"] = TERMINATIONS[self.rterm.currentIndex()][1]
        o["write_termination"] = TERMINATIONS[self.wterm.currentIndex()][1]
        o["timeout"] = self.timeout.value()
        if self.query_delay.value():
            o["query_delay"] = self.query_delay.value()
        else:
            o.pop("query_delay", None)
        d["options"] = o
        return True


class ChannelsPage(_Page):
    def __init__(self):
        super().__init__()
        self.setTitle("Channels")
        self.setSubTitle("Add as many data and configuration channels as the instrument has.")
        self.table = QTableWidget(0, len(CH_COLS))
        self.table.setHorizontalHeaderLabels(CH_COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for c, w in enumerate((110, 70, 58, 68, 50, 130, 150, 140, 170, 50)):
            self.table.setColumnWidth(c, w)
        help_ = shrinkable_label(QLabel(CH_HELP))
        help_.setWordWrap(True)
        help_.setStyleSheet("color: gray")
        bar = QHBoxLayout()
        for text, fn in (("+ data channel", lambda: self.add_row({"config": False})),
                         ("+ config channel", lambda: self.add_row({"config": True, "dtype": "str",
                                                                    "access": "rw"})),
                         ("Duplicate", self._dup), ("Remove", self._remove),
                         ("▲", lambda: self._move(-1)), ("▼", lambda: self._move(1))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch(1)
        lay = QVBoxLayout(self)
        lay.addWidget(self.table, 1)
        lay.addLayout(bar)
        lay.addWidget(help_)

    def initializePage(self):                               # noqa: N802
        self.table.setRowCount(0)
        for c in self.d.get("channels", []):
            self.add_row(c)

    def add_row(self, c: dict, at: Optional[int] = None) -> None:
        r = self.table.rowCount() if at is None else at
        self.table.insertRow(r)
        numeric = c.get("dtype", "float") != "str"
        name = c.get("name") or self._free_name("config" if c.get("config") else "value")
        self.table.setItem(r, 0, _cell(name))
        self.table.setCellWidget(r, 1, _combo(["data", "config"], "config" if c.get("config") else "data"))
        self.table.setCellWidget(r, 2, _combo(["r", "w", "rw"], c.get("access", "r")))
        self.table.setCellWidget(r, 3, _combo(["float", "str", "vector"], c.get("dtype", "float")))
        self.table.setItem(r, 4, _cell(c.get("unit", "")))
        self.table.setItem(r, 5, _cell(c.get("get", "")))
        self.table.setItem(r, 6, _cell(c.get("set", "")))
        self.table.setItem(r, 7, _cell(_fmt_list(c.get("allowed"))))
        self.table.setItem(r, 8, _cell(_fmt_map(c.get("map"))))
        self.table.setItem(r, 9, _cell("" if c.get("scale") in (None, "") else "%g" % c["scale"]))
        self.table.setItem(r, 10, _cell(c.get("description", "")))
        self.table.item(r, 7).setToolTip("numbers or words separated by |" if numeric else
                                         "words separated by |, e.g. ON | OFF")

    def _free_name(self, base: str) -> str:
        names = {self._text(r, 0) for r in range(self.table.rowCount())}
        k = 1
        while "%s%d" % (base, k) in names:
            k += 1
        return "%s%d" % (base, k)

    def _text(self, r, c) -> str:
        it = self.table.item(r, c)
        return it.text().strip() if it else ""

    def row_dict(self, r: int) -> dict:
        dtype = self.table.cellWidget(r, 3).currentText()
        d = {"name": self._text(r, 0),
             "config": self.table.cellWidget(r, 1).currentText() == "config",
             "access": self.table.cellWidget(r, 2).currentText(), "dtype": dtype,
             "unit": self._text(r, 4), "get": self._text(r, 5), "set": self._text(r, 6),
             "description": self._text(r, 10)}
        allowed = _parse_list(self._text(r, 7), dtype == "float")
        if allowed:
            d["allowed"] = allowed
        mp = _parse_map(self._text(r, 8))
        if mp:
            d["map"] = mp
        sc = self._text(r, 9)
        if sc:
            try:
                d["scale"] = float(sc)
            except ValueError:
                d["scale"] = sc
        return d

    def _dup(self):
        r = self.table.currentRow()
        if r >= 0:
            c = self.row_dict(r)
            c["name"] = self._free_name(c["name"] + "_")
            self.add_row(c, r + 1)

    def _remove(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def _move(self, step):
        r = self.table.currentRow()
        if r < 0 or not 0 <= r + step < self.table.rowCount():
            return
        a, b = self.row_dict(r), self.row_dict(r + step)
        self.table.removeRow(r)
        self.add_row(b, r)
        self.table.removeRow(r + 1)
        self.add_row(a, r + 1)
        self.table.selectRow(r + step)

    def validatePage(self):                                  # noqa: N802
        self.d["channels"] = [self.row_dict(r) for r in range(self.table.rowCount())]
        return True


class ActionsPage(_Page):
    def __init__(self):
        super().__init__()
        self.setTitle("Actions")
        self.setSubTitle("Commands without a value: reset, trigger, auto-zero … (buttons in "
                         "the GUI, action('name') in scripts).  Several commands: separate "
                         "them with |.")
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name", "Commands", "Description"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 130)
        self.table.setColumnWidth(1, 300)
        bar = QHBoxLayout()
        for text, fn in (("+ action", lambda: self._add({})), ("Remove", self._remove)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch(1)
        lay = QVBoxLayout(self)
        lay.addWidget(self.table, 1)
        lay.addLayout(bar)

    def initializePage(self):                               # noqa: N802
        self.table.setRowCount(0)
        for a in self.d.get("actions", []):
            self._add(a)

    def _add(self, a):
        r = self.table.rowCount()
        self.table.insertRow(r)
        cmds = a.get("commands", [])
        if isinstance(cmds, str):
            cmds = cmds.splitlines()
        self.table.setItem(r, 0, _cell(a.get("name", "action%d" % (r + 1))))
        self.table.setItem(r, 1, _cell(" | ".join(cmds)))
        self.table.setItem(r, 2, _cell(a.get("description", "")))

    def _remove(self):
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)

    def validatePage(self):                                  # noqa: N802
        acts = []
        for r in range(self.table.rowCount()):
            t = lambda c: self.table.item(r, c).text().strip() if self.table.item(r, c) else ""
            acts.append({"name": t(0), "commands": [x.strip() for x in t(1).split("|") if x.strip()],
                         "description": t(2)})
        self.d["actions"] = acts
        return True


class InitPage(_Page):
    def __init__(self):
        super().__init__()
        self.setTitle("Initialization")
        self.setSubTitle("Sent every time the instrument connects (and reconnects after an error).")
        mono = self.font()
        mono.setFamily("monospace")
        self.cmds = QPlainTextEdit()
        self.cmds.setPlaceholderText("one command per line, e.g.\n*CLS\nTRIG:SOUR IMM\n"
                                     "(a command ending in ? is a query: its reply is logged)")
        self.cmds.setFont(mono)
        self.script = QPlainTextEdit()
        self.script.setFont(mono)
        self.script.setPlaceholderText(
            "# optional Python, run after the commands above:\n"
            "#   write(cmd), query(cmd) -> str, read(), sleep(s), log(msg), options, np, math\n"
            "# raise an exception to refuse the connection, e.g.\n"
            "# if '34461' not in query('*IDN?'):\n"
            "#     raise IOError('wrong model')\n"
            "# for k in range(1, 5):\n"
            "#     write('ROUT:CLOS (@%d)' % k)")
        self.shutdown = QPlainTextEdit()
        self.shutdown.setFont(mono)
        self.shutdown.setMaximumHeight(70)
        self.shutdown.setPlaceholderText("sent on disconnect (best effort), e.g. SYST:LOC")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Commands"))
        lay.addWidget(self.cmds, 1)
        lay.addWidget(QLabel("Script"))
        lay.addWidget(self.script, 2)
        lay.addWidget(QLabel("On disconnect"))
        lay.addWidget(self.shutdown)

    def initializePage(self):                               # noqa: N802
        init = self.d.get("init", {})
        self.cmds.setPlainText("\n".join(init.get("commands", [])))
        self.script.setPlainText(init.get("script", ""))
        self.shutdown.setPlainText("\n".join(self.d.get("shutdown", {}).get("commands", [])))

    def validatePage(self):                                  # noqa: N802
        lines = lambda w: [x.strip() for x in w.toPlainText().splitlines() if x.strip()]
        self.d["init"] = {"commands": lines(self.cmds), "script": self.script.toPlainText()}
        self.d["shutdown"] = {"commands": lines(self.shutdown)}
        return True


class FinishPage(_Page):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.setTitle("Check, try and save")
        self.setSubTitle("Try the driver on the instrument (it only connects and reads the "
                         "readable channels), then Save.")
        self.problems = shrinkable_label(QLabel(""))
        self.problems.setWordWrap(True)
        self.address = QLineEdit()
        self.address.setPlaceholderText("GPIB0::12::INSTR, TCPIP0::192.168.1.5::inst0::INSTR, "
                                        "COM3, TCP::host::5025, SIM::bus …")
        self.try_btn = QPushButton("Try it")
        self.try_btn.clicked.connect(self._try)
        self.results = QTableWidget(0, 3)
        self.results.setHorizontalHeaderLabels(["Channel", "Value / error", "Time"])
        self.results.verticalHeader().setVisible(False)
        self.results.horizontalHeader().setStretchLastSection(True)
        self.results.setColumnWidth(1, 360)
        self.results.setEditTriggers(QTableWidget.NoEditTriggers)
        self.idn = shrinkable_label(QLabel(""))
        self.idn.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.source = QPlainTextEdit()
        self.source.setReadOnly(True)
        f = self.source.font()
        f.setFamily("monospace")
        self.source.setFont(f)
        self.show_source = QCheckBox("show the generated driver file")
        self.show_source.toggled.connect(self.source.setVisible)
        self.source.setVisible(False)
        row = QHBoxLayout()
        row.addWidget(QLabel("Address"))
        row.addWidget(self.address, 1)
        row.addWidget(self.try_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.problems)
        lay.addLayout(row)
        lay.addWidget(self.idn)
        lay.addWidget(self.results, 1)
        lay.addWidget(self.show_source)
        lay.addWidget(self.source, 2)
        self.ok = False
        self.saved = None

    def initializePage(self):                               # noqa: N802
        self.ok = False
        self.problems.setText("checking…")
        self.conn.request("wizard_check", {"definition": self.d}, on_ok=self._checked,
                          on_err=lambda e: self.problems.setText("✘ %s" % e))
        self.completeChanged.emit()

    def _checked(self, r):
        errs, warns = r.get("errors", []), r.get("warnings", [])
        self.ok = not errs
        lines = ["<span style='color:#d55'>✘ %s</span>" % e for e in errs]
        lines += ["<span style='color:#e39a1b'>⚠ %s</span>" % w for w in warns]
        if self.ok:
            lines.append("<span style='color:#3a3'>✔ ready to save as %s</span>" % r.get("path", ""))
        else:
            lines.append("go back and fix the problems above")
        self.problems.setText("<br>".join(lines))
        self.source.setPlainText(r.get("source", ""))
        self.try_btn.setEnabled(self.ok)
        self.completeChanged.emit()

    def isComplete(self):                                    # noqa: N802
        return self.ok

    def _try(self):
        addr = self.address.text().strip()
        if not addr:
            self.idn.setText("enter the instrument's address first")
            return
        self.try_btn.setEnabled(False)
        self.idn.setText("connecting to %s …" % addr)
        self.results.setRowCount(0)
        self.conn.request("wizard_try", {"definition": self.d, "address": addr,
                                         "options": {"retries": 0}},
                          on_ok=self._tried, on_err=self._try_failed, timeout=300)

    def _try_failed(self, e):
        self.try_btn.setEnabled(True)
        self.idn.setText("<span style='color:#d55'>✘ %s</span>" % e)

    def _tried(self, r):
        self.try_btn.setEnabled(True)
        if not r.get("connected"):
            self.idn.setText("<span style='color:#d55'>✘ did not connect: %s</span>" % r.get("error"))
            return
        self.idn.setText("<span style='color:#3a3'>✔ connected</span>%s" % (
            (" — " + r["idn"]) if r.get("idn") else ""))
        rows = r.get("readings", {})
        self.results.setRowCount(len(rows))
        for k, (name, v) in enumerate(rows.items()):
            self.results.setItem(k, 0, _cell(name))
            if "error" in v:
                it = _cell(v["error"])
                it.setForeground(Qt.red)
                self.results.setItem(k, 1, it)
                self.results.setItem(k, 2, _cell(""))
            else:
                val = v["value"]
                text = ("%.8g" % val) if isinstance(val, float) else (
                    "array %s" % (getattr(val, "shape", ""),) if hasattr(val, "shape") else str(val))
                self.results.setItem(k, 1, _cell("%s %s" % (text, v.get("unit", ""))))
                self.results.setItem(k, 2, _cell("%s ms" % v.get("ms", "")))

    def validatePage(self):                                  # noqa: N802
        """Save (asks before replacing an existing driver)."""
        wiz = self.wizard()
        overwrite = wiz.editing == self.d["name"]
        done = {}

        def save(over):
            done.clear()
            self.conn.request("wizard_save", {"definition": self.d, "overwrite": over},
                              on_ok=lambda r: done.setdefault("r", r),
                              on_err=lambda e: done.setdefault("e", str(e)), timeout=120)
            from PySide6.QtCore import QCoreApplication, QElapsedTimer
            t = QElapsedTimer()
            t.start()
            while not done and t.elapsed() < 120000:
                QCoreApplication.processEvents()
                time.sleep(0.005)

        save(overwrite)
        if "e" in done and "exists already" in done["e"]:
            if QMessageBox.question(self, "Driver exists", "Replace the existing driver %s?"
                                    % self.d["name"]) != QMessageBox.Yes:
                return False
            save(True)
        if "r" not in done:
            QMessageBox.warning(self, "Save", "Not saved: %s" % done.get("e", "no reply"))
            return False
        self.saved = done["r"]
        return True


class DriverWizard(QWizard):
    """Create or edit a command-based driver.  After exec(), ``saved`` holds the
    backend's reply (driver name, file, reconnected instruments) or None."""

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.definition: dict = {}
        self.editing: Optional[str] = None
        self.setWindowTitle("Driver Wizard")
        self.setWizardStyle(QWizard.ClassicStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.resize(1100, 720)
        self.pages = [TemplatePage(conn), BasicsPage(), ChannelsPage(), ActionsPage(),
                      InitPage(), FinishPage(conn)]
        for p in self.pages:
            self.addPage(p)
        self.setButtonText(QWizard.FinishButton, "Save")

    @property
    def saved(self):
        return self.pages[-1].saved

    def definition_json(self) -> str:
        return json.dumps(self.definition, indent=2)
