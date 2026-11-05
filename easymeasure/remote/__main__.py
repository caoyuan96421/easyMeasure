"""Remote access: run the gateway and manage accounts.

    python -m easymeasure.remote serve [--port 8443] [--host 0.0.0.0]
    python -m easymeasure.remote adduser NAME [--role viewer|operator|admin]
    python -m easymeasure.remote passwd NAME
    python -m easymeasure.remote role NAME ROLE
    python -m easymeasure.remote deluser NAME
    python -m easymeasure.remote users
    python -m easymeasure.remote cert [--regenerate]
    python -m easymeasure.remote status
    python -m easymeasure.remote config [key=value ...]

Usually the backend starts the gateway itself (``python -m easymeasure.backend
--remote``) and restarts it if it ever stops.  Changes to accounts take effect
immediately, also for running gateways (sessions of a removed user, or of a user
whose password or role changed, are closed).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path


def _home(args) -> Path:
    if args.home:
        os.environ["EASYMEASURE_HOME"] = str(Path(args.home).resolve())
    from ..common import paths
    return paths.home_dir()


def _ask_password(args) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    a = getpass.getpass("New password: ")
    b = getpass.getpass("Repeat password: ")
    if a != b:
        raise SystemExit("passwords do not match")
    return a


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m easymeasure.remote", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default=None, help="state directory (default ~/.easymeasure)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="run the gateway")
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
    sv.add_argument("--quiet", action="store_true")
    for name in ("adduser", "passwd"):
        p = sub.add_parser(name)
        p.add_argument("name")
        p.add_argument("--password-stdin", action="store_true",
                       help="read the password from the first line of stdin")
        if name == "adduser":
            p.add_argument("--role", default="operator", choices=["viewer", "operator", "admin"])
    p = sub.add_parser("role")
    p.add_argument("name")
    p.add_argument("role", choices=["viewer", "operator", "admin"])
    p = sub.add_parser("deluser")
    p.add_argument("name")
    sub.add_parser("users")
    p = sub.add_parser("cert")
    p.add_argument("--regenerate", action="store_true",
                   help="make a new self-signed certificate (clients must re-pin it)")
    sub.add_parser("status")
    p = sub.add_parser("config")
    p.add_argument("settings", nargs="*", help="key=value (JSON values)")
    args = ap.parse_args(argv)
    home = _home(args)

    from . import config as rconfig
    from .users import UserStore

    if args.cmd == "serve":
        from .gateway import run
        run(home, {"host": args.host, "port": args.port}, console=not args.quiet)
        # leave immediately: nothing to flush that run() has not closed
        sys.stdout.flush()
        os._exit(0)
    users = UserStore(rconfig.users_path(home))
    try:
        if args.cmd == "adduser":
            if users.get(args.name):
                raise SystemExit("user %s exists (use passwd / role)" % args.name)
            users.set_user(args.name, _ask_password(args), args.role)
            print("user %s added (%s)" % (args.name, args.role))
        elif args.cmd == "passwd":
            if not users.get(args.name):
                raise SystemExit("no user %s" % args.name)
            users.set_user(args.name, _ask_password(args))
            print("password of %s changed; their open sessions are closed" % args.name)
        elif args.cmd == "role":
            if not users.get(args.name):
                raise SystemExit("no user %s" % args.name)
            users.set_user(args.name, None, args.role)
            print("%s is now %s" % (args.name, args.role))
        elif args.cmd == "deluser":
            print("removed %s" % args.name if users.remove(args.name) else "no user %s" % args.name)
        elif args.cmd == "users":
            for u in users.list():
                print("%-24s %-9s created %s" % (u["name"], u["role"], time.strftime(
                    "%Y-%m-%d", time.localtime(u["created"] or 0))))
            if not len(users):
                print("(no users)")
        elif args.cmd == "cert":
            from .tlsutil import cert_info, ensure_certificate
            cfg = rconfig.load(home)
            d = rconfig.tls_dir(home)
            if args.regenerate:
                if cfg.get("certfile"):
                    raise SystemExit("a certificate is configured (certfile); nothing to regenerate")
                for f in ("cert.pem", "key.pem"):
                    try:
                        (d / f).unlink()
                    except OSError:
                        pass
            cert, _, self_signed = ensure_certificate(d, cfg.get("certfile"), cfg.get("keyfile"),
                                                     cfg.get("cert_names") or ())
            info = cert_info(cert)
            print("certificate: %s (%s)" % (cert, "self-signed" if self_signed else "configured"))
            print("names:       %s" % ", ".join(info["names"]))
            print("expires:     %s" % info["expires"])
            print("SHA-256:     %s" % info["fingerprint"])
            if args.regenerate:
                print("restart the gateway (or the backend) to use the new certificate")
        elif args.cmd == "status":
            try:
                st = json.loads(rconfig.status_path(home).read_text())
            except Exception:
                print("gateway not running")
                return 1
            alive = time.time() - st.get("t", 0) < 10
            print("gateway %s, pid %s, port %s, %d live connection(s), %d session(s)" % (
                "running" if alive else "NOT RESPONDING (stale status)", st.get("pid"),
                st.get("port"), len(st.get("connections", [])), st.get("sessions", 0)))
            for c in st.get("connections", []):
                print("  %-20s %-9s %-40s since %s" % (c["user"], c["role"], c["ip"],
                                                       time.strftime("%H:%M:%S",
                                                                     time.localtime(c["since"]))))
        elif args.cmd == "config":
            cfg = rconfig.load(home)
            for kv in args.settings:
                if "=" not in kv:
                    raise SystemExit("use key=value")
                k, v = kv.split("=", 1)
                if k not in rconfig.DEFAULTS:
                    raise SystemExit("unknown setting %s (known: %s)" % (k, ", ".join(rconfig.DEFAULTS)))
                try:
                    cfg[k] = json.loads(v)
                except ValueError:
                    cfg[k] = v
            if args.settings:
                print("saved %s (restart the gateway to apply)" % rconfig.save(cfg, home))
            print(json.dumps(cfg, indent=2))
    except ValueError as exc:
        raise SystemExit(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
