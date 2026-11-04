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
