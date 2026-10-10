"""zen.sqlite3 (+ zen-identity.sqlite3): locations, connections, migration, backups.

Schema: DBA-reviewed v2 (database-review.md) plus the admin tables. PII (accounts, API
tokens, speaker real names, voiceprints) lives in a *separate* identity file that only the
admin process opens; the live-room ledger never touches it.

Locations (env overrides first)
- main DB      ZEN_DB_PATH          | ZEN_DATA_DIR/data/zen.sqlite3
- identity DB  ZEN_IDENTITY_DB_PATH | ZEN_DATA_DIR/data/zen-identity.sqlite3
- backups      ZEN_BACKUP_DIR       | ZEN_DATA_DIR/backups  (put it on another disk!)
- ZEN_DATA_DIR default: %LOCALAPPDATA%\\ZenBridge (Windows), else $XDG_DATA_HOME|~/.local/share/ZenBridge
The live-room caption file (data/captions.sqlite3) is a different database and is never touched.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
IDENTITY_SCHEMA_PATH = Path(__file__).with_name("identity_schema.sql")
SCHEMA_VERSION = 2
IDENTITY_VERSION = 1
MIN_SQLITE = (3, 42, 0)   # unixepoch('subsec'); FTS5 rank=1 integrity-check verified on 3.46
FTS_TABLES = ("transcripts_fts", "transcripts_fts_uni", "translations_fts", "glossary_fts", "tm_fts")

_migrated: set[str] = set()
_migrate_lock = threading.Lock()
_CJK = re.compile(r"([\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003134f])")
# Folders synced by cloud clients break WAL (they copy -wal / -shm separately).
_SYNCED = re.compile(r"(?i)[\\/](onedrive[^\\/]*|dropbox|google drive|icloud ?drive)[\\/]")


class SchemaError(RuntimeError):
    pass


def to_uni(text: str) -> str:
    """Space around every CJK char (insert only), for the unigram FTS table (DBA §4.2)."""
    return _CJK.sub(r" \1 ", text or "")


# ---------------------------------------------------------------- locations
def data_dir(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    explicit = (env.get("ZEN_DATA_DIR") or "").strip()
    if explicit:
        return Path(explicit)
    local = (env.get("LOCALAPPDATA") or "").strip()
    if local:
        return Path(local) / "ZenBridge"
    xdg = (env.get("XDG_DATA_HOME") or "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "ZenBridge"


def default_db_path(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    explicit = (env.get("ZEN_DB_PATH") or "").strip()
    return Path(explicit) if explicit else data_dir(env) / "data" / "zen.sqlite3"


def identity_db_path(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    explicit = (env.get("ZEN_IDENTITY_DB_PATH") or "").strip()
    return Path(explicit) if explicit else data_dir(env) / "data" / "zen-identity.sqlite3"


def backup_dir(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    explicit = (env.get("ZEN_BACKUP_DIR") or "").strip()
    return Path(explicit) if explicit else data_dir(env) / "backups"


def check_db_location(path: str | Path) -> None:
    """Refuse UNC/network paths and cloud-synced folders for a WAL database (DBA §5.5)."""
    raw = str(path)
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise SchemaError(f"資料庫不能放在網路路徑：{raw}")
    if _SYNCED.search(raw.replace("/", "\\") + "\\"):
        raise SchemaError(f"資料庫不能放在雲端同步資料夾（OneDrive/Dropbox…）：{raw}")


def check_sqlite_version(version: str | None = None) -> None:
    raw = version or sqlite3.sqlite_version
    parts = tuple(int(p) for p in raw.split(".")[:3])
    if parts < MIN_SQLITE:
        need = ".".join(map(str, MIN_SQLITE))
        raise SchemaError(f"SQLite {raw} 太舊，zen.sqlite3 需要 {need} 以上（請用 Python 3.12 內建版本）")


# ---------------------------------------------------------------- connections
def connect(path: str | Path, *, readonly: bool = False, timeout_ms: int = 5000) -> sqlite3.Connection:
    """Per-connection PRAGMAs every time (DBA §5.1). Autocommit; callers BEGIN IMMEDIATE."""
    p = Path(path)
    if readonly:
        conn = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=0,
                               check_same_thread=False, isolation_level=None)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=0, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA recursive_triggers = ON")   # REPLACE conflicts fire FTS delete triggers
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA secure_delete = ON")
    if readonly:
        conn.execute("PRAGMA query_only = ON")
    else:
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint = 1000")
        conn.execute("PRAGMA journal_size_limit = 67108864")
    return conn


# ---------------------------------------------------------------- migration
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def migrate(path: str | Path) -> int:
    """Create or verify zen.sqlite3 at schema v2. Idempotent. Returns PRAGMA user_version."""
    check_sqlite_version()
    check_db_location(path)
    conn = connect(path)
    try:
        return migrate_conn(conn)
    finally:
        conn.close()


def migrate_conn(conn: sqlite3.Connection) -> int:
    check_sqlite_version()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise SchemaError(f"資料庫 schema v{version} 比程式 v{SCHEMA_VERSION} 新，請更新程式")
    if version not in (0, SCHEMA_VERSION) or (version == 0 and tables):
        raise SchemaError(
            f"資料庫 schema v{version}（{tables} 張表）不是空檔也不是 v{SCHEMA_VERSION}；"
            "請先備份並用 migrations/0002 升級（v1→v2 腳本尚未提供），或改用新的 ZEN_DB_PATH")
    started = time.perf_counter()
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))   # all CREATE ... IF NOT EXISTS
    if version == 0:
        conn.execute("UPDATE schema_migrations SET checksum=?, duration_ms=? WHERE version=2 AND checksum='see-runner'",
                     (_sha256(SCHEMA_PATH), int((time.perf_counter() - started) * 1000)))
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate_identity(path: str | Path) -> int:
    check_sqlite_version()
    check_db_location(path)
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > IDENTITY_VERSION:
            raise SchemaError(f"個資庫 v{version} 比程式新")
        conn.executescript(IDENTITY_SCHEMA_PATH.read_text(encoding="utf-8"))
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def ensure_migrated(path: str | Path) -> None:
    key = str(Path(path).resolve())
    with _migrate_lock:
        if key in _migrated:
            return
        migrate(path)
        _migrated.add(key)


def fts_integrity(conn: sqlite3.Connection) -> dict[str, str]:
    """rank=1 integrity-check for every external-content FTS table (DBA §3.5)."""
    out = {}
    for name in FTS_TABLES:
        try:
            conn.execute(f"INSERT INTO {name}({name}, rank) VALUES ('integrity-check', 1)")
            out[name] = "ok"
        except sqlite3.DatabaseError as exc:
            out[name] = f"FAIL: {exc}"
    return out


# ---------------------------------------------------------------- backups
def backup_to(src_path: str | Path, dest_dir: str | Path, *, stamp: str | None = None) -> Path:
    """VACUUM INTO <dest_dir>/zen-<stamp>.sqlite3 via a .tmp file, then verify, then rename."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    final = (dest_dir / f"zen-{stamp}.sqlite3").resolve()
    if final.parent != dest_dir.resolve():
        raise SchemaError("備份路徑不在備份資料夾內")
    tmp = final.with_name(final.name + ".tmp")
    tmp.unlink(missing_ok=True)
    conn = connect(src_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        conn.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        conn.close()
    chk = sqlite3.connect(str(tmp))
    try:
        if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SchemaError("備份 integrity_check 失敗")
        bad = {k: v for k, v in fts_integrity(chk).items() if v != "ok"}
        if bad:
            raise SchemaError(f"備份 FTS 檢查失敗：{bad}")
    finally:
        chk.close()
    os.replace(tmp, final)
    return final
