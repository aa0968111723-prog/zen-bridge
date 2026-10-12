from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from tools import ja_typography_check as checker


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "ja_typography_check.py"


@pytest.mark.parametrize("text,rule,count", [
    ("日本語,。", "ascii_punctuation", 1),
    ("日本語.?!:;", "ascii_punctuation", 5),
    ("「日本語", "unbalanced_brackets", 1),
    ("日本語』", "unbalanced_brackets", 1),
    ("『（日本語』）", "unbalanced_brackets", 2),
    ("这对汉语", "chinese_residue", 4),
    ("日本語，字幕", "chinese_residue", 1),
    ("日本語。。、、", "doubled_punctuation", 2),
    ("。。。", "doubled_punctuation", 2),
    ("　、字幕", "kinsoku_line_start", 1),
    ("。字幕", "kinsoku_line_start", 1),
    ("あ" * 17, "line_too_long", 1),
])
def test_every_rule(text, rule, count):
    assert sum(item[0] == rule for item in checker.check_line(text)) == count


@pytest.mark.parametrize("text", [
    "日本語、字幕。？！：；", "「『（字幕）』」",
    "値は3.5です。", "U.S.へ行く。", "時刻12:30です。",
    "数は1,000です。", "日本語でfoo.barを使う。",
    "ABC, hello?!", "台湾、學校、龍、國。", "与える。", "数は１，０００。",
    "日本語，ABC", "ABC，日本語",
    "あ" * 16, "A" * 32, "{漢字|かんじ}" * 8,
    "{日本|にほん。。这}語。",
])
def test_clean_and_conservative_exceptions(text):
    assert checker.check_line(text) == []


@pytest.mark.parametrize("text", ["A" * 33, "{漢字|かんじ}" * 8 + "A", "あ" * 16 + "A"])
def test_ruby_and_half_width_length(text):
    assert checker.check_line(text) == [("line_too_long", 0)]
    assert checker._width(text) > 16


def _caption_db(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("""CREATE TABLE captions (
            id TEXT, en TEXT, status TEXT, room_id TEXT, session_id TEXT, seq INTEGER)""")
        conn.executemany("INSERT INTO captions VALUES (?, ?, ?, 'r', 's', ?)", [
            ("final", "字幕,。\n。次", "ready", 1),
            ("draft", "这", "zh_ready", 2),
            ("null", None, "ready", 3),
        ])
        conn.commit()
    return path


def _ledger_db(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("""CREATE TABLE segments (
            id TEXT, status TEXT, room_id TEXT, session_id TEXT, seq INTEGER)""")
        conn.execute("""CREATE TABLE translations (
            segment_id TEXT, tgt_lang TEXT, is_current INTEGER, status TEXT, text TEXT)""")
        conn.executemany("INSERT INTO segments VALUES (?, ?, 'r', 's', ?)", [
            ("final", "translated", 1), ("draft", "draft", 2),
        ])
        conn.executemany("INSERT INTO translations VALUES (?, ?, ?, ?, ?)", [
            ("final", "ja", 1, "ok", "字幕,。"),
            ("final", "ja", 0, "ok", "这"),
            ("final", "zh", 1, "ok", "这"),
            ("final", "ja", 1, "error", "这"),
            ("draft", "ja", 1, "ok", "这"),
            ("final", "ja", 1, "merged", "（字幕"),
        ])
        conn.commit()
    return path


def test_caption_schema_and_multiline_ids(tmp_path):
    report = checker.analyze(db=_caption_db(tmp_path / "captions.sqlite3"))
    assert report["lines_checked"] == 2
    assert report["finding_count"] == 2
    assert report["findings"] == [
        {"id": "final", "line": 1, "rule": "ascii_punctuation", "snippet": "字幕,。"},
        {"id": "final", "line": 2, "rule": "kinsoku_line_start", "snippet": "。次"},
    ]


def test_ledger_current_successful_japanese_only(tmp_path):
    report = checker.analyze(db=_ledger_db(tmp_path / "zen.sqlite3"))
    assert report["lines_checked"] == 2
    assert report["counts"]["chinese_residue"] == 0
    assert report["finding_count"] == 2


def test_strictly_readonly_uri_and_no_sidecars(tmp_path, monkeypatch):
    path = _caption_db(tmp_path / "字幕 #%.sqlite3")
    before = path.read_bytes(), path.stat().st_mtime_ns
    connect = sqlite3.connect
    calls = []

    def readonly(database, **kwargs):
        calls.append((database, kwargs))
        conn = connect(database, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE forbidden (id INTEGER)")
        return conn

    monkeypatch.setattr(checker.sqlite3, "connect", readonly)
    checker.analyze(db=path)
    assert calls == [(path.resolve().as_uri() + "?mode=ro&immutable=1", {"uri": True})]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]


def test_checkpointed_wal_and_missing_shm(tmp_path):
    path = _caption_db(tmp_path / "captions.sqlite3")
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    checker.analyze(db=path)
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()
    wal = Path(str(path) + "-wal")
    wal.write_bytes(b"offline WAL")
    with pytest.raises(ValueError, match="WAL without SHM"):
        checker.analyze(db=path)
    assert not Path(str(path) + "-shm").exists()


def test_live_wal_is_read_without_modifying_database(tmp_path):
    path = _caption_db(tmp_path / "captions.sqlite3")
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("UPDATE captions SET en='字幕。' WHERE id='final'")
        conn.commit()
        wal = Path(str(path) + "-wal")
        before = [(item.read_bytes(), item.stat().st_mtime_ns) for item in (path, wal)]
        assert checker.analyze(db=path)["finding_count"] == 0
        assert [(item.read_bytes(), item.stat().st_mtime_ns) for item in (path, wal)] == before


@pytest.mark.parametrize("limit", [None, 0, 1, 100])
def test_text_counts_not_limited_by_findings(tmp_path, limit):
    path = tmp_path / "captions.txt"
    path.write_text("字幕,。\n\n。次\n", encoding="utf-8")
    report = checker.analyze(text=path, max_findings=limit)
    assert report["finding_count"] == 2
    assert report["counts"]["ascii_punctuation"] == 1
    assert report["counts"]["kinsoku_line_start"] == 1
    assert len(report["findings"]) == (2 if limit is None else min(limit, 2))
    assert report["truncated"] == (limit in (0, 1))
    if limit is None:
        assert [item["line"] for item in report["findings"]] == [1, 3]


def test_jsonl_ids_and_embedded_display_lines(tmp_path):
    path = tmp_path / "captions.jsonl"
    path.write_text('\n' + json.dumps({"id": "ja-1", "text": "字幕。\n。次"}) +
                    '\n"字幕,。"\n', encoding="utf-8")
    report = checker.analyze(text=path)
    assert [(item["id"], item["line"], item["caption_line"]) for item in report["findings"]] == [
        ("ja-1", 2, 2), (3, 3, 1),
    ]


@pytest.mark.parametrize("payload", [b"\xff", b"{invalid", b'{"text": 3}',
                                    b'{"text":"ok","id":true}', b'["text"]', b"null"])
def test_bad_text_input_exit_two(tmp_path, capsys, payload):
    path = tmp_path / "bad.jsonl"
    path.write_bytes(payload)
    assert checker.main(["--text", str(path)]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "ja_typography_check:" in captured.err


@pytest.mark.parametrize("kind", ["missing", "invalid", "schema"])
def test_bad_database_exit_two_without_modification(tmp_path, kind, capsys):
    path = tmp_path / "captions.sqlite3"
    if kind == "invalid":
        path.write_bytes(b"not sqlite")
    elif kind == "schema":
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE other (id INTEGER)")
    before = path.read_bytes() if path.exists() else None
    assert checker.main(["--db", str(path)]) == 2
    assert (path.read_bytes() if path.exists() else None) == before
    assert not capsys.readouterr().out


@pytest.mark.parametrize("text,limit,code", [("字幕。", None, 0), ("字幕,。", None, 1),
                                           ("字幕,。", "0", 1), ("字幕。", "-1", 2)])
def test_cli_exit_codes_and_utf8_json(tmp_path, text, limit, code):
    path = tmp_path / "captions.txt"
    path.write_text(text, encoding="utf-8")
    args = [sys.executable, str(TOOL), "--text", str(path)]
    if limit is not None:
        args += ["--max-findings", limit]
    result = subprocess.run(args, capture_output=True)
    assert result.returncode == code
    if code != 2:
        report = json.loads(result.stdout.decode("utf-8"))
        assert report["finding_count"] == (code == 1)
    else:
        assert not result.stdout and result.stderr


@pytest.mark.parametrize("args", [[], ["--db", "x", "--text", "y"],
                                 ["--text", "x", "--max-findings", "invalid"]])
def test_invalid_cli_arguments(args):
    with pytest.raises(SystemExit) as exc:
        checker.main(args)
    assert exc.value.code == 2


def test_windows_stdout_utf8(tmp_path, monkeypatch):
    path = tmp_path / "captions.txt"
    path.write_text("字幕,。", encoding="utf-8")

    class Output:
        def __init__(self):
            self.encoding = None
            self.value = ""

        def reconfigure(self, *, encoding):
            self.encoding = encoding

        def write(self, value):
            self.value += value

    output = Output()
    monkeypatch.setattr(checker.sys, "platform", "win32")
    monkeypatch.setattr(checker.sys, "stdout", output)
    assert checker.main(["--text", str(path)]) == 1
    assert output.encoding == "utf-8"
    assert json.loads(output.value)["findings"][0]["snippet"] == "字幕,。"
