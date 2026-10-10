"""python -m app.admin.restore <backup.sqlite3> [--identity <zen-identity-backup>] [--list]

Verified restore of zen.sqlite3 (and optionally zen-identity.sqlite3). Close the live room
and the admin backend first; the restore refuses to run while the database is in use.
The current files are kept next to the database as *.pre-restore-<stamp>.
"""
from __future__ import annotations

import argparse
import json
import sys

from app.admin import db


def main(argv=None) -> int:
    from app.console import safe_console
    safe_console()
    ap = argparse.ArgumentParser(prog="python -m app.admin.restore")
    ap.add_argument("backup", nargs="?")
    ap.add_argument("--identity", default=None)
    ap.add_argument("--list", action="store_true", help="list backups in ZEN_BACKUP_DIR")
    args = ap.parse_args(argv)
    if args.list or not args.backup:
        for f in db.list_backups(db.backup_dir()):
            print(f.name)
        return 0
    try:
        out = [db.restore_from(args.backup, db.default_db_path())]
        if args.identity:
            out.append(db.restore_from(args.identity, db.identity_db_path()))
        db.migrate(db.default_db_path())
    except db.SchemaError as exc:
        print(f"還原失敗：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
