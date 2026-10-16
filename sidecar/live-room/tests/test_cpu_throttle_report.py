"""Tests for tools/cpu_throttle_report.py (queue item 34).

All CSVs are temp fixtures; nothing depends on host typeperf, network or ports.
"""
from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "cpu_throttle_report.py"

sys.path.insert(0, str(ROOT / "tools"))
import cpu_throttle_report as ctr  # noqa: E402


REPORT_KEYS = {
    "tool",
    "schema_version",
    "status",
    "exit_code",
    "csv_columns",
    "thresholds",
    "baseline",
    "sample_count",
    "sample_interval_s",
    "session_duration_s",
    "throttle_interval_count",
    "throttle_total_s",
    "throttle_fraction",
    "longest_throttle_s",
    "frequency_pct",
    "utilization_pct",
    "intervals",
    "summary_zh",
    "advice_zh",
    "notes",
}


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def _write_csv(
    path: Path,
    rows: list[dict[str, str]],
    *,
    fieldnames: list[str] | None = None,
    encoding: str = "utf-8",
) -> Path:
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    path.write_text(buf.getvalue(), encoding=encoding)
    return path


def _ts(base: datetime, seconds: float) -> str:
    return (base + timedelta(seconds=seconds)).strftime("%m/%d/%Y %H:%M:%S.%f")[:-3]


TS_COL = "(PDH-CSV 4.0) (UTC) Timestamp"
FREQ_COL = r"\\DESKTOP\Processor Information(_Total)\% Processor Performance"
UTIL_COL = r"\\DESKTOP\Processor(_Total)\% Processor Time"
FREQ_COL_ZH = r"\\筆電\Processor Information(_Total)\處理器效能"
UTIL_COL_ZH = r"\\筆電\Processor(_Total)\% 處理器時間"


def _english_rows(
    freqs: list[float],
    utils: list[float] | None = None,
    *,
    base: datetime | None = None,
    interval: float = 1.0,
) -> list[dict[str, str]]:
    base = base or datetime(2026, 10, 16, 14, 0, 0)
    utils = utils if utils is not None else [50.0] * len(freqs)
    rows = []
    for i, (f, u) in enumerate(zip(freqs, utils)):
        rows.append(
            {
                TS_COL: _ts(base, i * interval),
                FREQ_COL: f"{f:.3f}",
                UTIL_COL: f"{u:.3f}",
            }
        )
    return rows


# ---------------------------------------------------------------- column detect


def test_detect_columns_english_pdh():
    cols = ctr.detect_columns([TS_COL, FREQ_COL, UTIL_COL])
    assert cols["timestamp"] == TS_COL
    assert cols["frequency"] == FREQ_COL
    assert cols["utilization"] == UTIL_COL


def test_detect_columns_chinese_headers():
    cols = ctr.detect_columns([TS_COL, FREQ_COL_ZH, UTIL_COL_ZH])
    assert cols["frequency"] == FREQ_COL_ZH
    assert cols["utilization"] == UTIL_COL_ZH


def test_detect_columns_missing_frequency():
    cols = ctr.detect_columns([TS_COL, UTIL_COL])
    assert cols["frequency"] is None
    assert cols["utilization"] == UTIL_COL


def test_detect_columns_max_frequency_alias():
    alias = r"\\X\Processor Information(0)\% of Maximum Frequency"
    cols = ctr.detect_columns([TS_COL, alias, UTIL_COL])
    assert cols["frequency"] == alias


# ---------------------------------------------------------------- parse helpers


def test_parse_timestamp_typeperf_and_iso():
    assert ctr.parse_timestamp("10/16/2026 14:00:01.123") == datetime(
        2026, 10, 16, 14, 0, 1, 123000
    )
    assert ctr.parse_timestamp("2026-10-16T14:00:01") == datetime(2026, 10, 16, 14, 0, 1)
    assert ctr.parse_timestamp("") is None
    assert ctr.parse_timestamp("not-a-date") is None


def test_parse_number_tolerates_noise():
    assert ctr.parse_number("98.500") == 98.5
    assert ctr.parse_number('"12,345.5"') == 12345.5
    assert ctr.parse_number("  ") is None
    assert ctr.parse_number("--") is None
    assert ctr.parse_number("nan") is None


# ---------------------------------------------------------------- CSV load


def test_read_typeperf_csv_english(tmp_path: Path):
    path = _write_csv(tmp_path / "ok.csv", _english_rows([100, 99, 80, 100]))
    loaded = ctr.read_typeperf_csv(path)
    assert loaded["error"] is None
    assert len(loaded["samples"]) == 4
    assert loaded["samples"][2]["frequency_pct"] == 80.0
    assert loaded["columns"]["frequency"] == FREQ_COL


def test_read_typeperf_csv_chinese(tmp_path: Path):
    base = datetime(2026, 10, 16, 15, 0, 0)
    rows = [
        {
            TS_COL: _ts(base, i),
            FREQ_COL_ZH: f"{f:.1f}",
            UTIL_COL_ZH: "40.0",
        }
        for i, f in enumerate([100.0, 70.0, 100.0])
    ]
    path = _write_csv(tmp_path / "zh.csv", rows)
    loaded = ctr.read_typeperf_csv(path)
    assert loaded["error"] is None
    assert loaded["columns"]["frequency"] == FREQ_COL_ZH
    assert len(loaded["samples"]) == 3


def test_read_typeperf_csv_utf16(tmp_path: Path):
    path = tmp_path / "utf16.csv"
    rows = _english_rows([100.0, 95.0])
    # Write UTF-16 LE with BOM like typeperf often does.
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    path.write_bytes(buf.getvalue().encode("utf-16"))
    loaded = ctr.read_typeperf_csv(path)
    assert loaded["error"] is None
    assert len(loaded["samples"]) == 2


def test_read_missing_freq_column_unknown(tmp_path: Path):
    path = _write_csv(
        tmp_path / "nocol.csv",
        [{TS_COL: "10/16/2026 14:00:00.000", UTIL_COL: "10.0"}],
    )
    loaded = ctr.read_typeperf_csv(path)
    assert loaded["error"]
    assert "frequency" in loaded["error"].lower() or "效能" in loaded["error"]
    report = ctr.evaluate(loaded)
    assert report["status"] == "unknown"
    assert report["exit_code"] == ctr.EXIT_UNKNOWN


def test_read_empty_file_unknown(tmp_path: Path):
    path = tmp_path / "empty.csv"
    path.write_bytes(b"")
    loaded = ctr.read_typeperf_csv(path)
    assert loaded["error"]
    assert ctr.evaluate(loaded)["status"] == "unknown"


# ---------------------------------------------------------------- intervals


def test_find_throttle_intervals_contiguous():
    base = datetime(2026, 10, 16, 14, 0, 0)
    samples = []
    # 100, 90, 85, 80, 100, 70, 100  → two intervals (idx 1-3 and 5)
    freqs = [100, 90, 85, 80, 100, 70, 100]
    for i, f in enumerate(freqs):
        samples.append(
            {
                "index": i,
                "timestamp": base + timedelta(seconds=i),
                "frequency_pct": float(f),
                "utilization_pct": 50.0,
            }
        )
    intervals = ctr.find_throttle_intervals(samples, throttle_below=95)
    assert len(intervals) == 2
    assert intervals[0]["samples"] == 3
    assert intervals[0]["min_frequency_pct"] == 80.0
    assert intervals[1]["samples"] == 1
    assert intervals[1]["min_frequency_pct"] == 70.0


def test_find_throttle_intervals_none_when_all_above():
    samples = [
        {
            "index": i,
            "timestamp": datetime(2026, 10, 16, 14, 0, i),
            "frequency_pct": 100.0,
            "utilization_pct": 20.0,
        }
        for i in range(5)
    ]
    assert ctr.find_throttle_intervals(samples, throttle_below=95) == []


def test_interval_duration_uses_timestamps():
    base = datetime(2026, 10, 16, 14, 0, 0)
    samples = [
        {
            "index": i,
            "timestamp": base + timedelta(seconds=i * 2),
            "frequency_pct": 50.0 if 1 <= i <= 3 else 100.0,
            "utilization_pct": 90.0,
        }
        for i in range(6)
    ]
    intervals = ctr.find_throttle_intervals(samples, throttle_below=95, delta_s=2.0)
    assert len(intervals) == 1
    # start at t=2, end at t=6, + delta → 6s
    assert intervals[0]["duration_s"] == pytest.approx(6.0, abs=0.01)


# ---------------------------------------------------------------- classify


def test_classify_ok_no_throttle():
    assert ctr.classify([], session_s=100.0) == "ok"


def test_classify_ok_brief_throttle():
    # 3s of 100s session = 3% < 5%
    intervals = [
        {
            "duration_s": 3.0,
            "avg_utilization_pct": 90.0,
            "min_frequency_pct": 80.0,
            "avg_frequency_pct": 82.0,
            "samples": 3,
        }
    ]
    assert ctr.classify(intervals, session_s=100.0) == "ok"


def test_classify_warn_moderate():
    # 10% of session
    intervals = [
        {
            "duration_s": 10.0,
            "avg_utilization_pct": 40.0,
            "min_frequency_pct": 70.0,
            "avg_frequency_pct": 75.0,
            "samples": 10,
        }
    ]
    assert ctr.classify(intervals, session_s=100.0) == "warn"


def test_classify_fail_by_fraction():
    intervals = [
        {
            "duration_s": 25.0,
            "avg_utilization_pct": 30.0,
            "min_frequency_pct": 60.0,
            "avg_frequency_pct": 65.0,
            "samples": 25,
        }
    ]
    assert ctr.classify(intervals, session_s=100.0) == "fail"


def test_classify_fail_long_continuous_high_util():
    # Only 10% of session but one ≥60s run at high util → fail
    intervals = [
        {
            "duration_s": 60.0,
            "avg_utilization_pct": 85.0,
            "min_frequency_pct": 50.0,
            "avg_frequency_pct": 55.0,
            "samples": 60,
        }
    ]
    assert ctr.classify(intervals, session_s=600.0, high_util=80.0) == "fail"


def test_classify_long_low_util_is_warn_not_fail():
    intervals = [
        {
            "duration_s": 60.0,
            "avg_utilization_pct": 20.0,
            "min_frequency_pct": 50.0,
            "avg_frequency_pct": 55.0,
            "samples": 60,
        }
    ]
    # 60/600 = 10% → warn; high-util gate not met
    assert ctr.classify(intervals, session_s=600.0, high_util=80.0) == "warn"


# ---------------------------------------------------------------- evaluate / report


def test_evaluate_ok_report_shape(tmp_path: Path):
    path = _write_csv(tmp_path / "ok.csv", _english_rows([100.0] * 20))
    report = ctr.evaluate(ctr.read_typeperf_csv(path))
    assert set(report.keys()) >= REPORT_KEYS
    assert report["tool"] == "cpu_throttle_report"
    assert report["status"] == "ok"
    assert report["exit_code"] == 0
    assert report["throttle_interval_count"] == 0
    assert report["baseline"]["cpu"] == "AMD Ryzen 5 5600H"
    assert "高效能" in "\n".join(report["advice_zh"]) or "插電" in "\n".join(
        report["advice_zh"]
    )
    assert "正常" in report["summary_zh"]


def test_evaluate_fail_report(tmp_path: Path):
    # 30 throttled of 50 → 60%
    freqs = [50.0] * 30 + [100.0] * 20
    utils = [90.0] * 50
    path = _write_csv(tmp_path / "fail.csv", _english_rows(freqs, utils))
    report = ctr.evaluate(ctr.read_typeperf_csv(path))
    assert report["status"] == "fail"
    assert report["exit_code"] == 2
    assert report["throttle_interval_count"] >= 1
    assert report["throttle_fraction"] >= 0.2
    assert any("限速嚴重" in a or "高效能" in a for a in report["advice_zh"])


def test_evaluate_warn_report(tmp_path: Path):
    # 10 of 100 = 10%
    freqs = [100.0] * 90 + [70.0] * 10
    path = _write_csv(tmp_path / "warn.csv", _english_rows(freqs))
    report = ctr.evaluate(ctr.read_typeperf_csv(path))
    assert report["status"] == "warn"
    assert report["exit_code"] == 1


def test_format_text_contains_zh(tmp_path: Path):
    path = _write_csv(
        tmp_path / "t.csv",
        _english_rows([100.0, 60.0, 60.0, 100.0], [80.0, 80.0, 80.0, 20.0]),
    )
    text = ctr.format_text(ctr.evaluate(ctr.read_typeperf_csv(path)))
    assert "CPU 限速檢查" in text
    assert "exit code:" in text
    assert "基準" in text or "Ryzen" in text


# ---------------------------------------------------------------- CLI


def test_cli_ok_exit_zero(tmp_path: Path):
    path = _write_csv(tmp_path / "cli_ok.csv", _english_rows([100.0] * 10))
    proc = _run("--csv", str(path))
    assert proc.returncode == 0
    assert "正常" in proc.stdout or "ok" in proc.stdout


def test_cli_json_writes_new_file(tmp_path: Path):
    path = _write_csv(tmp_path / "cli.json.csv", _english_rows([100.0] * 5))
    out = tmp_path / "report.json"
    proc = _run("--csv", str(path), "--json", str(out))
    assert proc.returncode == 0
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["status"] == "ok"
    assert data["tool"] == "cpu_throttle_report"
    # stdout also has JSON when --json is used
    assert '"status"' in proc.stdout


def test_cli_json_refuses_overwrite(tmp_path: Path):
    path = _write_csv(tmp_path / "cli.ow.csv", _english_rows([100.0] * 3))
    out = tmp_path / "exists.json"
    out.write_text("{}", encoding="utf-8")
    proc = _run("--csv", str(path), "--json", str(out))
    assert proc.returncode == ctr.EXIT_USAGE
    assert "overwrite" in proc.stderr.lower() or "拒絕" in proc.stderr or "refusing" in proc.stderr.lower()
    assert out.read_text(encoding="utf-8") == "{}"


def test_cli_json_refuses_same_as_csv(tmp_path: Path):
    path = _write_csv(tmp_path / "same.csv", _english_rows([100.0] * 3))
    proc = _run("--csv", str(path), "--json", str(path))
    assert proc.returncode == ctr.EXIT_USAGE


def test_cli_missing_csv_usage():
    proc = _run()
    assert proc.returncode == ctr.EXIT_USAGE


def test_cli_nonexistent_csv(tmp_path: Path):
    proc = _run("--csv", str(tmp_path / "nope.csv"))
    assert proc.returncode == ctr.EXIT_USAGE


def test_cli_unknown_missing_column(tmp_path: Path):
    path = _write_csv(
        tmp_path / "bad.csv",
        [{TS_COL: "10/16/2026 14:00:00.000", UTIL_COL: "10.0"}],
    )
    proc = _run("--csv", str(path))
    assert proc.returncode == ctr.EXIT_UNKNOWN


def test_cli_fail_exit_two(tmp_path: Path):
    freqs = [40.0] * 40 + [100.0] * 10
    path = _write_csv(tmp_path / "cli_fail.csv", _english_rows(freqs, [90.0] * 50))
    proc = _run("--csv", str(path))
    assert proc.returncode == 2


def test_cli_custom_thresholds(tmp_path: Path):
    # All at 96 — default throttle-below 95 → ok; with 97 → all throttled
    path = _write_csv(tmp_path / "thr.csv", _english_rows([96.0] * 20))
    ok = _run("--csv", str(path), "--throttle-below", "95")
    assert ok.returncode == 0
    fail = _run("--csv", str(path), "--throttle-below", "97")
    assert fail.returncode in {1, 2}


def test_cli_invalid_throttle_below(tmp_path: Path):
    path = _write_csv(tmp_path / "inv.csv", _english_rows([100.0] * 3))
    proc = _run("--csv", str(path), "--throttle-below", "-1")
    assert proc.returncode == ctr.EXIT_USAGE


# ---------------------------------------------------------------- baseline / advice / stdlib


def test_baseline_ryzen_5600h_constants():
    assert ctr.BASELINE["base_ghz"] == 3.3
    assert ctr.BASELINE["boost_ghz"] == 4.2
    assert "5600H" in ctr.BASELINE["cpu"]
    assert any("高效能" in a for a in ctr.ADVICE_ZH)
    assert any("插電" in a for a in ctr.ADVICE_ZH)
    assert any("OBS" in a for a in ctr.ADVICE_ZH)
    assert any("溫度" in a for a in ctr.ADVICE_ZH)


def test_advice_unknown_mentions_typeperf():
    tips = ctr.advice_zh("unknown")
    assert any("typeperf" in t.lower() for t in tips)


def test_stdlib_only_imports():
    """Tool module must not import third-party packages."""
    banned = {"numpy", "pandas", "psutil", "requests", "httpx", "aiohttp", "torch"}
    for name in banned:
        assert name not in sys.modules or name not in dir(ctr)
    src = TOOL.read_text(encoding="utf-8")
    for name in banned:
        assert f"import {name}" not in src
        assert f"from {name}" not in src


def test_script_is_executable_shebang():
    text = TOOL.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env python3")
    assert TOOL.stat().st_mode & 0o111  # some execute bit


def test_session_duration_without_timestamps():
    samples = [
        {
            "index": i,
            "timestamp": None,
            "frequency_pct": 100.0,
            "utilization_pct": 10.0,
        }
        for i in range(10)
    ]
    assert ctr.session_duration_s(samples, delta_s=1.0) == pytest.approx(10.0)


def test_skips_non_numeric_freq_rows(tmp_path: Path):
    base = datetime(2026, 10, 16, 14, 0, 0)
    rows = [
        {TS_COL: _ts(base, 0), FREQ_COL: "100.0", UTIL_COL: "10.0"},
        {TS_COL: _ts(base, 1), FREQ_COL: "", UTIL_COL: "10.0"},
        {TS_COL: _ts(base, 2), FREQ_COL: "90.0", UTIL_COL: "10.0"},
    ]
    path = _write_csv(tmp_path / "skip.csv", rows)
    loaded = ctr.read_typeperf_csv(path)
    assert len(loaded["samples"]) == 2
    assert loaded["samples"][1]["frequency_pct"] == 90.0


def test_main_returns_exit_codes_directly(tmp_path: Path):
    path = _write_csv(tmp_path / "main.csv", _english_rows([100.0] * 5))
    assert ctr.main(["--csv", str(path)]) == 0
    bad = tmp_path / "missing.csv"
    assert ctr.main(["--csv", str(bad)]) == ctr.EXIT_USAGE