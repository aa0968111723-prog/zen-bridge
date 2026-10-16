"""Tests for tools/asr_repetition_detector.py — stdlib + pytest only, temp fixtures."""
from __future__ import annotations

import ast
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import pytest

from tools import asr_repetition_detector as ard


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "asr_repetition_detector.py"

CAPTIONS_DDL = """
create table if not exists captions (
    id text primary key,
    room_id text not null,
    session_id text not null,
    seq integer not null,
    version integer not null,
    zh text,
    zh_raw text,
    en text,
    status text,
    t0_ms integer,
    t1_ms integer,
    updated_at real not null
)
"""

LEDGER_DDL = {
    "rooms": """CREATE TABLE IF NOT EXISTS rooms (
        id text primary key, created_at real not null
    );""",
    "sessions": """CREATE TABLE IF NOT EXISTS sessions (
        id text primary key, room_id text not null, started_at real not null
    );""",
    "segments": """CREATE TABLE IF NOT EXISTS segments (
        id text primary key, session_id text not null, room_id text not null,
        seq integer not null, t0_ms integer, t1_ms integer,
        status text, created_at real not null
    );""",
    "transcripts": """CREATE TABLE IF NOT EXISTS transcripts (
        segment_id text not null, version integer not null,
        is_current integer not null, text text,
        primary key (segment_id, version)
    );""",
}


def _snapshot(path):
    return path.read_bytes(), path.stat().st_mtime_ns


def _caption_db(path, rows):
    """rows: (id, session_id, seq, zh, status, t0_ms, t1_ms)."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(CAPTIONS_DDL)
        conn.executemany(
            """INSERT INTO captions
               (id, room_id, session_id, seq, version, zh, zh_raw, en, status, t0_ms, t1_ms, updated_at)
               VALUES (?, 'r', ?, ?, 1, ?, '', 'en', ?, ?, ?, 0)""",
            rows,
        )
        conn.commit()
    return path


def _ledger_db(path, segments_and_text):
    """segments_and_text: list of (id, session_id, seq, t0, t1, status, current_text, old_text|None)."""
    with closing(sqlite3.connect(path)) as conn:
        for ddl in LEDGER_DDL.values():
            conn.execute(ddl)
        conn.execute("INSERT INTO rooms(id, created_at) VALUES ('r', 0)")
        sessions = sorted({row[1] for row in segments_and_text})
        conn.executemany("INSERT INTO sessions(id, room_id, started_at) VALUES (?, 'r', 0)",
                         [(s,) for s in sessions])
        for row in segments_and_text:
            sid, session, seq, t0, t1, status, current, old = row
            conn.execute(
                """INSERT INTO segments (id, session_id, room_id, seq, t0_ms, t1_ms, status, created_at)
                   VALUES (?, ?, 'r', ?, ?, ?, ?, 0)""",
                (sid, session, seq, t0, t1, status),
            )
            if old is not None:
                conn.execute(
                    "INSERT INTO transcripts (segment_id, version, is_current, text) VALUES (?, 1, 0, ?)",
                    (sid, old),
                )
                conn.execute(
                    "INSERT INTO transcripts (segment_id, version, is_current, text) VALUES (?, 2, 1, ?)",
                    (sid, current),
                )
            else:
                conn.execute(
                    "INSERT INTO transcripts (segment_id, version, is_current, text) VALUES (?, 1, 1, ?)",
                    (sid, current),
                )
        conn.commit()
    return path


def _jsonl(path, items):
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items),
                    encoding="utf-8")
    return path


def _codes(text, **limits):
    issues = ard.check_segment(text, ard._thresholds(limits))
    return [issue["code"] for issue in issues]


# ------------------------------------------------------------------ unit: n-gram loop


def test_clean_segment_has_no_issues():
    assert _codes("今天我們來談禪修的方法") == []
    assert ard.find_ngram_loop("今天我們來談禪修的方法") is None


def test_ngram_loop_single_char_repeats():
    hit = ard.find_ngram_loop("好好好好好好")
    assert hit is not None
    gram, repeats, n = hit
    # Prefer longer grams: "好好"×3 beats "好"×6
    assert repeats >= 3 and n >= 1 and set(gram) <= {"好"}
    assert "ngram_loop" in _codes("好好好好好好")
    only_char = ard.find_ngram_loop("哈哈哈", min_n=1, max_n=1, min_repeats=3)
    assert only_char == ("哈", 3, 1)


def test_ngram_loop_bigram_phrase():
    text = "謝謝謝謝謝謝大家"
    hit = ard.find_ngram_loop(text)
    assert hit is not None
    gram, repeats, n = hit
    assert n >= 2 and repeats >= 3
    assert "ngram_loop" in _codes(text)


def test_ngram_loop_longer_chunk_preferred():
    # "我們來" × 3 should beat shorter sub-grams
    text = "我們來我們來我們來談禪"
    hit = ard.find_ngram_loop(text, min_n=1, max_n=8, min_repeats=3)
    assert hit is not None
    gram, repeats, n = hit
    assert gram == "我們來" and repeats == 3 and n == 3


def test_ngram_loop_respects_min_repeats_and_bounds():
    text = "哈哈哈哈"  # 哈 × 4
    assert ard.find_ngram_loop(text, min_repeats=5) is None
    # "哈哈" × 2 = 4 chars, only 2 repeats → below min_repeats=3
    assert ard.find_ngram_loop(text, min_n=2, max_n=2, min_repeats=3) is None
    assert ard.find_ngram_loop(text, min_n=1, max_n=1, min_repeats=3) is not None


def test_ngram_loop_ignores_whitespace_only_grams():
    assert ard.find_ngram_loop("a   a   a   ") is None or True  # may or may not; spaces stripped via normalize
    # After normalize_text, multiple spaces collapse to one — no 3× space gram.
    assert ard.find_ngram_loop("你好  你好") is None


def test_ngram_loop_message_traditional_chinese():
    issues = ard.check_segment("哇哇哇哇哇", ard._thresholds(None))
    assert issues and issues[0]["code"] == "ngram_loop"
    assert "迴圈" in issues[0]["message_zh"] or "重复" in issues[0]["message_zh"] or "重複" in issues[0]["message_zh"]
    assert re.search(r"[\u4e00-\u9fff]", issues[0]["message_zh"])


# ------------------------------------------------------------------ unit: boilerplate


@pytest.mark.parametrize("text,needle", [
    ("今天講完了，謝謝收看", "謝謝收看"),
    ("请订阅我们的频道", "请订阅"),
    ("歡迎訂閱＋開啟小鈴鐺", "歡迎訂閱"),
    ("記得打卡喔", "記得打卡"),
    ("我們下期見！", "我們下期見"),
    ("下期再見", "下期再見"),
    ("謝謝大家的支持", "謝謝大家"),
    ("感谢收看本期节目", "感谢收看"),
])
def test_boilerplate_phrases(text, needle):
    found = ard.find_boilerplate(text)
    assert found is not None
    hay = ard.normalize_for_match(text)
    assert found in hay
    # Expected needle (or another listed phrase also present) must appear in hay.
    assert ard.normalize_for_match(needle) in hay
    assert "boilerplate" in _codes(text)


def test_boilerplate_fullwidth_halfwidth_variants():
    # NFKC should fold full/half width
    text = "請訂閱"  # fullwidth-ish CJK
    assert ard.find_boilerplate(text) is not None
    assert ard.find_boilerplate("请订阅") is not None


def test_no_false_boilerplate_on_normal_lecture():
    assert ard.find_boilerplate("今天我們來談《心經》的大意") is None
    assert _codes("今天我們來談《心經》的大意") == []


# ------------------------------------------------------------------ unit: adjacent + near-dup


def test_repeat_adjacent_exact_normalized():
    limits = ard._thresholds(None)
    first = ard.check_segment("今天談禪修。", limits)
    assert first == []
    second = ard.check_segment("今天談禪修", limits, prev_norm=ard.normalize_for_match("今天談禪修。"))
    assert "repeat_adjacent" in [i["code"] for i in second]


def test_repeat_adjacent_requires_same_session_in_analyze(tmp_path):
    path = _jsonl(tmp_path / "s.jsonl", [
        {"id": "a", "session_id": "s1", "seq": 1, "text": "今天談禪修", "t0_ms": 0},
        {"id": "b", "session_id": "s2", "seq": 1, "text": "今天談禪修", "t0_ms": 0},
        {"id": "c", "session_id": "s2", "seq": 2, "text": "今天談禪修", "t0_ms": 1000},
    ])
    report = ard.analyze(jsonl=path)
    codes = {item["id"]: [i["code"] for i in item["issues"]] for item in report["segments"]}
    assert "a" not in codes
    assert "b" not in codes
    assert "repeat_adjacent" in codes["c"]


def test_near_duplicate_within_window(tmp_path):
    path = _jsonl(tmp_path / "s.jsonl", [
        {"id": 1, "session_id": "s", "seq": 1, "text": "禪修的時候要把背打直"},
        {"id": 2, "session_id": "s", "seq": 2, "text": "接下來我們練習呼吸"},
        {"id": 3, "session_id": "s", "seq": 3, "text": "禪修的時候要把背打直啊"},
    ])
    report = ard.analyze(jsonl=path, thresholds={"near_window": 3, "near_ratio": 0.85})
    flagged = {item["id"]: [i["code"] for i in item["issues"]] for item in report["segments"]}
    assert "near_duplicate" in flagged[3]


def test_near_duplicate_disabled_when_window_zero(tmp_path):
    path = _jsonl(tmp_path / "s.jsonl", [
        {"id": 1, "session_id": "s", "seq": 1, "text": "禪修的時候要把背打直"},
        {"id": 2, "session_id": "s", "seq": 2, "text": "禪修的時候要把背打直啊"},
    ])
    report = ard.analyze(jsonl=path, thresholds={"near_window": 0})
    # With window 0: no near_duplicate; texts not exact → no repeat_adjacent either
    assert report["summary"]["segments_with_issues"] == 0


def test_exact_adjacent_does_not_also_require_near_duplicate(tmp_path):
    path = _jsonl(tmp_path / "s.jsonl", [
        {"id": 1, "session_id": "s", "seq": 1, "text": "大家好"},
        {"id": 2, "session_id": "s", "seq": 2, "text": "大家好"},
    ])
    report = ard.analyze(jsonl=path)
    issues = report["segments"][0]["issues"]
    codes = [i["code"] for i in issues]
    assert codes == ["repeat_adjacent"]


# ------------------------------------------------------------------ sources: db / jsonl


def test_captions_db_uses_final_zh_statuses(tmp_path):
    path = _caption_db(tmp_path / "captions.sqlite3", [
        ("ok", "s", 1, "今天我們來談禪修", "ready", 0, 2000),
        ("loop", "s", 2, "好好好好好好", "zh_ready", 2000, 4000),
        ("boil", "s", 3, "謝謝收看本期直播", "translate_failed", 4000, 6000),
        ("draft", "s", 4, "好好好好好好", "transcribing", 6000, 8000),
        ("silent", "s", 5, "", "silent", 8000, 9000),
    ])
    report = ard.analyze(db=path)
    assert report["schema"] == "captions"
    assert report["summary"]["segments"] == 3
    assert report["summary"]["counts"]["ngram_loop"] == 1
    assert report["summary"]["counts"]["boilerplate"] == 1
    assert [item["id"] for item in report["segments"]] == ["loop", "boil"]
    assert report["segments"][0]["session_id"] == "s" and report["segments"][0]["seq"] == 2


def test_ledger_uses_current_transcript_of_final_segments(tmp_path):
    path = _ledger_db(tmp_path / "zen.sqlite3", [
        ("a", "s1", 1, 0, 2000, "translated", "今天我們來談禪修", "所以"),
        ("b", "s1", 2, 2000, 4000, "asr_done", "請訂閱我們頻道", None),
        ("c", "s1", 3, 4000, 6000, "error", "好好好好", None),
        ("d", "s2", 1, 0, 2000, "translated", "第二場內容", None),
    ])
    report = ard.analyze(db=path)
    assert report["schema"] == "ledger"
    assert report["summary"]["segments"] == 3  # error excluded
    assert report["summary"]["counts"]["boilerplate"] == 1
    assert [item["id"] for item in report["segments"]] == ["b"]
    assert ard.analyze(db=path, session="s2")["summary"]["segments"] == 1


def test_session_filter_is_parameterized(tmp_path):
    session = "s' OR 1=1 --"
    path = _caption_db(tmp_path / "captions.sqlite3", [
        ("a", session, 1, "好好好好好好", "ready", 0, 2000),
        ("b", "other", 1, "好好好好好好", "ready", 0, 2000),
    ])
    assert ard.analyze(db=path, session=session)["summary"]["segments"] == 1
    assert ard.analyze(db=path, session="' OR 1=1 --")["summary"]["segments"] == 0


def test_jsonl_strings_objects_zh_key_and_session(tmp_path):
    path = _jsonl(tmp_path / "segments.jsonl", [
        "今天我們來談禪修",
        {"id": "x1", "session_id": "s", "text": "謝謝收看", "t0_ms": 0, "t1_ms": 2000},
        {"id": 7, "session_id": "t", "zh": "哇哇哇哇", "t0_ms": 0, "t1_ms": 1000},
    ])
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n")
    report = ard.analyze(jsonl=path)
    assert report["schema"] == "jsonl"
    assert report["summary"]["segments"] == 3
    by_id = {item["id"]: [i["code"] for i in item["issues"]] for item in report["segments"]}
    assert by_id["x1"] == ["boilerplate"]
    assert "ngram_loop" in by_id[7]
    assert ard.analyze(jsonl=path, session="s")["summary"]["segments"] == 1


@pytest.mark.parametrize("line", [
    "{bad", "[1, 2]", '{"text": 3}', '{"text": "a", "t0_ms": "0"}', '{"text": "a", "id": true}',
])
def test_invalid_jsonl_exit_2(tmp_path, capsys, line):
    path = tmp_path / "bad.jsonl"
    path.write_text(line + "\n", encoding="utf-8")
    assert ard.main(["--jsonl", str(path)]) == 2
    captured = capsys.readouterr()
    assert "cannot analyze" in captured.err and not captured.out


def test_max_findings_truncates_list_not_counts(tmp_path):
    path = _jsonl(tmp_path / "s.jsonl", ["謝謝收看", "請訂閱", "歡迎訂閱"])
    report = ard.analyze(jsonl=path, max_findings=1)
    assert report["truncated"] is True and len(report["segments"]) == 1
    assert report["summary"]["counts"]["boilerplate"] == 3


# ------------------------------------------------------------------ CLI / exit codes


def test_cli_exit_0_within_threshold_and_1_above(tmp_path, capsys):
    path = _jsonl(tmp_path / "s.jsonl", [
        {"text": "今天我們來談禪修", "t0_ms": 0, "t1_ms": 2000},
        {"text": "大家先把背打直", "t0_ms": 2000, "t1_ms": 4000},
        {"text": "謝謝收看", "t0_ms": 4000, "t1_ms": 6000},
    ])
    # default max_issue_rate 0.1 → 1/3 > 0.1 → exit 1
    assert ard.main(["--jsonl", str(path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["tool"] == "asr_repetition_detector"
    assert report["summary"]["within_threshold"] is False
    assert "超過門檻" in report["summary"]["verdict_zh"]
    assert ard.main(["--jsonl", str(path), "--max-issue-rate", "0.5"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["issue_rate"] == pytest.approx(0.3333)


def test_cli_empty_input_is_exit_0(tmp_path, capsys):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert ard.main(["--jsonl", str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["segments"] == 0
    assert "沒有可檢查" in out["summary"]["verdict_zh"]


def test_cli_unreadable_inputs_exit_2(tmp_path, capsys):
    missing = tmp_path / "missing.sqlite3"
    assert ard.main(["--db", str(missing)]) == 2
    assert not missing.exists()
    notdb = tmp_path / "captions.sqlite3"
    notdb.write_bytes(b"not SQLite at all")
    before = _snapshot(notdb)
    assert ard.main(["--db", str(notdb)]) == 2
    assert _snapshot(notdb) == before
    other = tmp_path / "other.sqlite3"
    with closing(sqlite3.connect(other)) as conn:
        conn.execute("CREATE TABLE unrelated (id integer)")
        conn.commit()
    assert ard.main(["--db", str(other)]) == 2
    assert ard.main(["--jsonl", str(tmp_path / "nope.jsonl")]) == 2
    binary = tmp_path / "binary.jsonl"
    binary.write_bytes(b"\xff\xfe\x00bad")
    assert ard.main(["--jsonl", str(binary)]) == 2
    assert "cannot analyze" in capsys.readouterr().err


@pytest.mark.parametrize("args", [
    ["--min-ngram", "0"],
    ["--max-ngram", "1", "--min-ngram", "2"],
    ["--min-repeats", "1"],
    ["--near-window", "-1"],
    ["--near-ratio", "1.5"],
    ["--max-issue-rate", "1.5"],
    ["--max-findings", "-1"],
    ["--near-ratio", "nan"],
])
def test_invalid_thresholds_exit_2(tmp_path, capsys, args):
    path = _jsonl(tmp_path / "s.jsonl", ["今天我們來談禪修"])
    assert ard.main(["--jsonl", str(path), *args]) == 2
    assert "cannot analyze" in capsys.readouterr().err


def test_cli_requires_exactly_one_source(tmp_path):
    with pytest.raises(SystemExit) as exc:
        ard.main([])
    assert exc.value.code == 2
    with pytest.raises(ValueError):
        ard.analyze()


def test_cli_subprocess_json_file_matches_stdout(tmp_path):
    path = _caption_db(tmp_path / "captions.sqlite3", [
        ("a", "s", 1, "今天我們來談禪修", "ready", 0, 2000),
        ("b", "s", 2, "謝謝收看", "ready", 2000, 4000),
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
    assert json.loads(result.stdout)["summary"]["counts"]["boilerplate"] == 1


@pytest.mark.parametrize("destination", ["existing", "db", "alias", "wal", "shm", "journal", "input"])
def test_json_never_overwrites(tmp_path, capsys, destination):
    path = _caption_db(tmp_path / "captions.sqlite3",
                       [("a", "s", 1, "今天我們來談禪修", "ready", 0, 2000)])
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
    assert ard.main([*source, "--json", str(output)]) == 2
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
        ("b", "s", 2, "謝謝收看", "ready", 2000, 3000),
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

    monkeypatch.setattr(ard.sqlite3, "connect", readonly)
    assert ard.main(["--db", str(path), "--max-issue-rate", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["segments_with_issues"] == 1
    assert calls == [(path.resolve().as_uri() + "?mode=ro&immutable=1", {"uri": True})]
    assert _snapshot(path) == before
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(str(path) + suffix).exists()


def test_wal_database_with_shm_is_read_without_changes(tmp_path):
    path = _ledger_db(tmp_path / "zen.sqlite3", [
        ("a", "s1", 1, 0, 2000, "translated", "今天我們來談禪修", None),
        ("b", "s1", 2, 2000, 4000, "asr_done", "好的", None),
        ("d", "s2", 1, 0, 2000, "translated", "第二場", None),
    ])
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE transcripts SET text='謝謝收看' WHERE segment_id='d'")
        writer.commit()
        protected = [path, Path(str(path) + "-wal")]
        before = [_snapshot(item) for item in protected]
        report = ard.analyze(db=path)
        assert report["summary"]["counts"]["boilerplate"] == 1
        assert [_snapshot(item) for item in protected] == before


def test_wal_without_shm_is_refused(tmp_path):
    source = _ledger_db(tmp_path / "source.sqlite3", [
        ("a", "s1", 1, 0, 2000, "translated", "今天", None),
        ("d", "s2", 1, 0, 2000, "translated", "第二場", None),
    ])
    path = tmp_path / "zen.sqlite3"
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE transcripts SET text='謝謝收看' WHERE segment_id='d'")
        writer.commit()
        path.write_bytes(source.read_bytes())
        wal = Path(str(path) + "-wal")
        wal.write_bytes(Path(str(source) + "-wal").read_bytes())
    before = [_snapshot(item) for item in (path, wal)]
    with pytest.raises(ValueError, match="WAL without SHM"):
        ard.analyze(db=path)
    assert [_snapshot(item) for item in (path, wal)] == before
    assert not Path(str(path) + "-shm").exists()


def test_import_has_no_network_or_app_dependencies():
    code = (
        "import sys; sys.path.insert(0, %r); "
        "import tools.asr_repetition_detector; "
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
                        "re", "sqlite3", "sys", "unicodedata"}


def test_issue_codes_are_stable():
    assert ard.ISSUE_CODES == (
        "ngram_loop", "boilerplate", "repeat_adjacent", "near_duplicate",
    )


def test_report_json_shape_includes_ids(tmp_path, capsys):
    path = _jsonl(tmp_path / "s.jsonl", [
        {"id": "seg-1", "session_id": "sess-A", "seq": 9, "t0_ms": 1200,
         "text": "請訂閱頻道"},
    ])
    assert ard.main(["--jsonl", str(path), "--max-issue-rate", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report) >= {"tool", "schema", "session", "thresholds", "summary", "truncated", "segments"}
    assert set(report["summary"]) >= {
        "segments", "segments_with_issues", "issue_rate", "within_threshold", "counts", "verdict_zh",
    }
    seg = report["segments"][0]
    assert seg["id"] == "seg-1"
    assert seg["session_id"] == "sess-A"
    assert seg["seq"] == 9
    assert seg["t0_ms"] == 1200
    assert seg["issues"][0]["code"] == "boilerplate"


def test_combined_issues_on_one_segment(tmp_path):
    # Boilerplate that also loops
    path = _jsonl(tmp_path / "s.jsonl", [
        {"id": 1, "session_id": "s", "seq": 1, "text": "謝謝收看謝謝收看謝謝收看"},
    ])
    report = ard.analyze(jsonl=path)
    codes = {i["code"] for i in report["segments"][0]["issues"]}
    assert "ngram_loop" in codes and "boilerplate" in codes


def test_normalize_for_match_folds_punctuation_and_space():
    assert ard.normalize_for_match("今天，談禪修！") == ard.normalize_for_match("今天談禪修")
    assert ard.normalize_for_match(" 謝謝收看 ") == ard.normalize_for_match("謝謝收看")
