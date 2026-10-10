import threading
import time

from app.admin import db
from app.ledger import Ledger
from tests.local_fakes import migrated_db


def caption(segment_id, seq, zh="", en="", status="ready"):
    return {
        "type": "caption",
        "id": segment_id,
        "room_id": "edge-room",
        "session_id": "edge-session",
        "seq": seq,
        "zh": zh,
        "en": en,
        "status": status,
        "version": 1,
        "t0_ms": seq * 1000,
        "t1_ms": (seq + 1) * 1000,
    }


def test_busy_database_retries_after_lock_is_released(tmp_path):
    path = migrated_db(tmp_path)
    blocker = db.connect(path, timeout_ms=10)
    blocker.execute("BEGIN IMMEDIATE")
    ledger = Ledger(path, connect=lambda p: db.connect(p, timeout_ms=10))
    ledger.submit(caption("edge-room:edge-session:1", 1, "因緣具足", "Conditions are complete."))
    release = threading.Timer(0.15, blocker.execute, args=("COMMIT",))
    release.start()

    assert ledger.wait_idle(5)
    ledger.close()
    release.join()
    blocker.close()

    assert ledger.stats()["ledger_retries"] >= 1
    assert ledger.stats()["ledger_written"] == 1
    assert not ledger.spool_path.exists()


def test_concurrent_ledger_writers_persist_all_events(tmp_path):
    path = migrated_db(tmp_path)
    ledgers = [
        Ledger(path, connect=lambda p: db.connect(p, timeout_ms=20),
               busy_retries=30, sleep=lambda seconds: time.sleep(0.01))
        for _ in range(4)
    ]
    submitters = []
    for writer, ledger in enumerate(ledgers):
        thread = threading.Thread(
            target=lambda writer=writer, ledger=ledger: [
                ledger.submit(caption(
                    f"edge-room:edge-session:{writer * 20 + seq}",
                    writer * 20 + seq,
                    f"第{writer}-{seq}段，日本語かな🙂",
                    f"Writer {writer}, segment {seq}",
                ))
                for seq in range(1, 21)
            ]
        )
        thread.start()
        submitters.append(thread)
    for thread in submitters:
        thread.join()
    for ledger in ledgers:
        assert ledger.wait_idle(10)
        ledger.close()
        assert ledger.stats()["ledger_errors"] == 0
        assert ledger.stats()["ledger_spooled"] == 0

    conn = db.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 80
        assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 80
        assert conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0] == 80
    finally:
        conn.close()


def test_long_unicode_text_round_trips(tmp_path):
    path = migrated_db(tmp_path)
    zh = ("非常に長い日本語かなと中文🙂😀 " * 12_000) + "終わり"
    en = ("Long text with emoji 🧪 and CJK 日本語 " * 8_000) + "end"
    ledger = Ledger(path)
    ledger.submit(caption("edge-room:edge-session:1", 1, zh, en))
    assert ledger.wait_idle(10)
    ledger.close()

    conn = db.connect(path)
    try:
        assert conn.execute("SELECT text FROM transcripts").fetchone()[0] == zh
        assert conn.execute("SELECT text FROM translations").fetchone()[0] == en
    finally:
        conn.close()


def test_empty_caption_text_does_not_create_text_rows(tmp_path):
    path = migrated_db(tmp_path)
    ledger = Ledger(path)
    ledger.submit(caption("edge-room:edge-session:1", 1))
    assert ledger.wait_idle(5)
    ledger.close()

    conn = db.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0] == 0
    finally:
        conn.close()


def test_duplicate_segment_id_is_idempotent(tmp_path):
    path = migrated_db(tmp_path)
    event = caption(
        "edge-room:edge-session:duplicate", 1, "因緣具足", "Conditions are complete."
    )
    ledger = Ledger(path)
    ledger.submit(event)
    ledger.submit(event.copy())
    assert ledger.wait_idle(5)
    ledger.close()

    conn = db.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM segments WHERE id=?", (event["id"],)).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM transcripts WHERE segment_id=?", (event["id"],)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM translations WHERE segment_id=?", (event["id"],)
        ).fetchone()[0] == 1
    finally:
        conn.close()
