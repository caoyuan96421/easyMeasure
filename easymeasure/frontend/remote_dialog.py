"""Backend → Remote access…: switch the internet gateway on/off, see who is
connected and manage the accounts of remote users."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from .widgets import shrinkable_label

ROLE_HELP = ("viewer: watch only · operator: run measurements within the configured limits "
             "(no code, no setup changes) · admin: everything, including Python scripts "
             "(= full control of this computer)")


class UserDialog(QDialog):
    def __init__(self, parent=None, name: str = "", role: str = "operator", new: bool = True):
        super().__init__(parent)
        self.setWindowTitle("Remote user" if new else "Change %s" % name)
        self.name = QLineEdit(name)
        self.name.setEnabled(new)
        self.role = QComboBox()
        self.role.addItems(["viewer", "operator", "admin"])
        self.role.setCurrentText(role)
        self.pw1 = QLineEdit()
        self.pw1.setEchoMode(QLineEdit.Password)
        self.pw2 = QLineEdit()
        self.pw2.setEchoMode(QLineEdit.Password)
        if not new:
            self.pw1.setPlaceholderText("leave empty to keep the password")
        form = QFormLayout(self)
        form.addRow("User name", self.name)
        form.addRow("Role", self.role)
        help_ = shrinkable_label(QLabel(ROLE_HELP))
        help_.setStyleSheet("color: gray")
        form.addRow(help_)
        form.addRow("Password", self.pw1)
        form.addRow("Repeat", self.pw2)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.new = new

    def _ok(self):
        if self.pw1.text() != self.pw2.text():
            QMessageBox.warning(self, "Password", "The passwords do not match.")
            return
        if self.new and not self.pw1.text():
            QMessageBox.warning(self, "Password", "A new user needs a password.")
            return
        if not self.name.text().strip():
            QMessageBox.warning(self, "User", "Enter a user name.")
            return
        self.accept()


class RemoteAccessDialog(QDialog):
    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Remote access")
        self.resize(720, 560)
        self.status = shrinkable_label(QLabel("…"))
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.urls = shrinkable_label(QLabel(""))
        self.urls.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.fp = QLineEdit("")
        self.fp.setReadOnly(True)
        self.fp.setToolTip("compare with the browser's certificate details, or pass to "
                           "RemoteClient(fingerprint=...)")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(8443)
        self._port_touched = False
        self.port.valueChanged.connect(lambda _: setattr(self, "_port_touched", True))
        self.toggle = QPushButton("Enable remote access")
        self.toggle.clicked.connect(self._toggle)
        top = QGroupBox("Gateway (HTTPS / secure WebSocket)")
        tf = QFormLayout(top)
        row = QHBoxLayout()
        row.addWidget(QLabel("Port"))
        row.addWidget(self.port)
        row.addWidget(self.toggle)
        row.addStretch(1)
        tf.addRow(row)
        tf.addRow("State", self.status)
        tf.addRow("Address", self.urls)
        tf.addRow("Certificate SHA-256", self.fp)
        note = shrinkable_label(QLabel(
            "The gateway runs in its own process: remote users can never crash the backend. "
            "Open the address in a browser, or use easymeasure.client.RemoteClient with the "
            "fingerprint above. A self-signed certificate makes browsers warn once - check the "
            "fingerprint, or configure a real certificate (python -m easymeasure.remote config). "
            "Open the port in the firewall / router only if you need access from outside."))
        note.setStyleSheet("color: gray")
        tf.addRow(note)
        self.conns = QTableWidget(0, 4)
        self.conns.setHorizontalHeaderLabels(["User", "Role", "Address", "Since"])
        self.conns.horizontalHeader().setStretchLastSection(True)
        self.conns.setEditTriggers(QTableWidget.NoEditTriggers)
        self.conns.verticalHeader().setVisible(False)
        cbox = QGroupBox("Live remote connections")
        QVBoxLayout(cbox).addWidget(self.conns)
        self.users = QTableWidget(0, 3)
        self.users.setHorizontalHeaderLabels(["User", "Role", "Created"])
        self.users.horizontalHeader().setStretchLastSection(True)
        self.users.setEditTriggers(QTableWidget.NoEditTriggers)
        self.users.setSelectionBehavior(QTableWidget.SelectRows)
        self.users.verticalHeader().setVisible(False)
        ub = QHBoxLayout()
        for text, fn in (("Add…", self._add), ("Change…", self._change), ("Remove", self._remove)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            ub.addWidget(b)
        ub.addStretch(1)
        ubox = QGroupBox("Accounts")
        ul = QVBoxLayout(ubox)
        ul.addWidget(self.users)
        ul.addLayout(ub)
        self.msg = shrinkable_label(QLabel(""))
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(top)
        lay.addWidget(cbox, 1)
        lay.addWidget(ubox, 1)
        lay.addWidget(self.msg)
        lay.addWidget(bb)
        self._st = {}
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def _err(self, prefix):
        return lambda e: self.msg.setText("<span style='color:#d55'>✘ %s: %s</span>" % (prefix, e))

    def refresh(self):
        self.conn.request("remote_status", on_ok=self._show, on_err=self._err("status"))

    def _show(self, st):
        self._st = st
        running, enabled = st.get("running"), st.get("enabled")
        self.status.setText(("<b style='color:#3a3'>running</b> (pid %s)" % st.get("pid")) if running
                            else ("<b style='color:#e39a1b'>starting / restarting…</b>" if enabled
                                  else "off"))
        self.toggle.setText("Disable remote access" if enabled else "Enable remote access")
        if not self._port_touched or running:
            self.port.blockSignals(True)
            self.port.setValue(int(st.get("port") or 8443))
            self.port.blockSignals(False)
            self._port_touched = False
        self.urls.setText("<br>".join(st.get("urls") or []) or "—")
        fp = (st.get("fingerprint") or "—") + ("  (self-signed)" if st.get("self_signed") else "")
        if self.fp.text() != fp:
            self.fp.setText(fp)
            self.fp.setCursorPosition(0)
        conns = st.get("connections") or []
        self.conns.setRowCount(len(conns))
        for r, c in enumerate(conns):
            for k, v in enumerate((c.get("user"), c.get("role"), c.get("ip"),
                                   time.strftime("%H:%M:%S", time.localtime(c.get("since") or 0)))):
                self.conns.setItem(r, k, QTableWidgetItem(str(v)))
        users = st.get("users") or []
        sel = self._sel()
        self.users.setRowCount(len(users))
        for r, u in enumerate(users):
            for k, v in enumerate((u["name"], u["role"],
                                   time.strftime("%Y-%m-%d", time.localtime(u.get("created") or 0)))):
                self.users.setItem(r, k, QTableWidgetItem(str(v)))
            if u["name"] == sel:
                self.users.selectRow(r)
        if enabled and not users:
            self.msg.setText("<span style='color:#e39a1b'>No accounts yet: add one to log in remotely.</span>")

    def _sel(self):
        r = self.users.currentRow()
        it = self.users.item(r, 0) if r >= 0 else None
        return it.text() if it else None

    def _toggle(self):
        if self._st.get("enabled"):
            self.conn.request("remote_stop", on_ok=self._show, on_err=self._err("stop"))
        else:
            if not self._st.get("users"):
                QMessageBox.information(self, "Remote access", "Add an account first (Accounts → Add…).")
            self.conn.request("remote_start", {"port": self.port.value()}, on_ok=self._show,
                              on_err=self._err("start"), timeout=30)

    def _add(self):
        dlg = UserDialog(self)
        if dlg.exec():
            self.conn.request("remote_set_user", {"name": dlg.name.text().strip(),
                                                  "password": dlg.pw1.text(),
                                                  "role": dlg.role.currentText()},
                              on_ok=lambda _: (self.msg.setText("account added"), self.refresh()),
                              on_err=self._err("add"))

    def _change(self):
        name = self._sel()
        if not name:
            return
        role = next((u["role"] for u in self._st.get("users", []) if u["name"] == name), "operator")
        dlg = UserDialog(self, name, role, new=False)
        if dlg.exec():
            self.conn.request("remote_set_user", {"name": name, "password": dlg.pw1.text() or None,
                                                  "role": dlg.role.currentText()},
                              on_ok=lambda _: (self.msg.setText(
                                  "account changed; its open sessions were closed"), self.refresh()),
                              on_err=self._err("change"))

    def _remove(self):
        name = self._sel()
        if not name or QMessageBox.question(self, "Remove", "Remove remote user %s? Their open "
                                            "sessions are closed." % name) != QMessageBox.Yes:
            return
        self.conn.request("remote_remove_user", {"name": name},
                          on_ok=lambda _: (self.msg.setText("removed %s" % name), self.refresh()),
                          on_err=self._err("remove"))
