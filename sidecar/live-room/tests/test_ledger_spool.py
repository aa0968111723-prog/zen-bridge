"""Ledger never drops a batch: BUSY past the budget, I/O errors, or an unopenable DB spool the
events to <db>.spool.jsonl, replayed on the next successful write (DBA D1 / 全站 D5, D6).
Plus: a DB half-migrated by the old non-transactional migrate is completed by migrate()."""
import sqlite3

from app.admin import db
from app.ledger import Ledger
from tests.test_db_durability import cap


def count(path, sql="SELECT COUNT(*) FROM segments"):
    c = db.connect(path)
    try:
        return c.execute(sql).fetchone()[0]
    finally:
        c.close()


def test_busy_past_budget_spools_then_replays(tmp_path):
    path = tmp_path / "zen.sqlite3"
    db.migrate(path)
    blocker = db.connect(path)
    blocker.execute("BEGIN IMMEDIATE")
    led = Ledger(path, connect=lambda p: db.connect(p, timeout_ms=10), busy_retries=1, sleep=lambda s: None)
    for i in range(3):
        led.submit(cap(seq=i + 1, zh=f"第{i + 1}句"))
    assert led.wait_idle(5)
    blocker.execute("ROLLBACK")
    blocker.close()
    assert led.stats()["ledger_spooled"] == 3 and count(path) == 0
    led.submit(cap(seq=9, zh="第九句"))
    assert led.wait_idle(5)
    led.close()
    assert count(path) == 4 and led.stats()["ledger_replayed"] == 3 and led.stats()["ledger_errors"] == 0
    assert not led.spool_path.exists()


def test_unopenable_db_spools_instead_of_dropping(tmp_path):
    path = tmp_path / "zen.sqlite3"
    db.migrate(path)
    state = {"fail": True}

    def connect(p):
        if state["fail"]:
            raise sqlite3.OperationalError("unable to open database file")
        return db.connect(p)
    clock = {"t": 1000.0}
    led = Ledger(path, connect=connect, clock=lambda: clock["t"])
    led.submit(cap(seq=1))
    assert led.wait_idle(5)
    led.submit(cap(seq=2))                     # inside the 30 s reopen window
    assert led.wait_idle(5)
    assert led.stats()["ledger_spooled"] == 2 and led.stats()["ledger_errors"] == 0
    state["fail"] = False
    clock["t"] += 60
    led.submit(cap(seq=3))
    assert led.wait_idle(5)
    led.close()
    assert count(path) == 3


def test_half_migrated_db_is_completed(tmp_path):
    """Old v2 migrate ran statements one by one outside a transaction; a crash left some tables."""
    path = tmp_path / "zen.sqlite3"
    stmts = db.split_sql(db.SCHEMA_PATH.read_text(encoding="utf-8"))
    raw = sqlite3.connect(path)
    done = 0
    for st in stmts:
        if st.strip().upper().startswith("PRAGMA"):
            continue
        raw.execute(st)
        done += 1
        if done == len(stmts) // 2:
            break
    raw.commit()
    raw.close()
    assert db.migrate(path) >= 1
    assert list(tmp_path.glob("zen.sqlite3.half-migrated-*.bak"))          # a copy was kept
    c = db.connect(path)
    assert all(v == "ok" for v in db.fts_integrity(c).values())
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT COUNT(*) FROM retention_policy").fetchone()[0] >= 9
    c.close()
    led = Ledger(path)
    led.submit(cap())
    assert led.wait_idle(5)
    led.close()
    assert count(path) == 1


def test_v0_file_with_foreign_data_still_refused(tmp_path):
    import pytest
    path = tmp_path / "zen.sqlite3"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE notes(x)")
    raw.execute("INSERT INTO notes VALUES (1)")
    raw.commit()
    raw.close()
    with pytest.raises(db.SchemaError):
        db.migrate(path)


def test_close_timeout_spools_pending_events_for_next_start(tmp_path):
    """全站 D7: events still queued when close() gives up are not lost with the process."""
    import threading
    path = tmp_path / "zen.sqlite3"
    db.migrate(path)
    gate = threading.Event()

    def slow_connect(p):
        gate.wait(3)
        return db.connect(p)
    led = Ledger(path, connect=slow_connect)
    led.submit(cap(seq=1, zh="第1句"))
    import time
    time.sleep(0.15)                        # writer is stuck opening the DB with event 1
    for i in range(1, 5):
        led.submit(cap(seq=i + 1, zh=f"第{i + 1}句"))
    led.close(timeout=0.2)
    assert led.stats()["ledger_spooled"] == 4
    gate.set()
    led._thread.join(5)
    led2 = Ledger(path)
    led2.submit(cap(seq=99, zh="重新啟動"))
    assert led2.wait_idle(5)
    led2.close()
    assert count(path) == 6          # event 1 + the 4 spooled lines (replayed after restart) + new one
