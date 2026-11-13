"""Continuing an aborted sweep: where it stopped, why, where it goes on, and how
long to wait after setting the channels to that point before measuring again."""

from __future__ import annotations

import time

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QLabel, QVBoxLayout)


def _when(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "?"


def _num(v) -> str:
    if isinstance(v, float):
        return "%.6g" % v
    return str(v)


def describe(info: dict) -> str:
    """The dialog's text (rich text) for a ``continue_info`` reply."""
    why = info.get("error") or {"aborted": "aborted by the user"}.get(info.get("status"), "")
    lines = ["<b>Run %s</b> (%s) stopped: <b>%s</b>%s, at %s." % (
        info.get("run"), info.get("name", ""), info.get("status"),
        (" — " + why) if why else "", _when(info.get("ended"))),
        "%d of %d points were measured; they are copied into a <b>new data file</b> (the old "
        "file stays as it is) and the run goes on from the first point not measured." % (
            info.get("points", 0), info.get("total", 0))]
    nxt = info.get("next_index")
    if nxt is not None:
        sp = ", ".join("%s = %s" % (k, _num(v)) for k, v in info.get("next_setpoints", {}).items())
        lines.append("Next point: index %s%s." % (nxt, (": " + sp) if sp else ""))
    n = len(info.get("aborts") or [])
    if n:
        lines.append("It was continued %d time%s before; every abort is kept in the data file."
                     % (n, "" if n == 1 else "s"))
    return "<br><br>".join(lines)


class ContinueDialog(QDialog):
    """Asks how to continue; ``values()`` gives the arguments of ``continue_sweep``."""

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Continue run %s" % info.get("run"))
        self.info = info
        lay = QVBoxLayout(self)
        text = QLabel(describe(info))
        text.setWordWrap(True)
        lay.addWidget(text)
        form = QFormLayout()
        self.wait = QDoubleSpinBox()
        self.wait.setRange(0.0, 7 * 24 * 3600.0)
        self.wait.setDecimals(1)
        self.wait.setSuffix(" s")
        self.wait.setValue(0.0)
        self.wait.setToolTip("after every sweep channel is set to the next point, wait this long "
                             "before measuring (the sample settles, a temperature recovers ...)")
        form.addRow("Wait after setting the channels:", self.wait)
        self.rerun_pre = QCheckBox("run the pre-sweep settings and hook again first")
        self.rerun_pre.setChecked(True)
        self.rerun_pre.setToolTip("the post-sweep settings may have changed things at the abort "
                                  "(outputs off, ranges); the pre-sweep values recorded now are "
                                  "kept as an event, the original ones stay")
        form.addRow("", self.rerun_pre)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.start_btn = bb.addButton("Continue now", QDialogButtonBox.AcceptRole)
        self.queue_btn = bb.addButton("Add to queue", QDialogButtonBox.AcceptRole)
        self.start_btn.setToolTip("queue the continuation and start the queue")
        self.queue_btn.setToolTip("queue the continuation without starting the queue")
        self.start = True
        self.start_btn.clicked.connect(lambda: setattr(self, "start", True))
        self.queue_btn.clicked.connect(lambda: setattr(self, "start", False))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.start_btn.setDefault(True)
        self.wait.setFocus()
        self.wait.selectAll()

    def values(self) -> dict:
        return {"path": self.info["path"], "wait": float(self.wait.value()),
                "rerun_pre": self.rerun_pre.isChecked(), "start": self.start}
