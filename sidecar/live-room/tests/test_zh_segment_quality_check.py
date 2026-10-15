import ast
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import pytest

from tools import zh_segment_quality_check as zq


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "zh_segment_quality_check.py"


def _snapshot(path):
    return path.read_bytes(), path.stat().st_mtime_ns


def _caption_db(path, rows):
    """rows: (id, session_id, seq, zh, status, t0_ms, t1_ms) using app/store.py's real DDL."""
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
               (id, room_id, session_id, seq, version, zh, zh_raw, en, status, t0_ms, t1_ms, updated_at)
               VALUES (?, 'r', ?, ?, 1, ?, '', 'en', ?, ?, ?, 0)""",
            rows,
        )
        conn.commit()
    return path


def _ledger_db(path):
    schema = (ROOT / "app" / "admin" / "schema.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(path)) as conn:
        for table in ("rooms", "sessions", "segments", "transcripts"):
            ddl = re.search(r"CREATE TABLE IF NOT EXISTS " + table + r" \([\s\S]*?\n\);", schema)
            assert ddl is not None
            conn.execute(ddl.group())
        conn.execute("INSERT INTO rooms(id, created_at) VALUES ('r', 0)")
        conn.executemany("INSERT INTO sessions(id, room_id, started_at) VALUES (?, 'r', 0)",
                         [("s1",), ("s2",)])
        conn.executemany(
            """INSERT INTO segments (id, session_id, room_id, seq, t0_ms, t1_ms, status, created_at)
               VALUES (?, ?, 'r', ?, ?, ?, ?, 0)""",
            [("a", "s1", 1, 0, 2000, "translated"),
             ("b", "s1", 2, 2000, 4000, "asr_done"),
             ("c", "s1", 3, 4000, 6000, "error"),
             ("d", "s2", 1, 0, 2000, "translated")],
        )
        conn.executemany(
            """INSERT INTO transcripts (segment_id, version, is_current, text)
               VALUES (?, ?, ?, ?)""",
            [("a", 1, 0, "所以"),
             ("a", 2, 1, "今天我們來談禪修"),
             ("b", 1, 1, "我覺得很好，但是"),
             ("c", 1, 1, "然後"),
             ("d", 1, 1, "第二場的內容")],
        )
        conn.commit()
    return path


def _jsonl(path, items):
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items),
                    encoding="utf-8")
    return path


def _codes(text, t0=0, t1=2000, **limits):
    issues, _ = zq.check_segment(text, t0, t1, zq._thresholds(limits))
    return [issue["code"] for issue in issues]


# ------------------------------------------------------------------ single checks

def test_clean_segment_has_no_issues():
    assert _codes("今天我們來談禪修的方法") == []


@pytest.mark.parametrize("text", ["", "   ", "\u3000\t\n"])
def test_empty_or_whitespace_only(text):
    assert _codes(text) == ["empty"]


@pytest.mark.parametrize("text", [" 今天我們來談禪修", "今天我們來談禪修 ", "今天我們來談禪修\u3000"])
def test_leading_or_trailing_whitespace(text):
    assert _codes(text) == ["edge_whitespace"]


def test_too_short_counts_only_letters_and_digits():
    assert zq.char_count("嗯。") == 1
    assert zq.char_count("AI 很好！123") == 7
    assert _codes("嗯。", t1=500) == ["too_short"]
    assert _codes("好的", t1=500) == []


def test_too_long_and_threshold_override():
    text = "禪" * 41
    assert _codes(text, t1=6000) == ["too_long"]
    assert _codes(text, t1=6000, max_chars=50) == []


def test_duration_too_short_by_cps_and_absolute_floor():
    assert _codes("今天我們來談禪修的方法", t1=1000) == ["duration_too_short"]  # 10 cps
    assert _codes("好的", t1=300) == ["duration_too_short"]                # under 0.4 s
    assert _codes("今天我們來談禪修的方法", t1=1000, max_cps=12) == []


def test_duration_too_long_by_cps_and_absolute_ceiling():
    assert _codes("好的好的", t1=6000) == ["duration_too_long"]            # 0.67 cps
    assert _codes("禪" * 30, t1=16000) == ["duration_too_long"]            # over 15 s
    assert _codes("禪" * 30, t1=16000, max_duration_s=20) == []


@pytest.mark.parametrize("t0,t1", [(None, None), (1000, None), (2000, 2000), (3000, 1000)])
def test_missing_or_invalid_span_skips_duration_checks(t0, t1):
    issues, metrics = zq.check_segment("好的好的", t0, t1, zq._thresholds(None))
    assert issues == [] and metrics["duration_s"] is None and metrics["cps"] is None


@pytest.mark.parametrize("text,word", [
    ("我們先坐好然後", "然後"), ("這個很重要，但是", "但是"), ("所以…", "所以"),
    ("因為，", "因為"), ("而且。", "而且"), ("這就是", "就是"), ("好，那", "那"),
    ("如果", "如果"), ("我们先坐好然后", "然后"),
])
def test_dangling_connective(text, word):
    assert zq.dangling_connective(text) == word
    assert "dangling_connective" in _codes(text)


@pytest.mark.parametrize("text", ["我們把書放在那", "大家吃飯了", "這不是我說的", "然後我們開始打坐"])
def test_no_false_dangling_connective(text):
    assert zq.dangling_connective(text) is None


def test_extra_connective_option():
    assert zq.dangling_connective("我們接下來") is None
    assert zq.dangling_connective("我們接下來", ("接下來",)) == "接下來"


@pytest.mark.parametrize("text,stray", [
    ("他說「你好", ["「"]), ("你好」他說", ["」"]), ("（括號沒關", ["（"]),
    ("《心經》和『金剛經", ["『"]), ("(note", ["("]), ("「（錯）」", []),
    ("「（錯」）", ["」", "「"]), ('他說"好', ['"']), ("“引號", ["“"]),
])
def test_unbalanced_brackets(text, stray):
    assert zq.unbalanced_brackets(text) == stray
    assert ("unbalanced_brackets" in _codes(text)) == bool(stray)


def test_issue_messages_are_traditional_chinese():
    issues, _ = zq.check_segment(" 嗯「然後", 0, 5000, zq._thresholds(None))
    codes = {issue["code"]: issue["message_zh"] for issue in issues}
    assert set(codes) == {"edge_whitespace", "duration_too_long", "dangling_connective",
                          "unbalanced_brackets"}
    assert "連接詞「然後」" in codes["dangling_connective"]
    assert all(re.search(r"[\u4e00-\u9fff]", message) for message in codes.values())


# ------------------------------------------------------------------ sources

def test_captions_db_uses_final_zh_statuses(tmp_path):
    path = _caption_db(tmp_path / "captions.sqlite3", [
        ("ok", "s", 1, "今天我們來談禪修", "ready", 0, 2000),
        ("zh", "s", 2, "大家坐好，然後", "zh_ready", 2000, 4000),
        ("tf", "s", 3, "「沒有關閉", "translate_failed", 4000, 6000),
        ("draft", "s", 4, "然後", "transcribing", 6000, 8000),
        ("silent", "s", 5, "", "silent", 8000, 9000),
        ("err", "s", 6, "", "error", 9000, 10000),
    ])
    report = zq.analyze(db=path)
    assert report["schema"] == "captions"
    assert report["summary"]["segments"] == 3
    assert report["summary"]["segments_with_issues"] == 2
    assert report["summary"]["counts"]["dangling_connective"] == 1
    assert report["summary"]["counts"]["unbalanced_brackets"] == 1
    assert [item["id"] for item in report["segments"]] == ["zh", "tf"]
    assert report["segments"][0]["session_id"] == "s" and report["segments"][0]["seq"] == 2


def test_ledger_uses_current_transcript_of_final_segments(tmp_path):
    path = _ledger_db(tmp_path / "zen.sqlite3")
    report = zq.analyze(db=path)
    assert report["schema"] == "ledger"
    assert report["summary"]["segments"] == 3          # error segment excluded
    assert [item["id"] for item in report["segments"]] == ["b"]   # old "所以" version ignored
    assert report["summary"]["counts"]["dangling_connective"] == 1
    assert zq.analyze(db=path, session="s2")["summary"]["segments"] == 1


def test_session_filter_is_parameterized(tmp_path):
    session = "s' OR 1=1 --"
    path = _caption_db(tmp_path / "captions.sqlite3", [
        ("a", session, 1, "今天我們來談禪修", "ready", 0, 2000),
        ("b", "other", 1, "然後", "ready", 0, 2000),
    ])
    assert zq.analyze(db=path, session=session)["summary"]["segments"] == 1
    assert zq.analyze(db=path, session="' OR 1=1 --")["summary"]["segments"] == 0


def test_jsonl_strings_objects_zh_key_and_session(tmp_path):
    path = _jsonl(tmp_path / "segments.jsonl", [
        "今天我們來談禪修",
        {"id": "x1", "session_id": "s", "text": "好，所以", "t0_ms": 0, "t1_ms": 2000},
        {"id": 7, "session_id": "t", "zh": "  ", "t0_ms": 0, "t1_ms": 1000},
    ])
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n")
    report = zq.analyze(jsonl=path)
    assert report["schema"] == "jsonl"
    assert report["summary"]["segments"] == 3
    assert report["summary"]["duration_unknown"] == 1
    assert {item["id"]: [i["code"] for i in item["issues"]] for item in report["segments"]} == {
        "x1": ["dangling_connective"], 7: ["empty"]}
    assert zq.analyze(jsonl=path, session="s")["summary"]["segments"] == 1


@pytest.mark.parametrize("line", ["{bad", "[1, 2]", '{"text": 3}', '{"text": "a", "t0_ms": "0"}',
                                  '{"text": "a", "id": true}'])
def test_invalid_jsonl_exit_2(tmp_path, capsys, line):
    path = tmp_path / "bad.jsonl"
    path.write_text(line + "\n", encoding="utf-8")
    assert zq.main(["--jsonl", str(path)]) == 2
    captured = capsys.readouterr()
    assert "cannot analyze" in captured.err and not captured.out


def test_max_findings_truncates_list_not_counts(tmp_path):
    path = _jsonl(tmp_path / "s.jsonl", ["然後", "所以", "但是"])
    report = zq.analyze(jsonl=path, max_findings=1)
    assert report["truncated"] is True and len(report["segments"]) == 1
    assert report["summary"]["counts"]["dangling_connective"] == 3


# ------------------------------------------------------------------ CLI / exit codes

def test_cli_exit_0_within_threshold_and_1_above(tmp_path, capsys):
    path = _jsonl(tmp_path / "s.jsonl", [
        {"text": "今天我們來談禪修", "t0_ms": 0, "t1_ms": 2000},
        {"text": "大家先把背打直", "t0_ms": 2000, "t1_ms": 4000},
        {"text": "呼吸放慢，然後", "t0_ms": 4000, "t1_ms": 6000},
    ])
    assert zq.main(["--jsonl", str(path)]) == 1                       # 1/3 > 0.2
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["within_threshold"] is False
    assert "超過門檻" in report["summary"]["verdict_zh"]
    assert zq.main(["--jsonl", str(path), "--max-issue-rate", "0.5"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["issue_rate"] == pytest.approx(0.3333)


def test_cli_empty_input_is_exit_0(tmp_path, capsys):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert zq.main(["--jsonl", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["segments"] == 0


def test_cli_unreadable_inputs_exit_2(tmp_path, capsys):
    missing = tmp_path / "missing.sqlite3"
    assert zq.main(["--db", str(missing)]) == 2
    assert not missing.exists()
    notdb = tmp_path / "captions.sqlite3"
    notdb.write_bytes(b"not SQLite at all")
    before = _snapshot(notdb)
    assert zq.main(["--db", str(notdb)]) == 2
    assert _snapshot(notdb) == before
    other = tmp_path / "other.sqlite3"
    with closing(sqlite3.connect(other)) as conn:
        conn.execute("CREATE TABLE unrelated (id integer)")
        conn.commit()
    assert zq.main(["--db", str(other)]) == 2
    assert zq.main(["--jsonl", str(tmp_path / "nope.jsonl")]) == 2
    binary = tmp_path / "binary.jsonl"
    binary.write_bytes(b"\xff\xfe\x00bad")
    assert zq.main(["--jsonl", str(binary)]) == 2
    assert "cannot analyze" in capsys.readouterr().err


@pytest.mark.parametrize("args", [
    ["--min-chars", "0"], ["--max-chars", "1", "--min-chars", "2"], ["--max-cps", "nan"],
    ["--min-cps", "-1"], ["--max-duration", "inf"], ["--max-issue-rate", "1.5"],
    ["--min-cps", "10", "--max-cps", "5"], ["--max-findings", "-1"],
])
def test_invalid_thresholds_exit_2(tmp_path, capsys, args):
    path = _jsonl(tmp_path / "s.jsonl", ["今天我們來談禪修"])
    assert zq.main(["--jsonl", str(path), *args]) == 2
    assert "cannot analyze" in capsys.readouterr().err


def test_cli_requires_exactly_one_source(tmp_path):
    with pytest.raises(SystemExit) as exc:
        zq.main([])
    assert exc.value.code == 2
    with pytest.raises(ValueError):
        zq.analyze()


def test_cli_subprocess_json_file_matches_stdout(tmp_path):
    path = _caption_db(tmp_path / "captions.sqlite3", [
        ("a", "s", 1, "今天我們來談禪修", "ready", 0, 2000),
        ("b", "s", 2, "（沒關的括號", "ready", 2000, 4000),
    ])
    output = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, str(TOOL), "--db", str(path), "--max-issue-rate", "0.5",
         "--json", str(output)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    assert json.loads(result.stdout) == json.loads(output.read_text(encoding="utf-8"))
    assert json.loads(result.stdout)["summary"]["counts"]["unbalanced_brackets"] == 1


@pytest.mark.parametrize("destination", ["existing", "db", "alias", "wal", "shm", "journal", "input"])
def test_json_never_overwrites(tmp_path, capsys, destination):
    path = _caption_db(tmp_path / "captions.sqlite3", [("a", "s", 1, "今天我們來談禪修", "ready", 0, 2000)])
    source = ["--db", str(path)]
    if destination == "existing":
        output = tmp_path / "existing.json"
        output.write_text("keep", encoding="utf-8")
    elif destination == "db":
        output = path
    elif destination == "alias":
        output = path.parent / "." / path.name
    elif destination == "input":
        output = _jsonl(tmp_path / "in.jsonl", ["今天我們來談禪修"])
        source = ["--jsonl", str(output)]
    else:
        output = Path(str(path) + "-" + destination)
    before = _snapshot(path)
    keep = output.read_bytes() if output.exists() else None
    assert zq.main([*source, "--json", str(output)]) == 2
    captured = capsys.readouterr()
    assert "cannot analyze" in captured.err and not captured.out
    assert _snapshot(path) == before
    if keep is None:
        assert not output.exists()
    else:
        assert output.read_bytes() == keep


# ------------------------------------------------------------------ read-only / isolation

def test_read_only_uri_and_database_unchanged(tmp_path, monkeypatch, capsys):
    path = _caption_db(tmp_path / "字幕 #%.sqlite3", [
        ("a", "s", 1, "今天我們來談禪修", "ready", 0, 2000),
        ("b", "s", 2, "然後", "ready", 2000, 3000),
    ])
    before = _snapshot(path)
    connect = sqlite3.connect
    calls = []

    def readonly(database, **kwargs):
        calls.append((database, kwargs))
        conn = connect(database, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE forbidden (id integer)")
        return conn

    monkeypatch.setattr(zq.sqlite3, "connect", readonly)
    assert zq.main(["--db", str(path), "--max-issue-rate", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["segments_with_issues"] == 1
    assert calls == [(path.resolve().as_uri() + "?mode=ro&immutable=1", {"uri": True})]
    assert _snapshot(path) == before
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(str(path) + suffix).exists()


def test_wal_database_with_shm_is_read_without_changes(tmp_path):
    path = _ledger_db(tmp_path / "zen.sqlite3")
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE transcripts SET text='然後' WHERE segment_id='d'")
        writer.commit()
        protected = [path, Path(str(path) + "-wal")]
        before = [_snapshot(item) for item in protected]
        report = zq.analyze(db=path)
        assert report["summary"]["counts"]["dangling_connective"] == 2
        assert [_snapshot(item) for item in protected] == before


def test_wal_without_shm_is_refused(tmp_path):
    source = _ledger_db(tmp_path / "source.sqlite3")
    path = tmp_path / "zen.sqlite3"
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE transcripts SET text='然後' WHERE segment_id='d'")
        writer.commit()
        path.write_bytes(source.read_bytes())
        wal = Path(str(path) + "-wal")
        wal.write_bytes(Path(str(source) + "-wal").read_bytes())
    before = [_snapshot(item) for item in (path, wal)]
    with pytest.raises(ValueError, match="WAL without SHM"):
        zq.analyze(db=path)
    assert [_snapshot(item) for item in (path, wal)] == before
    assert not Path(str(path) + "-shm").exists()


def test_import_has_no_network_or_app_dependencies():
    code = (
        "import sys; sys.path.insert(0, %r); "
        "import tools.zh_segment_quality_check; "
        "bad = [m for m in ('socket', 'ssl', 'http.client', 'urllib.request', 'app') if m in sys.modules]; "
        "print(','.join(bad))"
    ) % str(ROOT)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == ""


def test_source_is_stdlib_only_and_never_mentions_server_port():
    source = TOOL.read_text(encoding="utf-8")
    assert "8645" not in source
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "argparse", "contextlib", "json", "math", "pathlib",
                        "sqlite3", "sys", "unicodedata"}
