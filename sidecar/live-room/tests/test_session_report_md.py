from contextlib import closing
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tools import session_report_md as report


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "session_report_md.py"
SCHEMA = ROOT / "app" / "admin" / "schema.sql"


def _ddl(schema: str, table: str) -> str:
    match = re.search(r"CREATE TABLE IF NOT EXISTS " + table + r" \([\s\S]*?\n\);", schema)
    assert match is not None
    return match.group()


def _ledger_db(path: Path) -> Path:
    schema = SCHEMA.read_text(encoding="utf-8")
    with closing(sqlite3.connect(path)) as conn:
        for table in ("rooms", "sessions", "segments", "translations", "glossaries", "glossary_terms"):
            conn.execute(_ddl(schema, table))
        conn.execute("INSERT INTO rooms(id, created_at) VALUES ('private-room-id', 0)")
        conn.executemany(
            """INSERT INTO sessions(id, room_id, started_at, ended_at, tgt_lang)
               VALUES (?, 'private-room-id', ?, ?, ?)""",
            [
                ("english-session", 1700000000, 1700000060, "en"),
                ("japanese-session", 1700000100, None, "ja"),
            ],
        )
        conn.executemany(
            """INSERT INTO segments
               (id, session_id, room_id, seq, t0_ms, t1_ms, status, speaker_id, created_at)
               VALUES (?, ?, 'private-room-id', ?, ?, ?, ?, ?, 0)""",
            [
                ("segment-a", "english-session", 1, 0, 1000, "translated", 987654),
                ("segment-b", "english-session", 2, 1000, 2000, "translated", 987654),
                ("segment-j", "japanese-session", 1, 0, 1000, "translated", 987654),
            ],
        )
        conn.executemany(
            """INSERT INTO translations
               (segment_id, tgt_lang, version, is_current, text, latency_ms, status)
               VALUES (?, ?, 1, 1, ?, ?, 'ok')""",
            [
                ("segment-a", "en", "a" * 18, 100),
                ("segment-b", "en", "safe caption", 300),
                ("segment-j", "ja", "猫" * 9, 200),
            ],
        )
        conn.execute(
            """INSERT INTO glossaries(id, name, scope, session_id)
               VALUES (1, 'private glossary name', 'session', 'english-session')"""
        )
        conn.executemany(
            """INSERT INTO glossary_terms(glossary_id, zh, en, hit_count)
               VALUES (1, ?, ?, ?)""",
            [(f"詞{i}", f"term{i}", 20 - i) for i in range(12)],
        )
        conn.commit()
    return path


def _caption_db(path: Path) -> Path:
    import ast

    tree = ast.parse((ROOT / "app" / "store.py").read_text(encoding="utf-8"))
    prepare = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "_prepare")
    ddl = next(node.value for node in ast.walk(prepare)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)
               and "create table if not exists captions (" in node.value)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(ddl)
        conn.execute(
            """INSERT INTO captions
               (id, room_id, session_id, seq, version, en, status, t0_ms, t1_ms, updated_at)
               VALUES ('private-caption-id', 'private-room-id', 'caption-session', 1, 1,
                       ?, 'ready', 0, 1000, 0)""",
            ("b" * 18,),
        )
        conn.commit()
    return path


def test_ledger_report_summarizes_only_selected_session_without_other_identifiers(tmp_path):
    path = _ledger_db(tmp_path / "captions.sqlite3")
    result = report.analyze(path, "english-session")
    assert "## 場次：english-session" in result
    assert "2023-11-14 22:13:20 UTC" in result
    assert "- 目標語言：en" in result
    assert "- 草稿字幕數：0" in result
    assert "- 定稿字幕數：2" in result
    assert "草稿→定稿改寫率：無法計算" in result
    assert "p50 200.0 ms／p95 290.0 ms" in result
    assert "18.0 字元／秒：" + "a" * 18 in result
    assert "| 詞0 | term0 | 20 |" in result
    assert "| 詞9 | term9 | 11 |" in result
    assert "詞10" not in result
    for private_value in ("private-room-id", "private glossary name", "987654",
                          "segment-a", "segment-b", "segment-j"):
        assert private_value not in result
    assert "japanese-session" not in result
    assert "未保留草稿與定稿文字配對" in result


def test_ledger_report_uses_japanese_threshold_and_all_sessions_by_default(tmp_path):
    path = _ledger_db(tmp_path / "captions.sqlite3")
    result = report.analyze(path)
    assert "## 場次：english-session" in result
    assert "## 場次：japanese-session" in result
    assert "- 目標語言：ja" in result
    assert "9.0 字元／秒：" + "猫" * 9 in result


def test_caption_store_report_notes_missing_ledger_metrics(tmp_path):
    result = report.analyze(_caption_db(tmp_path / "captions.sqlite3"), "caption-session")
    assert "- 目標語言：en" in result
    assert "- 草稿字幕數：0" in result
    assert "- 定稿字幕數：1" in result
    assert "開始時間：無資料" in result
    assert "非 ledger 資料庫" in result
    assert "latency_ms 欄位" in result
    assert "沒有場次關聯的 glossary_terms" in result
    assert "18.0 字元／秒：" + "b" * 18 in result


def test_read_only_uri_and_cli_output_is_created_without_overwrite(tmp_path, monkeypatch, capsys):
    path = _ledger_db(tmp_path / "captions # %.sqlite3")
    output = tmp_path / "report.md"
    connect = sqlite3.connect
    calls = []

    def readonly(database, **kwargs):
        calls.append((database, kwargs))
        conn = connect(database, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE forbidden (id integer)")
        return conn

    monkeypatch.setattr(report.sqlite3, "connect", readonly)
    assert report.main(["--db", str(path), "--session", "english-session", "--out", str(output)]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    assert output.read_text(encoding="utf-8") == captured.out
    assert calls == [(path.resolve().as_uri() + "?mode=ro&immutable=1", {"uri": True})]

    output.write_text("keep", encoding="utf-8")
    assert report.main(["--db", str(path), "--session", "english-session", "--out", str(output)]) == 2
    assert output.read_text(encoding="utf-8") == "keep"
    assert "cannot analyze" in capsys.readouterr().err


def test_cli_script_emits_markdown_and_does_not_create_missing_database(tmp_path):
    path = tmp_path / "missing.sqlite3"
    result = subprocess.run(
        [sys.executable, str(TOOL), "--db", str(path), "--out", str(tmp_path / "out.md")],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "cannot analyze" in result.stderr
    assert not path.exists()


def test_partial_schema_reports_missing_fields_without_crashing(tmp_path):
    path = tmp_path / "partial.sqlite3"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO sessions (id) VALUES ('partial-session')")
        conn.commit()
    result = report.analyze(path)
    assert "- 目標語言：無資料" in result
    assert "- 草稿字幕數：無資料" in result
    assert "缺少 tgt_lang 欄位" in result
    assert "找不到支援的" in result
