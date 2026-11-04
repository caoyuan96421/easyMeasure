"""Default locations for runtime files, logs and data."""

from __future__ import annotations

import os
from pathlib import Path


def home_dir() -> Path:
    """Root directory for easyMeasure state (override with EASYMEASURE_HOME)."""
    env = os.environ.get("EASYMEASURE_HOME")
    p = Path(env) if env else Path.home() / ".easymeasure"
    p.mkdir(parents=True, exist_ok=True)
    return p


def runtime_file() -> Path:
    """JSON file in which a running backend advertises its port and token."""
    return home_dir() / "backend.json"


def log_dir() -> Path:
    p = home_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_data_dir() -> Path:
    p = home_dir() / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p
