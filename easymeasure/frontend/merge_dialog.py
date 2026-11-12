"""The "Merge setup…" wizard: take instruments, parameters, channels, composite
channels and actions from a saved setup file into the current setup.

Pages, in the order things depend on each other: the file, then one page per
kind (instruments → parameters → channels → composite channels → actions), then
a review.  Every change is checked at once (``setup_merge.SetupMerge``): each item
shows whether it can be merged, and Next stays disabled while a selected item on
the page has a problem.  The merge itself is a series of ordinary backend commands
(``add_instrument``, ``define_parameter`` …), sent one after the other.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWizard, QWizardPage)

from .setup_merge import (ERROR, KINDS, NEW, SAME, SINGULAR, TITLE, UNSELECTED, WARNING,
                          MergeFileError, SetupMerge)

STATUS_TEXT = {UNSELECTED: "", NEW: "✔ merge", SAME: "= already here", WARNING: "⚠ merge",
               ERROR: "✘ problem"}
STATUS_COLOR = {NEW: "#1e8449", SAME: "#7f8c8d", WARNING: "#b9770e", ERROR: "#c0392b"}

INTRO = {
    "instruments": "Instruments to add. An instrument already in this setup under the same "
                   "name is used as it is; give a different name in <i>Merge as</i> to add "
                   "a second one.",
    "parameters": "Parameters (named constants). Formulas may use other parameters: select "
                  "those too, or have them in this setup already.",
    "aliases": "Channels (aliases of instrument channels). Each needs its instrument: "
               "selected on the Instruments page, or already in this setup.",
    "composites": "Composite channels. Their formulas use channels and parameters, which "
                  "must be selected or already be in this setup.",
    "actions": "User actions. Names they quote (<code>get('Vxx')</code>) are renamed with "
               "the channels; check the code of an action that uses something not merged.",
}


class _ItemPage(QWizardPage):
    """One kind of item: a table with a check box, the name, "Merge as", what it is
    and whether it can be merged."""

    COLS = ["Name", "Merge as", "Definition", "Status"]

    def __init__(self, wizard: "MergeSetupDialog", kind: str):
        super().__init__()
        self.wiz = wizard
        self.kind = kind
        self.setTitle(TITLE[kind])
        self.setSubTitle(INTRO[kind])
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._changed)
        self.all_btn = QPushButton("Select all")
        self.none_btn = QPushButton("Select none")
        self.needs_btn = QPushButton("Also select what these need")
        self.needs_btn.setToolTip("select, on every page, the items of the file the selected "
                                  "ones use (unless this setup has them already)")
        self.all_btn.clicked.connect(lambda: self._select_all(True))
        self.none_btn.clicked.connect(lambda: self._select_all(False))
        self.needs_btn.clicked.connect(self._select_needs)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bl = QHBoxLayout()
        for b in (self.all_btn, self.none_btn, self.needs_btn):
            bl.addWidget(b)
        bl.addStretch(1)
        lay = QVBoxLayout(self)
        lay.addWidget(self.table, 1)
        lay.addLayout(bl)
        lay.addWidget(self.info)
        self._filling = False

    # -- content ------------------------------------------------------------------------
    def initializePage(self):                      # noqa: N802 - Qt API
        self.fill()

    def fill(self) -> None:
        m = self.wiz.merge
        items = m.items[self.kind] if m is not None else []
        self._filling = True
        try:
            self.table.setRowCount(len(items))
            for r, it in enumerate(items):
                name = QTableWidgetItem(it.name)
                name.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                name.setCheckState(Qt.Checked if it.selected else Qt.Unchecked)
                self.table.setItem(r, 0, name)
                ren = QTableWidgetItem(it.new_name)
                ren.setFlags(Qt.ItemIsEnabled | Qt.ItemIsEditable)
                ren.setToolTip("a new name for it here (leave empty to keep %s)" % it.name)
                self.table.setItem(r, 1, ren)
                d = QTableWidgetItem(it.summary())
                d.setFlags(Qt.ItemIsEnabled)
                d.setToolTip(it.summary())
                self.table.setItem(r, 2, d)
                st = QTableWidgetItem("")
                st.setFlags(Qt.ItemIsEnabled)
                self.table.setItem(r, 3, st)
            self.show_status()
        finally:
            self._filling = False
        self.table.resizeColumnToContents(0)
        self.table.setColumnWidth(1, max(90, self.table.columnWidth(1)))

    def show_status(self) -> None:
        m = self.wiz.merge
        if m is None:
            return
        was = self._filling
        self._filling = True
        try:
            for r, it in enumerate(m.items[self.kind]):
                st = self.table.item(r, 3)
                if st is None:
                    continue
                text = STATUS_TEXT.get(it.status, "")
                if it.status == NEW and it.renamed:
                    text = "✔ merge as %s" % it.target
                if it.messages:
                    text += ": " + "; ".join(it.messages)
                if it.status == UNSELECTED and m.needed_by(self.kind, it.name):
                    text = "needed by %s" % ", ".join(m.needed_by(self.kind, it.name))
                st.setText(text)
                st.setToolTip("\n".join(it.messages) or text)
                col = STATUS_COLOR.get(it.status)
                st.setForeground(QBrush(QColor(col)) if col else QBrush())
        finally:
            self._filling = was
        bad = [it for it in m.items[self.kind] if it.selected and it.status == ERROR]
        n = len([it for it in m.items[self.kind] if it.selected])
        if not m.items[self.kind]:
            self.info.setText("The file has no %s." % TITLE[self.kind].lower())
        elif bad:
            self.info.setText("<span style='color:#c0392b'>✘ %d selected %s cannot be merged "
                              "as they are: rename them, select what they need, or leave them "
                              "out.</span>" % (len(bad), TITLE[self.kind].lower()))
        else:
            self.info.setText("%d of %d selected." % (n, len(m.items[self.kind])))
        self.completeChanged.emit()

    def isComplete(self) -> bool:                  # noqa: N802 - Qt API
        m = self.wiz.merge
        return m is not None and not any(it.selected and it.status == ERROR
                                         for it in m.items[self.kind])

    # -- edits ---------------------------------------------------------------------------
    def _changed(self, cell: QTableWidgetItem) -> None:
        if self._filling or self.wiz.merge is None:
            return
        it = self.wiz.merge.items[self.kind][cell.row()]
        if cell.column() == 0:
            it.selected = cell.checkState() == Qt.Checked
        elif cell.column() == 1:
            it.new_name = cell.text().strip()
        else:
            return
        self.wiz.recheck()

    def _select_all(self, on: bool) -> None:
        for it in self.wiz.merge.items[self.kind]:
            it.selected = on
        self.fill()
        self.wiz.recheck()

    def _select_needs(self) -> None:
        m = self.wiz.merge
        added = []
        for k in KINDS:
            for it in list(m.items[k]):
                if it.selected:
                    added += m.select_with_needs(k, it.name)
        self.wiz.recheck(refill=True)
        self.info.setText(("also selected: %s" % ", ".join(
            "%s %s" % (SINGULAR[k], n) for k, n in added)) if added else
            "nothing else is needed")

    def set_checked(self, name: str, on: bool = True) -> None:
        """Tick an item as the user would (used by tests and the screenshots)."""
        r = [it.name for it in self.wiz.merge.items[self.kind]].index(name)
        self.table.item(r, 0).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def set_new_name(self, name: str, new: str) -> None:
        r = [it.name for it in self.wiz.merge.items[self.kind]].index(name)
        self.table.item(r, 1).setText(new)


class _FilePage(QWizardPage):
    def __init__(self, wizard: "MergeSetupDialog"):
        super().__init__()
        self.wiz = wizard
        self.setTitle("Merge a setup")
        self.setSubTitle("Choose a saved setup file. On the next pages, pick what to take from "
                         "it: instruments, parameters, channels, composite channels and "
                         "actions. Nothing changes until you click Merge on the last page.")
        self.path = QLineEdit()
        self.path.setPlaceholderText("setup file (.json)")
        self.path.editingFinished.connect(lambda: self.load(self.path.text()))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        lay = QVBoxLayout(self)
        lay.addLayout(row)
        lay.addWidget(self.summary)
        lay.addStretch(1)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Merge setup", "", "Setup (*.json)")
        if path:
            self.path.setText(path)
            self.load(path)

    def load(self, path: str) -> None:
        path = (path or "").strip()
        if not path:
            return
        try:
            self.wiz.merge = SetupMerge.from_file(path, self.wiz.model.state, self.wiz.drivers)
        except MergeFileError as exc:
            self.wiz.merge = None
            self.summary.setText("<span style='color:#c0392b'>✘ %s</span>" % exc)
            self.completeChanged.emit()
            return
        m = self.wiz.merge
        m.validate()
        parts = ["%d %s" % (len(m.items[k]), TITLE[k].lower()) for k in KINDS]
        text = "%s contains %s." % (path, ", ".join(parts))
        if m.file_problems:
            text += "<br>⚠ " + "<br>⚠ ".join(m.file_problems)
        self.summary.setText(text)
        self.completeChanged.emit()

    def isComplete(self) -> bool:                  # noqa: N802
        return self.wiz.merge is not None


class _ReviewPage(QWizardPage):
    def __init__(self, wizard: "MergeSetupDialog"):
        super().__init__()
        self.wiz = wizard
        self.setTitle("Review")
        self.setSubTitle("What will be merged. Click Merge to add it to the current setup.")
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.addWidget(self.text, 1)
        lay.addWidget(self.result)
        self.setCommitPage(False)

    def initializePage(self):                      # noqa: N802
        self.refresh()

    def refresh(self) -> None:
        m = self.wiz.merge
        if m is None:
            return
        m.validate()
        lines = [m.report()]
        if m.errors:
            lines += ["", "Problems (go back and fix them):"] + ["  ✘ " + e for e in m.errors]
        elif m.can_merge:
            lines += ["", "Commands that will be sent:"]
            lines += ["  %s %s" % (c, a.get("name")) for c, a in m.commands()]
        else:
            lines += ["", "Nothing to merge: select something, or everything selected is "
                      "already here."]
        self.text.setPlainText("\n".join(lines))
        self.completeChanged.emit()

    def isComplete(self) -> bool:                  # noqa: N802
        m = self.wiz.merge
        return m is not None and m.can_merge and not self.wiz.running


class MergeSetupDialog(QWizard):
    """The merge wizard.  *drivers*: the backend's driver list (for checks)."""

    def __init__(self, conn, model, drivers: Optional[dict] = None, parent=None,
                 on_done: Optional[Callable[[List[str]], None]] = None):
        super().__init__(parent)
        self.setWindowTitle("Merge setup")
        self.conn = conn
        self.model = model
        self.drivers = drivers or None
        self.merge: Optional[SetupMerge] = None
        self.running = False
        self.on_done = on_done
        self.results: List[str] = []
        self.setWizardStyle(QWizard.ClassicStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.file_page = _FilePage(self)
        self.addPage(self.file_page)
        self.pages = {}
        for k in KINDS:
            p = _ItemPage(self, k)
            self.pages[k] = p
            self.addPage(p)
        self.review = _ReviewPage(self)
        self.addPage(self.review)
        self.setButtonText(QWizard.FinishButton, "Merge")
        self.resize(820, 560)

    def recheck(self, refill: bool = False) -> None:
        if self.merge is None:
            return
        self.merge.validate()
        for p in self.pages.values():
            if refill:
                p.fill()
            else:
                p.show_status()

    # -- merging ----------------------------------------------------------------------------
    def accept(self) -> None:                      # noqa: D401 - Qt API
        m = self.merge
        if m is None or self.running:
            return
        m.validate()
        if m.errors or not m.can_merge:
            self.review.refresh()
            return
        cmds = m.commands()
        self.running = True
        self.results = []
        self.review.completeChanged.emit()
        self.button(QWizard.BackButton).setEnabled(False)
        self._send(cmds, 0)

    def _send(self, cmds, i: int) -> None:
        if i >= len(cmds):
            self._finished()
            return
        cmd, args = cmds[i]
        self.review.result.setText("merging: %s %s (%d of %d)…" % (cmd, args.get("name"),
                                                                  i + 1, len(cmds)))

        def ok(r, cmd=cmd, args=args):
            note = ""
            if cmd == "add_instrument" and isinstance(r, dict) and r.get("state") != "connected":
                note = " (not connected: %s)" % (r.get("last_error") or r.get("state"))
            self.results.append("✔ %s %s%s" % (cmd, args.get("name"), note))
            self._send(cmds, i + 1)

        def err(e, cmd=cmd, args=args):
            self.results.append("✘ %s %s: %s" % (cmd, args.get("name"), e))
            self._send(cmds, i + 1)            # independent items still go in
        self.conn.request(cmd, args, on_ok=ok, on_err=err, timeout=120)

    def _finished(self) -> None:
        self.running = False
        self.model.schedule_refresh()
        bad = [r for r in self.results if r.startswith("✘")]
        self.review.result.setText(("<span style='color:#c0392b'>%d of %d failed</span>: %s"
                                    % (len(bad), len(self.results), "; ".join(bad)))
                                   if bad else "✔ merged %d items" % len(self.results))
        if self.on_done is not None:
            self.on_done(list(self.results))
        super().accept()
