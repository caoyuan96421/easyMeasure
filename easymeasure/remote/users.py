"""User accounts for remote access (``<home>/remote/users.json``).

Passwords are stored as salted scrypt hashes (N=2^15, r=8, p=1; ~32 MB and tens
of milliseconds per check, which makes offline guessing expensive).  The file is
written atomically with owner-only permissions and re-read automatically when it
changes, so accounts can be added or removed while the gateway runs.  Removing a
user, changing their role or password ends their open sessions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Dict, Optional

ROLES = ("viewer", "operator", "admin")
NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
MIN_PASSWORD = 10
SCRYPT = {"n": 1 << 15, "r": 8, "p": 1}
_MAXMEM = 64 * 1024 * 1024


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str, salt: Optional[bytes] = None, **params) -> dict:
    prm = dict(SCRYPT, **params)
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=prm["n"], r=prm["r"], p=prm["p"],
                        maxmem=_MAXMEM, dklen=32)
    return {"alg": "scrypt", "n": prm["n"], "r": prm["r"], "p": prm["p"],
            "salt": _b64(salt), "hash": _b64(dk)}


def check_password(password: str, rec: dict) -> bool:
    try:
        salt = base64.b64decode(rec["salt"])
        want = base64.b64decode(rec["hash"])
        n, r, p = int(rec["n"]), int(rec["r"]), int(rec["p"])
        if n > (1 << 20) or r > 32 or p > 16:          # refuse absurd (DoS) parameters
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                            maxmem=_MAXMEM, dklen=len(want))
    except Exception:
        return False
    return hmac.compare_digest(dk, want)


# verifying an unknown user costs the same as a known one (no user enumeration by timing)
_DUMMY = None


def _dummy() -> dict:
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password(secrets.token_urlsafe(16))
    return _DUMMY


def password_problem(password: str) -> Optional[str]:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        return "the password needs at least %d characters" % MIN_PASSWORD
    if len(password) > 1024:
        return "the password is too long"
    if len(set(password)) < 4:
        return "the password is too simple"
    return None


class UserStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._users: Dict[str, dict] = {}
        self._mtime = None
        self.reload(force=True)

    # -- persistence -------------------------------------------------------------
    def reload(self, force: bool = False) -> None:
        with self._lock:
            try:
                st = self.path.stat()
                key = (st.st_mtime_ns, st.st_size)
            except OSError:
                self._users, self._mtime = {}, None
                return
            if not force and key == self._mtime:
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                users = data.get("users", {}) if isinstance(data, dict) else {}
                self._users = {k: v for k, v in users.items()
                               if NAME_RE.match(k) and isinstance(v, dict) and v.get("role") in ROLES}
            except Exception:
                # keep the previous accounts rather than locking everybody out (or in)
                pass
            self._mtime = key

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        data = json.dumps({"format": "easyMeasure-users-1", "users": self._users}, indent=1)
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        st = self.path.stat()
        self._mtime = (st.st_mtime_ns, st.st_size)

    # -- queries -------------------------------------------------------------------
    def list(self) -> list:
        self.reload()
        with self._lock:
            return [{"name": n, "role": u["role"], "created": u.get("created"),
                     "changed": u.get("changed")} for n, u in sorted(self._users.items())]

    def get(self, name: str) -> Optional[dict]:
        self.reload()
        with self._lock:
            u = self._users.get(name)
            return dict(u) if u else None

    def generation(self, name: str) -> Optional[str]:
        """Changes whenever the password or role changes (sessions are bound to it)."""
        u = self.get(name)
        if not u:
            return None
        return "%s:%s" % (u["role"], u["pw"].get("hash", ""))

    def verify(self, name: str, password: str) -> Optional[dict]:
        """Return ``{"name", "role", "generation"}`` or None.  Constant-ish time."""
        u = self.get(name) if isinstance(name, str) and NAME_RE.match(name or "") else None
        if not isinstance(password, str) or len(password) > 1024:
            password = ""
        ok = check_password(password, u["pw"] if u else _dummy())
        if not (u and ok):
            return None
        return {"name": name, "role": u["role"], "generation": self.generation(name)}

    # -- changes -------------------------------------------------------------------
    def set_user(self, name: str, password: Optional[str], role: Optional[str] = None) -> None:
        if not NAME_RE.match(name or ""):
            raise ValueError("user names may contain letters, digits and _ . @ - (max 64)")
        with self._lock:
            self.reload()
            u = self._users.get(name)
            if u is None and password is None:
                raise ValueError("a new user needs a password")
            if role is not None and role not in ROLES:
                raise ValueError("role must be one of %s" % ", ".join(ROLES))
            now = time.time()
            if u is None:
                u = {"role": role or "operator", "created": now}
            if password is not None:
                prob = password_problem(password)
                if prob:
                    raise ValueError(prob)
                u["pw"] = hash_password(password)
            if role is not None:
                u["role"] = role
            u["changed"] = now
            self._users[name] = u
            self._save()

    def remove(self, name: str) -> bool:
        with self._lock:
            self.reload()
            if self._users.pop(name, None) is None:
                return False
            self._save()
            return True

    def __len__(self) -> int:
        self.reload()
        with self._lock:
            return len(self._users)
