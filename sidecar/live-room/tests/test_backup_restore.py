"""CTO-03 (P0 backup), DB備份 D4/D5/D7/D8, 實測專家 D4/D5: backup set, rotation, restore, corruption."""
import asyncio
import os
import sqlite3
import time

import pytest

from app.admin import db
from app.admin import restore as restore_cli
from app.admin.jobs import JobContext, backup_handler


def fresh(tmp_path, name="zen.sqlite3"):
    p = tmp_path / name
    db.migrate(p)
    return p


def seed(path, n=3):
    c = db.connect(path)
    c.execute("INSERT OR IGNORE INTO rooms(id, title) VALUES ('r1','r')")
    c.execute("INSERT OR IGNORE INTO sessions(id, room_id, started_at) VALUES ('s1','r1',0)")
    base = c.execute("SELECT coalesce(max(seq), -1) + 1 FROM segments").fetchone()[0]
    for i in range(base, base + n):
        c.execute("INSERT INTO segments(id, session_id, room_id, seq, t0_ms, t1_ms) VALUES (?,?,?,?,0,1)",
                  (f"g{i}", "s1", "r1", i))
    c.commit()
    c.close()


def count(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT count(*) FROM segments").fetchone()[0]
    finally:
        c.close()


def test_same_stamp_never_overwrites(tmp_path):
    p = fresh(tmp_path)
    a = db.backup_to(p, tmp_path / "b", stamp="20261010-120000")
    b = db.backup_to(p, tmp_path / "b", stamp="20261010-120000")
    assert a != b and a.exists() and b.exists()


def test_failed_backup_leaves_no_tmp(tmp_path, monkeypatch):
    p = fresh(tmp_path)
    monkeypatch.setattr(db, "_verify_copy", lambda path: (_ for _ in ()).throw(db.SchemaError("bad")))
    with pytest.raises(db.SchemaError):
        db.backup_to(p, tmp_path / "b", stamp="x")
    assert list((tmp_path / "b").iterdir()) == []


def test_backup_set_includes_identity_and_manifest(tmp_path):
    main = fresh(tmp_path)
    ident = tmp_path / "zen-identity.sqlite3"
    db.migrate_identity(ident)
    out = db.backup_set(main, tmp_path / "b", ident, stamp="20261010-010101")
    names = {f.name for f in (tmp_path / "b").iterdir()}
    assert out["files"]["identity"] in names and out["manifest"] in names
    import json
    m = json.loads((tmp_path / "b" / out["manifest"]).read_text(encoding="utf-8"))
    assert len(m["files"]["main"]["sha256"]) == 64


def test_rotation_keeps_daily_and_weekly(tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    for day in range(1, 31):                       # 30 days, two backups each
        for hh in ("01", "13"):
            (d / f"zen-202609{day:02d}-{hh}0000.sqlite3").write_bytes(b"x")
            (d / f"zen-identity-202609{day:02d}-{hh}0000.sqlite3").write_bytes(b"x")
    db.rotate_backups(d, keep_daily=7, keep_weekly=4)
    left = [f.name for f in db.list_backups(d)]
    assert "zen-20260930-130000.sqlite3" in left and "zen-20260930-010000.sqlite3" not in left
    assert len(left) <= 7 + 4 and len(left) >= 7
    assert not (d / "zen-identity-20260901-010000.sqlite3").exists()


def test_backup_job_rotates_and_reports_disk(tmp_path):
    main = fresh(tmp_path)
    run = backup_handler(main, lambda: tmp_path / "b")
    ctx = JobContext.__new__(JobContext)
    ctx.progress = lambda *a, **k: None
    out = run(ctx)
    assert (tmp_path / "b" / out["file"]).exists() and out["same_disk"] is True


def test_restore_roundtrip_moves_old_files_aside(tmp_path):
    main = fresh(tmp_path)
    seed(main, 3)
    bk = db.backup_to(main, tmp_path / "b", stamp="20261010-000000")
    seed_more = db.connect(main)
    seed_more.execute("DELETE FROM segments")
    seed_more.commit()
    seed_more.close()
    out = db.restore_from(bk, main, stamp="T")
    assert count(main) == 3
    assert any(n.endswith("pre-restore-T") for n in out["moved_aside"])
    assert not os.path.exists(str(main) + "-wal") or os.path.getsize(str(main) + "-wal") == 0


def test_restore_refuses_while_db_in_use(tmp_path):
    main = fresh(tmp_path)
    bk = db.backup_to(main, tmp_path / "b")
    holder = db.connect(main)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(db.SchemaError):
            db.restore_from(bk, main)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_restore_rejects_corrupt_backup(tmp_path):
    main = fresh(tmp_path)
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"not a database" * 100)
    with pytest.raises((db.SchemaError, sqlite3.DatabaseError)):
        db.restore_from(bad, main)
    assert count(main) == 0                        # untouched


def test_restore_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("ZEN_DB_PATH", str(tmp_path / "zen.sqlite3"))
    monkeypatch.setenv("ZEN_BACKUP_DIR", str(tmp_path / "b"))
    main = fresh(tmp_path)
    seed(main, 2)
    bk = db.backup_to(main, tmp_path / "b")
    c = db.connect(main)
    c.execute("DELETE FROM segments")
    c.commit()
    c.close()
    assert restore_cli.main([str(bk)]) == 0
    assert count(main) == 2


def test_corrupt_db_refuses_to_start(tmp_path):
    main = fresh(tmp_path)
    seed(main, 50)
    c = sqlite3.connect(main)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    raw = bytearray(main.read_bytes())
    page = 4096
    for off in range(page * 2, min(len(raw), page * 6)):
        raw[off] = 0x5A
    main.write_bytes(bytes(raw))
    with pytest.raises((db.SchemaError, sqlite3.DatabaseError)):
        db.migrate(main)


def test_health_reports_backup_state(tmp_path):
    from fastapi.testclient import TestClient
    from app.admin.server import create_admin_app, API
    from app.admin.security import token_hash
    app = create_admin_app(tmp_path / "zen.sqlite3", token_hash_hex=token_hash("t" * 43), probes={},
                           backup_dir=lambda: tmp_path / "b", start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8791", client=("127.0.0.1", 50000)) as c:
        h = c.get(f"{API}/health", headers={"authorization": "Bearer " + "t" * 43}).json()
    assert "backup" in h, h
    assert h["backup"]["stale"] is True and h["backup"]["last_age_s"] is None
    db.backup_to(tmp_path / "zen.sqlite3", tmp_path / "b")
    with TestClient(app, base_url="http://127.0.0.1:8791", client=("127.0.0.1", 50000)) as c:
        h = c.get(f"{API}/health", headers={"authorization": "Bearer " + "t" * 43}).json()
    assert h["backup"]["stale"] is False and "same_disk" in h["backup"]


def test_scheduled_backup_enqueues_when_due(tmp_path):
    from app.admin.server import create_admin_app
    from app.admin.security import token_hash
    app = create_admin_app(tmp_path / "zen.sqlite3", token_hash_hex=token_hash("t" * 43), probes={},
                           backup_dir=lambda: tmp_path / "b", start_worker=False, backup_scan_s=0.05)

    async def go():
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.2)
    asyncio.run(go())
    c = db.connect(tmp_path / "zen.sqlite3")
    n = c.execute("SELECT count(*) FROM jobs WHERE kind='backup'").fetchone()[0]
    c.close()
    assert n == 1                                  # deduped while queued
