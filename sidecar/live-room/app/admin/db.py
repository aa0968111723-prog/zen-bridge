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
from contextlib import closing
import os
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("zen.admin.db")

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
IDENTITY_SCHEMA_PATH = Path(__file__).with_name("identity_schema.sql")
BASELINE_VERSION = 2          # schema.sql creates v2; never edit it (DBA D6) - add a migration
SCHEMA_VERSION = 12           # round3 C3: = highest migrations/NNNN_*.sql (0004–0009 reserved; backend staging = 0010+)
MIGRATIONS_DIR = Path(__file__).with_name("migrations")
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
    if _is_remote_drive(raw):
        raise SchemaError(f"資料庫不能放在網路磁碟機：{raw}")


DRIVE_REMOTE = 4


def _is_remote_drive(raw: str, get_drive_type=None) -> bool:
    """DBA D13: a mapped network drive letter (Z:) is as unsafe for WAL as a UNC path. Windows only."""
    m = re.match(r"^([A-Za-z]):", raw)
    if not m:
        return False
    if get_drive_type is None:
        if os.name != "nt":
            return False
        try:
            import ctypes
            fn = ctypes.windll.kernel32.GetDriveTypeW
            fn.argtypes = [ctypes.c_wchar_p]
            fn.restype = ctypes.c_uint
            get_drive_type = fn
        except Exception:
            return False
    try:
        return int(get_drive_type(m.group(1).upper() + ":\\")) == DRIVE_REMOTE
    except Exception:
        return False


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


_FILE_PRAGMAS = ("PRAGMA AUTO_VACUUM", "PRAGMA JOURNAL_MODE")


def split_sql(text: str) -> list[str]:
    """Split a schema script into complete statements (trigger bodies stay whole)."""
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            body = "\n".join(ln for ln in buf.splitlines() if not ln.lstrip().startswith("--")).strip()
            if body:
                out.append(body)
            buf = ""
    if buf.strip() and any(not ln.lstrip().startswith("--") for ln in buf.splitlines() if ln.strip()):
        raise SchemaError("schema 檔結尾有不完整的 SQL 敘述")
    return out


def _norm(stmt: str) -> str:
    return " ".join(stmt.split()).upper()


_OBJ_RE = re.compile(r"CREATE\s+(?:VIRTUAL\s+|UNIQUE\s+)?(?:TABLE|INDEX|TRIGGER|VIEW)\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)",
                     re.IGNORECASE)
_SEEDED = {"schema_migrations", "schema_meta", "retention_policy"}


def half_migrated(conn: sqlite3.Connection, body: list[str]) -> bool:
    """A v0 file whose objects all come from this schema and whose tables hold no user rows:
    the old non-transactional migrate crashed part-way. Safe to finish with the IF NOT EXISTS body."""
    expected = {m.group(1) for st in body for m in [_OBJ_RE.search(st)] if m}
    fts = {n for n in expected if n.endswith("_fts") or n.endswith("_fts_uni")}
    rows = conn.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    for kind, name in rows:
        if name in expected or any(name.startswith(f + "_") for f in fts):
            continue
        return False
    for kind, name in rows:
        if kind == "table" and name in expected and name not in _SEEDED and name not in fts:
            if conn.execute(f'SELECT 1 FROM "{name}" LIMIT 1').fetchone():
                return False
    return True


def _schema_sums(path: Path) -> set[str]:
    """sha256 of schema.sql with LF and with CRLF endings (git autocrlf on Windows may flip them)."""
    raw = Path(path).read_bytes()
    lf = raw.replace(b"\r\n", b"\n")
    return {hashlib.sha256(v).hexdigest() for v in (raw, lf, lf.replace(b"\n", b"\r\n"))}


def _apply_schema(conn: sqlite3.Connection, schema_path: Path, target: int, label: str,
                  *, record_checksum: bool, past_ok: bool = False) -> int:
    """Atomic, cross-process-safe migration (QA 資料庫管理員 D2/D3, 實測專家 D1/D2).

    File-level PRAGMAs (auto_vacuum, journal_mode) run first on a brand-new file, outside any
    transaction (auto_vacuum only takes effect before the first table). Everything else runs
    statement by statement inside ONE BEGIN IMMEDIATE; the version is re-read under that write
    lock, so a second process waits and then sees the finished schema. Any failure rolls the
    whole schema back, leaving an empty file that the next start can migrate again.
    """
    check_sqlite_version()
    stmts = split_sql(schema_path.read_text(encoding="utf-8"))
    file_pragmas = [x for x in stmts if _norm(x).startswith(_FILE_PRAGMAS)]
    body = [x for x in stmts if x not in file_pragmas and not _norm(x).startswith("PRAGMA FOREIGN_KEYS")]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    if version > target and past_ok:
        return version                 # baseline already applied and migrated further (C3)
    if version > target:
        raise SchemaError(f"{label} schema v{version} 比程式 v{target} 新，請更新程式")
    if version == 0 and tables == 0:
        for pragma in file_pragmas:
            conn.execute(pragma).fetchall()
    recovering = False
    if version == 0 and tables and half_migrated(conn, body):
        # Recovery path for a file half-migrated by the old non-transactional migrate:
        # keep a copy, then finish it in one transaction below.
        recovering = True
        main_file = conn.execute("PRAGMA database_list").fetchone()[2]
        if main_file:
            dest = sqlite3.connect(f"{main_file}.half-migrated-{int(time.time())}.bak")
            try:
                conn.backup(dest)
            finally:
                dest.close()
        log.warning("%s was half-migrated (v0, %d tables, no data); completing the schema", label, tables)
    started = time.perf_counter()
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        if version > target and past_ok:
            conn.execute("ROLLBACK")
            return version             # another process migrated past the baseline while we waited
        if version > target:
            raise SchemaError(f"{label} schema v{version} 比程式 v{target} 新，請更新程式")
        if version == target:
            # 實測專家 D5: a corrupt file must not start silently.
            qc = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
            if qc != "ok":
                raise SchemaError(f"{label} 損毀（quick_check: {qc}）；請從備份還原（python -m app.admin.restore）")
            if record_checksum:
                # DBA D6: schema.sql changed without a version bump -> refuse instead of a mixed schema.
                row = conn.execute("SELECT checksum FROM schema_migrations WHERE version=?", (target,)).fetchone()
                if row and row[0] not in ("see-runner", *_schema_sums(schema_path)):
                    raise SchemaError(f"{label} 的 schema.sql 和已套用的 v{target} 不一致；"
                                      f"請新增遷移腳本並提高 SCHEMA_VERSION，不要直接改 schema.sql")
        if version not in (0, target) or (version == 0 and tables and not recovering):
            raise SchemaError(
                f"{label} schema v{version}（{tables} 張表）不是空檔也不是 v{target}；"
                "v1 檔沒有遷移腳本：請先備份，再改用新的 ZEN_DB_PATH")
        fresh = version == 0
        for stmt in body:                       # all CREATE ... IF NOT EXISTS / INSERT OR IGNORE
            conn.execute(stmt)
        if fresh and record_checksum:
            conn.execute("UPDATE schema_migrations SET checksum=?, duration_ms=? WHERE version=? AND checksum='see-runner'",
                         (_sha256(schema_path), int((time.perf_counter() - started) * 1000), target))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(path: str | Path) -> int:
    """Create or verify zen.sqlite3 at schema v2. Idempotent and atomic. Returns user_version."""
    check_sqlite_version()
    check_db_location(path)
    for attempt in range(6):
        conn = connect(path, timeout_ms=30000)
        try:
            return migrate_conn(conn)
        except sqlite3.OperationalError as exc:
            # Concurrent first start (architect A3): another process can be creating the FTS5 tables while
            # this connection prepares against them. FTS5's xConnect then fails as "vtable constructor
            # failed" instead of waiting on busy_timeout. Each step is one BEGIN IMMEDIATE, so a fresh
            # connection (fresh schema) retries safely and sees the other process's committed steps.
            if "vtable constructor failed" not in str(exc) or attempt == 5:
                raise
            log.warning("migrate: %s; retrying with a fresh connection (%d)", exc, attempt + 1)
            time.sleep(0.05 * (attempt + 1))
        finally:
            conn.close()
    raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------- stepwise migrations (round3 C3)
_STEP_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


def migration_steps(directory: Path | None = None) -> list[tuple[int, str, Path]]:
    """[(version, name, path)] for migrations/NNNN_name.sql, NNNN > baseline, ascending.

    The first step must be baseline+1. Later numbers may skip reserved ranges (zen-impl BRIEF:
    each agent numbers from its own block, e.g. backend staging = 0010), but a number may appear
    only once. run_migrations refuses a step numbered below the DB's version that was never applied,
    so a late-merged lower number can never be skipped silently."""
    d = Path(directory or MIGRATIONS_DIR)
    steps = []
    for p in sorted(d.glob("*.sql")) if d.is_dir() else []:
        m = _STEP_RE.match(p.name)
        if m and int(m.group(1)) > BASELINE_VERSION:
            steps.append((int(m.group(1)), m.group(2), p))
    nums = [v for v, _, _ in steps]
    if len(set(nums)) != len(nums) or (nums and nums[0] != BASELINE_VERSION + 1):
        raise SchemaError(f"遷移檔編號重複或不是從 v{BASELINE_VERSION + 1} 開始：{[p.name for _, _, p in steps]}")
    return steps


def snapshot_before_migrate(conn: sqlite3.Connection, version: int) -> Path | None:
    """VACUUM INTO <db dir>/pre-migrate-v{version}.sqlite3 (never overwritten; a second attempt
    gets a timestamped name), integrity-verified. Rollback (scripts/update.py) points here."""
    main_file = conn.execute("PRAGMA database_list").fetchone()[2]
    if not main_file:
        return None                        # :memory:
    base = Path(main_file).with_name(f"pre-migrate-v{version}.sqlite3")
    dest = base if not base.exists() else base.with_name(f"pre-migrate-v{version}-{int(time.time() * 1000)}.sqlite3")
    conn.execute("VACUUM INTO ?", (str(dest),))
    _verify_copy(dest)
    _restrict(dest)
    log.warning("pre-migration snapshot: %s", dest)
    return dest


def _verify_steps(conn: sqlite3.Connection, steps) -> None:
    applied = {r[0]: r[1] for r in conn.execute("SELECT version, checksum FROM schema_migrations WHERE version > ?",
                                                (BASELINE_VERSION,))}
    for v, name, path in steps:
        if v in applied and applied[v] not in _schema_sums(path):
            raise SchemaError(f"遷移檔 {path.name} 和已套用的 v{v} 不一致；不要修改已發布的遷移檔，請新增下一號")


def run_migrations(conn: sqlite3.Connection, *, target: int | None = None, directory: Path | None = None,
                   snapshot: bool = True) -> int:
    """Apply every NNNN step above the current user_version, one BEGIN IMMEDIATE per step (version
    re-read under the write lock, so concurrent starts apply each step once). A DB that held data
    before gets a verified pre-migration snapshot first. Returns the final user_version."""
    steps = migration_steps(directory)
    target = SCHEMA_VERSION if target is None else target
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > target:
        raise SchemaError(f"資料庫 schema v{version} 比程式 v{target} 新，請更新程式；"
                          f"或用更新前的快照 pre-migrate-v{target}.sqlite3 還原（python -m app.admin.restore）")
    _verify_steps(conn, steps)
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations WHERE version > ?", (BASELINE_VERSION,))}
    late = [p.name for v, _, p in steps if v <= version and v not in applied]
    if late:
        raise SchemaError(f"遷移檔 {late} 的編號低於資料庫目前的 v{version} 卻從未套用；"
                          f"不要插入比已發布版本更小的編號，請改成下一個未使用的號碼")
    todo = [st for st in steps if version < st[0] <= target]
    if not todo:
        return version
    if snapshot and conn.execute("SELECT count(*) FROM segments").fetchone()[0] + \
            conn.execute("SELECT count(*) FROM glossary_terms").fetchone()[0] > 0:
        snapshot_before_migrate(conn, version)
    for v, name, path in todo:
        stmts = [x for x in split_sql(path.read_text(encoding="utf-8")) if not _norm(x).startswith("PRAGMA")]
        started = time.perf_counter()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] >= v:
                conn.execute("ROLLBACK")
                continue                       # another process applied it while we waited
            for stmt in stmts:
                conn.execute(stmt)
            conn.execute("INSERT OR REPLACE INTO schema_migrations(version, name, checksum, duration_ms) VALUES (?,?,?,?)",
                         (v, name, _sha256(path), int((time.perf_counter() - started) * 1000)))
            conn.execute(f"PRAGMA user_version = {int(v)}")
            conn.execute("COMMIT")
            log.warning("migrated 資料庫 to v%d (%s)", v, path.name)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate_conn(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise SchemaError(f"資料庫 schema v{version} 比程式 v{SCHEMA_VERSION} 新，請更新程式；"
                          f"或用更新前的快照 pre-migrate-v{SCHEMA_VERSION}.sqlite3 還原（python -m app.admin.restore）")
    if version <= BASELINE_VERSION:
        _apply_schema(conn, SCHEMA_PATH, BASELINE_VERSION, "資料庫", record_checksum=True, past_ok=True)
    else:
        _verify_current(conn, "資料庫", SCHEMA_PATH, BASELINE_VERSION)
    return run_migrations(conn)


def _verify_current(conn: sqlite3.Connection, label: str, schema_path: Path, baseline: int) -> None:
    """Checks _apply_schema does at version==target, for a DB already past the baseline."""
    qc = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
    if qc != "ok":
        raise SchemaError(f"{label} 損毀（quick_check: {qc}）；請從備份還原（python -m app.admin.restore）")
    row = conn.execute("SELECT checksum FROM schema_migrations WHERE version=?", (baseline,)).fetchone()
    if row and row[0] not in ("see-runner", *_schema_sums(schema_path)):
        raise SchemaError(f"{label} 的 schema.sql 和已套用的 v{baseline} 不一致；"
                          f"請新增遷移腳本並提高 SCHEMA_VERSION，不要直接改 schema.sql")


def migrate_identity(path: str | Path) -> int:
    check_sqlite_version()
    check_db_location(path)
    conn = connect(path, timeout_ms=30000)
    try:
        return _apply_schema(conn, IDENTITY_SCHEMA_PATH, IDENTITY_VERSION, "個資庫", record_checksum=False)
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
def _restrict(path: Path) -> None:
    try:
        from app.admin.security import restrict_file
        restrict_file(path)
    except Exception:          # pragma: no cover - best effort
        pass


def _verify_copy(path: Path) -> None:
    chk = sqlite3.connect(str(path))
    try:
        if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SchemaError("備份 integrity_check 失敗")
        names = {r[0] for r in chk.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        bad = {k: v for k, v in fts_integrity(chk).items() if k in names and v != "ok"}
        if bad:
            raise SchemaError(f"備份 FTS 檢查失敗：{bad}")
    finally:
        chk.close()


def backup_to(src_path: str | Path, dest_dir: str | Path, *, stamp: str | None = None, prefix: str = "zen") -> Path:
    """VACUUM INTO <dest_dir>/<prefix>-<stamp>.sqlite3 via a .tmp file, then verify, then rename.

    QA fixes: never overwrites an existing backup (suffix -2, -3 …; DB備份 D8 / CTO-20), removes
    the .tmp on failure (D5), restricts permissions (D9), PASSIVE checkpoint only (D7).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    final = (dest_dir / f"{prefix}-{stamp}.sqlite3").resolve()
    n = 2
    while final.exists():
        final = (dest_dir / f"{prefix}-{stamp}-{n}.sqlite3").resolve()
        n += 1
    if final.parent != dest_dir.resolve():
        raise SchemaError("備份路徑不在備份資料夾內")
    tmp = final.with_name(final.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        conn = connect(src_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
            conn.execute("VACUUM INTO ?", (str(tmp),))
        finally:
            conn.close()
        _restrict(tmp)
        _verify_copy(tmp)
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return final


def _drive(path: Path) -> str:
    p = Path(path).resolve()
    if os.name == "nt":
        return (p.drive or str(p)[:2]).upper()
    try:
        probe = p if p.is_dir() else p.parent      # dirs: overlayfs reports per-file st_dev
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return str(os.stat(probe).st_dev)
    except OSError:
        return ""


def same_disk(a: str | Path, b: str | Path) -> bool:
    """True when the backup folder sits on the same drive/device as the database (CTO-03)."""
    da, db_ = _drive(Path(a)), _drive(Path(b))
    return bool(da) and da == db_


def backup_set(main_path: str | Path, dest_dir: str | Path, identity_path: str | Path | None = None,
               *, stamp: str | None = None) -> dict:
    """Back up zen.sqlite3 and (when present) zen-identity.sqlite3 together + a manifest."""
    import json
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    main = backup_to(main_path, dest_dir, stamp=stamp)
    files = {"main": main.name}
    if identity_path and Path(identity_path).exists():
        ident = backup_to(identity_path, dest_dir, stamp=main.stem.split("zen-", 1)[1], prefix="zen-identity")
        files["identity"] = ident.name
    with closing(sqlite3.connect(str(Path(dest_dir) / main.name))) as chk:
        actual = chk.execute("PRAGMA user_version").fetchone()[0]     # C3: the file's, not the code's
    manifest = {"stamp": stamp, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "files": {},
                "schema_version": actual}
    for k, name in files.items():
        f = Path(dest_dir) / name
        manifest["files"][k] = {"name": name, "bytes": f.stat().st_size, "sha256": _sha256(f)}
    mpath = main.with_suffix(".json")
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    _restrict(mpath)
    return {"file": main.name, "files": files, "manifest": mpath.name, "bytes": main.stat().st_size}


_BACKUP_RE = re.compile(r"^zen-(\d{8})-(\d{6})(?:-\d+)?\.sqlite3$")


def list_backups(dest_dir: str | Path) -> list[Path]:
    d = Path(dest_dir)
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir() if _BACKUP_RE.match(p.name)), key=lambda p: p.name)


def latest_backup_age_s(dest_dir: str | Path, now: float | None = None) -> float | None:
    files = list_backups(dest_dir)
    if not files:
        return None
    return max(0.0, (now or time.time()) - files[-1].stat().st_mtime)


def rotate_backups(dest_dir: str | Path, *, keep_daily: int = 14, keep_weekly: int = 8) -> list[str]:
    """Keep the newest backup of each of the last keep_daily days and of the last keep_weekly
    ISO weeks; delete the rest (main + identity + manifest together). Returns deleted names."""
    import datetime as dt
    files = list_backups(dest_dir)
    keep: set[str] = set()
    days: dict[str, Path] = {}
    weeks: dict[tuple, Path] = {}
    for f in files:
        m = _BACKUP_RE.match(f.name)
        day = m.group(1)
        days[day] = f                              # sorted ascending -> newest wins
        d = dt.date(int(day[:4]), int(day[4:6]), int(day[6:]))
        weeks[tuple(d.isocalendar()[:2])] = f
    keep.update(p.name for _, p in sorted(days.items())[-keep_daily:])
    keep.update(p.name for _, p in sorted(weeks.items())[-keep_weekly:])
    deleted = []
    for f in files:
        if f.name in keep:
            continue
        rest = f.name[len("zen-"):]
        for extra in (f, f.with_suffix(".json"), f.with_name("zen-identity-" + rest)):
            if extra.exists():
                extra.unlink()
                deleted.append(extra.name)
    return deleted


def restore_from(backup: str | Path, target: str | Path, *, stamp: str | None = None) -> dict:
    """Verified restore (實測專家 D4 / DB備份 D4). Never leaves a stale -wal next to the new file.

    1. verify the backup (integrity + FTS); 2. take an EXCLUSIVE lock on the target so a running
    live room / admin makes this fail instead of corrupting; 3. move target + -wal/-shm aside as
    *.pre-restore-<stamp>; 4. copy the backup in via a temp file and os.replace.
    """
    import shutil
    backup, target = Path(backup), Path(target)
    _verify_copy(backup)
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    moved = []
    if target.exists():
        lock = sqlite3.connect(str(target), timeout=1.0, isolation_level=None)
        try:
            lock.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            lock.execute("BEGIN EXCLUSIVE")
            lock.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            raise SchemaError("資料庫正在使用中：請先關閉直播服務與後台再還原") from exc
        finally:
            lock.close()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(target) + suffix)
            if p.exists():
                aside = Path(f"{p}.pre-restore-{stamp}")
                os.replace(p, aside)
                moved.append(aside.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".restore.tmp")
    shutil.copyfile(backup, tmp)
    _restrict(tmp)
    os.replace(tmp, target)
    _verify_copy(target)
    return {"restored": str(target), "from": backup.name, "moved_aside": moved}
