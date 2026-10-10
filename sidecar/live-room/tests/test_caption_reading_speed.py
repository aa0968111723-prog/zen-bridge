import ast
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import pytest

from tools import caption_reading_speed as speed


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "caption_reading_speed.py"


def _snapshot(path):
    return path.read_bytes(), path.stat().st_mtime_ns


def _caption_db(path, rows):
    tree = ast.parse((ROOT / "app" / "store.py").read_text(encoding="utf-8"))
    prepare = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "_prepare")
    ddl = next(node.value for node in ast.walk(prepare)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)
               and "create table if not exists captions (" in node.value)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(ddl)
        conn.executemany(
            """INSERT INTO captions
               (id, room_id, session_id, seq, version, en, status, t0_ms, t1_ms, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, 0)""",
            rows,
        )
        conn.commit()
    return path


def _ledger_db(path):
    schema = (ROOT / "app" / "admin" / "schema.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(path)) as conn:
        for table in ("rooms", "sessions", "segments", "translations"):
            ddl = re.search(r"CREATE TABLE IF NOT EXISTS " + table + r" \([\s\S]*?\n\);", schema)
            assert ddl is not None
            conn.execute(ddl.group())
        conn.execute("INSERT INTO rooms(id, created_at) VALUES ('r', 0)")
        conn.executemany(
            "INSERT INTO sessions(id, room_id, started_at, tgt_lang) VALUES (?, 'r', 0, ?)",
            [("english", "en"), ("japanese", "ja")],
        )
        conn.executemany(
            """INSERT INTO segments
               (id, session_id, room_id, seq, t0_ms, t1_ms, status, created_at)
               VALUES (?, ?, 'r', ?, ?, ?, ?, 0)""",
            [("e1", "english", 1, 0, 1000, "translated"),
             ("e2", "english", 2, 1000, 2000, "translated"),
             ("j1", "japanese", 1, 0, 1000, "translated"),
             ("j2", "japanese", 2, 1000, 2000, "translated"),
             ("deleted", "japanese", 3, 2000, 3000, "deleted"),
             ("error", "japanese", 4, 3000, 4000, "error")],
        )
        conn.executemany(
            """INSERT INTO translations
               (segment_id, tgt_lang, version, is_current, text, created_at, status)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            [("e1", "en", 1, 0, "old " * 30, "ok"),
             ("e1", "en", 2, 1, " \t" + "a" * 17 + "\n", "ok"),
             ("e2", "en", 1, 1, "b" * 18, "merged"),
             ("j1", "ja", 1, 1, "猫" * 7 + "AB", "ok"),
             ("j2", "ja", 1, 1, "猫" * 8 + "A", "ok"),
             ("j1", "en", 1, 1, "not the session target " * 20, "ok"),
             ("deleted", "ja", 1, 1, "猫" * 20, "ok"),
             ("error", "ja", 1, 1, "猫" * 20, "ok")],
        )
        conn.commit()
    return path


def test_thresholds_final_translations_and_percentiles(tmp_path):
    report = speed.analyze(_ledger_db(tmp_path / "captions.sqlite3"))
    en, ja = report["by_language"]["en"], report["by_language"]["ja"]
    assert en == {"count": 2, "p50_cps": 17.5, "p90_cps": 17.9,
                  "max_cps": 18, "threshold_cps": 17, "flagged_count": 1}
    assert ja == {"count": 2, "p50_cps": 8.25, "p90_cps": 8.45,
                  "max_cps": 8.5, "threshold_cps": 8, "flagged_count": 1}
    assert report["flagged_count"] == 2
    assert report["flagged"] == [
        {"id": "e2", "session": "english", "lang": "en", "cps": 18, "text": "b" * 18},
        {"id": "j2", "session": "japanese", "lang": "ja", "cps": 8.5, "text": "猫" * 8 + "A"},
    ]
    overridden = speed.analyze(tmp_path / "captions.sqlite3", en_cps=18, ja_cps=8.5)
    assert overridden["flagged_count"] == 0


@pytest.mark.parametrize("text,count", [
    ("猫かなカナＡ１！", 8), ("ABC 123", 3.5), (" ｶﾅ AB ", 2.5),
    ("𠮷漢字", 3), ("猫A", 1.5), (" \n\t ", 0),
])
def test_japanese_full_width_rule(text, count):
    assert speed.character_count(text, "ja") == count


def test_english_strips_only_outer_whitespace():
    assert speed.character_count(" \tHello world!\n", "en") == 12


def test_missing_end_and_invalid_spans_use_next_start_or_fallback(tmp_path):
    path = _caption_db(tmp_path / "captions.sqlite3", [
        ("first", "r", "s", 1, "a" * 40, "ready", 0, None),
        ("second", "r", "s", 2, "b" * 40, "ready", 2000, 2000),
        ("third", "r", "s", 3, "c" * 40, "ready", 4000, 3000),
        ("missing-start", "r", "s", 4, "d" * 40, "ready", None, None),
        ("draft", "r", "s", 5, "x" * 100, "zh_ready", 5000, 6000),
        ("blank", "r", "s", 6, " \t", "ready", 6000, None),
        ("other-session", "r", "other", 1, "a", "ready", 4500, 5500),
        ("other-room", "z", "s", 1, "a", "ready", 4100, 5100),
    ])
    report = speed.analyze(path, default_duration=4, en_cps=9)
    assert {item["id"]: item["cps"] for item in report["flagged"]} == {
        "first": 20, "second": 20, "third": 20, "missing-start": 10,
    }
    assert report["by_language"]["en"]["count"] == 6
    assert report["by_language"]["ja"]["count"] == 0
    assert report["by_language"]["ja"]["p50_cps"] is None


def test_last_caption_uses_default_and_truncates_flagged_text(tmp_path):
    path = _caption_db(tmp_path / "captions.sqlite3", [
        ("last", "r", "s", 1, "é" * 90, "ready", 0, None),
        ("other-session", "r", "t", 1, "a", "ready", 100, 1100),
        ("other-room", "z", "s", 1, "a", "ready", 100, 1100),
    ])
    report = speed.analyze(path)
    assert report["flagged"] == [
        {"id": "last", "session": "s", "lang": "en", "cps": 30, "text": "é" * 80},
    ]
    assert speed.analyze(path, default_duration=6)["flagged_count"] == 0


@pytest.mark.parametrize("schema", ["captions", "ledger"])
def test_session_filter_is_parameterized(tmp_path, schema):
    path = tmp_path / "captions.sqlite3"
    if schema == "ledger":
        _ledger_db(path)
        session, lang = "japanese", "ja"
    else:
        session, lang = "s' OR 1=1 --", "en"
        _caption_db(path, [
            ("selected", "r", session, 1, "a" * 18, "ready", 0, 1000),
            ("excluded", "r", "other", 1, "a" * 18, "ready", 0, 1000),
        ])
    selected = speed.analyze(path, session)
    assert selected["by_language"][lang]["count"] == (2 if schema == "ledger" else 1)
    assert all(item["session"] == session for item in selected["flagged"])
    assert speed.analyze(path, "' OR 1=1 --")["by_language"][lang]["count"] == 0


def test_read_only_uri_and_database_integrity(tmp_path, monkeypatch, capsys):
    path = _ledger_db(tmp_path / "字幕 ?#.sqlite3")
    before = _snapshot(path)
    connect = sqlite3.connect
    calls = []

    def readonly(database, **kwargs):
        calls.append((database, kwargs))
        conn = connect(database, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE forbidden (id integer)")
        return conn

    monkeypatch.setattr(speed.sqlite3, "connect", readonly)
    assert speed.main(["--db", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["flagged_count"] == 2
    assert calls == [(path.resolve().as_uri() + "?mode=ro", {"uri": True})]
    assert _snapshot(path) == before
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()


def test_cli_json_and_wal_are_unchanged(tmp_path):
    path = _ledger_db(tmp_path / "captions.sqlite3")
    output = tmp_path / "report.json"
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE translations SET text='猫' WHERE segment_id='j2'")
        writer.commit()
        protected = [path, Path(str(path) + "-wal")]
        before = [_snapshot(item) for item in protected]
        result = subprocess.run(
            [sys.executable, str(TOOL), "--db", str(path), "--session", "japanese",
             "--ja-cps", "7", "--en-cps", "16", "--default-duration", "4",
             "--json", str(output)],
            capture_output=True, text=True, check=True,
        )
        assert not result.stderr
        report = json.loads(result.stdout)
        assert report == json.loads(output.read_text(encoding="utf-8"))
        assert report["by_language"]["ja"]["max_cps"] == 8
        assert report["by_language"]["en"]["count"] == 0
        assert report["flagged_count"] == 1
        assert [_snapshot(item) for item in protected] == before


@pytest.mark.parametrize("destination", ["db", "alias", "symlink", "wal", "shm", "journal", "existing"])
def test_json_never_overwrites_or_creates_sqlite_sidecars(tmp_path, capsys, destination):
    path = _ledger_db(tmp_path / "captions.sqlite3")
    output = path
    if destination == "alias":
        output = path.parent / "." / path.name
    elif destination == "symlink":
        output = tmp_path / "alias.json"
        output.symlink_to(path)
    elif destination in ("wal", "shm", "journal"):
        output = Path(str(path) + "-" + destination)
    elif destination == "existing":
        output = tmp_path / "existing.json"
        output.write_text("keep", encoding="utf-8")
    before = _snapshot(path)
    assert speed.main(["--db", str(path), "--json", str(output)]) == 2
    captured = capsys.readouterr()
    assert "cannot analyze" in captured.err and not captured.out
    assert _snapshot(path) == before
    if destination in ("wal", "shm", "journal"):
        assert not output.exists()
    elif destination == "existing":
        assert output.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("option", ["--default-duration", "--en-cps", "--ja-cps"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_numeric_options(tmp_path, capsys, option, value):
    path = _ledger_db(tmp_path / "captions.sqlite3")
    assert speed.main(["--db", str(path), option, value]) == 2
    assert "finite and positive" in capsys.readouterr().err


def test_missing_and_invalid_databases_are_not_modified(tmp_path, capsys):
    path = tmp_path / "captions.sqlite3"
    assert speed.main(["--db", str(path)]) == 2
    assert not path.exists()
    path.write_bytes(b"not SQLite")
    before = _snapshot(path)
    assert speed.main(["--db", str(path)]) == 2
    assert _snapshot(path) == before
    assert "cannot analyze" in capsys.readouterr().err
