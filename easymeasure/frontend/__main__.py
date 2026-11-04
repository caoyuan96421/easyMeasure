"""Start the easyMeasure GUI.

    python -m easymeasure.frontend [--start-backend]

The GUI connects to the running backend (``~/.easymeasure/backend.json``).  With
``--start-backend`` (or the toolbar button) it launches a detached backend if
none is running.  Closing or killing the GUI never stops the backend.
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="easymeasure")
    ap.add_argument("--start-backend", action="store_true",
                    help="start a detached backend if none is running")
    ap.add_argument("--home", default=None, help="state directory (default ~/.easymeasure)")
    args, qt_args = ap.parse_known_args(argv)
    if args.home:
        os.environ["EASYMEASURE_HOME"] = os.path.abspath(args.home)
    from PySide6.QtWidgets import QApplication
    import pyqtgraph as pg
    from .display import prepare_environment
    prepare_environment()          # UI scale / high-DPI policy: before the QApplication
    app = QApplication([sys.argv[0]] + qt_args)
    app.setApplicationName("easyMeasure")
    app.setStyle("Fusion")
    pg.setConfigOptions(background="#1e1e1e", foreground="#d0d0d0")
    from .main_window import MainWindow
    w = MainWindow(autostart_backend=args.start_backend)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
