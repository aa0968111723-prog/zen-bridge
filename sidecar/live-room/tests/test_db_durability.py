"""Data-loss regressions: ledger BUSY retry, atomic + cross-process migration (QA DB reports)."""
import multiprocessing as mp
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app.admin import db
from app.ledger import Ledger


def cap(seq=1, zh="因緣具足", en="", status="zh_ready", **kw):
    ev = {"type": "caption", "id": f"class:s1:{seq}", "room_id": "class", "session_id": "s1", "seq": seq,
          "zh": zh, "en": en, "status": status, "version": 1, "t0_ms": seq * 6000, "t1_ms": seq * 6000 + 6000}
    ev.update(kw)
    return ev


# ---------------------------------------------------------------- ledger BUSY
def test_ledger_busy_retries_then_writes(tmp_path):
    path = tmp_path / "zen.sqlite3"
    db.migrate(path)
    blocker = db.connect(path)
    blocker.execute("BEGIN IMMEDIATE")
    led = Ledger(path, connect=lambda p: db.connect(p, timeout_ms=100))
    for i in range(5):
        led.submit(cap(seq=i + 1, zh=f"第{i + 1}句因緣具足"))
    threading.Timer(0.8, lambda: blocker.execute("COMMIT")).start()
    assert led.wait_idle(15)
    led.close()
    blocker.close()
    c = db.connect(path)
    assert c.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 5
    c.close()
    st = led.stats()
    assert st["ledger_written"] == 5 and st["ledger_errors"] == 0 and st["ledger_retries"] >= 1


def test_ledger_gives_up_after_retry_budget(tmp_path):
    path = tmp_path / "zen.sqlite3"
    db.migrate(path)
    blocker = db.connect(path)
    blocker.execute("BEGIN IMMEDIATE")
    led = Ledger(path, connect=lambda p: db.connect(p, timeout_ms=10), busy_retries=2, sleep=lambda s: None)
    led.submit(cap())
    assert led.wait_idle(5)
    led.close()
    blocker.execute("ROLLBACK")
    blocker.close()
    st = led.stats()
    # no batch drop: after the retry budget the batch is spooled to disk, not counted lost
    assert st["ledger_errors"] == 0 and st["ledger_retries"] == 2 and st["ledger_spooled"] == 1
    assert led.spool_path.exists()


# ---------------------------------------------------------------- atomic migration
def test_failed_migration_leaves_empty_file_and_rerun_works(tmp_path, monkeypatch):
    path = tmp_path / "zen.sqlite3"
    good = db.SCHEMA_PATH.read_text(encoding="utf-8")
    cut = good.index("CREATE TRIGGER")
    bad = tmp_path / "bad.sql"
    bad.write_text(good[:cut] + "\nCREATE TABLE broken (;\n" + good[cut:], encoding="utf-8")
    monkeypatch.setattr(db, "SCHEMA_PATH", bad)
    with pytest.raises(sqlite3.Error):
        db.migrate(path)
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0
    c.close()
    monkeypatch.setattr(db, "SCHEMA_PATH", Path(db.__file__).with_name("schema.sql"))
    assert db.migrate(path) == db.SCHEMA_VERSION
    c = db.connect(path)
    assert c.execute("PRAGMA auto_vacuum").fetchone()[0] == 2           # INCREMENTAL survived
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert all(v == "ok" for v in db.fts_integrity(c).values())
    c.close()


def test_split_sql_keeps_trigger_bodies():
    stmts = db.split_sql(db.SCHEMA_PATH.read_text(encoding="utf-8"))
    trig = [s for s in stmts if s.upper().startswith("CREATE TRIGGER")]
    assert trig and all(s.rstrip().upper().endswith("END;") for s in trig)


def _mig(path, q):
    try:
        q.put(("ok", db.migrate(path)))
    except Exception as exc:          # pragma: no cover - the failure we guard against
        q.put(("err", f"{type(exc).__name__}: {exc}"))


def test_concurrent_first_migration_never_fails(tmp_path):
    ctx = mp.get_context("spawn")
    for rnd in range(6):
        path = tmp_path / f"z{rnd}.sqlite3"
        q = ctx.Queue()
        procs = [ctx.Process(target=_mig, args=(str(path), q)) for _ in range(3)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        res = [q.get(timeout=5) for _ in procs]
        assert all(r == ("ok", db.SCHEMA_VERSION) for r in res), (rnd, res)


def test_identity_migration_idempotent(tmp_path):
    p = tmp_path / "zen-identity.sqlite3"
    assert db.migrate_identity(p) == 1
    assert db.migrate_identity(p) == 1


def test_migrate_retries_fts_vtable_race(tmp_path, monkeypatch):
    """Box flake under load: concurrent first start hit 'vtable constructor failed: tm_fts'."""
    import sqlite3 as _sq
    real = db.migrate_conn
    calls = {"n": 0}

    def flaky(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _sq.OperationalError("vtable constructor failed: tm_fts")
        return real(conn)
    monkeypatch.setattr(db, "migrate_conn", flaky)
    assert db.migrate(tmp_path / "z.sqlite3") == db.SCHEMA_VERSION
    assert calls["n"] == 2

    def locked(conn):
        raise _sq.OperationalError("no such table: nope")
    monkeypatch.setattr(db, "migrate_conn", locked)
    import pytest as _pt
    with _pt.raises(_sq.OperationalError):
        db.migrate(tmp_path / "y.sqlite3")
