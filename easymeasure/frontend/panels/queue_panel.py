"""Run queue panel: state, controls, queued items, history and a script editor."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QCheckBox, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

STATUS_COLORS = {"finished": "#3a3", "error": "#d44", "aborted": "#c80", "running": "#38f",
                 "queued": "#888"}

SCRIPT_HELP = ("# get(name), set(name, value), set_raw('inst.ch', v), sleep(s),\n"
               "# wait_until(fn, timeout), log(msg), instrument(name).raw_query(cmd),\n"
               "# run_sweep(definition), state (persistent dict), np, math, time\n")


class QueuePanel(QWidget):
    rerunRequested = Signal(dict)     # a history item (sweep definition or script)
    openDataRequested = Signal(str)   # path of a run's data file

    def __init__(self, conn, model, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.model = model
        self.state_label = QLabel("<b>state: ?</b>")
        btns = []
        for text, cmd in (("▶ Start queue", "queue_start"), ("Stop after current", "queue_stop"),
                          ("⏸ Pause", "pause"), ("Resume", "resume"), ("■ Abort current", "abort"),
                          ("Abort all", "abort_all")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, c=cmd: self._cmd(c))
            btns.append(b)
        self.stop_on_error = QCheckBox("stop queue on error")
        self.stop_on_error.toggled.connect(
            lambda v: self.conn.request("queue_options", {"stop_on_error": v}))
        top = QHBoxLayout()
        top.addWidget(self.state_label)
        top.addStretch(1)
        for b in btns:
            top.addWidget(b)
        top.addWidget(self.stop_on_error)

        self.queued = QTableWidget(0, 4)
        self.queued.setHorizontalHeaderLabels(["#", "Kind", "Name", "Shape / info"])
        self.queued.horizontalHeader().setStretchLastSection(True)
        self.queued.setSelectionBehavior(QTableWidget.SelectRows)
        self.queued.setEditTriggers(QTableWidget.NoEditTriggers)
        qb = QHBoxLayout()
        for text, fn in (("▲ up", lambda: self._move(-1)), ("▼ down", lambda: self._move(1)),
                         ("Remove", self._remove), ("Clear queue", lambda: self._cmd("queue_clear"))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            qb.addWidget(b)
        qb.addStretch(1)
        qbox = QGroupBox("Queued")
        ql = QVBoxLayout(qbox)
        self.current = QLabel("current: —")
        ql.addWidget(self.current)
        ql.addWidget(self.queued)
        ql.addLayout(qb)

        self.history = QTableWidget(0, 6)
        self.history.setHorizontalHeaderLabels(["#", "Kind", "Name", "Status", "Ended", "Run / error"])
        self.history.horizontalHeader().setStretchLastSection(True)
        self.history.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history.setSelectionBehavior(QTableWidget.SelectRows)
        self.history.setSelectionMode(QTableWidget.SingleSelection)
        self._hist_items = []
        self.rerun_btn = QPushButton("⟳ Rerun selected")
        self.rerun_btn.setToolTip("queue the selected sweep or script again (same definition) and start")
        self.rerun_btn.clicked.connect(self._rerun)
        self.open_btn = QPushButton("Open data")
        self.open_btn.setToolTip("open the data file of the selected run")
        self.open_btn.clicked.connect(self._open_data)
        self.history.itemSelectionChanged.connect(self._hist_selection)
        self._hist_selection()
        hb = QHBoxLayout()
        hb.addWidget(self.rerun_btn)
        hb.addWidget(self.open_btn)
        hb.addStretch(1)
        hbox = QGroupBox("History")
        hl = QVBoxLayout(hbox)
        hl.addWidget(self.history)
        hl.addLayout(hb)

        self.script = QPlainTextEdit()
        f = QFont("monospace")
        f.setStyleHint(QFont.Monospace)
        self.script.setFont(f)
        self.script.setPlainText(SCRIPT_HELP + "set('Vg', 0.1)\nsleep(1)\nlog('I = %g' % get('I'))\n")
        self.script_name = QLineEdit("script")
        b1 = QPushButton("Add script to queue")
        b2 = QPushButton("Run now (front of queue)")
        b1.clicked.connect(lambda: self._add_script(False))
        b2.clicked.connect(lambda: self._add_script(True))
        sb = QHBoxLayout()
        sb.addWidget(QLabel("Name"))
        sb.addWidget(self.script_name)
        sb.addStretch(1)
        sb.addWidget(b1)
        sb.addWidget(b2)
        sbox = QGroupBox("Script")
        sl = QVBoxLayout(sbox)
        sl.addWidget(self.script)
        sl.addLayout(sb)
        self.msg = QLabel("")
        split = QSplitter(Qt.Vertical)
        split.addWidget(qbox)
        split.addWidget(hbox)
        split.addWidget(sbox)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addWidget(self.msg)
        self._ids = []
        model.queueChanged.connect(self.update_queue)

    def _cmd(self, cmd, args=None):
        self.conn.request(cmd, args or {}, on_err=lambda e: self.msg.setText("✘ %s: %s" % (cmd, e)))

    def _selected_id(self):
        r = self.queued.currentRow()
        if 0 <= r < len(self._ids):
            return self._ids[r], r
        return None, None

    def _move(self, d):
        i, r = self._selected_id()
        if i is not None:
            self._cmd("queue_move", {"item_id": i, "position": max(0, r + d)})
            self.queued.selectRow(max(0, min(self.queued.rowCount() - 1, r + d)))

    def _remove(self):
        i, _ = self._selected_id()
        if i is not None:
            self._cmd("queue_remove", {"item_id": i})

    def _add_script(self, now: bool):
        code = self.script.toPlainText()
        name = self.script_name.text() or "script"
        try:
            compile(code, name, "exec")
        except SyntaxError as exc:
            self.msg.setText("✘ syntax error line %s: %s" % (exc.lineno, exc.msg))
            return
        if now:
            self.conn.request("run_script_now", {"code": code, "name": name},
                              on_ok=lambda i: self.msg.setText("script #%s started" % i),
                              on_err=lambda e: self.msg.setText("✘ %s" % e))
        else:
            self.conn.request("queue_add", {"item": {"kind": "script", "code": code, "name": name}},
                              on_ok=lambda i: self.msg.setText("script queued as #%s" % i),
                              on_err=lambda e: self.msg.setText("✘ %s" % e))

    def _selected_history(self):
        r = self.history.currentRow()
        if not self.history.selectedItems() or not 0 <= r < len(self._hist_items):
            return None
        return self._hist_items[r]

    def _hist_selection(self):
        it = self._selected_history()
        self.rerun_btn.setEnabled(it is not None and (
            (it.get("kind") == "sweep" and bool(it.get("definition"))) or
            (it.get("kind") == "script" and it.get("code") is not None)))
        self.open_btn.setEnabled(it is not None and bool(it.get("path")))

    def _rerun(self):
        it = self._selected_history()
        if it is not None:
            self.rerunRequested.emit(it)

    def _open_data(self):
        it = self._selected_history()
        if it is not None and it.get("path"):
            self.openDataRequested.emit(it["path"])

    @staticmethod
    def _info(item):
        if item.get("kind") == "sweep":
            return "×".join(str(n) for n in item.get("shape", []))
        code = item.get("code", "")
        return code.strip().splitlines()[0][:60] if code.strip() else ""

    def update_queue(self, q: dict) -> None:
        state = q.get("state", "?")
        color = {"idle": "#888", "running": "#38f", "paused": "#c80", "aborting": "#d44"}.get(state, "#888")
        self.state_label.setText("<b>state: <span style='color:%s'>%s</span></b>" % (color, state))
        self.stop_on_error.blockSignals(True)
        self.stop_on_error.setChecked(bool(q.get("stop_on_error")))
        self.stop_on_error.blockSignals(False)
        cur = q.get("current")
        if cur:
            self.current.setText("current: #%s %s <b>%s</b> %s" % (
                cur["id"], cur["kind"], cur.get("name", ""), self._info(cur)))
        else:
            self.current.setText("current: —")
        items = q.get("queued", [])
        self._ids = [i["id"] for i in items]
        self.queued.setRowCount(len(items))
        for r, it in enumerate(items):
            for c, v in enumerate((it["id"], it["kind"], it.get("name", ""), self._info(it))):
                self.queued.setItem(r, c, QTableWidgetItem(str(v)))
        hist = list(reversed(q.get("history", [])))
        sel = self._selected_history()
        sel_id = sel.get("id") if sel else None
        self._hist_items = hist
        self.history.blockSignals(True)
        self.history.setRowCount(len(hist))
        for r, it in enumerate(hist):
            ended = time.strftime("%H:%M:%S", time.localtime(it["ended"])) if it.get("ended") else ""
            extra = ("run %s" % it["run"]) if it.get("run") else ""
            if it.get("error"):
                extra = (extra + "  " if extra else "") + it["error"]
            vals = (it["id"], it["kind"], it.get("name", ""), it.get("status", ""), ended, extra)
            for c, v in enumerate(vals):
                cell = QTableWidgetItem(str(v))
                if c == 3:
                    cell.setForeground(QColor(STATUS_COLORS.get(it.get("status"), "#888")))
                if c == 5 and it.get("path"):
                    cell.setToolTip(it["path"])
                self.history.setItem(r, c, cell)
        self.history.clearSelection()
        for r, it in enumerate(hist):
            if sel_id is not None and it.get("id") == sel_id:
                self.history.selectRow(r)        # keep the selection across refreshes
        self.history.blockSignals(False)
        self._hist_selection()
