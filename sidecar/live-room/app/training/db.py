"""training.sqlite3 路徑與連線。"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 1
SCHEMA = Path(__file__).with_name("schema.sql")


def training_dir(env: dict | None = None) -> Path:
    """%LOCALAPPDATA%\\ZenBridge\\training（ZEN_TRAINING_DIR 覆寫；否則跟著 ZEN_DATA_DIR）。"""
    env = os.environ if env is None else env
    explicit = (env.get("ZEN_TRAINING_DIR") or "").strip()
    if explicit:
        return Path(explicit)
    from app.admin.db import data_dir
    return Path(data_dir(env)) / "training"


def db_path(root: Path | None = None) -> Path:
    return (root or training_dir()) / "training.sqlite3"


def connect(root: Path | None = None, *, create: bool = True) -> sqlite3.Connection:
    root = Path(root) if root else training_dir()
    path = db_path(root)
    if not create and not path.exists():
        raise FileNotFoundError(path)
    root.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=10000")
    if create:
        migrate(c)
    return c


def migrate(c: sqlite3.Connection) -> None:
    row = None
    try:
        row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        pass
    if row and int(row[0]) >= SCHEMA_VERSION:
        return
    c.executescript(SCHEMA.read_text(encoding="utf-8"))
    c.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))


def audit(c: sqlite3.Connection, action: str, *, speaker_id: str | None = None, actor: str | None = None,
          **detail) -> None:
    c.execute("INSERT INTO audit(at, action, speaker_id, actor, detail) VALUES (?,?,?,?,?)",
              (time.time(), action, speaker_id, actor, json.dumps(detail, ensure_ascii=False)))


def rel(root: Path, p: Path) -> str:
    return Path(p).resolve().relative_to(Path(root).resolve()).as_posix()


def absolute(root: Path, relpath: str) -> Path:
    """Resolve a stored relative path; refuses anything that escapes the training dir."""
    base = Path(root).resolve()
    p = (base / relpath).resolve()
    if base != p and base not in p.parents:
        raise ValueError("路徑超出訓練資料夾")
    return p
