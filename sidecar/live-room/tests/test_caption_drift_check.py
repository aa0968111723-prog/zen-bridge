import ast
from contextlib import closing
import json
from pathlib import Path
import re
import runpy
import sqlite3
import sys

import pytest

from tools import caption_drift_check as drift


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "caption_drift_check.py"


def _snapshot(path):
    return path.read_bytes(), path.stat().st_mtime_ns


def _caption_db(path):
    # Read the real CaptionStore DDL without importing its writer or the app.
    tree = ast.parse((ROOT / "app" / "store.py").read_text(encoding="utf-8"))
    prepare = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "_prepare")
    ddl = next(node.value for node in ast.walk(prepare)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)
               and "create table if not exists captions (" in node.value)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(ddl)
        conn.execute("ALTER TABLE captions ADD COLUMN session_ord integer")
        conn.execute("ALTER TABLE captions ADD COLUMN term_flags text")
        conn.executemany(
            """INSERT INTO captions
               (id, room_id, session_id, seq, version, zh, zh_raw, en, status,
                t0_ms, t1_ms, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                ("r:s:1", "r", "s", 1, 7, "你好", "您好", "Hello", "ready", 0, 6000, 10),
                ("r:s:2", "r", "s", 2, 1, "", "", "", "error", 6000, 12000, 20),
                ("other:s:1", "other", "s", 1, 2, "猫", "貓", "Cat", "ready", 0, 500, 30),
            ],
        )
        conn.commit()
    return path


def _ledger_db(path, populated=True):
    schema = (ROOT / "app" / "admin" / "schema.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(path)) as conn:
        for table in ("rooms", "sessions", "segments", "translations"):
            ddl = re.search(r"CREATE TABLE IF NOT EXISTS " + table + r" \([\s\S]*?\n\);", schema)
            assert ddl is not None
            conn.execute(ddl.group())
        if populated:
            conn.execute("INSERT INTO rooms(id, created_at) VALUES ('r', 0)")
            conn.executemany(
                "INSERT INTO sessions(id, room_id, started_at, tgt_lang) VALUES (?, 'r', 0, ?)",
                [("english", "en"), ("japanese", "ja"), ("empty-session", "en")],
            )
            conn.executemany(
                """INSERT INTO segments(id, session_id, room_id, seq, t0_ms, t1_ms, status, created_at)
                   VALUES (?, ?, 'r', ?, 0, 6000, ?, 0)""",
                [("e1", "english", 1, "translated"), ("e2", "english", 2, "translated"),
                 ("j1", "japanese", 1, "translated"), ("j2", "japanese", 2, "translated"),
                 ("j3", "japanese", 3, "error")],
            )
            conn.executemany(
                """INSERT INTO translations
                   (segment_id, tgt_lang, version, is_current, text, origin, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    ("e1", "en", 1, 0, "cat", "mt", 10),
                    ("e1", "en", 2, 0, "cats", "mt", 10.5),
                    ("e1", "en", 3, 1, "cut", "post_edit", 11),
                    ("e2", "en", 1, 0, "same", "mt", 10),
                    ("e2", "en", 2, 1, "same", "human", 13),
                    ("j1", "ja", 1, 0, "今日は晴れ", "mt", 10),
                    ("j1", "ja", 2, 1, "今日は雨だ", "human", 12),
                    ("j2", "ja", 1, 0, "猫", "mt", 10),
                    ("j2", "ja", 2, 1, "猫です", "post_edit", 14),
                    # Other target versions must not contaminate session-language metrics.
                    ("j1", "en", 1, 1, "not the target", "mt", 99),
                ],
            )
        conn.commit()
    return path


def _assert_missing(summary):
    assert summary["rewrite_rate"] is None
    assert summary["share_changed"] is None
    assert summary["latency_ms"] == {"p50": None, "p90": None, "p99": None, "max": None}
    assert summary["missing_metrics"] == {
        "rewrite_rate": drift.DRIFT_REASON,
        "share_changed": drift.DRIFT_REASON,
        "latency_ms": drift.LATENCY_REASON,
    }


@pytest.mark.parametrize("left,right,distance,rate", [
    ("", "", 0, 0), ("", "猫です", 3, 1), ("猫です", "", 3, 1),
    ("cat", "cut", 1, 1 / 3), ("kitten", "sitting", 3, 3 / 7),
    ("今日は晴れ", "今日は雨だ", 2, 2 / 5), ("猫", "猫です", 2, 2 / 3),
    ("𠮷🙂", "𠮷🙃", 1, 1 / 2), ("同じ", "同じ", 0, 0),
])
def test_unicode_levenshtein(left, right, distance, rate):
    assert drift.levenshtein(left, right) == distance
    assert drift.levenshtein(right, left) == distance
    assert drift.normalized_distance(left, right) == rate


def test_ledger_counts_and_saved_revision_numbers(tmp_path):
    path = _ledger_db(tmp_path / "zen.sqlite3")
    before = _snapshot(path)
    report = drift.analyze(path)
    assert report["schema"] == "ledger"
    overall = report["overall"]
    assert overall["segments"] == 5
    _assert_missing(overall)
    proxy = overall["saved_revision_proxy"]
    assert proxy["samples"] == proxy["elapsed_samples"] == 4
    assert proxy["rewrite_rate"] == (1 / 3 + 2 / 5 + 2 / 3) / 4
    assert proxy["share_changed"] == 3 / 4
    assert proxy["elapsed_ms"] == {"p50": 2500, "p90": 3700, "p99": 3970, "max": 4000}
    sessions = {row["session_id"]: row for row in report["sessions"]}
    en = sessions["english"]
    ja = sessions["japanese"]
    assert en["segments"] == 2 and ja["segments"] == 3
    assert en["saved_revision_proxy"]["rewrite_rate"] == 1 / 6
    assert en["saved_revision_proxy"]["share_changed"] == 1 / 2
    assert en["saved_revision_proxy"]["elapsed_ms"] == {
        "p50": 2000, "p90": 2800, "p99": 2980, "max": 3000,
    }
    assert ja["saved_revision_proxy"]["rewrite_rate"] == (2 / 5 + 2 / 3) / 2
    assert ja["saved_revision_proxy"]["share_changed"] == 1
    assert ja["saved_revision_proxy"]["elapsed_ms"] == {
        "p50": 3000, "p90": 3800, "p99": 3980, "max": 4000,
    }
    for lang, summary in (("en", en), ("ja", ja)):
        _assert_missing(summary)
        assert overall["by_target_language"][lang] == {
            key: value for key, value in summary.items()
            if key not in {"room_id", "session_id", "by_target_language"}
        }
        assert summary["by_target_language"][lang]["segments"] == summary["segments"]
    assert en["by_target_language"]["ja"]["segments"] == 0
    assert sessions["empty-session"]["segments"] == 0
    assert _snapshot(path) == before


def test_caption_store_does_not_mistake_raw_asr_or_version_for_drafts(tmp_path):
    path = _caption_db(tmp_path / "captions.sqlite3")
    before = _snapshot(path)
    report = drift.analyze(path, "s")
    assert report["schema"] == "captions"
    assert [(s["room_id"], s["segments"]) for s in report["sessions"]] == [("other", 1), ("r", 2)]
    overall = report["overall"]
    assert overall["segments"] == overall["by_target_language"]["en"]["segments"] == 3
    assert overall["by_target_language"]["ja"]["segments"] == 0
    _assert_missing(overall)
    assert overall["saved_revision_proxy"]["samples"] == 0
    assert overall["saved_revision_proxy"]["rewrite_rate"] is None
    assert _snapshot(path) == before


@pytest.mark.parametrize("schema", ["ledger", "captions", "bare"])
def test_empty_database(tmp_path, schema):
    path = tmp_path / "empty.sqlite3"
    if schema == "ledger":
        _ledger_db(path, populated=False)
    elif schema == "captions":
        _caption_db(path)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("DELETE FROM captions")
            conn.commit()
    else:
        with closing(sqlite3.connect(path)):
            pass
    before = _snapshot(path)
    report = drift.analyze(path)
    assert report["sessions"] == []
    assert report["overall"]["segments"] == 0
    _assert_missing(report["overall"])
    assert report["overall"]["saved_revision_proxy"]["samples"] == 0
    assert _snapshot(path) == before


def test_single_version_missing_current_and_backdated_revision(tmp_path):
    path = _ledger_db(tmp_path / "zen.sqlite3")
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("DELETE FROM translations WHERE segment_id='e1' AND version<>3")
        conn.execute("UPDATE translations SET is_current=0 WHERE segment_id='e2'")
        conn.execute("UPDATE translations SET created_at=9 WHERE segment_id='j1' AND is_current=1")
        conn.commit()
    before = _snapshot(path)
    report = drift.analyze(path)
    en = report["overall"]["by_target_language"]["en"]["saved_revision_proxy"]
    assert en["samples"] == en["elapsed_samples"] == 1
    assert en["rewrite_rate"] == en["share_changed"] == 0
    assert en["elapsed_ms"] == {"p50": 0, "p90": 0, "p99": 0, "max": 0}
    ja = report["overall"]["by_target_language"]["ja"]["saved_revision_proxy"]
    assert ja["samples"] == 2 and ja["elapsed_samples"] == 1
    assert ja["elapsed_ms"] == {"p50": 4000, "p90": 4000, "p99": 4000, "max": 4000}
    _assert_missing(report["overall"])
    assert _snapshot(path) == before


def test_session_filter_and_parameter_binding(tmp_path):
    path = _ledger_db(tmp_path / "zen.sqlite3")
    report = drift.analyze(path, "japanese")
    assert [s["session_id"] for s in report["sessions"]] == ["japanese"]
    assert report["overall"]["segments"] == 3
    assert report["overall"]["by_target_language"]["en"]["segments"] == 0
    assert drift.analyze(path, "' OR 1=1 --")["overall"]["segments"] == 0


def test_cli_entrypoint_json_and_readonly_uri(tmp_path, capsys, monkeypatch):
    path = _ledger_db(tmp_path / "captions # %.sqlite3")
    output = tmp_path / "report.json"
    before = _snapshot(path)
    connect = sqlite3.connect
    calls = []

    def readonly(database, **kwargs):
        calls.append((database, kwargs))
        conn = connect(database, **kwargs)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("CREATE TABLE should_not_exist (id integer)")
        except BaseException:
            conn.close()
            raise
        return conn

    monkeypatch.setattr(drift.sqlite3, "connect", readonly)
    monkeypatch.setattr(sys, "argv", [str(TOOL), "--db", str(path), "--session", "japanese", "--json", str(output)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(TOOL), run_name="__main__")
    assert exc.value.code == 0
    stdout = capsys.readouterr()
    assert not stdout.err
    assert json.loads(stdout.out) == json.loads(output.read_text(encoding="utf-8"))
    assert json.loads(stdout.out)["overall"]["segments"] == 3
    assert calls == [(path.resolve().as_uri() + "?mode=ro", {"uri": True})]
    assert _snapshot(path) == before


def test_missing_database_is_not_created(tmp_path, capsys):
    path = tmp_path / "missing.sqlite3"
    output = tmp_path / "report.json"
    assert drift.main(["--db", str(path), "--json", str(output)]) == 2
    captured = capsys.readouterr()
    assert "cannot analyze" in captured.err and "unable to open database" in captured.err
    assert not captured.out and not path.exists() and not output.exists()


@pytest.mark.parametrize("destination", ["db", "alias", "wal", "existing"])
def test_json_cannot_overwrite_database_or_existing_files(tmp_path, capsys, destination):
    path = _ledger_db(tmp_path / "zen.sqlite3")
    output = path
    if destination == "alias":
        output = path.parent / "." / path.name
    elif destination == "wal":
        output = Path(str(path) + "-wal")
    elif destination == "existing":
        output = tmp_path / "existing.json"
        output.write_text("keep", encoding="utf-8")
    before = _snapshot(path)
    assert drift.main(["--db", str(path), "--json", str(output)]) == 2
    assert "cannot analyze" in capsys.readouterr().err
    assert _snapshot(path) == before
    if destination == "wal":
        assert not output.exists()
    elif destination == "existing":
        assert output.read_text(encoding="utf-8") == "keep"


def test_invalid_database_and_unsupported_schema_fail_clearly(tmp_path, capsys):
    path = tmp_path / "bad.sqlite3"
    path.write_bytes(b"not SQLite")
    before = _snapshot(path)
    assert drift.main(["--db", str(path)]) == 2
    assert "cannot analyze" in capsys.readouterr().err
    assert _snapshot(path) == before
    other = tmp_path / "other.sqlite3"
    with closing(sqlite3.connect(other)) as conn:
        conn.execute("CREATE TABLE unrelated (id integer)")
        conn.commit()
    before = _snapshot(other)
    assert drift.main(["--db", str(other)]) == 2
    assert "unsupported captions database schema" in capsys.readouterr().err
    assert _snapshot(other) == before
