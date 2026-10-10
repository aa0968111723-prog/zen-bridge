from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from app.admin import db
from app.store import CaptionStore
from tools.glossary_term_stats import _open_readonly, collect_stats, main
from tests.local_fakes import seed_segment


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "ledger # 術語.sqlite3"
    db.migrate(path)
    with closing(db.connect(path)) as conn:
        seed_segment(conn, seg="en-1", session="en-session", zh="禪修禪修與公案", en="ZEN meditation")
        seed_segment(conn, seg="en-2", session="en-session", seq=2, zh="禪修", en="practice")
        seed_segment(conn, seg="en-3", session="other-session", zh="禪修", en=None)
        seed_segment(conn, seg="ja-1", session="ja-session", zh="禪修與公案")
        seed_segment(conn, seg="ja-2", session="ja-session", seq=2, zh="禪修")
        conn.execute("UPDATE sessions SET tgt_lang='ja' WHERE id='ja-session'")
        conn.execute(
            "INSERT INTO translations(segment_id,tgt_lang,text) VALUES ('ja-1','ja','禅修と公案について')"
        )
        conn.execute("INSERT INTO glossaries(id,name) VALUES (1,'club')")
        conn.executemany(
            "INSERT INTO glossary_terms(id,glossary_id,zh,en) VALUES (?,1,?,?)",
            [(1, "禪修", "Zen meditation"), (2, "公案", "koan"), (3, "坐禪", "zazen")],
        )
        conn.executemany(
            "INSERT INTO glossary_term_targets(term_id,tgt_lang,text) VALUES (?,'ja',?)",
            [(1, "禅修"), (2, "公案")],
        )
    return path


def by_zh(report):
    return {term["zh"]: term for term in report["terms"]}


def test_hits_omissions_and_top(ledger):
    report = collect_stats(ledger)
    terms = by_zh(report)
    assert terms["禪修"] == {
        "zh": "禪修", "target": "Zen meditation", "source_segments": 3,
        "hits": 1, "omissions": 2, "hit_rate": 1 / 3,
    }
    assert terms["公案"]["omissions"] == 1
    assert terms["坐禪"]["hit_rate"] is None
    assert report["overall"] == {"source_segments": 4, "hits": 1, "omissions": 3, "hit_rate": 0.25}
    limited = collect_stats(ledger, top=1)
    assert limited["terms"] == [terms["禪修"]]
    assert limited["overall"] == report["overall"]


def test_japanese_kanji_are_valid_targets(ledger):
    report = collect_stats(ledger, lang="ja")
    assert report["overall"] == {"source_segments": 3, "hits": 2, "omissions": 1, "hit_rate": 2 / 3}
    assert by_zh(report)["公案"]["hits"] == 1
    assert by_zh(report)["禪修"]["source_segments"] == 2


def test_session_filter_is_parameterized(ledger):
    report = collect_stats(ledger, session="en-session")
    assert report["overall"]["source_segments"] == 3
    assert by_zh(report)["禪修"]["omissions"] == 1
    assert collect_stats(ledger, session="' OR 1=1 --")["overall"]["source_segments"] == 0


def test_only_current_versions_and_zh_sources(ledger):
    with closing(db.connect(ledger)) as conn:
        conn.execute("UPDATE translations SET is_current=0 WHERE segment_id='en-1'")
        conn.execute(
            "INSERT INTO translations(segment_id,version,text) VALUES ('en-1',2,'practice')"
        )
        conn.execute(
            "INSERT INTO transcripts(segment_id,version,is_current,text) VALUES ('en-1',2,0,'坐禪')"
        )
        seed_segment(conn, seg="deleted", session="en-session", seq=4, zh="禪修", en="Zen meditation")
        conn.execute("UPDATE segments SET status='deleted' WHERE id='deleted'")
        seed_segment(conn, seg="not-zh", session="en-session", seq=5, zh="禪修", en="Zen meditation")
        conn.execute("UPDATE transcripts SET lang='ja' WHERE segment_id='not-zh'")
    report = collect_stats(ledger)
    assert report["overall"]["hits"] == 0
    assert report["overall"]["source_segments"] == 4
    assert by_zh(report)["坐禪"]["source_segments"] == 0


def test_room_and_session_glossary_scopes(ledger):
    with closing(db.connect(ledger)) as conn:
        conn.execute(
            "INSERT INTO glossaries(id,name,scope,session_id) VALUES (2,'session','session','en-session')"
        )
        conn.execute("INSERT INTO glossary_terms(glossary_id,zh,en) VALUES (2,'禪修','practice')")
        conn.execute("INSERT INTO rooms(id) VALUES ('elsewhere')")
        conn.execute(
            "INSERT INTO glossaries(id,name,scope,room_id) VALUES (3,'room','room','elsewhere')"
        )
        conn.execute("INSERT INTO glossary_terms(glossary_id,zh,en) VALUES (3,'公案','case')")
    terms = collect_stats(ledger)["terms"]
    assert next(t for t in terms if t["target"] == "practice")["source_segments"] == 2
    assert next(t for t in terms if t["target"] == "case")["source_segments"] == 0


def test_translation_of_old_transcript_is_an_omission(ledger):
    with closing(db.connect(ledger)) as conn:
        conn.execute(
            """UPDATE translations SET transcript_id=(
                   SELECT id FROM transcripts WHERE segment_id='en-1' AND version=1)
               WHERE segment_id='en-1'"""
        )
        conn.execute("UPDATE transcripts SET is_current=0 WHERE segment_id='en-1'")
        conn.execute("INSERT INTO transcripts(segment_id,version,text) VALUES ('en-1',2,'禪修')")
    assert collect_stats(ledger)["overall"]["hits"] == 0
    assert by_zh(collect_stats(ledger))["禪修"]["source_segments"] == 3


def test_legacy_glossary_language_metadata(ledger):
    with closing(db.connect(ledger)) as conn:
        conn.execute("INSERT INTO glossaries(id,name) VALUES (2,'Japanese')")
        conn.execute("INSERT INTO admin_glossary_lang VALUES (2,'ja')")
        conn.execute("INSERT INTO glossary_terms(glossary_id,zh,en) VALUES (2,'禪修','禅修')")
        conn.execute("DROP TABLE glossary_term_targets")
    assert collect_stats(ledger, lang="ja")["overall"]["hits"] == 1
    assert all(term["target"] != "禅修" for term in collect_stats(ledger)["terms"])


@pytest.fixture
def captions(tmp_path):
    path = tmp_path / "captions.sqlite3"
    store = CaptionStore(path)
    store.save({
        "id": "c1", "room_id": "class", "session_id": "s1", "seq": 1,
        "version": 1, "zh": "禪修禪修", "en": "Zen meditation", "status": "ready",
    })
    store.save({
        "id": "c2", "room_id": "other", "session_id": "s2", "seq": 1,
        "version": 1, "zh": "禪修", "en": "practice", "status": "ready",
    })
    store.close()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO room_glossary VALUES (?,?,?,?)",
            ("class", 1, json.dumps([{"zh": "禪修", "en": "Zen meditation"}]), 1.0),
        )
        conn.commit()
    return path


def test_caption_schema_and_default_room_glossary(captions):
    assert collect_stats(captions)["overall"]["hit_rate"] == 1
    assert collect_stats(captions, session="s2")["overall"]["source_segments"] == 0
    with pytest.raises(ValueError, match="only an en column"):
        collect_stats(captions, lang="ja")


@pytest.mark.parametrize("suffix", [".json", ".csv"])
def test_exported_glossary(captions, tmp_path, suffix):
    path = tmp_path / ("glossary" + suffix)
    content = ('{"terms":[{"zh":"禪修","en":"Zen meditation","ja":"禅修"}]}' if suffix == ".json"
               else "zh,en,ja\n禪修,Zen meditation,禅修\n")
    path.write_text(content, encoding="utf-8-sig")
    before = path.read_bytes()
    assert collect_stats(captions, path)["overall"]["source_segments"] == 2
    assert collect_stats(captions, path)["overall"]["hit_rate"] == 0.5
    assert path.read_bytes() == before


def test_separate_sqlite_glossary(captions, ledger):
    before = ledger.read_bytes()
    assert collect_stats(captions, ledger)["overall"]["source_segments"] == 2
    assert ledger.read_bytes() == before


def test_empty_database(tmp_path):
    path = tmp_path / "empty.sqlite3"
    db.migrate(path)
    report = collect_stats(path)
    assert report["terms"] == []
    assert report["overall"] == {"source_segments": 0, "hits": 0, "omissions": 0, "hit_rate": None}


def test_missing_glossary_and_db(ledger, tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        collect_stats(ledger, missing)
    with pytest.raises(sqlite3.OperationalError):
        collect_stats(missing)
    assert not missing.exists()
    with closing(sqlite3.connect(missing)) as conn:
        conn.execute("CREATE TABLE unrelated(id)")
    with pytest.raises(ValueError, match="glossary tables not found"):
        collect_stats(missing)


@pytest.mark.parametrize("raw", ['{"terms":"bad"}', '[{"zh":"","en":"bad"}]', '[{"zh":"禪修","en":7}]'])
def test_malformed_glossary(ledger, tmp_path, raw):
    path = tmp_path / "bad.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        collect_stats(ledger, path)


@pytest.mark.parametrize("options", [{"top": 0}, {"top": -1}, {"lang": "fr"}])
def test_invalid_options(ledger, options):
    with pytest.raises(ValueError):
        collect_stats(ledger, **options)


def test_readonly_connection_and_unchanged_inputs(ledger):
    before = ledger.read_bytes()
    modified = ledger.stat().st_mtime_ns
    with closing(_open_readonly(ledger)) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM glossary_terms")
    collect_stats(ledger)
    collect_stats(ledger, lang="ja")
    assert ledger.read_bytes() == before
    assert ledger.stat().st_mtime_ns == modified


def test_committed_wal_captions_are_included(captions):
    with closing(sqlite3.connect(captions)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            """INSERT INTO captions(id,room_id,session_id,seq,version,zh,en,updated_at)
               VALUES ('wal','class','s1',2,1,'禪修','Zen meditation',1)"""
        )
        writer.commit()
        before = captions.read_bytes()
        wal_path = Path(str(captions) + "-wal")
        wal_before = wal_path.read_bytes()
        assert collect_stats(captions)["overall"]["hits"] == 2
        assert captions.read_bytes() == before
        assert wal_path.read_bytes() == wal_before


def test_cli_json_and_text_and_readonly_run(ledger):
    script = Path(__file__).resolve().parents[1] / "tools" / "glossary_term_stats.py"
    before = ledger.read_bytes()
    for output_format in ("json", "text"):
        result = subprocess.run(
            [sys.executable, str(script), "--db", str(ledger), "--lang", "ja",
             "--session", "ja-session", "--format", output_format, "--top", "1"],
            capture_output=True, encoding="utf-8", check=True,
        )
        if output_format == "json":
            report = json.loads(result.stdout)
            assert report["overall"]["hits"] == 2
            assert len(report["terms"]) == 1
        else:
            assert "2/3 hits (66.7%)" in result.stdout
            assert "禪修\t禅修\t2\t1\t1\t50.0%" in result.stdout
    assert ledger.read_bytes() == before


def test_cli_missing_glossary_error(ledger, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(ledger), "--glossary", str(tmp_path / "missing.json")])
    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err
