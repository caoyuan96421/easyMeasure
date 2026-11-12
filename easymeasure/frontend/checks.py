"""Checking what is typed into a code or formula field, with the backend.

Every editor of code or formulas (parameters, composite channels, user actions,
point actions, scripts) has a **Check** button, and its OK (or Add / Run) checks
first: the dialog closes only when the check passes.  The backend answers - it
knows the channels, the parameters and the rules - so the front ends never repeat
those rules.  Without a connection only the local checks run.

``BackendCheck`` wires that up: give it how to build the request, how to show the
answer, and what to do once it passed.
"""

from __future__ import annotations

from typing import Callable, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialogButtonBox, QLabel, QPushButton

OK_COLOUR, WARN_COLOUR, ERR_COLOUR, BUSY_COLOUR = "#1e8449", "#b9770e", "#c0392b", "gray"


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class BackendCheck:
    """*request()* returns ``(command, args)`` or raises ValueError for what can be
    told without the backend; *ok_text(reply)* the line shown when it passed;
    *accept()* finishes the dialog.  A reply ``{"ok": False, "errors": [...]}``
    (``check_code``) counts as failed; its ``warnings`` are shown, not refused."""

    def __init__(self, conn, info: QLabel, request: Callable[[], Tuple[str, dict]],
                 ok_text: Callable[[object], str], accept: Optional[Callable[[], None]] = None):
        self.conn = conn
        self.info = info
        self.request = request
        self.ok_text = ok_text
        self.accept = accept
        self.busy = False
        info.setWordWrap(True)
        info.setTextFormat(Qt.RichText)
        info.setStyleSheet("color: gray")

    def button(self, bb: Optional[QDialogButtonBox] = None, tip: str = "") -> QPushButton:
        """A Check button (added to *bb*, or free-standing)."""
        if bb is not None:
            b = bb.addButton("Check", QDialogButtonBox.ActionRole)
        else:
            b = QPushButton("Check")
        b.setToolTip(tip or "check it with the backend without saving anything")
        b.clicked.connect(lambda: self.check())
        self.check_btn = b
        return b

    def show(self, text: str, colour: str) -> None:
        self.info.setText("<span style='color:%s'>%s</span>" % (
            colour, esc(text).replace("\n", "<br>")))

    def _conn(self):
        return self.conn() if callable(self.conn) and not hasattr(self.conn, "request") \
            else self.conn

    def check(self, then_accept: bool = False) -> None:
        try:
            cmd, args = self.request()
        except ValueError as exc:
            self.show("✘ %s" % exc, ERR_COLOUR)
            return
        conn = self._conn()
        if conn is None:                          # local checks only
            self.show("✔ looks right (not checked with the backend: not connected)", OK_COLOUR)
            if then_accept and self.accept is not None:
                self.accept()
            return
        self.busy = True
        self.show("checking …", BUSY_COLOUR)

        def ok(r):
            self.busy = False
            if isinstance(r, dict) and r.get("ok") is False:
                fail("; ".join(r.get("errors") or ["not valid"]))
                return
            warns = (r.get("warnings") if isinstance(r, dict) else None) or []
            text = self.ok_text(r) + "".join("\n⚠ %s" % w for w in warns)
            self.show(text, WARN_COLOUR if warns else OK_COLOUR)
            if then_accept and self.accept is not None:
                self.accept()

        def fail(e):
            self.busy = False
            self.show("✘ %s" % e, ERR_COLOUR)
        conn.request(cmd, args, on_ok=ok, on_err=fail)


def conn_of(widget):
    """The connection of the nearest enclosing panel that has one (``conn``)."""
    w = widget
    while w is not None:
        c = getattr(w, "conn", None)
        if c is not None and hasattr(c, "request"):
            return c
        w = w.parentWidget()
    return None


def code_checker(widget, code_edit, what: str):
    """A Check button and a result line for a code box (``check_code``)."""
    info = QLabel("")

    def request():
        code = code_edit.toPlainText()
        if not code.strip():
            raise ValueError("no code")
        try:
            compile(code, "<%s>" % what, "exec")
        except SyntaxError as exc:
            raise ValueError("line %s: %s" % (exc.lineno, exc.msg)) from None
        return "check_code", {"code": code, "what": what}
    chk = BackendCheck(lambda: conn_of(widget), info, request,
                       lambda r: "✔ valid Python")
    btn = chk.button(None, "check the code: syntax, and that the channels it quotes exist")
    return chk, btn, info
