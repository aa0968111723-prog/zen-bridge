"""Tests for tools/draft_first_token_latency.py (stdlib tool; synthetic JSONL + SQLite)."""
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

from tools import draft_first_token_latency as dft


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "draft_first_token_latency.py"

LATENCY_DDL = """
CREATE TABLE latency_samples (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    speech_start_ms REAL,
    draft_ms REAL,
    first_token_ms REAL,
    target_lang TEXT
)
"""


def _jsonl(path: Path, items: list) -> Path:
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items),
        encoding="utf-8",
    )
    return path


def _db(path: Path, rows: list[tuple]) -> Path:
    """rows: (id, session_id, speech_start, draft, first_token, lang)"""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(LATENCY_DDL)
        conn.executemany(
            """INSERT INTO latency_samples
               (id, session_id, speech_start_ms, draft_ms, first_token_ms, target_lang)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
    return path


def _sample(speech, token, lang="en", **extra):
    row = {"speech_start_ms": speech, "first_token_ms": token, "target_lang": lang}
    row.update(extra)
    return row


# ------------------------------------------------------------------ percentiles / deltas


def test_nearest_rank_percentile_basic_and_empty():
    assert dft.nearest_rank_percentile([], 0.95) is None
    assert dft.nearest_rank_percentile([10.0], 0.50) == 10.0
    values = [float(i) for i in range(1, 21)]
    assert dft.nearest_rank_percentile(values, 0.95) == 19.0
    assert dft.nearest_rank_percentile(values, 0.90) == 18.0
    assert dft.nearest_rank_percentile(values, 0.50) == 10.0
    with pytest.raises(ValueError):
        dft.nearest_rank_percentile([1.0], 0.0)


def test_first_token_delta_and_missing():
    assert dft.first_token_delta_ms(_sample(0, 800)) == 800.0
    assert dft.first_token_delta_ms({"speech_start_ms": 0}) is None
    assert dft.first_token_delta_ms(_sample(1000, 500)) is None  # reversed


@pytest.mark.parametrize(
    "alias_row,expected",
    [
        ({"t0_ms": 0, "draft_ms": 900, "lang": "en"}, 900.0),
        ({"opening_ms": 10, "t_first_token": 810, "tgt_lang": "ja"}, 800.0),
        ({"t0": 0, "t_draft": 1500, "target_lang": "en"}, 1500.0),
        ({"speech_start": 100, "first_draft_ms": 700, "target_lang": "ja"}, 600.0),
    ],
)
def test_field_aliases(alias_row, expected):
    assert dft.first_token_delta_ms(alias_row) == expected
    assert dft._normalize_lang(dft._pick(alias_row, "target_lang")) in ("en", "ja")


def test_normalize_lang_variants():
    assert dft._normalize_lang("EN") == "en"
    assert dft._normalize_lang("japanese") == "ja"
    assert dft._normalize_lang("日本語") == "ja"
    assert dft._normalize_lang("  ") is None
    assert dft._normalize_lang(None) is None
    with pytest.raises(ValueError):
        dft._normalize_lang(1)


# ------------------------------------------------------------------ JSONL analyze


def test_analyze_jsonl_pass_and_fail(tmp_path: Path):
    path = _jsonl(
        tmp_path / "ok.jsonl",
        [
            _sample(0, 500, "en"),
            _sample(0, 800, "en"),
            _sample(0, 900, "ja"),
            _sample(0, 1200, "ja"),
        ],
    )
    report = dft.analyze(jsonl=path, budget_p95_ms=2000)
    assert report["pass"] is True
    assert report["overall"]["first_token"]["count"] == 4
    assert report["overall"]["first_token"]["p95"] == 1200
    assert "開口→第一個草稿字" in report["summary_zh"]

    slow = _jsonl(
        tmp_path / "slow.jsonl",
        [_sample(0, 500, "en")] + [_sample(0, 3000 + i, "en") for i in range(19)],
    )
    bad = dft.analyze(jsonl=slow, budget_p95_ms=2000)
    assert bad["pass"] is False
    assert bad["overall"]["first_token"]["p95"] >= 2000


def test_analyze_jsonl_session_filter(tmp_path: Path):
    path = _jsonl(
        tmp_path / "sess.jsonl",
        [
            _sample(0, 400, "en", session_id="a"),
            _sample(0, 5000, "en", session_id="b"),
        ],
    )
    report = dft.analyze(jsonl=path, session="a")
    assert report["overall"]["first_token"]["count"] == 1
    assert report["overall"]["first_token"]["p95"] == 400


def test_analyze_rejects_empty_or_unusable(tmp_path: Path):
    empty = _jsonl(tmp_path / "empty.jsonl", [])
    with pytest.raises(ValueError, match="no usable"):
        dft.analyze(jsonl=empty)
    broken = _jsonl(tmp_path / "rev.jsonl", [_sample(1000, 100, "en")])
    with pytest.raises(ValueError, match="no usable"):
        dft.analyze(jsonl=broken)


def test_analyze_requires_exactly_one_source(tmp_path: Path):
    path = _jsonl(tmp_path / "one.jsonl", [_sample(0, 100, "en")])
    with pytest.raises(ValueError, match="exactly one"):
        dft.analyze()
    with pytest.raises(ValueError, match="exactly one"):
        dft.analyze(jsonl=path, db=tmp_path / "x.db")


# ------------------------------------------------------------------ SQLite


def test_analyze_sqlite_ro(tmp_path: Path):
    db = _db(
        tmp_path / "lat.db",
        [
            ("1", "s1", 0, 700, 700, "en"),
            ("2", "s1", 0, 1100, 1100, "ja"),
            ("3", "s2", 0, 900, 900, "en"),
        ],
    )
    before = db.read_bytes()
    report = dft.analyze(db=db, session="s1", budget_p95_ms=2000)
    assert db.read_bytes() == before
    assert report["schema"] == "sqlite:latency_samples"
    assert report["overall"]["first_token"]["count"] == 2
    assert report["pass"] is True


def test_analyze_sqlite_prefers_first_token_column(tmp_path: Path):
    """When both draft_ms and first_token_ms exist, prefer first_token_ms."""
    db = _db(
        tmp_path / "both.db",
        [
            ("1", "s", 0, 5000, 600, "en"),  # draft slow, first_token fast
        ],
    )
    report = dft.analyze(db=db)
    assert report["overall"]["first_token"]["p95"] == 600


# ------------------------------------------------------------------ CLI


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_cli_jsonl_exit_codes_and_out_file(tmp_path: Path):
    ok = _jsonl(tmp_path / "ok.jsonl", [_sample(0, 400, "en"), _sample(0, 500, "ja")])
    out = tmp_path / "report.json"
    proc = _run("--jsonl", str(ok), "--json", str(out), "--budget-p95-ms", "2000")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["tool"] == "draft_first_token_latency"
    assert data["pass"] is True
    assert "開口→第一個草稿字" in data["summary_zh"]

    # refuse overwrite
    proc2 = _run("--jsonl", str(ok), "--json", str(out))
    assert proc2.returncode == 2
    assert "overwrite" in proc2.stderr.lower() or "refusing" in proc2.stderr.lower()


def test_cli_fail_on_budget(tmp_path: Path):
    slow = _jsonl(
        tmp_path / "slow.jsonl",
        [_sample(0, 500, "en")] + [_sample(0, 4000, "en") for _ in range(19)],
    )
    proc = _run("--jsonl", str(slow), "--budget-p95-ms", "2000")
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["pass"] is False


def test_cli_missing_input_exit_2(tmp_path: Path):
    proc = _run("--jsonl", str(tmp_path / "missing.jsonl"))
    assert proc.returncode == 2


def test_cli_refuses_writing_onto_input(tmp_path: Path):
    path = _jsonl(tmp_path / "in.jsonl", [_sample(0, 100, "en")])
    proc = _run("--jsonl", str(path), "--json", str(path))
    assert proc.returncode == 2


def test_tool_is_stdlib_only_no_network_imports():
    source = TOOL.read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned = {"requests", "urllib", "http", "httpx", "aiohttp", "socket"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in banned, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root not in banned, node.module
    # no app imports
    assert not re.search(r"^\s*(from|import)\s+app\b", source, re.M)


def test_tool_file_has_shebang_and_doc():
    text = TOOL.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env python3")
    assert "開口→第一個草稿字" in text or "first" in text.lower()
