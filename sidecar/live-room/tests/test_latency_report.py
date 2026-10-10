from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

import pytest

from app.admin import db
from app.rtf import percentile as app_percentile
from app.store import CaptionStore
from tools.latency_report import (
    _open_readonly,
    build_report,
    load_db,
    load_metrics,
    main,
    percentile,
    render_html,
    summarize,
)

EVIL = '<script>alert("x")</script> & http://evil.example/'


def _stage(report, sid):
    return next(s for s in report["stages"] if s["id"] == sid)


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "zen 延遲.sqlite3"
    db.migrate(path)
    with closing(db.connect(path)) as conn:
        conn.execute("INSERT INTO rooms(id) VALUES ('class')")
        conn.executemany(
            "INSERT INTO sessions(id, room_id, started_at, tgt_lang) VALUES (?, 'class', 1000, ?)",
            [("en-s", "en"), ("ja-s", "ja"), (EVIL, "en")],
        )
        rows = []
        for i in range(1, 11):
            seg = json.dumps({"segment_id": f"en-{i}"})
            rows += [(i, "a2_ms", 100 * i, "en-s", seg), (i, "asr_ms", 1000 + i, "en-s", seg),
                     (i, "mt_ms", 300, "en-s", seg), (i, "rtf", 0.5, "en-s", None)]
        rows += [(20, "mt_ms", 900, "ja-s", None), (21, "mt_ms", 950, "ja-s", None),
                 (22, "a2_ms", 50, EVIL, None), (23, "cpu_pct", 99, "en-s", None)]
        conn.executemany(
            "INSERT INTO metrics(ts, name, value, room_id, session_id, labels) VALUES (?,?,?,'class',?,?)", rows
        )
        conn.commit()
    return path


def test_percentiles_on_known_inputs():
    values = list(range(1, 101))
    assert percentile(values, 0.50) == pytest.approx(50.5)
    assert percentile(values, 0.95) == pytest.approx(95.05)
    assert percentile(values, 0.99) == pytest.approx(99.01)
    assert percentile([7], 0.99) == 7
    assert percentile([10, 20, 30, 40], 0.5) == pytest.approx(25)
    for p in (0.5, 0.95, 0.99):
        assert percentile([3, 1, 4, 1, 5, 9, 2, 6], p) == pytest.approx(app_percentile([3, 1, 4, 1, 5, 9, 2, 6], p))
    assert summarize([]) == {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}
    assert summarize([5, 1, 3])["max"] == 5
    with pytest.raises(ValueError):
        percentile([], 0.5)


def test_db_report_stages_e2e_rtf_and_missing_stages(ledger):
    samples, notes = load_db(ledger)
    assert notes == []
    report = build_report(samples, session="en-s")
    a2 = _stage(report, "A2")
    assert a2["count"] == 10 and a2["p50"] == pytest.approx(550) and a2["max"] == 1000
    assert _stage(report, "A4")["source"] == "asr_ms"
    assert _stage(report, "A5")["p95"] == 300
    for sid in ("A3", "A6", "A7"):
        assert _stage(report, sid)["count"] == 0 and _stage(report, sid)["p50"] is None
    assert report["e2e"]["count"] == 10
    assert report["e2e"]["p50"] == pytest.approx(550 + 1005.5 + 300)
    assert report["rtf"]["p95"] == pytest.approx(0.5)
    page = render_html(report)
    assert "n/a" in page and "A7" in page and "<svg" in page


def test_lang_filtering_en_and_ja(ledger):
    samples, _ = load_db(ledger)
    ja = build_report(samples, lang="ja")
    assert _stage(ja, "A5")["count"] == 2 and _stage(ja, "A5")["p50"] == pytest.approx(925)
    assert _stage(ja, "A2")["count"] == 0
    en = build_report(samples, lang="en")
    assert _stage(en, "A5")["count"] == 10 and _stage(en, "A5")["max"] == 300
    assert _stage(en, "A2")["count"] == 11


def test_jsonl_lang_filter_keeps_untagged_samples(tmp_path):
    path = tmp_path / "m.jsonl"
    lines = [
        {"session_id": "s", "lang": "en", "mt_ms": 100},
        {"session_id": "s", "lang": "ja-JP", "mt_ms": 900},
        {"session_id": "s", "name": "asr_ms", "value": 400},
        {"session_id": "s", "labels": {"lang": "ja"}, "name": "a7_ms", "value": 30},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n{broken\n", encoding="utf-8")
    samples, notes = load_metrics(path)
    assert notes == ["Skipped 1 malformed line(s)."]
    ja = build_report(samples, lang="ja")
    assert _stage(ja, "A5")["max"] == 900 and _stage(ja, "A4")["count"] == 1 and _stage(ja, "A7")["count"] == 1
    en = build_report(samples, lang="en")
    assert _stage(en, "A5")["max"] == 100 and _stage(en, "A7")["count"] == 0


def test_fallback_e2e_skips_draft_and_incomplete_segments(tmp_path):
    path = _write(tmp_path / "m.jsonl", "\n".join(json.dumps(x) for x in [
        {"segment_id": "1", "a2_ms": 100, "a3_ms": 5000, "asr_ms": 1000, "a7_ms": 10},
        {"segment_id": "2", "a2_ms": 200, "asr_ms": 2000, "a7_ms": 20},
        {"segment_id": "3", "a2_ms": 300, "asr_ms": 3000},
        {"a2_ms": 1, "asr_ms": 1, "a7_ms": 1},
    ]))
    e2e = build_report(load_metrics(path)[0])["e2e"]
    assert e2e["count"] == 2 and e2e["p50"] == pytest.approx((1110 + 2220) / 2)
    assert e2e["source"] == "sum of A2, A4, A7 per segment"


def test_json_export_shape_and_explicit_e2e(tmp_path):
    path = tmp_path / "export.json"
    rows = [[1.0, "e2e_ms", 2000, "class", "s"], [2.0, "e2e_ms", 4000, "class", "s"],
            [3.0, "a6_ms", "bad", "class", "s"], [4.0, "a6_ms", -5, "class", "s"], [5.0, "a6_ms", True, "class", "s"]]
    path.write_text(json.dumps({"columns": ["ts", "name", "value", "room_id", "session_id"], "rows": rows}),
                    encoding="utf-8")
    samples, _ = load_metrics(path)
    report = build_report(samples)
    assert report["e2e"]["source"] == "e2e_ms" and report["e2e"]["p50"] == pytest.approx(3000)
    assert _stage(report, "A6")["count"] == 0


def test_html_escapes_malicious_session_and_title(ledger):
    samples, _ = load_db(ledger)
    page = render_html(build_report(samples, session=EVIL), title=EVIL, source=EVIL)
    assert _stage(build_report(samples, session=EVIL), "A2")["count"] == 1
    assert "<script" not in page.lower()
    assert "http" not in page.lower()
    assert "&lt;script&gt;" in page
    assert "&amp;" in page


def test_empty_inputs_render_no_data_page(tmp_path):
    empty_db = tmp_path / "empty.sqlite3"
    sqlite3.connect(empty_db).close()
    for samples, notes in (load_db(empty_db), load_metrics(_write(tmp_path / "e.jsonl", ""))):
        assert samples == []
        page = render_html(build_report(samples), notes=notes)
        assert page.startswith("<!DOCTYPE html>") and page.rstrip().endswith("</html>")
        assert "No data." in page and "n/a" in page and "<script" not in page


def test_captions_db_has_no_latency_rows(tmp_path):
    path = tmp_path / "captions.sqlite3"
    store = CaptionStore(path)
    store.close()
    samples, notes = load_db(path)
    assert samples == [] and "no metrics table" in notes[0]


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_readonly_connection_and_unchanged_db(ledger, tmp_path):
    before = (ledger.read_bytes(), os.stat(ledger).st_mtime_ns)
    with closing(_open_readonly(ledger)) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO metrics(ts, name, value) VALUES (1, 'a2_ms', 1)")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE x(y)")
    out = tmp_path / "report.html"
    assert main(["--db", str(ledger), "--session", "en-s", "--out", str(out)]) == 0
    assert (ledger.read_bytes(), os.stat(ledger).st_mtime_ns) == before
    with closing(sqlite3.connect(ledger)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 44


def test_cli_outputs_and_exit_codes(ledger, tmp_path, capsys):
    assert main(["--db", str(ledger), "--lang", "ja", "--title", EVIL]) == 0
    page = capsys.readouterr().out
    assert page.startswith("<!DOCTYPE html>") and "<script" not in page and "http" not in page.lower()
    jsonl = _write(tmp_path / "m.jsonl", json.dumps({"a2_ms": 10}) + "\n")
    out = tmp_path / "r.html"
    assert main(["--metrics", str(jsonl), "--out", str(out)]) == 0
    assert "A2" in out.read_text(encoding="utf-8")
    bad = [
        [],
        ["--db", str(ledger), "--metrics", str(jsonl)],
        ["--db", str(tmp_path / "missing.sqlite3")],
        ["--metrics", str(tmp_path / "missing.jsonl")],
        ["--db", str(_write(tmp_path / "not-a-db.sqlite3", "x" * 200))],
        ["--db", str(ledger), "--lang", "fr"],
        ["--metrics", str(jsonl), "--out", str(jsonl)],
    ]
    for argv in bad:
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2, argv
    assert not (tmp_path / "missing.sqlite3").exists()
