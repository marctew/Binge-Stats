"""python -m bingestats {web|worker|migrate|create-admin}"""
from __future__ import annotations

import argparse
import getpass
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bingestats")
    p.add_argument("--data-dir", default=None, help="defaults to $DATA_DIR or /data")
    sub = p.add_subparsers(dest="cmd", required=True)
    web = sub.add_parser("web", help="run the web UI (gunicorn in Docker; this is the dev server)")
    web.add_argument("--dev", action="store_true")
    web.add_argument("--port", type=int, default=8181)
    sub.add_parser("worker", help="run the ingest worker")
    sub.add_parser("migrate", help="apply database migrations")
    ca = sub.add_parser("create-admin", help="create or reset the admin login")
    ca.add_argument("username")
    args = p.parse_args(argv)

    if args.cmd == "worker":
        from .worker.main import main as worker_main
        worker_main(args.data_dir)
        return 0

    from . import config, db
    cfg = config.load(args.data_dir)
    if args.cmd == "migrate":
        print(f"schema version {db.migrate_with_lock(cfg.db_path)}")
        return 0
    if args.cmd == "create-admin":
        from .services import auth
        db.migrate_with_lock(cfg.db_path)
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Again: "):
            print("Passwords don't match", file=sys.stderr)
            return 1
        conn = db.connect(cfg.db_path)
        auth.set_admin(conn, args.username, pw)
        print("Admin saved")
        return 0
    if args.cmd == "web":
        from . import create_app
        app = create_app(args.data_dir)
        app.run(host="0.0.0.0", port=args.port, debug=args.dev)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
