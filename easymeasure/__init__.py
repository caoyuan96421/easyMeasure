"""easyMeasure - a generic framework for multi-dimensional automated measurement.

The package is split into:

* :mod:`easymeasure.backend`  - the long-running measurement service (instruments,
  channels, sweeps, run queue, storage, logging).  It runs in its own process and
  survives frontend crashes.
* :mod:`easymeasure.client`   - a small thread-based client used by scripts/tests.
* :mod:`easymeasure.frontend` - the PySide6 GUI, which talks to the backend only
  through non-blocking sockets.
* :mod:`easymeasure.common`   - shared code (wire codec, errors, safe expressions).
* :mod:`easymeasure.report`   - figure rendering and PowerPoint generation.
"""

__version__ = "0.1.0"


def code_stamp() -> float:
    """Newest modification time of the package sources.

    The GUI compares its own stamp with the backend's to warn when the running
    backend still executes older code (e.g. after a ``git pull``)."""
    import os
    root = os.path.dirname(os.path.abspath(__file__))
    newest = 0.0
    for d, _, files in os.walk(root):
        for f in files:
            if f.endswith(".py"):
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(d, f)))
                except OSError:
                    pass
    return newest


#: stamp of the code this process imported (computed once, at import time)
CODE_STAMP = code_stamp()
