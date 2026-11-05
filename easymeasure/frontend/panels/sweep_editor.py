"""Sweep editor: create / modify / save / load arbitrary-dimensional sweeps."""

from __future__ import annotations

import json
from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QStyledItemDelegate, QCheckBox, QComboBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
                               QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from ...common.codec import to_jsonable

from ..widgets import FlowLayout, shrinkable_label

SPECIAL_RECORD = ["@time", "@elapsed"]


def _mono() -> QFont:
    f = QFont("monospace")
    f.setStyleHint(QFont.Monospace)
    return f


class CheckList(QListWidget):
    """List of channel names with check boxes; keeps checks across refreshes."""

    def set_names(self, names: List[str]) -> None:
        checked = set(self.checked())
        self.clear_items()
        for n in names + [c for c in sorted(checked) if c not in names]:
            it = QListWidgetItem(n)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if n in checked else Qt.Unchecked)
            self.addItem(it)

    def clear_items(self):
        super().clear()

    _order: List[str] = []

    def checked(self) -> List[str]:
        """Checked names, in the order they were loaded (recording order matters)."""
        names = [self.item(i).text() for i in range(self.count())
                 if self.item(i).checkState() == Qt.Checked]
        order = {n: i for i, n in enumerate(self._order)}
        return sorted(names, key=lambda n: (order.get(n, len(order)), names.index(n)))

    def set_checked(self, names: List[str]) -> None:
        self._order = list(names)
        present = {self.item(i).text() for i in range(self.count())}
        for n in names:
            if n not in present:
                it = QListWidgetItem(n)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                self.addItem(it)
        for i in range(self.count()):
            it = self.item(i)
            it.setCheckState(Qt.Checked if it.text() in names else Qt.Unchecked)

    def set_read_only(self, ro: bool) -> None:
        for i in range(self.count()):
            it = self.item(i)
            if ro:
                it.setFlags(it.flags() & ~Qt.ItemIsUserCheckable & ~Qt.ItemIsEnabled)
            else:
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)


class SweepChannelTable(QTableWidget):
    COLS = ["Channel", "Start", "Stop", "Mapping f(i,N,t,start,stop,x)", "Values (comma list)"]

    def __init__(self, parent=None):
        super().__init__(0, len(self.COLS), parent)
        self.setHorizontalHeaderLabels(self.COLS)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.horizontalHeader().setStretchLastSection(True)
        self.setColumnWidth(0, 140)
        self.setColumnWidth(3, 260)
        self.channels: List[str] = []

    def set_channels(self, names: List[str]) -> None:
        self.channels = names
        for r in range(self.rowCount()):
            cb = self.cellWidget(r, 0)
            if isinstance(cb, QComboBox):
                cur = cb.currentText()
                cb.blockSignals(True)
                cb.clear()
                cb.addItems(names)
                cb.setEditText(cur)
                cb.blockSignals(False)

    def add_row(self, entry: Optional[dict] = None) -> None:
        entry = entry or {}
        r = self.rowCount()
        self.insertRow(r)
        cb = QComboBox()
        cb.setEditable(True)
        cb.addItems(self.channels)
        cb.setEditText(entry.get("channel", self.channels[0] if self.channels else ""))
        self.setCellWidget(r, 0, cb)
        vals = entry.get("values")
        for c, key in ((1, "start"), (2, "stop"), (3, "mapping")):
            v = entry.get(key, "" if key == "mapping" else 0.0)
            self.setItem(r, c, QTableWidgetItem("" if v is None else str(v)))
        self.setItem(r, 4, QTableWidgetItem(", ".join(str(v) for v in vals) if vals else ""))

    def entries(self) -> List[dict]:
        out = []
        for r in range(self.rowCount()):
            cb = self.cellWidget(r, 0)
            ch = cb.currentText().strip() if cb else ""
            if not ch:
                continue
            e = {"channel": ch}
            txt = lambda c: (self.item(r, c).text().strip() if self.item(r, c) else "")
            values = txt(4)
            mapping = txt(3)
            if values and not mapping:
                try:
                    e["values"] = [float(x) for x in values.replace(";", ",").split(",") if x.strip()]
                except ValueError:
                    raise ValueError("row %d: values must be numbers" % (r + 1))
            else:
                for c, key in ((1, "start"), (2, "stop")):
                    t = txt(c)
                    if t:
                        try:
                            e[key] = float(t)
                        except ValueError:
                            raise ValueError("row %d: %s must be a number" % (r + 1, key))
                if mapping:
                    e["mapping"] = mapping
                e.setdefault("start", 0.0)
                e.setdefault("stop", 0.0)
            out.append(e)
        return out

    def set_read_only(self, ro: bool) -> None:
        trig = QAbstractItemView.NoEditTriggers if ro else QAbstractItemView.AllEditTriggers
        self.setEditTriggers(trig)
        for r in range(self.rowCount()):
            w = self.cellWidget(r, 0)
            if w:
                w.setEnabled(not ro)


class DimensionEditor(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = QSpinBox()
        self.points.setRange(1, 10_000_000)
        self.points.setValue(11)
        self.pre = QDoubleSpinBox()
        self.pre.setRange(0, 1e5)
        self.pre.setDecimals(4)
        self.pre.setSuffix(" s")
        self.post = QDoubleSpinBox()
        self.post.setRange(0, 1e5)
        self.post.setDecimals(4)
        self.post.setSuffix(" s")
        self.snake = QCheckBox("snake scan")
        self.snake.setToolTip("alternate the sweep direction on every pass")
        self.after = QCheckBox("record after inner dims")
        self.after.setToolTip("record this dimension's channels after the inner dimensions "
                              "have run instead of before")
        self.label = QLineEdit()
        form = QFormLayout()
        form.addRow("Points", self.points)
        form.addRow("Pre-sampling delay", self.pre)
        form.addRow("Post-sampling delay", self.post)
        form.addRow("Label", self.label)
        form.addRow(self.snake)
        form.addRow(self.after)
        self.table = SweepChannelTable()
        add = QPushButton("+ sweep channel")
        rm = QPushButton("− remove selected")
        add.clicked.connect(lambda: self.table.add_row())
        rm.clicked.connect(self._remove_rows)
        self._row_buttons = [add, rm]
        self.record = CheckList()
        sweep_box = QGroupBox("Swept channels")
        sweep_box.setToolTip("channels set at every step of this dimension")
        sl = QVBoxLayout(sweep_box)
        sl.addWidget(self.table)
        hb = FlowLayout()
        hb.addWidget(add)
        hb.addWidget(rm)
        sl.addLayout(hb)
        rec_box = QGroupBox("Recorded channels")
        rl = QVBoxLayout(rec_box)
        rl.addWidget(self.record)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)   # labels go above fields when narrow
        form_box = QWidget()
        form_box.setLayout(form)
        top = QSplitter(Qt.Horizontal)
        top.addWidget(form_box)
        top.addWidget(rec_box)
        top.setChildrenCollapsible(False)
        lay = QVBoxLayout(self)
        lay.addWidget(top)
        lay.addWidget(sweep_box, 1)

    def _remove_rows(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows and self.table.rowCount():
            rows = [self.table.rowCount() - 1]
        for r in rows:
            self.table.removeRow(r)

    def set_channels(self, writable: List[str], readable: List[str]) -> None:
        self.table.set_channels(writable)
        self.record.set_names(readable + SPECIAL_RECORD)

    def to_dict(self) -> dict:
        return {"points": self.points.value(), "sweep": self.table.entries(),
                "record": self.record.checked(), "pre_delay": self.pre.value(),
                "post_delay": self.post.value(), "snake": self.snake.isChecked(),
                "record_after_inner": self.after.isChecked(), "label": self.label.text()}

    def load(self, d: dict) -> None:
        self.points.setValue(int(d.get("points", 1)))
        self.pre.setValue(float(d.get("pre_delay", 0) or 0))
        self.post.setValue(float(d.get("post_delay", 0) or 0))
        self.snake.setChecked(bool(d.get("snake")))
        self.after.setChecked(bool(d.get("record_after_inner")))
        self.label.setText(d.get("label", "") or "")
        self.table.setRowCount(0)
        for e in d.get("sweep", []):
            self.table.add_row(e)
        self.record.set_checked(list(d.get("record", [])))

    def set_read_only(self, ro: bool) -> None:
        for w in (self.points, self.pre, self.post, self.label):
            w.setReadOnly(ro)
        for w in (self.snake, self.after, *self._row_buttons):
            w.setEnabled(not ro)
        self.table.set_read_only(ro)
        self.record.set_read_only(ro)


class _ValueDelegate(QStyledItemDelegate):
    """Value cell of a constant: a combo box when the channel only accepts certain values."""

    def __init__(self, editor: "PhaseEditor"):
        super().__init__(editor.consts)
        self.phase_editor = editor

    def _choices(self, index):
        cb = self.phase_editor.consts.cellWidget(index.row(), 0)
        return self.phase_editor.choices.get(cb.currentText().strip()) if cb is not None else None

    def createEditor(self, parent, option, index):          # noqa: N802 - Qt API
        ch = self._choices(index)
        if not ch:
            return super().createEditor(parent, option, index)
        box = QComboBox(parent)
        box.addItems([c if isinstance(c, str) else "%g" % c for c in ch])
        return box

    def setEditorData(self, editor, index):                 # noqa: N802
        if isinstance(editor, QComboBox):
            cur = str(index.data() or "").casefold()
            i = next((k for k in range(editor.count()) if editor.itemText(k).casefold() == cur), 0)
            editor.setCurrentIndex(i)
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):           # noqa: N802
        if isinstance(editor, QComboBox):
            model.setData(index, editor.currentText())
        else:
            super().setModelData(editor, model, index)


class PhaseEditor(QWidget):
    """Pre- or post-sweep settings: constants, one-off recordings, hook code."""

    def __init__(self, phase: str, parent=None):
        super().__init__(parent)
        self.phase = phase
        self.consts = QTableWidget(0, 2)
        self.consts.setHorizontalHeaderLabels(["Channel", "Value (number or text)"])
        self.consts.horizontalHeader().setStretchLastSection(True)
        self.consts.setColumnWidth(0, 160)
        self.channels: List[str] = []
        self.choices: dict = {}
        self.consts.setItemDelegateForColumn(1, _ValueDelegate(self))
        add = QPushButton("+ constant")
        rm = QPushButton("− remove")
        add.clicked.connect(lambda: self.add_const())
        rm.clicked.connect(lambda: self.consts.removeRow(self.consts.currentRow()
                                                         if self.consts.currentRow() >= 0
                                                         else self.consts.rowCount() - 1))
        self._btns = [add, rm]
        self.record = CheckList()
        self.hook = QPlainTextEdit()
        from ..display import register_font
        register_font(self.hook, 1.0, monospace=True)
        self.hook.setPlaceholderText(
            "# Python executed %s the sweep, e.g.\n# action('dmm.reset')\n# set('Tset', 4.2)\n"
            "# sleep(1)\n# log('ready, T = %%g' %% get('T'))"
            % ("before" if phase == "pre" else "after"))
        self.action_box = QComboBox()
        self.action_box.setToolTip("instrument actions (commands without arguments)")
        self.add_action_btn = QPushButton("+ action")
        self.add_action_btn.setToolTip("append the selected action to the hook code")
        self.add_action_btn.clicked.connect(self.append_action)
        cbox = QGroupBox("Constants")
        cbox.setToolTip("set %s the sweep" % ("before" if phase == "pre" else "after"))
        cl = QVBoxLayout(cbox)
        cl.addWidget(self.consts)
        hb = FlowLayout()
        hb.addWidget(add)
        hb.addWidget(rm)
        cl.addLayout(hb)
        rbox = QGroupBox("Record once")
        rbox.setToolTip("recorded once %s the sweep" % ("before" if phase == "pre" else "after"))
        QVBoxLayout(rbox).addWidget(self.record)
        hbox = QGroupBox("Hook code")
        hl = QVBoxLayout(hbox)
        hl.addWidget(self.hook)
        ab = FlowLayout()
        ab.addWidget(QLabel("Action"))
        ab.addWidget(self.action_box)
        ab.addWidget(self.add_action_btn)
        hl.addLayout(ab)
        top = QSplitter(Qt.Horizontal)
        top.addWidget(cbox)
        top.addWidget(rbox)
        top.setChildrenCollapsible(False)
        lay = QVBoxLayout(self)
        order = shrinkable_label(QLabel("Order: hook → constants → record" if phase == "pre"
                       else "Order: record → constants → hook (always runs, also after errors/abort)"))
        order.setStyleSheet("color: gray")
        lay.addWidget(order)
        lay.addWidget(top, 1)
        lay.addWidget(hbox, 1)

    def set_actions(self, actions: List[str]) -> None:
        cur = self.action_box.currentText()
        self.action_box.clear()
        self.action_box.addItems(actions)
        if cur in actions:
            self.action_box.setCurrentText(cur)
        self.add_action_btn.setEnabled(bool(actions) and not self.hook.isReadOnly())

    def append_action(self) -> None:
        ref = self.action_box.currentText().strip()
        if not ref:
            return
        text = self.hook.toPlainText()
        if text and not text.endswith("\n"):
            text += "\n"
        self.hook.setPlainText(text + "action(%r)\n" % ref)

    def add_const(self, entry: Optional[dict] = None):
        entry = entry or {}
        r = self.consts.rowCount()
        self.consts.insertRow(r)
        cb = QComboBox()
        cb.setEditable(True)
        cb.addItems(self.channels)
        cb.setEditText(entry.get("channel", ""))
        self.consts.setCellWidget(r, 0, cb)
        self.consts.setItem(r, 1, QTableWidgetItem(str(entry.get("value", ""))))

    def set_channels(self, writable, readable, choices=None):
        self.channels = writable
        self.choices = dict(choices or {})
        for r in range(self.consts.rowCount()):
            cb = self.consts.cellWidget(r, 0)
            cur = cb.currentText()
            cb.blockSignals(True)
            cb.clear()
            cb.addItems(writable)
            cb.setEditText(cur)
            cb.blockSignals(False)
        self.record.set_names(readable)

    def to_dict(self) -> dict:
        consts = []
        for r in range(self.consts.rowCount()):
            ch = self.consts.cellWidget(r, 0).currentText().strip()
            txt = self.consts.item(r, 1).text().strip() if self.consts.item(r, 1) else ""
            if not ch:
                continue
            try:
                val = float(txt)
            except ValueError:
                val = txt
            consts.append({"channel": ch, "value": val})
        return {"constants": consts, "record": self.record.checked(),
                "hook": self.hook.toPlainText()}

    def load(self, d: dict) -> None:
        d = d or {}
        self.consts.setRowCount(0)
        for c in d.get("constants", []):
            self.add_const(c)
        self.record.set_checked(list(d.get("record", [])))
        self.hook.setPlainText(d.get("hook", "") or "")

    def set_read_only(self, ro: bool):
        self.consts.setEditTriggers(QAbstractItemView.NoEditTriggers if ro
                                    else QAbstractItemView.AllEditTriggers)
        for r in range(self.consts.rowCount()):
            self.consts.cellWidget(r, 0).setEnabled(not ro)
        for b in self._btns:
            b.setEnabled(not ro)
        self.record.set_read_only(ro)
        self.hook.setReadOnly(ro)
        self.add_action_btn.setEnabled(not ro and self.action_box.count() > 0)
        self.action_box.setEnabled(not ro)


class SweepEditor(QWidget):
    """Full sweep definition editor.  ``read_only=True`` for the running sweep."""

    queueRequested = Signal(dict, bool)   # (definition, start)

    def __init__(self, conn=None, model=None, read_only: bool = False, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.model = model
        self.read_only = read_only
        self.name = QLineEdit("sweep")
        self.comment = QLineEdit()
        self.comment.setPlaceholderText("comment / sample (saved as metadata)")
        self.on_read = QComboBox()
        self.on_read.addItems(["nan", "abort"])
        self.on_read.setToolTip("what to do when a recording fails after all retries")
        self.on_set = QComboBox()
        self.on_set.addItems(["abort", "skip"])
        self.post_on_abort = QCheckBox("post actions on abort")
        self.post_on_abort.setToolTip("run the post-sweep record/constants/hook also when aborted")
        self.post_on_abort.setChecked(True)
        self.ppt = QCheckBox("PPT slide when done")
        self.ppt.setToolTip("append a summary slide to the configured PowerPoint file")
        self.ppt.setChecked(True)
        head = QFormLayout()
        head.addRow("Name", self.name)
        head.addRow("Comment", self.comment)
        opts = FlowLayout()
        for lab, w in (("on read error", self.on_read), ("on set error", self.on_set)):
            box = QWidget()
            hl = QHBoxLayout(box)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.addWidget(QLabel(lab))
            hl.addWidget(w)
            opts.addWidget(box)
        opts.addWidget(self.post_on_abort)
        opts.addWidget(self.ppt)
        head.addRow(opts)

        # dimensions
        self.dim_list = QListWidget()
        self.dim_list.setMaximumWidth(220)
        self.dim_list.setMinimumWidth(70)
        self.dim_stack = QStackedWidget()
        self.dim_list.currentRowChanged.connect(self.dim_stack.setCurrentIndex)
        self.dims: List[DimensionEditor] = []
        b_add = QPushButton("+ dimension")
        b_rm = QPushButton("− dimension")
        b_up = QPushButton("▲ inner")
        b_dn = QPushButton("▼ outer")
        b_add.clicked.connect(lambda: self.add_dimension())
        b_rm.clicked.connect(self.remove_dimension)
        b_up.clicked.connect(lambda: self.move_dimension(-1))
        b_dn.clicked.connect(lambda: self.move_dimension(+1))
        self._dim_buttons = [b_add, b_rm, b_up, b_dn]
        left = QVBoxLayout()
        lab = shrinkable_label(QLabel("Dim 1 = innermost (fastest)"))
        lab.setStyleSheet("color: gray")
        left.addWidget(lab)
        left.addWidget(self.dim_list, 1)
        for b in self._dim_buttons:
            left.addWidget(b)
        dims_host = QWidget()
        dl = QHBoxLayout(dims_host)
        dl.addLayout(left)
        dl.addWidget(self.dim_stack, 1)

        self.pre = PhaseEditor("pre")
        self.post = PhaseEditor("post")
        self.tabs = QTabWidget()
        self.tabs.addTab(dims_host, "Dimensions")
        self.tabs.addTab(self.pre, "Pre-sweep")
        self.tabs.addTab(self.post, "Post-sweep")

        # buttons
        self.status = shrinkable_label(QLabel(""))
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.btn_new = QPushButton("New")
        self.btn_load = QPushButton("Load…")
        self.btn_from_data = QPushButton("Load from data file…")
        self.btn_save = QPushButton("Save…")
        self.btn_validate = QPushButton("Validate")
        self.btn_queue = QPushButton("Add to queue")
        self.btn_run = QPushButton("Add && start")
        self.btn_new.clicked.connect(self.new)
        self.btn_load.clicked.connect(self.load_file)
        self.btn_from_data.clicked.connect(self.load_from_data)
        self.btn_save.clicked.connect(self.save_file)
        self.btn_validate.clicked.connect(self.validate)
        self.btn_queue.clicked.connect(lambda: self._queue(False))
        self.btn_run.clicked.connect(lambda: self._queue(True))
        bar = FlowLayout()
        for b in (self.btn_new, self.btn_load, self.btn_from_data, self.btn_save,
                  self.btn_validate, self.btn_queue, self.btn_run):
            bar.addWidget(b)
        lay = QVBoxLayout(self)
        lay.addLayout(head)
        lay.addWidget(self.tabs, 1)
        lay.addWidget(self.status)
        lay.addLayout(bar)
        if model is not None:
            model.stateChanged.connect(lambda _: self.refresh_channels())
        self.new()
        self.set_read_only(read_only)

    # ------------------------------------------------------------ dimensions --
    def _channel_lists(self):
        if self.model is None:
            return [], []
        return self.model.writable_channels(numeric_only=True), \
            self.model.readable_channels()

    def refresh_channels(self) -> None:
        w, r = self._channel_lists()
        for d in self.dims:
            d.set_channels(w, r)
        # pre/post constants and one-off records may also use configuration channels
        m = self.model
        wall = (m.writable_channels() + m.config_channels(writable=True)) if m else []
        rall = (r + m.config_channels()) if m else r
        ch = m.channel_choices() if m else {}
        self.pre.set_channels(wall, rall, ch)
        self.post.set_channels(wall, rall, ch)
        acts = m.actions() if m else []
        self.pre.set_actions(acts)
        self.post.set_actions(acts)
        if self.read_only:
            self.set_read_only(True)

    def _relabel(self):
        for i in range(self.dim_list.count()):
            d = self.dims[i].to_dict() if i < len(self.dims) else {}
            sw = ", ".join(e["channel"] for e in d.get("sweep", [])) or "—"
            self.dim_list.item(i).setText("Dim %d: %s ×%d" % (i + 1, sw, d.get("points", 0)))

    def add_dimension(self, d: Optional[dict] = None) -> DimensionEditor:
        ed = DimensionEditor()
        w, r = self._channel_lists()
        ed.set_channels(w, r)
        if d:
            ed.load(d)
        ed.points.valueChanged.connect(self._relabel)
        ed.table.itemChanged.connect(lambda *_: self._relabel())
        self.dims.append(ed)
        self.dim_stack.addWidget(ed)
        self.dim_list.addItem("")
        self.dim_list.setCurrentRow(len(self.dims) - 1)
        self._relabel()
        if self.read_only:
            ed.set_read_only(True)
        return ed

    def remove_dimension(self):
        i = self.dim_list.currentRow()
        if i < 0 or len(self.dims) <= 1:
            return
        ed = self.dims.pop(i)
        self.dim_stack.removeWidget(ed)
        ed.deleteLater()
        self.dim_list.takeItem(i)
        self._relabel()

    def move_dimension(self, delta: int):
        i = self.dim_list.currentRow()
        j = i + delta
        if i < 0 or not 0 <= j < len(self.dims):
            return
        defs = [d.to_dict() for d in self.dims]
        defs[i], defs[j] = defs[j], defs[i]
        self._load_dims(defs)
        self.dim_list.setCurrentRow(j)

    def _load_dims(self, defs: List[dict]):
        for ed in self.dims:
            self.dim_stack.removeWidget(ed)
            ed.deleteLater()
        self.dims = []
        self.dim_list.clear()
        for d in defs:
            self.add_dimension(d)
        if self.dims:
            self.dim_list.setCurrentRow(0)

    # -------------------------------------------------------------- (de)serialise
    def definition(self) -> dict:
        meta = {}
        if self.comment.text().strip():
            meta["comment"] = self.comment.text().strip()
        return {"name": self.name.text().strip() or "sweep",
                "dimensions": [d.to_dict() for d in self.dims],
                "pre": self.pre.to_dict(), "post": self.post.to_dict(),
                "on_read_error": self.on_read.currentText(),
                "on_set_error": self.on_set.currentText(),
                "run_post_on_abort": self.post_on_abort.isChecked(),
                "ppt": self.ppt.isChecked(), "metadata": meta}

    def load_definition(self, d: Optional[dict]) -> None:
        if not d:
            self.new()
            return
        self.name.setText(str(d.get("name", "sweep")))
        meta = d.get("metadata") or {}
        self.comment.setText(str(meta.get("comment", "")))
        self.on_read.setCurrentText(d.get("on_read_error", "nan"))
        self.on_set.setCurrentText(d.get("on_set_error", "abort"))
        self.post_on_abort.setChecked(bool(d.get("run_post_on_abort", True)))
        self.ppt.setChecked(bool(d.get("ppt", True)))
        self._load_dims(list(d.get("dimensions", [])) or [{"points": 11}])
        self.pre.load(d.get("pre"))
        self.post.load(d.get("post"))
        if self.read_only:
            self.set_read_only(True)

    def new(self) -> None:
        self.load_definition({"name": "sweep", "dimensions": [{"points": 11, "sweep": [],
                                                                 "record": []}]})

    def set_read_only(self, ro: bool) -> None:
        self.read_only = ro
        for w in (self.name, self.comment):
            w.setReadOnly(ro)
        for w in (self.on_read, self.on_set, self.post_on_abort, self.ppt, *self._dim_buttons,
                  self.btn_new, self.btn_load, self.btn_from_data, self.btn_validate,
                  self.btn_queue, self.btn_run):
            w.setEnabled(not ro)
            w.setVisible(not ro or w in (self.on_read, self.on_set, self.post_on_abort, self.ppt))
        for d in self.dims:
            d.set_read_only(ro)
        self.pre.set_read_only(ro)
        self.post.set_read_only(ro)

    # --------------------------------------------------------------- file I/O --
    def load_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load sweep", "", "Sweep (*.json);;All (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if "definition" in d and "dimensions" not in d:
                d = d["definition"]
            self.load_definition(d)
            self.status.setText("loaded %s" % path)
        except Exception as exc:
            QMessageBox.warning(self, "Load sweep", "Cannot load %s:\n%s" % (path, exc))

    def load_from_data(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load sweep from data file", "",
                                              "easyMeasure data (*.emj *.h5);;All (*)")
        if not path:
            return
        from ...backend.storage import read_header
        from ..workers import run_in_background
        run_in_background(lambda: read_header(path),
                          lambda h: (self.load_definition(h.get("definition")),
                                     self.status.setText("sweep loaded from %s" % path)),
                          lambda e: QMessageBox.warning(self, "Load", e))

    def save_file(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save sweep", self.name.text() + ".json",
                                              "Sweep (*.json)")
        if not path:
            return
        try:
            d = self.definition()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(to_jsonable(d), f, indent=2)
            self.status.setText("saved %s" % path)
        except Exception as exc:
            QMessageBox.warning(self, "Save sweep", str(exc))

    # ------------------------------------------------------------- backend --
    def _collect(self) -> Optional[dict]:
        try:
            return self.definition()
        except ValueError as exc:
            self.status.setText("<span style='color:#d55'>%s</span>" % exc)
            return None

    def validate(self):
        d = self._collect()
        if d is None or self.conn is None:
            return
        self.status.setText("validating…")

        def ok(res):
            txt = "✔ valid: shape %s = %d points, ≥ %.1f s of delays" % (
                "×".join(map(str, res["shape"])), res["total_points"], res["min_duration"])
            if res.get("warnings"):
                txt += "<br>⚠ " + "<br>⚠ ".join(res["warnings"])
            self.status.setText(txt)
        self.conn.request("validate_sweep", {"definition": d}, on_ok=ok,
                          on_err=lambda e: self.status.setText(
                              "<span style='color:#d55'>✘ %s</span>" % e.replace("; ", "<br>")))

    def _queue(self, start: bool):
        d = self._collect()
        if d is None or self.conn is None:
            return
        self.status.setText("queueing…")
        self.conn.request("queue_add", {"item": {"kind": "sweep", "definition": d}, "start": start},
                          on_ok=lambda i: self.status.setText("queued as item #%s%s" % (
                              i, " and started" if start else "")),
                          on_err=lambda e: self.status.setText(
                              "<span style='color:#d55'>✘ %s</span>" % e.replace("; ", "<br>")))


class RunningSweepView(QWidget):
    """Read-only view of the sweep currently being executed."""

    copyDefinition = Signal(dict)

    def __init__(self, conn, model, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.label = shrinkable_label(QLabel("No sweep running."))
        self.editor = SweepEditor(conn, model, read_only=True)
        copy = QPushButton("Copy to editor")
        copy.clicked.connect(lambda: self.copyDefinition.emit(self.editor.definition()))
        top = QHBoxLayout()
        top.addWidget(self.label, 1)
        top.addWidget(copy)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.editor, 1)
        self.run = None
        conn.event.connect(self._on_event)
        conn.connected.connect(lambda _: self.refresh())

    def refresh(self):
        self.conn.request("current_run", on_ok=self._got)

    def _got(self, cur):
        if not cur:
            self.label.setText("No sweep running." + (" (showing the last one)" if self.run else ""))
            return
        self.run = cur.get("run")
        self.label.setText("<b>Running: run %s – %s</b>" % (cur.get("run"), cur.get("name")))
        self.editor.load_definition(cur.get("definition"))

    def _on_event(self, kind, data):
        if kind == "sweep_start" and isinstance(data, dict):
            self.run = data.get("run")
            hdr = data.get("header") or {}
            self.label.setText("<b>Running: run %s – %s</b>" % (data.get("run"), hdr.get("name")))
            self.editor.load_definition(hdr.get("definition"))
        elif kind == "sweep_end" and isinstance(data, dict):
            self.label.setText("Run %s ended: %s %s" % (data.get("run"), data.get("status"),
                                                       data.get("error") or ""))
