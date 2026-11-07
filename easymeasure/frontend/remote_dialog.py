"""Backend → Remote access…: switch the internet gateway on/off, choose its
certificate (self-signed, Let's Encrypt, or own files), see who is connected and
manage the accounts of remote users."""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

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
        self.resize(760, 820)
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
            "fingerprint above. Port 443 is the standard HTTPS port (https://name without a "
            "port number). Open the port in the firewall / router only if you need access from "
            "outside."))
        note.setStyleSheet("color: gray")
        tf.addRow(note)
        cert_box = self._build_cert_box()
        self.conns = QTableWidget(0, 4)
        self.conns.setHorizontalHeaderLabels(["User", "Role", "Address", "Since"])
        self.conns.horizontalHeader().setStretchLastSection(True)
        self.conns.setEditTriggers(QTableWidget.NoEditTriggers)
        self.conns.verticalHeader().setVisible(False)
        self.conns.setMinimumHeight(90)
        cbox = QGroupBox("Live remote connections")
        QVBoxLayout(cbox).addWidget(self.conns)
        self.users = QTableWidget(0, 3)
        self.users.setHorizontalHeaderLabels(["User", "Role", "Created"])
        self.users.horizontalHeader().setStretchLastSection(True)
        self.users.setEditTriggers(QTableWidget.NoEditTriggers)
        self.users.setSelectionBehavior(QTableWidget.SelectRows)
        self.users.verticalHeader().setVisible(False)
        self.users.setMinimumHeight(110)
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
        lay.addWidget(cert_box)
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

    # -- certificate ----------------------------------------------------------------------
    def _build_cert_box(self) -> QGroupBox:
        qs = QSettings("easyMeasure", "gui")
        box = QGroupBox("Certificate")
        f = QFormLayout(box)
        self.cert_state = shrinkable_label(QLabel("…"))
        self.cert_state.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f.addRow("In use", self.cert_state)
        self.le_domain = QLineEdit(qs.value("acme_domain", "", type=str))
        self.le_domain.setPlaceholderText("lab.example.org - a DNS name pointing to this computer")
        self.le_email = QLineEdit(qs.value("acme_email", "", type=str))
        self.le_email.setPlaceholderText("optional: Let's Encrypt writes if renewal ever fails")
        self.le_http = QSpinBox()
        self.le_http.setRange(1, 65535)
        self.le_http.setValue(80)
        self.le_http.setToolTip("Let's Encrypt checks http://<domain>/… on port 80; change only if "
                                "the router forwards its port 80 to another port here")
        self.le_staging = QCheckBox("test certificate (Let's Encrypt staging: not trusted by "
                                    "browsers, for trying the setup)")
        self.le_agree = QCheckBox("I agree to the Let's Encrypt Subscriber Agreement")
        tos = QLabel('<a href="https://letsencrypt.org/repository/">read it</a>')
        tos.setOpenExternalLinks(True)
        agree_row = QHBoxLayout()
        agree_row.addWidget(self.le_agree)
        agree_row.addWidget(tos)
        agree_row.addStretch(1)
        f.addRow("Domain name", self.le_domain)
        f.addRow("E-mail", self.le_email)
        hp = QHBoxLayout()
        hp.addWidget(self.le_http)
        hp.addStretch(1)
        f.addRow("Check port (HTTP)", hp)
        f.addRow(self.le_staging)
        f.addRow(agree_row)
        need = shrinkable_label(QLabel(
            "For Let's Encrypt: the domain name must point to this computer's public address, "
            "and ports 80 (the check) and the gateway port must be reachable from the internet "
            "(Windows firewall, and the router must forward both to this computer)."))
        need.setStyleSheet("color: gray")
        f.addRow(need)
        bt = QHBoxLayout()
        self.le_get = QPushButton("Get Let's Encrypt certificate")
        self.le_get.setToolTip("proves that the domain points here (HTTP check on port 80), "
                               "then installs the certificate; it is renewed automatically")
        self.le_get.clicked.connect(self._get_cert)
        self.use_files = QPushButton("Use certificate files…")
        self.use_files.clicked.connect(self._use_files)
        self.use_self = QPushButton("Use self-signed")
        self.use_self.clicked.connect(self._use_self_signed)
        self.fw = QPushButton("Open Windows firewall…")
        self.fw.setToolTip("allow incoming connections on port 80 (certificate check) and on the "
                           "gateway port (asks for administrator rights)")
        self.fw.clicked.connect(self._open_firewall)
        self.fw.setVisible(os.name == "nt")
        for b in (self.le_get, self.use_files, self.use_self, self.fw):
            bt.addWidget(b)
        bt.addStretch(1)
        f.addRow(bt)
        self.cert_progress = shrinkable_label(QLabel(""))
        self.cert_progress.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f.addRow(self.cert_progress)
        self.cert_log = QPlainTextEdit()
        self.cert_log.setReadOnly(True)
        self.cert_log.setFixedHeight(76)
        self.cert_log.setVisible(False)
        f.addRow(self.cert_log)
        return box

    def _show_cert(self, c: dict) -> None:
        mode = c.get("mode")
        until = (c.get("expires") or "")[:10]
        if mode == "letsencrypt":
            a = c.get("acme") or {}
            text = "<b style='color:#3a3'>Let's Encrypt</b> for <b>%s</b>%s, valid until %s " \
                "(renewed automatically %s)" % (
                    a.get("domain"), " <b style='color:#e39a1b'>(TEST certificate)</b>"
                    if a.get("staging") else "", until, "30 days before")
            if not self.le_domain.text():
                self.le_domain.setText(a.get("domain", ""))
        elif mode == "files":
            text = "files: %s (%s, valid until %s)" % (c.get("certfile"),
                                                      ", ".join(c.get("names") or [])[:80], until)
        else:
            text = "self-signed: browsers warn once (compare the fingerprint)"
        if c.get("error"):
            text += " <span style='color:#d55'>✘ %s</span>" % c["error"]
        self.cert_state.setText(text)
        r = c.get("request") or {}
        state = r.get("state")
        if state:
            colour = {"running": "#e39a1b", "ok": "#3a3", "error": "#d55"}.get(state, "gray")
            label = {"running": "requesting…", "ok": "✔", "error": "✘"}.get(state, state)
            self.cert_progress.setText("<span style='color:%s'>%s</span> %s" % (
                colour, label, r.get("message", "")))
            lines = "\n".join(r.get("log") or [])
            if lines and self.cert_log.toPlainText() != lines:
                self.cert_log.setPlainText(lines)
                self.cert_log.verticalScrollBar().setValue(self.cert_log.verticalScrollBar().maximum())
            self.cert_log.setVisible(bool(lines))
        self.le_get.setEnabled(state != "running")

    def _get_cert(self) -> None:
        domain = self.le_domain.text().strip()
        if not domain:
            QMessageBox.warning(self, "Certificate", "Enter the domain name of this computer.")
            return
        if not self.le_agree.isChecked():
            QMessageBox.warning(self, "Certificate", "Let's Encrypt issues certificates only to "
                                "those who agree to its Subscriber Agreement.")
            return
        qs = QSettings("easyMeasure", "gui")
        qs.setValue("acme_domain", domain)
        qs.setValue("acme_email", self.le_email.text().strip())
        self.cert_progress.setText("<span style='color:#e39a1b'>requesting…</span>")
        self.conn.request("remote_cert_request", {
            "domain": domain, "email": self.le_email.text().strip(), "agree_tos": True,
            "staging": self.le_staging.isChecked(), "http_port": self.le_http.value()},
            on_ok=self._show, on_err=self._err("certificate"), timeout=30)

    def _use_files(self) -> None:
        cert, _ = QFileDialog.getOpenFileName(self, "Certificate chain (PEM)", "",
                                              "PEM (*.pem *.crt *.cer);;All files (*)")
        if not cert:
            return
        key, _ = QFileDialog.getOpenFileName(self, "Private key (PEM) of that certificate",
                                             os.path.dirname(cert), "PEM (*.pem *.key);;All files (*)")
        if key:
            self.conn.request("remote_cert_use", {"mode": "files", "certfile": cert,
                                                  "keyfile": key},
                              on_ok=self._show, on_err=self._err("certificate"))

    def _use_self_signed(self) -> None:
        if QMessageBox.question(self, "Certificate", "Go back to the self-signed certificate? "
                                "(Let's Encrypt renewal stops; browsers warn again.)") \
                != QMessageBox.Yes:
            return
        self.conn.request("remote_cert_use", {"mode": "self-signed"}, on_ok=self._show,
                          on_err=self._err("certificate"))

    def _open_firewall(self) -> None:
        from ..remote.acme import windows_firewall_command
        ports = [80, self.port.value()]
        if not self._run_elevated("cmd.exe", windows_firewall_command(ports)):
            self.msg.setText("<span style='color:#d55'>✘ the firewall was not changed "
                             "(administrator rights declined)</span>")
            return
        self.msg.setText("Windows firewall: incoming TCP %s allowed (rule 'easyMeasure remote "
                         "access'). The router must forward these ports too." %
                         ", ".join(str(p) for p in sorted(set(ports))))

    @staticmethod
    def _run_elevated(program: str, params: str) -> bool:
        """Run *program* with administrator rights (Windows asks the user)."""
        import ctypes
        r = ctypes.windll.shell32.ShellExecuteW(None, "runas", program, params, None, 0)
        return int(r) > 32

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
        self._show_cert(st.get("cert") or {})
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
