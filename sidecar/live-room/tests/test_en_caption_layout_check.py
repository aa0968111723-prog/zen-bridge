import ast
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import pytest

from tools import en_caption_layout_check as ec


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "en_caption_layout_check.py"


def _snapshot(path):
    return path.read_bytes(), path.stat().st_mtime_ns


def _caption_db(path, rows):
    """rows: (id, session_id, seq, en, status) using app/store.py's real DDL."""
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
               VALUES (?, 'r', ?, ?, 1, '中文', '', ?, ?, 0, 1000, 0)""",
            rows,
        )
        conn.commit()
    return path


def _ledger_db(path):
    schema = (ROOT / "app" / "admin" / "schema.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(path)) as conn:
        for table in ("rooms", "sessions", "segments", "transcripts", "translations"):
            ddl = re.search(r"CREATE TABLE IF NOT EXISTS " + table + r" \([\s\S]*?\n\);", schema)
            assert ddl is not None
            conn.execute(ddl.group())
        conn.execute("INSERT INTO rooms(id, created_at) VALUES ('r', 0)")
        conn.executemany("INSERT INTO sessions(id, room_id, started_at) VALUES (?, 'r', 0)",
                         [("s1",), ("s2",)])
        conn.executemany(
            """INSERT INTO segments (id, session_id, room_id, seq, t0_ms, t1_ms, status, created_at)
               VALUES (?, ?, 'r', ?, 0, 1000, ?, 0)""",
            [("a", "s1", 1, "translated"), ("b", "s1", 2, "translated"),
             ("c", "s1", 3, "asr_done"), ("d", "s2", 1, "translated")],
        )
        conn.executemany(
            """INSERT INTO translations (segment_id, tgt_lang, version, is_current, text, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [("a", "en", 1, 0, "old draft with no stop", "ok"),
             ("a", "en", 2, 1, "Today we talk about meditation.", "ok"),
             ("a", "ja", 1, 1, "今日は瞑想について話します。", "ok"),
             ("b", "en", 1, 1, "Breathe slowly , then", "merged"),
             ("c", "en", 1, 1, "not translated yet", "ok"),
             ("d", "en", 1, 1, "Failed row", "error")],
        )
        conn.commit()
    return path


def _jsonl(path, items):
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items),
                    encoding="utf-8")
    return path


def _codes(text, skip=(), **limits):
    issues, _ = ec.check_caption(text, limits or None, skip)
    return [issue["code"] for issue in issues]


def _run(*args, cwd=None):
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)], capture_output=True,
                          text=True, encoding="utf-8", cwd=cwd, timeout=60)


# ------------------------------------------------------------------ single checks

@pytest.mark.parametrize("text", [
    "Today we talk about meditation.",
    "Wait... what?",
    "The U.S. rate is 3.5 percent.",
    "Meet at 10:30, then sit.",
    "Visit https://example.com today.",
    "Don’t stop now.",
    "He said “breathe” and smiled.",
    "Is that (really) so?",
    "Silence…",
    "She asked, “Why?”",
    "Mr. Smith arrived. He sat down!",
])
def test_clean_captions_have_no_issues(text):
    assert _codes(text) == []


def test_empty_caption_short_circuits():
    assert _codes("") == ["empty"]
    assert _codes("   ") == ["empty"]


def test_line_width_42_fits_in_two_lines():
    text = ("a" * 20 + " ") * 4  # four 20-char words -> two 41-char lines
    assert _codes(text.strip() + ".") == []
    _, metrics = ec.check_caption(text.strip() + ".")
    assert metrics["lines"] == 2 and metrics["longest_line"] <= 42


def test_three_wrapped_lines_is_too_many():
    text = " ".join(["meditation"] * 9) + "."  # 3 words (32 chars) per line
    codes = _codes(text)
    assert codes == ["too_many_lines"]
    _, metrics = ec.check_caption(text)
    assert metrics["lines"] == 3


def test_unbreakable_token_is_line_too_long():
    issues, _ = ec.check_caption("See " + "x" * 50 + ".")
    assert [issue["code"] for issue in issues] == ["line_too_long"]
    assert issues[0]["tokens"] == ["x" * 50 + "."]


def test_explicit_line_breaks_are_respected():
    assert _codes("A short line\nand another.") == []
    assert _codes("One\ntwo\nthree.") == ["too_many_lines"]
    assert _codes("x" * 43 + "\nshort.") == ["line_too_long"]
    assert _codes("Windows line\r\nbreaks work.") == []


def test_custom_width_and_lines():
    text = "This caption is fine at default width."
    assert _codes(text) == []
    assert _codes(text, max_line_chars=20, max_lines=1) == ["too_many_lines"]
    assert _codes(text, max_line_chars=20, max_lines=2) == []


def test_wide_characters_count_double():
    assert ec.text_width("ab") == 2
    assert ec.text_width("禪") == 2
    assert ec.text_width("e\u0301") == 1  # combining accent


@pytest.mark.parametrize("text,chars", [
    ("Today 我們 talk about Zen.", ["我", "們"]),
    ("Breathe，then sit.", ["，"]),
    ("The master said 「sit」.", ["「", "」"]),
    ("We say ありがとう here.", ["あ", "り", "が", "と", "う"]),
    ("Fullwidth ＡＢＣ letters.", ["Ａ", "Ｂ", "Ｃ"]),
])
def test_residual_cjk(text, chars):
    issues, _ = ec.check_caption(text)
    found = [issue for issue in issues if issue["code"] == "residual_cjk"]
    assert found and found[0]["chars"] == chars


@pytest.mark.parametrize("text", [
    "Hello , world.",
    "Really ?",
    "Stop here .",
    "Hello,world.",
    "Yes!No.",
    "First sentence.Second one.",
    "Note:this matters.",
    "It ( really ) works.",
])
def test_punctuation_spacing(text):
    assert "punctuation_spacing" in _codes(text)


@pytest.mark.parametrize("text", ["1,000 people came.", "Version 3.5 is here.", "e.g. this works.",
                                  "Wait ... and see.", "Ratio 3:2 holds."])
def test_punctuation_spacing_false_positives_avoided(text):
    assert "punctuation_spacing" not in _codes(text)


def test_extra_whitespace():
    assert _codes(" Leading space.") == ["extra_whitespace"]
    assert _codes("Two  spaces here.") == ["extra_whitespace"]
    assert _codes("Trailing space. ") == ["extra_whitespace"]
    assert _codes("Line one \nline two.") == ["extra_whitespace"]


@pytest.mark.parametrize("text,stray", [
    ("He said (this.", ["("]),
    ("Then it ended).", [")"]),
    ("A [note) here.", [")", "["]),
    ('He said "breathe.', ['"']),
    ("He said “breathe.", ["“"]),
    ("Closing only” here.", ["”"]),
])
def test_unbalanced_brackets(text, stray):
    issues, _ = ec.check_caption(text)
    found = [issue for issue in issues if issue["code"] == "unbalanced_brackets"]
    assert found and found[0]["chars"] == stray


def test_apostrophe_is_not_a_bracket():
    assert "unbalanced_brackets" not in _codes("The teachers’ hall isn’t open.")
    assert "unbalanced_brackets" not in _codes("It's ‘fine’ now.")


@pytest.mark.parametrize("text", ["Done..", "Wait,, now.", "Yes., fine.", "Here;; there."])
def test_doubled_punctuation(text):
    assert "doubled_punctuation" in _codes(text)


def test_ellipsis_is_not_doubled():
    assert "doubled_punctuation" not in _codes("Well... maybe....")


@pytest.mark.parametrize("text,ok", [
    ("Sit down.", True), ("Really?", True), ("Now!", True), ("And so…", True),
    ("And so...", True), ('He said "sit."', True), ("(Quietly.)", True),
    ("Then we", False), ("Breathe,", False), ("He said “sit”", False),
])
def test_sentence_final_punctuation(text, ok):
    assert ("missing_final_punctuation" not in _codes(text)) is ok


def test_skip_rules():
    assert _codes("Then we", skip=("missing_final_punctuation",)) == []
    with pytest.raises(ValueError, match="unknown rule"):
        ec.check_caption("x.", None, ("nope",))


def test_multiple_issues_in_one_caption():
    codes = _codes("今天 we sit ,  then")
    assert set(codes) == {"residual_cjk", "punctuation_spacing", "extra_whitespace",
                          "missing_final_punctuation"}


@pytest.mark.parametrize("overrides", [
    {"max_line_chars": 0}, {"max_lines": -1}, {"max_line_chars": 4.5}, {"max_lines": True},
    {"max_issue_rate": 1.5}, {"max_issue_rate": float("nan")}, {"bogus": 1},
])
def test_invalid_thresholds(overrides):
    with pytest.raises(ValueError):
        ec._thresholds(overrides)


# ------------------------------------------------------------------ inputs

def test_caption_db_ready_rows_only_and_kana_skipped(tmp_path):
    db = _caption_db(tmp_path / "captions.sqlite3", [
        ("1", "s1", 1, "Today we talk about meditation.", "ready"),
        ("2", "s1", 2, "Breathe slowly , then", "ready"),
        ("3", "s1", 3, "draft text no stop", "draft"),
        ("4", "s1", 4, "", "translate_failed"),
        ("5", "s2", 1, "今日は瞑想について話します。", "ready"),
        ("6", "s2", 2, "Second session line.", "ready"),
    ])
    before = _snapshot(db)
    report = ec.analyze(db=db)
    assert report["schema"] == "captions"
    summary = report["summary"]
    assert summary["captions"] == 3
    assert summary["skipped_non_english"] == 1
    assert summary["captions_with_issues"] == 1
    assert report["captions"][0]["id"] == "2"
    assert summary["counts"]["punctuation_spacing"] == 1
    assert summary["counts"]["missing_final_punctuation"] == 1
    assert _snapshot(db) == before
    assert not (tmp_path / "captions.sqlite3-wal").exists()
    assert not (tmp_path / "captions.sqlite3-shm").exists()
    assert not (tmp_path / "captions.sqlite3-journal").exists()


def test_caption_db_session_filter(tmp_path):
    db = _caption_db(tmp_path / "c.sqlite3", [
        ("1", "s1", 1, "bad , text", "ready"),
        ("2", "s2", 1, "Good text.", "ready"),
    ])
    report = ec.analyze(db=db, session="s2")
    assert report["summary"]["captions"] == 1
    assert report["summary"]["captions_with_issues"] == 0


def test_caption_db_ready_row_with_empty_en(tmp_path):
    db = _caption_db(tmp_path / "c.sqlite3", [("1", "s1", 1, None, "ready")])
    report = ec.analyze(db=db)
    assert report["summary"]["counts"]["empty"] == 1


def test_ledger_db_current_ok_en_translations(tmp_path):
    db = _ledger_db(tmp_path / "zen.sqlite3")
    before = _snapshot(db)
    report = ec.analyze(db=db)
    assert report["schema"] == "ledger"
    assert report["summary"]["captions"] == 2  # a (current) and b (merged); ja/asr_done/error excluded
    assert [row["id"] for row in report["captions"]] == ["b"]
    assert ec.analyze(db=db, session="s2")["summary"]["captions"] == 0
    assert _snapshot(db) == before


def test_unsupported_schema_and_missing_db(tmp_path):
    db = tmp_path / "other.sqlite3"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE t (x)")
    with pytest.raises(ValueError, match="unsupported"):
        ec.analyze(db=db)
    with pytest.raises(ValueError, match="not found"):
        ec.analyze(db=tmp_path / "missing.sqlite3")
    assert not (tmp_path / "missing.sqlite3").exists()


def test_wal_without_shm_is_refused(tmp_path):
    db = _caption_db(tmp_path / "c.sqlite3", [("1", "s1", 1, "Fine.", "ready")])
    (tmp_path / "c.sqlite3-wal").write_bytes(b"")
    with pytest.raises(ValueError, match="WAL without SHM"):
        ec.analyze(db=db)


def test_jsonl_strings_objects_lang_and_session(tmp_path):
    path = _jsonl(tmp_path / "en.jsonl", [
        "A plain string caption.",
        {"text": "Object caption , bad", "id": "x1", "session_id": "s1", "seq": 3},
        {"en": "Uses the en key.", "session_id": "s1"},
        {"text": "今日は。", "lang": "ja", "session_id": "s1"},
        {"text": "English tagged.", "lang": "en-US", "session_id": "s2"},
    ])
    report = ec.analyze(jsonl=path)
    assert report["schema"] == "jsonl"
    assert report["summary"]["captions"] == 4
    assert report["summary"]["skipped_non_english"] == 1
    flagged = report["captions"]
    assert len(flagged) == 1 and flagged[0]["id"] == "x1" and flagged[0]["seq"] == 3
    s1 = ec.analyze(jsonl=path, session="s1")
    assert s1["summary"]["captions"] == 2 and s1["summary"]["skipped_non_english"] == 1


@pytest.mark.parametrize("line,match", [
    ("{bad json", "invalid JSON"),
    ("42", "expected a JSON string or object"),
    ('{"id": 1}', "text"),
    ('{"text": "x.", "seq": true}', "seq"),
    ('{"text": "x.", "lang": 5}', "lang"),
])
def test_jsonl_errors(tmp_path, line, match):
    path = tmp_path / "bad.jsonl"
    path.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        ec.analyze(jsonl=path)


def test_jsonl_bom_and_blank_lines(tmp_path):
    path = tmp_path / "bom.jsonl"
    path.write_text('\ufeff"First."\n\n"Second."\n', encoding="utf-8")
    assert ec.analyze(jsonl=path)["summary"]["captions"] == 2


def test_analyze_argument_errors(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        ec.analyze()
    path = _jsonl(tmp_path / "a.jsonl", ["Fine."])
    with pytest.raises(ValueError, match="exactly one"):
        ec.analyze(db=path, jsonl=path)
    with pytest.raises(ValueError, match="non-negative"):
        ec.analyze(jsonl=path, max_findings=-1)


def test_issue_rate_threshold_and_truncation(tmp_path):
    path = _jsonl(tmp_path / "a.jsonl", ["Good one.", "bad one", "bad two", "Good two."])
    report = ec.analyze(jsonl=path, max_findings=1)
    summary = report["summary"]
    assert summary["issue_rate"] == 0.5 and not summary["within_threshold"]
    assert summary["counts"]["missing_final_punctuation"] == 2
    assert report["truncated"] and len(report["captions"]) == 1
    relaxed = ec.analyze(jsonl=path, thresholds={"max_issue_rate": 0.5})
    assert relaxed["summary"]["within_threshold"]
    skipped = ec.analyze(jsonl=path, skip=("missing_final_punctuation",))
    assert skipped["summary"]["captions_with_issues"] == 0
    assert skipped["skipped_rules"] == ["missing_final_punctuation"]


def test_empty_input_is_within_threshold(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    report = ec.analyze(jsonl=path)
    assert report["summary"]["captions"] == 0 and report["summary"]["within_threshold"]
    assert "沒有" in report["summary"]["verdict_zh"]


# ------------------------------------------------------------------ CLI

def test_cli_exit_0_and_json_output(tmp_path):
    path = _jsonl(tmp_path / "a.jsonl", ["Today we sit.", "Then we breathe."])
    out = tmp_path / "report.json"
    result = _run("--jsonl", path, "--json", out)
    assert result.returncode == 0, result.stderr
    stdout = json.loads(result.stdout)
    assert stdout["tool"] == "en_caption_layout_check"
    assert json.loads(out.read_text(encoding="utf-8")) == stdout


def test_cli_exit_1_on_issues(tmp_path):
    path = _jsonl(tmp_path / "a.jsonl", ["bad , caption"])
    result = _run("--jsonl", path)
    assert result.returncode == 1
    assert json.loads(result.stdout)["summary"]["captions_with_issues"] == 1
    assert _run("--jsonl", path, "--max-issue-rate", "1").returncode == 0


def test_cli_db_readonly(tmp_path):
    db = _caption_db(tmp_path / "captions.sqlite3", [("1", "s1", 1, "Fine caption.", "ready")])
    before = _snapshot(db)
    result = _run("--db", db, "--max-line-chars", "42", "--max-lines", "2")
    assert result.returncode == 0, result.stderr
    assert _snapshot(db) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["captions.sqlite3"]


def test_cli_json_never_overwrites(tmp_path):
    path = _jsonl(tmp_path / "a.jsonl", ["Fine."])
    out = tmp_path / "exists.json"
    out.write_text("keep", encoding="utf-8")
    result = _run("--jsonl", path, "--json", out)
    assert result.returncode == 2 and "refusing to overwrite" in result.stderr
    assert out.read_text(encoding="utf-8") == "keep"


def test_cli_json_must_not_target_input_or_sidecar(tmp_path):
    path = _jsonl(tmp_path / "a.jsonl", ["Fine."])
    before = path.read_bytes()
    result = _run("--jsonl", path, "--json", path)
    assert result.returncode == 2 and path.read_bytes() == before
    db = _caption_db(tmp_path / "c.sqlite3", [("1", "s1", 1, "Fine.", "ready")])
    result = _run("--db", db, "--json", tmp_path / "c.sqlite3-wal")
    assert result.returncode == 2
    assert not (tmp_path / "c.sqlite3-wal").exists()


@pytest.mark.parametrize("args", [
    ["--max-line-chars", "0"], ["--max-lines", "0"], ["--max-issue-rate", "2"],
    ["--max-findings", "-1"],
])
def test_cli_bad_options_exit_2(tmp_path, args):
    path = _jsonl(tmp_path / "a.jsonl", ["Fine."])
    result = _run("--jsonl", path, *args)
    assert result.returncode == 2 and result.stdout == ""


def test_cli_missing_input_exit_2(tmp_path):
    result = _run("--jsonl", tmp_path / "missing.jsonl")
    assert result.returncode == 2
    assert "cannot analyze" in result.stderr


def test_cli_requires_exactly_one_source_and_valid_skip(tmp_path):
    assert _run().returncode == 2
    path = _jsonl(tmp_path / "a.jsonl", ["x"])
    assert _run("--jsonl", path, "--skip", "nope").returncode == 2
    assert _run("--jsonl", path, "--skip", "missing_final_punctuation").returncode == 0


def test_cli_runs_from_any_cwd_without_app(tmp_path):
    path = _jsonl(tmp_path / "a.jsonl", ["Fine."])
    result = _run("--jsonl", path, cwd=tmp_path)
    assert result.returncode == 0, result.stderr


# ------------------------------------------------------------------ hygiene

def test_stdlib_only_no_app_no_network_imports():
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    assert modules <= {"__future__", "argparse", "contextlib", "json", "math", "pathlib",
                       "re", "sqlite3", "sys", "unicodedata"}
    source = TOOL.read_text(encoding="utf-8")
    assert "8645" not in source
    for word in ("socket", "urllib", "http.client", "requests"):
        assert f"import {word}" not in source


def test_import_does_not_load_app():
    code = ("import sys; sys.path.insert(0, %r); import tools.en_caption_layout_check; "
            "print(any(m == 'app' or m.startswith('app.') for m in sys.modules))" % str(ROOT))
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert result.stdout.strip() == "False", result.stderr
