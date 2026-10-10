import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import latency_report as lr

TOOL = Path(__file__).resolve().parents[1] / "tools" / "latency_report.py"


def _snap(**extra):
    """Shaped like /api/metrics: pipeline.stats() plus the server-side fields."""
    base = {
        "pending": 0, "inflight": 0, "oldest_wait_ms": 0, "last_process_ms": None, "process_ms": None,
        "rejected": 0, "missing": 0, "held": 0, "results": 0, "translate_queued": 0,
        "translate_busy": 0, "asr_idle_s": None, "backlog_audio_s": 0, "backlog_s": 0,
        "asr_rtf_last": None, "asr_rtf_p50": None, "asr_rtf_p95": None, "asr_samples": 0,
        "rtf": {"window": {}, "session": {}, "sessions": []},
        "listeners": 0, "rooms": 0, "rss_bytes": 100 * 1024 * 1024, "store_errors": 0,
    }
    base.update(extra)
    return base


def _stage(summary, key):
    return next(row for row in summary["stages"] if row["key"] == key)


def test_percentile_on_known_data():
    values = list(range(1, 11))
    assert lr.percentile(values, 0.50) == 5.5
    assert lr.percentile(values, 0.95) == pytest.approx(9.55)
    assert lr.percentile([7], 0.95) == 7.0
    assert lr.percentile([], 0.5) is None
    assert lr.summarize([10, 20, 30, 40]) == {"count": 4, "p50": 25, "p95": 38.5, "max": 40}


def test_stages_rtf_and_rss_from_real_shape():
    snaps = [_snap(process_ms=v, oldest_wait_ms=v // 10, last_process_ms=v + 5,
                   asr_rtf_last=v / 1000, asr_rtf_p95=0.5, rss_bytes=v * 1024 * 1024, listeners=3)
             for v in range(100, 1100, 100)]
    summary = lr.build_summary(snaps)
    final = _stage(summary, "asr_final")
    assert final["stage"] == "A4"
    assert (final["count"], final["p50"], final["p95"], final["max"]) == (10, 550, 955, 1000)
    assert _stage(summary, "audio_capture")["max"] == 100
    assert _stage(summary, "asr_wall")["p50"] == 555
    assert _stage(summary, "mt")["count"] == 0 and _stage(summary, "mt")["p50"] is None
    assert summary["rtf"]["count"] == 10 and summary["rtf"]["max"] == 1
    assert summary["rtf"]["window_p95_max"] == 0.5
    assert summary["rss_bytes"]["min"] == 100 * 1024 * 1024
    assert summary["rss_bytes"]["max"] == 1000 * 1024 * 1024
    assert summary["listeners_max"] == 3


def test_explicit_stage_fields_win_and_are_reported():
    summary = lr.build_summary([{"asr_draft_ms": 80, "mt_ms": 400, "broadcast_ms": 5,
                                 "end_to_end_ms": 1500, "asr_final_ms": 300, "process_ms": 999}])
    assert _stage(summary, "asr_draft")["p50"] == 80
    assert _stage(summary, "asr_final")["max"] == 300
    assert _stage(summary, "mt")["stage"] == "A5"
    assert _stage(summary, "broadcast")["count"] == 1
    assert _stage(summary, "end_to_end")["max"] == 1500


def test_missing_none_bad_and_extra_fields_are_skipped():
    snaps = [
        {},
        {"process_ms": None, "rss_bytes": None, "asr_rtf_last": "0.3"},
        {"process_ms": True, "rss_bytes": -1, "oldest_wait_ms": float("nan")},
        {"process_ms": float("inf"), "unknown": {"deep": [1, 2]}, "listeners": "many"},
        {"process_ms": 42, "rtf": None},
    ]
    summary = lr.build_summary(snaps)
    assert summary["snapshots"] == 5
    assert _stage(summary, "asr_final")["count"] == 1
    assert _stage(summary, "audio_capture")["count"] == 0
    assert summary["rtf"]["count"] == 0
    assert summary["rss_bytes"]["count"] == 0 and summary["rss_bytes"]["min"] is None
    assert summary["listeners_max"] is None
    lr.render_html(summary)


def test_empty_input_renders_no_data():
    summary = lr.build_summary([])
    assert summary["snapshots"] == 0
    assert all(row["count"] == 0 for row in summary["stages"])
    page = lr.render_html(summary)
    assert "無資料" in page


def test_html_is_self_contained():
    snaps = [_snap(process_ms=v, rss_bytes=(100 + v) * 1024 * 1024) for v in (100, 200, 300)]
    page = lr.render_html(lr.build_summary(snaps, ["a.jsonl"]))
    lowered = page.lower()
    assert "<script" not in lowered
    assert "http://" not in lowered and "https://" not in lowered
    assert "<link" not in lowered and "@import" not in lowered and "url(" not in lowered
    assert "<svg" in lowered and "<style>" in lowered
    assert "延遲報告" in page and "asr_final" in page


def test_html_escapes_string_values():
    evil = '<script src="https://x">"&\'</script>'
    page = lr.render_html(lr.build_summary([_snap(process_ms=5)], [evil]))
    assert evil not in page
    assert "<script" not in page.lower()
    assert "&lt;script src=&quot;" in page
    assert "&amp;&#x27;" in page


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_load_json_list_object_jsonl_and_bom(tmp_path):
    a = _write(tmp_path, "a.json", json.dumps([_snap(process_ms=1), _snap(process_ms=2), 3]))
    b = _write(tmp_path, "b.json", "\ufeff" + json.dumps(_snap(process_ms=3)))
    c = _write(tmp_path, "c.jsonl", "\n".join([json.dumps(_snap(process_ms=4)), "{broken", "",
                                              json.dumps(_snap(process_ms=5)), "[]"]))
    snaps, skipped = lr.load_snapshots([a, b, c])
    assert [s["process_ms"] for s in snaps] == [1, 2, 3, 4, 5]
    assert skipped == 2


def test_cli_writes_html_and_json(tmp_path, capsys):
    src = _write(tmp_path, "m.jsonl", "\n".join(json.dumps(_snap(process_ms=v, asr_rtf_last=0.2))
                                                for v in (100, 200, 300)))
    out = tmp_path / "report.html"
    assert lr.main([str(src), "-o", str(out), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["snapshots"] == 3
    assert _stage(data, "asr_final")["p50"] == 200
    assert data["rtf"]["p95"] == 0.2
    assert out.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_cli_without_json_prints_nothing(tmp_path, capsys):
    src = _write(tmp_path, "empty.jsonl", "")
    out = tmp_path / "report.html"
    assert lr.main([str(src), "-o", str(out)]) == 0
    assert capsys.readouterr().out == ""
    assert "無資料" in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("content", [None, "not json at all\nstill not", b"\xff\xfe\x00bad"])
def test_cli_exit_2_on_unreadable_input(tmp_path, capsys, content):
    src = tmp_path / "in.json"
    if isinstance(content, str):
        src.write_text(content, encoding="utf-8")
    elif isinstance(content, bytes):
        src.write_bytes(content)
    out = tmp_path / "report.html"
    assert lr.main([str(src), "-o", str(out)]) == 2
    assert "latency_report: error:" in capsys.readouterr().err
    assert not out.exists()


def test_cli_refuses_to_overwrite_input(tmp_path, capsys):
    src = _write(tmp_path, "m.json", json.dumps(_snap()))
    assert lr.main([str(src), "-o", str(src)]) == 2
    assert json.loads(src.read_text(encoding="utf-8"))["rooms"] == 0
    assert "also an input" in capsys.readouterr().err


def test_cli_missing_output_is_usage_error(tmp_path):
    with pytest.raises(SystemExit) as exc:
        lr.main([str(tmp_path / "m.json")])
    assert exc.value.code == 2


def test_script_runs_standalone(tmp_path):
    src = _write(tmp_path, "m.json", json.dumps([_snap(process_ms=10)]))
    out = tmp_path / "r.html"
    proc = subprocess.run([sys.executable, str(TOOL), str(src), "-o", str(out), "--json"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["snapshots"] == 1
    missing = subprocess.run([sys.executable, str(TOOL), str(tmp_path / "nope.json"), "-o", str(out)],
                             capture_output=True, text=True, timeout=60)
    assert missing.returncode == 2
    assert "cannot read input" in missing.stderr


def test_tool_is_offline_stdlib_only():
    source = TOOL.read_text(encoding="utf-8")
    for banned in ("import socket", "import urllib", "import http", "import subprocess",
                   "import requests", "from app", "import app", "8645"):
        assert banned not in source
