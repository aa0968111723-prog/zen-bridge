"""round3 C3 (stepwise migration runner + pre-migration snapshot + rollback covers the DB)
and round2 T7 (per-target-language glossary, schema v3)."""
import json
import sqlite3
from pathlib import Path

import pytest

from app.admin import db
from app.admin.glossary_targets import terms_for


def v2_db(path: Path) -> Path:
    """A DB exactly as the previous release left it: baseline schema.sql, user_version 2."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = db.connect(path)
    try:
        db._apply_schema(c, db.SCHEMA_PATH, db.BASELINE_VERSION, "資料庫", record_checksum=True)
        c.execute("INSERT INTO glossaries(name, scope) VALUES ('全域', 'global')")
        g_en = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO glossary_terms(glossary_id, zh, en, locked) VALUES (?, '空性', 'emptiness', 1)", (g_en,))
        c.execute("INSERT INTO glossary_terms(glossary_id, zh, en) VALUES (?, '提案', '')", (g_en,))   # no target text
        # admin-info's ja glossary (created lazily by info.ensure_ext on v2)
        from app.admin.info import ensure_ext
        ensure_ext(c)
        c.execute("INSERT INTO glossaries(name, scope) VALUES ('日文詞表', 'global')")
        g_ja = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO admin_glossary_lang(glossary_id, tgt_lang) VALUES (?, 'ja')", (g_ja,))
        c.execute("INSERT INTO glossary_terms(glossary_id, zh, en) VALUES (?, '公案', '公案')", (g_ja,))
        tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO admin_term_meta(term_id, reading) VALUES (?, 'こうあん')", (tid,))
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        c.close()
    return path


def test_v2_upgrades_to_v3_with_snapshot_and_backfill(tmp_path):
    p = v2_db(tmp_path / "zen.sqlite3")
    assert db.migrate(p) == db.SCHEMA_VERSION == 11      # 0003 + backend staging 0010, 0011
    snap = tmp_path / "pre-migrate-v2.sqlite3"
    assert snap.is_file()
    with sqlite3.connect(snap) as s:
        assert s.execute("PRAGMA user_version").fetchone()[0] == 2
        assert s.execute("SELECT count(*) FROM glossary_terms").fetchone()[0] == 3
    c = db.connect(p)
    try:
        en = terms_for(c, "en")
        ja = terms_for(c, "ja")
        assert [(t["zh"], t["tgt"], t["locked"]) for t in en] == [("空性", "emptiness", 1)]
        assert [(t["zh"], t["tgt"], t["reading"]) for t in ja] == [("公案", "公案", "こうあん")]
        row = c.execute("SELECT name, checksum FROM schema_migrations WHERE version=3").fetchone()
        assert row["name"] == "glossary_targets" and len(row["checksum"]) == 64
    finally:
        c.close()


def test_running_twice_is_identical_and_no_second_snapshot(tmp_path):
    p = v2_db(tmp_path / "zen.sqlite3")
    db.migrate(p)
    c = db.connect(p)
    before = c.execute("SELECT * FROM glossary_term_targets ORDER BY id").fetchall()
    c.close()
    assert db.migrate(p) == db.SCHEMA_VERSION
    c = db.connect(p)
    assert c.execute("SELECT * FROM glossary_term_targets ORDER BY id").fetchall() == before
    c.close()
    assert sorted(x.name for x in tmp_path.glob("pre-migrate-*")) == ["pre-migrate-v2.sqlite3"]


def test_fresh_db_goes_straight_to_v3_without_snapshot(tmp_path):
    p = tmp_path / "new" / "zen.sqlite3"
    assert db.migrate(p) == db.SCHEMA_VERSION
    assert not list(p.parent.glob("pre-migrate-*"))


def test_triggers_keep_targets_in_sync_and_ja_session_sees_ja(tmp_path):
    p = tmp_path / "zen.sqlite3"
    db.migrate(p)
    c = db.connect(p)
    try:
        c.execute("INSERT INTO glossaries(name, scope) VALUES ('g', 'global')")
        g = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO glossary_terms(glossary_id, zh, en) VALUES (?, '菩提心', 'bodhicitta')", (g,))
        assert [t["tgt"] for t in terms_for(c, "en")] == ["bodhicitta"]
        c.execute("UPDATE glossary_terms SET en='bodhi-mind', locked=1 WHERE zh='菩提心'")
        assert [(t["tgt"], t["locked"]) for t in terms_for(c, "en")] == [("bodhi-mind", 1)]
        assert terms_for(c, "ja") == []                       # T7: ja never gets the English list
        c.execute("UPDATE glossary_terms SET status='retired' WHERE zh='菩提心'")
        assert terms_for(c, "en") == []
        c.execute("DELETE FROM glossary_terms WHERE zh='菩提心'")
        assert c.execute("SELECT count(*) FROM glossary_term_targets").fetchone()[0] == 0
    finally:
        c.close()


def test_view_follows_session_target_language(tmp_path):
    p = tmp_path / "zen.sqlite3"
    db.migrate(p)
    c = db.connect(p)
    try:
        c.execute("INSERT INTO rooms(id, title) VALUES ('r', 'r')")
        c.execute("INSERT INTO sessions(id, room_id, started_at, tgt_lang) VALUES ('s', 'r', 0, 'ja')")
        c.execute("INSERT INTO segments(id, session_id, room_id, seq, t0_ms, t1_ms) VALUES ('r:s:1', 's', 'r', 1, 0, 1)")
        c.execute("INSERT INTO translations(segment_id, tgt_lang, text) VALUES ('r:s:1', 'ja', '今日は')")
        row = c.execute("SELECT tgt_lang, tgt, en FROM v_segment_current WHERE segment_id='r:s:1'").fetchone()
        assert tuple(row) == ("ja", "今日は", None)
    finally:
        c.close()


def test_failed_step_rolls_back_to_v2(tmp_path):
    p = v2_db(tmp_path / "zen.sqlite3")
    bad = tmp_path / "mig"
    bad.mkdir()
    (bad / "0003_broken.sql").write_text("CREATE TABLE x_ok(a);\nINSERT INTO no_such_table VALUES (1);\n", encoding="utf-8")
    c = db.connect(p)
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.run_migrations(c, directory=bad, target=3)
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='x_ok'").fetchone()
    finally:
        c.close()
    assert (tmp_path / "pre-migrate-v2.sqlite3").is_file()      # snapshot taken before the step


def test_gap_in_step_numbers_and_edited_step_refused(tmp_path):
    gap = tmp_path / "gap"
    gap.mkdir()
    (gap / "0004_late.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(db.SchemaError):
        db.migration_steps(gap)
    p = tmp_path / "zen.sqlite3"
    db.migrate(p)
    c = db.connect(p)
    c.execute("UPDATE schema_migrations SET checksum='0'*64 WHERE version=3".replace("'0'*64", "'" + "0" * 64 + "'"))
    c.close()
    with pytest.raises(db.SchemaError, match="0003_glossary_targets.sql"):
        db.migrate(p)


def test_newer_db_is_a_clear_error_pointing_at_the_snapshot(tmp_path):
    p = tmp_path / "zen.sqlite3"
    db.migrate(p)
    c = db.connect(p)
    c.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    c.close()
    with pytest.raises(db.SchemaError, match="pre-migrate"):
        db.migrate(p)


def test_backup_manifest_records_file_schema_version(tmp_path):
    p = v2_db(tmp_path / "zen.sqlite3")
    out = db.backup_set(p, tmp_path / "b")
    assert json.loads((tmp_path / "b" / out["manifest"]).read_text())["schema_version"] == 2


# ---------------------------------------------------------------- scripts/update.py rollback
def test_update_snapshot_and_rollback_detects_newer_db(tmp_path, monkeypatch, capsys):
    from scripts import update
    data = tmp_path / "data"
    p = v2_db(data / "zen.sqlite3")
    monkeypatch.setenv("ZEN_DB_PATH", str(p))
    monkeypatch.setenv("ZEN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(update, "fill_process_environ", lambda path: None)
    backup = tmp_path / "backup-x"
    backup.mkdir()
    rec = update.snapshot_db(tmp_path, backup)
    assert rec["schema_version"] == 2 and (backup / "db" / rec["file"]).is_file()
    assert update.db_rollback_check(tmp_path, backup) == "compatible"     # not migrated yet
    db.migrate(p)                                                        # new software started
    c = db.connect(p)
    c.execute("INSERT INTO glossaries(name, scope) VALUES ('after-update', 'global')")
    c.close()
    assert update.db_rollback_check(tmp_path, backup) == "newer"         # prompt, no overwrite
    out = capsys.readouterr().out
    assert f"v{db.SCHEMA_VERSION}" in out and "-RestoreDb" in out
    assert update.db_version(p) == db.SCHEMA_VERSION
    assert update.db_rollback_check(tmp_path, backup, restore_db=True) == "restored"
    assert update.db_version(p) == 2
    aside = list(data.glob("zen.sqlite3.pre-restore-*"))
    assert aside and update.db_version(aside[0]) == db.SCHEMA_VERSION                    # post-update data kept


def test_update_snapshot_without_db_is_noop(tmp_path, monkeypatch):
    from scripts import update
    monkeypatch.setenv("ZEN_DB_PATH", str(tmp_path / "none.sqlite3"))
    monkeypatch.setattr(update, "fill_process_environ", lambda path: None)
    (tmp_path / "b").mkdir()
    assert update.snapshot_db(tmp_path, tmp_path / "b") is None
    assert update.db_rollback_check(tmp_path, tmp_path / "b") == "no-snapshot"
