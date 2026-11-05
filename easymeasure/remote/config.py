"""Remote-access configuration (``<home>/remote/config.json``)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from ..common import paths

DEFAULTS = {
    "host": "0.0.0.0",            # interface to listen on ("127.0.0.1" = this computer only)
    "port": 8443,
    "certfile": None,             # PEM certificate chain; None = self-signed (auto-generated)
    "keyfile": None,
    "cert_names": [],             # extra host names / IPs for the self-signed certificate
    "session_idle_hours": 12.0,   # a session with no connection for this long expires
    "session_max_days": 7.0,      # absolute session lifetime (log in again after that)
    "max_sessions_per_user": 20,
    "max_ws_total": 64,           # simultaneous live connections
    "max_ws_per_user": 16,
    "max_conn_per_ip": 64,        # TCP connections per client address
    "max_msg_bytes": 8 * 1024 * 1024,   # largest message a client may send
    "rate": 200.0,                # requests per second per connection (sustained)
    "burst": 600.0,
    "max_in_flight": 64,          # unanswered requests per connection
    "login_max_failures_ip": 5,
    "login_max_failures_user": 10,
    "lockout_s": 30.0,            # first lockout; doubles each time
    "lockout_max_s": 3600.0,
    "first_request_timeout": 15.0,  # TLS connection that sends no request in time is closed
    "tls_handshake_timeout": 10.0,
    "heartbeat_s": 20.0,          # WebSocket ping interval (detects dead peers only)
    "trust_proxy": False,         # take the client address from X-Forwarded-For
    "allowed_origins": [],        # extra browser origins allowed to connect
}


def remote_dir(home: Optional[Path] = None) -> Path:
    p = Path(home) if home else paths.home_dir()
    d = p / "remote"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path(home: Optional[Path] = None) -> Path:
    return remote_dir(home) / "config.json"


def load(home: Optional[Path] = None, overrides: Optional[dict] = None) -> dict:
    cfg = dict(DEFAULTS)
    p = config_path(home)
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except Exception:
        pass
    cfg.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return cfg


def save(cfg: dict, home: Optional[Path] = None) -> Path:
    p = config_path(home)
    data = {k: cfg[k] for k in DEFAULTS if k in cfg}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def users_path(home: Optional[Path] = None) -> Path:
    return remote_dir(home) / "users.json"


def tls_dir(home: Optional[Path] = None) -> Path:
    return remote_dir(home) / "tls"


def status_path(home: Optional[Path] = None) -> Path:
    """The running gateway advertises its address and state here."""
    return remote_dir(home) / "gateway.json"
