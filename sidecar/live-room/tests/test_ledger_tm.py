"""Ledger (non-blocking SQLite writer), schema/search, translation memory, feedback."""
import sqlite3
import time

import pytest

from app import feedback, tm
from app.admin import db, search
from app.ledger import Ledger, ledger_from_env, segment_status
from app.translate import TranslateResult
from tests.local_fakes import migrated_db, seed_segment


def cap(seq=1, zh="因緣具足", en="", status="zh_ready", **kw):
    ev = {"type": "caption", "id": f"class:s1:{seq}", "room_id": "class", "session_id": "s1", "seq": seq,
          "zh": zh, "en": en, "status": status, "version": 1, "t0_ms": seq * 6000, "t1_ms": seq * 6000 + 6000}
    ev.update(kw)
    return ev


# ------------------------------------------------------------------ db / schema
def test_migrate_is_idempotent_and_v2(tmp_path):
    path = migrated_db(tmp_path)
    assert db.migrate(path) == 2
    c = db.connect(path)
    assert c.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert all(v == "ok" for v in db.fts_integrity(c).values())
    c.close()


def test_identity_db_is_separate(tmp_path):
    main = migrated_db(tmp_path)
    ident = tmp_path / "zen-identity.sqlite3"
    db.migrate_identity(ident)
    c = db.connect(main)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not names & {"voiceprints", "speaker_identities", "api_tokens", "user_accounts"}
    c.close()
    c = db.connect(ident)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"voiceprints", "api_tokens"} <= names
    c.close()


def test_paths_env_override(tmp_path):
    env = {"ZEN_DATA_DIR": str(tmp_path / "d")}
    assert db.default_db_path(env).name == "zen.sqlite3"
    assert db.identity_db_path(env).name == "zen-identity.sqlite3"
    assert db.default_db_path({"ZEN_DB_PATH": str(tmp_path / "x.sqlite3")}) == tmp_path / "x.sqlite3"
    assert db.backup_dir({"ZEN_BACKUP_DIR": str(tmp_path / "bk")}) == tmp_path / "bk"


def test_windows_default_under_localappdata():
    env = {"LOCALAPPDATA": "/fake/AppData/Local"}
    assert db.default_db_path(env).as_posix() == "/fake/AppData/Local/ZenBridge/data/zen.sqlite3"
    assert db.identity_db_path(env).as_posix() == "/fake/AppData/Local/ZenBridge/data/zen-identity.sqlite3"


def test_unc_and_cloud_paths_refused():
    with pytest.raises(Exception):
        db.check_db_location(r"\\server\share\zen.sqlite3")
    with pytest.raises(Exception):
        db.check_db_location(r"C:\Users\u\OneDrive\zen.sqlite3")


def test_backup_to_separate_dir(tmp_path):
    path = migrated_db(tmp_path)
    out = db.backup_to(path, tmp_path / "backups", stamp="20261010")
    assert out.exists() and out.parent == tmp_path / "backups"
    c = sqlite3.connect(out)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.close()


def test_search_routes_by_length(tmp_path):
    path = migrated_db(tmp_path)
    c = db.connect(path)
    seed_segment(c, seg="a", seq=1, zh="今天講因緣具足的道理", en="Today on conditions")
    seed_segment(c, seg="b", seq=2, zh="佛法在世間", en="Dharma in the world")
    assert search.route("因緣具") == "trigram" and search.route("因緣") == "unigram"
    rows, mode = search.search_transcripts(c, "因緣具足")
    assert mode == "trigram" and [r["segment_id"] for r in rows] == ["a"]
    rows, mode = search.search_transcripts(c, "因緣")
    assert mode.startswith("unigram") and [r["segment_id"] for r in rows] == ["a"]
    rows, _ = search.search_transcripts(c, "佛")
    assert [r["segment_id"] for r in rows] == ["b"]
    rows, _ = search.search_transcripts(c, "緣因")             # 2 chars must be adjacent, in order
    assert rows == []
    rows, _ = search.search_translations(c, "condition")       # porter stemming
    assert [r["segment_id"] for r in rows] == ["a"]
    c.close()


def test_upsert_keeps_fts_exact_on_text_change(tmp_path):
    path = migrated_db(tmp_path)
    c = db.connect(path)
    seed_segment(c, seg="a", zh="舊的文字內容")
    c.execute("UPDATE transcripts SET text='新的文字內容', text_uni=? WHERE segment_id='a'", (db.to_uni("新的文字內容"),))
    assert search.search_transcripts(c, "舊的文字")[0] == []
    assert len(search.search_transcripts(c, "新的文字")[0]) == 1
    assert all(v == "ok" for v in db.fts_integrity(c).values())
    c.close()


# ------------------------------------------------------------------ ledger
def test_ledger_writes_versions_and_is_idempotent(tmp_path):
    path = tmp_path / "zen.sqlite3"
    led = Ledger(path)
    led.submit(cap(status="zh_ready"))
    led.submit(cap(status="ready", en="Conditions are complete.", en_origin="mt", speech_ratio=0.8))
    led.submit(cap(status="ready", en="Conditions are complete.", en_origin="mt"))   # replay
    led.submit({"type": "ping"})
    assert led.wait_idle(5)
    led.close()
    c = db.connect(path)
    assert tuple(c.execute("SELECT status, speech_ratio FROM segments").fetchone()) == ("translated", 0.8)
    assert c.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM translations").fetchone()[0] == 1
    assert c.execute("SELECT text_uni FROM transcripts").fetchone()[0] == db.to_uni("因緣具足")
    assert search.search_transcripts(c, "因緣")[0]
    c.close()
    assert led.stats()["ledger_errors"] == 0


def test_ledger_failure_never_raises(tmp_path):
    def broken(path):
        raise sqlite3.OperationalError("disk I/O error")
    led = Ledger(tmp_path / "x.sqlite3", connect=broken)
    led.submit(cap())
    led.wait_idle(2)
    led.close()
    assert led.stats()["ledger_errors"] >= 1


def test_ledger_full_queue_drops_without_blocking(tmp_path):
    led = Ledger(tmp_path / "x.sqlite3", maxsize=1)
    led._ensure_thread = lambda: None        # no consumer
    t0 = time.monotonic()
    for i in range(50):
        led.submit(cap(seq=i + 1))
    assert time.monotonic() - t0 < 0.5
    assert led.stats()["ledger_dropped"] == 49


def test_ledger_env_switch(tmp_path):
    assert ledger_from_env({"ZEN_LEDGER": "0"}).enabled is False
    assert ledger_from_env({}).enabled is False                     # opt-in
    led = ledger_from_env({"ZEN_LEDGER": "1", "ZEN_DB_PATH": str(tmp_path / "z.sqlite3")})
    assert led.enabled and led.path == tmp_path / "z.sqlite3"


def test_segment_status_mapping():
    assert segment_status({"status": "silent"}) == "silent"
    assert segment_status({"status": "ready", "en": "x"}) == "translated"
    assert segment_status({"status": "ready"}) == "asr_done"
    assert segment_status({"status": "timeout"}) == "timeout"


def test_ledger_hook_does_not_break_captions(tmp_path, monkeypatch):
    """server.on_event -> ledger.submit raising must not stop captions."""
    from app import server
    calls = []

    class Boom:
        enabled = True

        def submit(self, ev):
            calls.append(ev)
            raise RuntimeError("ledger down")

        def stats(self):
            return {}

        def close(self):
            pass
    monkeypatch.setattr(server, "ledger_from_env", lambda *a, **k: Boom())
    from tests.test_pipeline_repair import app_for
    from fastapi.testclient import TestClient
    app = app_for()
    with TestClient(app, base_url="http://127.0.0.1:8780") as client:
        tok = client.get("/api/host-token").json()["token"]
        r = client.post("/api/push", data={"room_id": "class", "session_id": "s", "seq": "1"},
                        files={"audio": ("a.bin", "你好".encode(), "application/octet-stream")},
                        headers={"authorization": f"Bearer {tok}", "origin": "http://127.0.0.1"})
        assert r.status_code == 200, r.text
        assert r.json()["zh"] == "你好"
    assert calls


# ------------------------------------------------------------------ translation memory
class Inner:
    enabled = True

    def __init__(self, status="ok"):
        self.calls = []
        self.status = status

    def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None, examples=None):
        self.calls.append({"zh": zh, "examples": examples})
        return TranslateResult("MT " + zh if self.status == "ok" else "", self.status)


def _tm(tmp_path, units):
    path = migrated_db(tmp_path)
    c = db.connect(path)
    c.execute("BEGIN IMMEDIATE")
    for zh, en in units:
        tm.add_unit(c, zh, en, quality=4)
    c.execute("COMMIT")
    c.close()
    return path, tm.TranslationMemory(lambda: db.connect(path, readonly=True))


def test_norm_folds_width_punct_and_simplified():
    assert tm.norm("因缘，具足！") == tm.norm("因緣 具足")
    assert tm.src_hash("因缘具足") == tm.src_hash("因緣具足。")


def test_exact_hit_bypasses_model(tmp_path):
    _, mem = _tm(tmp_path, [("因緣具足", "Causes and conditions are complete.")])
    inner = Inner()
    hits = []
    mt = tm.MemoryTranslator(inner, mem, on_hit=hits.append)
    res = mt.translate("因缘具足。")
    assert res.text == "Causes and conditions are complete." and res.origin == "tm_exact"
    assert inner.calls == [] and len(hits) == 1


def test_locked_term_violation_skips_exact_hit(tmp_path):
    _, mem = _tm(tmp_path, [("禪修很重要", "Meditation is important.")])
    inner = Inner()
    mt = tm.MemoryTranslator(inner, mem)
    glossary = [{"zh": "禪修", "en": "Chan practice", "locked": True}]
    res = mt.translate("禪修很重要", glossary=glossary)
    assert res.origin == "mt" and inner.calls
    assert mt.tm_locked_rejects == 1


def test_fuzzy_examples_are_passed_as_few_shot(tmp_path):
    _, mem = _tm(tmp_path, [("今天我們來講因緣具足的道理和方法", "Today we talk about complete conditions.")])
    inner = Inner()
    mt = tm.MemoryTranslator(inner, mem)
    mt.translate("今天我們來講因緣具足的道理與方法")
    ex = inner.calls[0]["examples"]
    assert ex and ex[0]["en"] == "Today we talk about complete conditions."


def test_tm_hit_is_fallback_when_model_fails(tmp_path):
    glossary = [{"zh": "禪修", "en": "Chan practice", "locked": True}]
    _, mem = _tm(tmp_path, [("禪修很重要", "Meditation is important.")])
    mt = tm.MemoryTranslator(Inner(status="network"), mem)
    res = mt.translate("禪修很重要", glossary=glossary)
    assert res.status == "ok" and res.origin == "tm_exact"


def test_tm_db_missing_falls_through(tmp_path):
    mt = tm.tm_from_env(Inner(), tmp_path / "missing.sqlite3", env={"BREEZE_TM": "1"})
    assert mt.translate("你好").text == "MT 你好"
    assert mt.tm_errors >= 1


def test_tm_off_returns_inner(tmp_path):
    inner = Inner()
    assert tm.tm_from_env(inner, tmp_path / "x", env={"BREEZE_TM": "0"}) is inner
    assert tm.tm_from_env(inner, tmp_path / "x", env={}) is inner     # opt-in


# ------------------------------------------------------------------ feedback
def test_correction_feeds_tm_and_proposes_term(tmp_path):
    path = migrated_db(tmp_path)
    c = db.connect(path)
    seed_segment(c, seg="a", zh="因緣具足", en="Fate is enough")
    c.execute("BEGIN IMMEDIATE")
    rec = feedback.record_correction(c, segment_id="a", target_type="translation", text="Conditions are complete.")
    out = feedback.promote_correction(c, rec["correction_id"], propose={"zh": "因緣", "en": "conditions"})
    again = feedback.promote_correction(c, rec["correction_id"], propose={"zh": "因緣", "en": "conditions"})
    c.execute("COMMIT")
    assert out["tm_id"] and out["term_id"] and again == out
    mem = tm.TranslationMemory(lambda: db.connect(path, readonly=True))
    assert mem.exact("因緣具足").tgt == "Conditions are complete."
    assert c.execute("SELECT origin FROM translations WHERE is_current=1").fetchone()[0] == "human"
    c.close()


def test_correction_validation(tmp_path):
    path = migrated_db(tmp_path)
    c = db.connect(path)
    with pytest.raises(feedback.FeedbackError):
        feedback.record_correction(c, segment_id="nope", target_type="translation", text="x")
    with pytest.raises(feedback.FeedbackError):
        feedback.record_correction(c, segment_id="nope", target_type="bogus", text="x")
    c.close()
