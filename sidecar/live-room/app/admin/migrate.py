"""python -m app.admin.migrate [--db PATH]  — create/upgrade zen.sqlite3 (idempotent)."""
from __future__ import annotations

import argparse
import sys

from app.admin.db import SchemaError, default_db_path, migrate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="套用 zen.sqlite3 schema（可重複執行）")
    parser.add_argument("--db", default=None, help="資料庫路徑；預設 ZEN_DB_PATH 或 %%LOCALAPPDATA%%\\ZenBridge\\data\\zen.sqlite3")
    args = parser.parse_args(argv)
    path = args.db or str(default_db_path())
    try:
        version = migrate(path)
    except SchemaError as exc:
        print(f"migrate 失敗：{exc}", file=sys.stderr)
        return 2
    print(f"zen.sqlite3 schema v{version} OK：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
