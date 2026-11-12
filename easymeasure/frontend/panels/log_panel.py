"""Log panel: backend log stream (pushed) with level filter."""

from __future__ import annotations

import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
COLORS = {"DEBUG": "#888", "INFO": "#ccc", "WARNING": "#e90", "ERROR": "#f55", "CRITICAL": "#f0f"}


class LogPanel(QWidget):
    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.level = QComboBox()
        self.level.addItems(list(LEVELS))
        self.level.setCurrentText("INFO")
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter text")
        clear = QPushButton("Clear")
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(5000)
        from ..display import register_font
        register_font(self.text, 0.95, monospace=True)
        top = QHBoxLayout()
        top.addWidget(self.level)
        top.addWidget(self.filter, 1)
        top.addWidget(clear)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addLayout(top)
        lay.addWidget(self.text)
        clear.clicked.connect(self.text.clear)
        self.level.currentTextChanged.connect(lambda *_: self.reload())
        self.filter.editingFinished.connect(self.reload)
        self._buf = []
        self._last_seq = 0
        conn.event.connect(self._on_event)
        conn.connected.connect(lambda _: self.reload())
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._flush)
        self._timer.start()

    def reload(self):
        self.text.clear()
        self._buf.clear()
        self.conn.request("recent_logs", {"n": 1500, "min_level": LEVELS[self.level.currentText()]},
                          on_ok=lambda entries: [self._add(e) for e in entries or []])

    def _accept(self, e) -> bool:
        if e.get("levelno", 0) < LEVELS[self.level.currentText()]:
            return False
        f = self.filter.text().strip().lower()
        return not f or f in e.get("msg", "").lower() or f in e.get("name", "").lower()

    def _add(self, e):
        if self._accept(e):
            self._buf.append(e)

    def add_local(self, level: str, text: str) -> None:
        """A message of this GUI (not of the backend), e.g. 'alias Vg saved'."""
        lv = {"error": "ERROR", "warning": "WARNING"}.get(level, "INFO")
        self._add({"t": time.time(), "level": lv, "levelno": LEVELS.get(lv, 20),
                   "name": "gui", "msg": text})

    def _on_event(self, kind, data):
        if kind == "log" and isinstance(data, dict):
            self._add(data)

    def _flush(self):
        if not self._buf:
            return
        buf, self._buf = self._buf[-1000:], []
        html = []
        for e in buf:
            t = time.strftime("%H:%M:%S", time.localtime(e.get("t", 0)))
            msg = (e.get("msg", "").replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;").replace("\n", "<br>"))
            html.append("<span style='color:%s'>%s %-7s %s: %s</span>" % (
                COLORS.get(e.get("level"), "#ccc"), t, e.get("level"),
                e.get("name", "").replace("easymeasure.", ""), msg))
        self.text.appendHtml("<br>".join(html))
        sb = self.text.verticalScrollBar()
        sb.setValue(sb.maximum())
