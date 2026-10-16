"""Tests for tools/latency_slo_check.py (stdlib tool; synthetic JSONL + SQLite)."""
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

from tools import latency_slo_check as slo


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "latency_slo_check.py"

LATENCY_DDL = """
CREATE TABLE latency_samples (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    speech_start_ms REAL,
    draft_ms REAL,
    final_ms REAL,
    translated_ms REAL,
    target_lang TEXT
)
"""


def _snapshot(path: Path):
    return path.read_bytes(), path.stat().st_mtime_ns


def _jsonl(path: Path, items: list) -> Path:
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items),
                    encoding="utf-8")
    return path


def _latency_db(path: Path, rows: list[tuple]) -> Path:
    """rows: (id, session_id, speech_start, draft, final, translated, lang)"""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(LATENCY_DDL)
        conn.executemany(
            """INSERT INTO latency_samples
               (id, session_id, speech_start_ms, draft_ms, final_ms, translated_ms, target_lang)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
    return path


def _sample(speech, draft, final, translated, lang="en", **extra):
    row = {
        "speech_start_ms": speech,
        "draft_ms": draft,
        "final_ms": final,
        "translated_ms": translated,
        "target_lang": lang,
    }
    row.update(extra)
    return row


# ------------------------------------------------------------------ percentiles / stages


def test_nearest_rank_percentile_basic_and_empty():
    assert slo.nearest_rank_percentile([], 0.95) is None
    assert slo.nearest_rank_percentile([10.0], 0.50) == 10.0
    # n=20 → ceil(0.95*20)=19 → index 18
    values = [float(i) for i in range(1, 21)]
    assert slo.nearest_rank_percentile(values, 0.95) == 19.0
    assert slo.nearest_rank_percentile(values, 0.90) == 18.0
    assert slo.nearest_rank_percentile(values, 0.50) == 10.0
    with pytest.raises(ValueError):
        slo.nearest_rank_percentile([1.0], 0.0)
    with pytest.raises(ValueError):
        slo.nearest_rank_percentile([1.0], 1.5)


def test_row_stages_deltas_and_missing():
    assert slo.row_stages(_sample(0, 1000, 5000, 6200)) == {
        "draft": 1000.0, "final": 4000.0, "translation": 1200.0}
    assert slo.row_stages({"speech_start_ms": 0, "draft_ms": 500}) == {
        "draft": 500.0, "final": None, "translation": None}
    # negative / reversed clocks discarded
    assert slo.row_stages(_sample(1000, 500, 600, 700))["draft"] is None
    assert slo.row_stages(_sample(0, 1000, 900, 2000))["final"] is None
    assert slo.row_stages(_sample(0, 1000, 2000, 1500))["translation"] is None


@pytest.mark.parametrize("alias_row,expected_draft", [
    ({"t0_ms": 0, "draft_ms": 800, "final_ms": 3000, "translated_ms": 3500, "lang": "en"}, 800.0),
    ({"opening_ms": 10, "draft": 910, "final": 2910, "translation_ms": 3410, "tgt_lang": "ja"}, 900.0),
    ({"t0": 0, "t_draft": 1500, "t_final": 4000, "t_mt1": 5200, "target_lang": "en"}, 1500.0),
])
def test_field_aliases(alias_row, expected_draft):
    assert slo.row_stages(alias_row)["draft"] == expected_draft
    assert slo._normalize_lang(slo._pick(alias_row, "target_lang")) in ("en", "ja")


def test_normalize_lang_variants():
    assert slo._normalize_lang("EN") == "en"
    assert slo._normalize_lang("japanese") == "ja"
    assert slo._normalize_lang("日本語") == "ja"
    assert slo._normalize_lang("  ") is None
    assert slo._normalize_lang(None) is None
    with pytest.raises(ValueError):
        slo._normalize_lang(1)


# ------------------------------------------------------------------ JSONL analyze


def test_jsonl_en_ja_split_and_pass(tmp_path):
    # 20 en samples well under SLO; 20 ja likewise
    items = []
    for i in range(20):
        items.append(_sample(0, 500 + i, 1500 + i, 2000 + i, "en", id=f"e{i}"))
        items.append(_sample(0, 600 + i, 1600 + i, 2100 + i, "ja", id=f"j{i}"))
    path = _jsonl(tmp_path / "ok.jsonl", items)
    report = slo.analyze(jsonl=path)
    assert report["schema"] == "jsonl"
    assert report["pass"] is True
    assert report["overall"]["rows"] == 40
    assert report["by_target_language"]["en"]["rows"] == 20
    assert report["by_target_language"]["ja"]["rows"] == 20
    assert report["overall"]["stages"]["draft"]["pass"] is True
    assert "通過" in report["summary_zh"]
    assert re.search(r"[\u4e00-\u9fff]", report["summary_zh"])


def test_jsonl_slo_fail_on_draft_p95(tmp_path):
    # nearest-rank p95 for n=20 is the 19th value (ceil(0.95*20)=19 → index 18).
    # Need at least two slow samples so index 18 is slow (>2000).
    items = [_sample(0, 100, 1100, 1200, "en", id=f"f{i}") for i in range(18)]
    items += [_sample(0, 5000, 6000, 6100, "en", id=f"slow{i}") for i in range(2)]
    path = _jsonl(tmp_path / "slow.jsonl", items)
    report = slo.analyze(jsonl=path)
    assert report["pass"] is False
    assert report["overall"]["stages"]["draft"]["pass"] is False
    assert report["overall"]["stages"]["draft"]["p95"] == 5000
    assert "未通過" in report["summary_zh"]


def test_jsonl_slo_fail_final_and_translation(tmp_path):
    # final over 6s
    items = [_sample(0, 100, 7000, 7100, "en", id=f"a{i}") for i in range(20)]
    report = slo.analyze(jsonl=_jsonl(tmp_path / "f.jsonl", items))
    assert report["overall"]["stages"]["final"]["pass"] is False
    assert report["overall"]["stages"]["draft"]["pass"] is True
    # translation over 1.5s
    items2 = [_sample(0, 100, 200, 3000, "ja", id=f"b{i}") for i in range(20)]
    report2 = slo.analyze(jsonl=_jsonl(tmp_path / "t.jsonl", items2))
    assert report2["overall"]["stages"]["translation"]["pass"] is False
    assert report2["by_target_language"]["ja"]["stages"]["translation"]["pass"] is False


def test_threshold_overrides_change_pass(tmp_path):
    items = [_sample(0, 2500, 3000, 3200, "en", id=str(i)) for i in range(20)]
    path = _jsonl(tmp_path / "o.jsonl", items)
    assert slo.analyze(jsonl=path)["pass"] is False
    assert slo.analyze(jsonl=path, thresholds={"draft": 3000})["pass"] is True


def test_missing_timings_counted(tmp_path):
    items = [
        {"id": "a", "target_lang": "en", "speech_start_ms": 0, "draft_ms": 500},
        {"id": "b", "target_lang": "en", "speech_start_ms": 0, "draft_ms": 600,
         "final_ms": 2000, "translated_ms": 2500},
        {"id": "c", "target_lang": "ja"},  # all missing — still a row
    ]
    # row c has no usable stages; rows a+b do → usable
    path = _jsonl(tmp_path / "m.jsonl", items)
    report = slo.analyze(jsonl=path)
    assert report["overall"]["rows"] == 3
    assert report["overall"]["stages"]["draft"]["count"] == 2
    assert report["overall"]["stages"]["draft"]["missing"] == 1
    assert report["overall"]["stages"]["final"]["count"] == 1
    assert report["overall"]["stages"]["final"]["missing"] == 2
    assert report["overall"]["stages"]["translation"]["count"] == 1


def test_session_filter_jsonl(tmp_path):
    items = [
        _sample(0, 500, 1500, 2000, "en", id="1", session_id="s1"),
        _sample(0, 500, 1500, 2000, "en", id="2", session_id="s2"),
    ]
    path = _jsonl(tmp_path / "s.jsonl", items)
    assert slo.analyze(jsonl=path, session="s1")["overall"]["rows"] == 1
    with pytest.raises(ValueError, match="no usable"):
        slo.analyze(jsonl=path, session="nope")


def test_en_fail_does_not_hide_ja_pass(tmp_path):
    items = [_sample(0, 5000, 5500, 5600, "en", id=f"e{i}") for i in range(20)]
    items += [_sample(0, 400, 1400, 1800, "ja", id=f"j{i}") for i in range(20)]
    report = slo.analyze(jsonl=_jsonl(tmp_path / "mix.jsonl", items))
    assert report["by_target_language"]["en"]["pass"] is False
    assert report["by_target_language"]["ja"]["pass"] is True
    assert report["pass"] is False


# ------------------------------------------------------------------ SQLite


def test_latency_samples_db(tmp_path):
    path = _latency_db(tmp_path / "lat.sqlite3", [
        (f"e{i}", "s", 0, 400 + i, 1400 + i, 1800 + i, "en") for i in range(20)
    ] + [
        (f"j{i}", "s", 0, 450 + i, 1450 + i, 1850 + i, "ja") for i in range(20)
    ])
    report = slo.analyze(db=path)
    assert report["schema"] == "sqlite:latency_samples"
    assert report["pass"] is True
    assert report["overall"]["rows"] == 40


def test_db_session_filter_parameterized(tmp_path):
    session = "s' OR 1=1 --"
    path = _latency_db(tmp_path / "lat.sqlite3", [
        ("a", session, 0, 500, 1500, 2000, "en"),
        ("b", "other", 0, 5000, 7000, 9000, "en"),
    ])
    report = slo.analyze(db=path, session=session)
    assert report["overall"]["rows"] == 1
    assert report["pass"] is True
    with pytest.raises(ValueError, match="no usable"):
        slo.analyze(db=path, session="' OR 1=1 --")


def test_captions_table_with_timing_columns(tmp_path):
    path = tmp_path / "captions.sqlite3"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("""
            CREATE TABLE captions (
                id TEXT PRIMARY KEY,
                speech_start_ms REAL, draft_ms REAL, final_ms REAL,
                translated_ms REAL, target_lang TEXT, session_id TEXT
            )""")
        conn.executemany(
            "INSERT INTO captions VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(f"c{i}", 0, 300, 1300, 1600, "en", "s") for i in range(20)],
        )
        conn.commit()
    report = slo.analyze(db=path)
    assert report["schema"] == "sqlite:captions"
    assert report["pass"] is True


def test_stock_captions_without_timing_refused(tmp_path):
    path = tmp_path / "captions.sqlite3"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("""
            CREATE TABLE captions (
                id TEXT PRIMARY KEY, room_id TEXT, session_id TEXT, seq INTEGER,
                version INTEGER, zh TEXT, zh_raw TEXT, en TEXT, status TEXT,
                t0_ms INTEGER, t1_ms INTEGER, updated_at REAL
            )""")
        conn.execute(
            "INSERT INTO captions VALUES ('a','r','s',1,1,'你好','','hi','ready',0,1000,0)")
        conn.commit()
    before = _snapshot(path)
    with pytest.raises(ValueError, match="unsupported"):
        slo.analyze(db=path)
    assert _snapshot(path) == before


# ------------------------------------------------------------------ CLI exits


def test_cli_exit_0_and_1(tmp_path, capsys):
    ok = _jsonl(tmp_path / "ok.jsonl",
                [_sample(0, 400, 1400, 1800, "en", id=str(i)) for i in range(20)])
    assert slo.main(["--jsonl", str(ok)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["pass"] is True and report["tool"] == "latency_slo_check"
    bad = _jsonl(tmp_path / "bad.jsonl",
                 [_sample(0, 5000, 6000, 7000, "en", id=str(i)) for i in range(20)])
    assert slo.main(["--jsonl", str(bad)]) == 1
    assert json.loads(capsys.readouterr().out)["pass"] is False


def test_cli_threshold_flags(tmp_path, capsys):
    path = _jsonl(tmp_path / "t.jsonl",
                  [_sample(0, 2500, 3000, 3200, "en", id=str(i)) for i in range(20)])
    assert slo.main(["--jsonl", str(path)]) == 1
    capsys.readouterr()
    assert slo.main(["--jsonl", str(path), "--draft-p95-ms", "3000"]) == 0
    assert json.loads(capsys.readouterr().out)["slos_ms"]["draft"] == 3000


def test_cli_no_usable_rows_exit_2(tmp_path, capsys):
    path = _jsonl(tmp_path / "empty_timings.jsonl",
                  [{"id": "x", "target_lang": "en"}, {"id": "y", "target_lang": "ja"}])
    assert slo.main(["--jsonl", str(path)]) == 2
    assert "cannot analyze" in capsys.readouterr().err
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert slo.main(["--jsonl", str(empty)]) == 2


def test_cli_unreadable_inputs_exit_2(tmp_path, capsys):
    missing = tmp_path / "missing.sqlite3"
    assert slo.main(["--db", str(missing)]) == 2
    assert not missing.exists()
    notdb = tmp_path / "lat.sqlite3"
    notdb.write_bytes(b"not SQLite at all")
    before = _snapshot(notdb)
    assert slo.main(["--db", str(notdb)]) == 2
    assert _snapshot(notdb) == before
    other = tmp_path / "other.sqlite3"
    with closing(sqlite3.connect(other)) as conn:
        conn.execute("CREATE TABLE unrelated (id integer)")
        conn.commit()
    assert slo.main(["--db", str(other)]) == 2
    assert slo.main(["--jsonl", str(tmp_path / "nope.jsonl")]) == 2
    binary = tmp_path / "binary.jsonl"
    binary.write_bytes(b"\xff\xfe\x00bad")
    assert slo.main(["--jsonl", str(binary)]) == 2
    assert "cannot analyze" in capsys.readouterr().err


@pytest.mark.parametrize("line", [
    "{bad", "[1, 2]", '"just a string"',
    '{"target_lang": "en", "draft_ms": "slow"}',
    '{"target_lang": "en", "speech_start_ms": true}',
])
def test_invalid_jsonl_exit_2(tmp_path, capsys, line):
    path = tmp_path / "bad.jsonl"
    path.write_text(line + "\n", encoding="utf-8")
    assert slo.main(["--jsonl", str(path)]) == 2
    assert "cannot analyze" in capsys.readouterr().err


@pytest.mark.parametrize("args", [
    ["--draft-p95-ms", "-1"], ["--final-p95-ms", "nan"],
    ["--translation-p95-ms", "inf"],
])
def test_invalid_slo_flags_exit_2(tmp_path, capsys, args):
    path = _jsonl(tmp_path / "s.jsonl", [_sample(0, 400, 1400, 1800, "en", id="1")])
    assert slo.main(["--jsonl", str(path), *args]) == 2
    assert "cannot analyze" in capsys.readouterr().err


def test_cli_requires_exactly_one_source():
    with pytest.raises(SystemExit) as exc:
        slo.main([])
    assert exc.value.code == 2
    with pytest.raises(ValueError):
        slo.analyze()


def test_cli_subprocess_json_and_out_alias(tmp_path):
    path = _latency_db(tmp_path / "lat.sqlite3", [
        (f"e{i}", "s", 0, 400, 1400, 1800, "en") for i in range(20)
    ])
    output = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, str(TOOL), "--db", str(path), "--json", str(output)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == json.loads(output.read_text(encoding="utf-8"))
    out2 = tmp_path / "report2.json"
    result2 = subprocess.run(
        [sys.executable, str(TOOL), "--db", str(path), "--out", str(out2)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result2.returncode == 0
    assert out2.is_file()


@pytest.mark.parametrize("destination", ["existing", "db", "alias", "wal", "shm", "journal", "input"])
def test_json_never_overwrites(tmp_path, capsys, destination):
    path = _latency_db(tmp_path / "lat.sqlite3", [
        (f"a{i}", "s", 0, 400, 1400, 1800, "en") for i in range(5)
    ])
    source = ["--db", str(path)]
    if destination == "existing":
        output = tmp_path / "existing.json"
        output.write_text("keep", encoding="utf-8")
    elif destination == "db":
        output = path
    elif destination == "alias":
        output = path.parent / "." / path.name
    elif destination == "input":
        output = _jsonl(tmp_path / "in.jsonl",
                        [_sample(0, 400, 1400, 1800, "en", id=str(i)) for i in range(5)])
        source = ["--jsonl", str(output)]
    else:
        output = Path(str(path) + "-" + destination)
    before = _snapshot(path)
    keep = output.read_bytes() if output.exists() else None
    assert slo.main([*source, "--json", str(output)]) == 2
    captured = capsys.readouterr()
    assert "cannot analyze" in captured.err and not captured.out
    assert _snapshot(path) == before
    if keep is None:
        assert not output.exists()
    else:
        assert output.read_bytes() == keep


# ------------------------------------------------------------------ read-only / isolation


def test_read_only_uri_and_database_unchanged(tmp_path, monkeypatch, capsys):
    path = _latency_db(tmp_path / "延遲 #%.sqlite3", [
        (f"a{i}", "s", 0, 400, 1400, 1800, "en") for i in range(20)
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

    monkeypatch.setattr(slo.sqlite3, "connect", readonly)
    assert slo.main(["--db", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["pass"] is True
    assert calls == [(path.resolve().as_uri() + "?mode=ro&immutable=1", {"uri": True})]
    assert _snapshot(path) == before
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(str(path) + suffix).exists()


def test_wal_database_with_shm_is_read_without_changes(tmp_path):
    path = _latency_db(tmp_path / "lat.sqlite3", [
        (f"a{i}", "s", 0, 400, 1400, 1800, "en") for i in range(20)
    ])
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE latency_samples SET draft_ms=450 WHERE id='a0'")
        writer.commit()
        protected = [path, Path(str(path) + "-wal")]
        before = [_snapshot(item) for item in protected]
        report = slo.analyze(db=path)
        assert report["pass"] is True
        assert [_snapshot(item) for item in protected] == before


def test_wal_without_shm_is_refused(tmp_path):
    source = _latency_db(tmp_path / "source.sqlite3", [
        (f"a{i}", "s", 0, 400, 1400, 1800, "en") for i in range(5)
    ])
    path = tmp_path / "lat.sqlite3"
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE latency_samples SET draft_ms=450 WHERE id='a0'")
        writer.commit()
        path.write_bytes(source.read_bytes())
        wal = Path(str(path) + "-wal")
        wal.write_bytes(Path(str(source) + "-wal").read_bytes())
    before = [_snapshot(item) for item in (path, wal)]
    with pytest.raises(ValueError, match="WAL without SHM"):
        slo.analyze(db=path)
    assert [_snapshot(item) for item in (path, wal)] == before
    assert not Path(str(path) + "-shm").exists()


def test_import_has_no_network_or_app_dependencies():
    code = (
        "import sys; sys.path.insert(0, %r); "
        "import tools.latency_slo_check; "
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
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    allowed = {
        "argparse", "contextlib", "json", "math", "pathlib", "sqlite3", "sys",
        "__future__", "annotations",
    }
    assert imported <= allowed


def test_summary_messages_are_traditional_chinese(tmp_path):
    path = _jsonl(tmp_path / "s.jsonl",
                  [_sample(0, 400, 1400, 1800, "en", id=str(i)) for i in range(20)])
    report = slo.analyze(jsonl=path)
    for text in (report["summary_zh"], report["overall"]["summary_zh"],
                 report["by_target_language"]["en"]["summary_zh"]):
        assert re.search(r"[\u4e00-\u9fff]", text)
        assert "開口→草稿" in text or "草稿→定稿" in text


def test_unknown_language_rows_counted(tmp_path):
    items = [_sample(0, 400, 1400, 1800, "en", id="e1")]
    items.append(_sample(0, 400, 1400, 1800, "fr", id="f1"))
    report = slo.analyze(jsonl=_jsonl(tmp_path / "u.jsonl", items))
    assert report["unknown_language_rows"] == 1
    assert report["overall"]["rows"] == 2


def test_p95_max_p50_consistency(tmp_path):
    values_draft = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000,
                    1100, 1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900, 5000]
    items = [_sample(0, d, d + 100, d + 200, "en", id=str(i))
             for i, d in enumerate(values_draft)]
    report = slo.analyze(jsonl=_jsonl(tmp_path / "p.jsonl", items))
    stage = report["overall"]["stages"]["draft"]
    assert stage["p50"] == 1000
    assert stage["p90"] == 1800
    assert stage["p95"] == 1900
    assert stage["max"] == 5000
    assert stage["pass"] is True  # 1900 <= 2000


def test_cli_db_and_jsonl_mutually_exclusive(tmp_path):
    db = _latency_db(tmp_path / "lat.sqlite3", [("a", "s", 0, 400, 1400, 1800, "en")])
    jl = _jsonl(tmp_path / "s.jsonl", [_sample(0, 400, 1400, 1800, "en", id="1")])
    with pytest.raises(SystemExit) as exc:
        slo.main(["--db", str(db), "--jsonl", str(jl)])
    assert exc.value.code == 2