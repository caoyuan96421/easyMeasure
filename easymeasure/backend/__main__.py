"""Run the easyMeasure backend service.

    python -m easymeasure.backend [--port 47811] [--host 127.0.0.1]
                                  [--setup my_setup.json | --restore]

The backend is independent of any frontend: frontends may be started, closed or
killed at any time without affecting a running measurement.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="easymeasure-backend", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="interface to listen on")
    ap.add_argument("--port", type=int, default=None, help="TCP port (default 47811)")
    ap.add_argument("--home", default=None, help="state directory (default ~/.easymeasure)")
    ap.add_argument("--setup", default=None, help="setup file to load at start")
    ap.add_argument("--restore", action="store_true",
                    help="restore the automatically saved setup of the last session")
    ap.add_argument("--quiet", action="store_true", help="no console logging")
    ap.add_argument("--no-tray", action="store_true",
                    help="do not show the system-tray indicator")
    ap.add_argument("--force", action="store_true",
                    help="start even if another backend seems to be running")
    args = ap.parse_args(argv)

    if args.home:
        os.environ["EASYMEASURE_HOME"] = str(Path(args.home).resolve())

    from .server import DEFAULT_PORT, Server, backend_alive, read_runtime_file
    from .service import Backend

    info = read_runtime_file()
    if info and backend_alive(info) and not args.force:
        print("A backend is already running (pid %s, port %s). Use --force to start another."
              % (info.get("pid"), info.get("port")), file=sys.stderr)
        return 2

    backend = Backend(log_to_console=not args.quiet)
    if args.setup:
        problems = backend.load_setup(path=args.setup)
        for p in problems:
            print("setup problem:", p, file=sys.stderr)
    elif args.restore:
        backend.restore_autosave()

    server = Server(backend, host=args.host, port=args.port if args.port is not None else DEFAULT_PORT)

    def _sig(signum, frame):
        threading.Thread(target=server.stop, daemon=True).start()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _sig)
        except (ValueError, OSError):
            pass
    if not args.no_tray:
        try:
            backend.start_tray()    # separate process: can never affect the backend
        except Exception as exc:
            print("tray icon not started: %s" % exc, file=sys.stderr)
    try:
        server.serve_forever()
    finally:
        backend.close()
    # Everything is closed and flushed.  Drivers that hung forever were abandoned on
    # daemon threads; tearing down the interpreter around them can crash inside
    # native extensions, so leave immediately.
    import logging
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
