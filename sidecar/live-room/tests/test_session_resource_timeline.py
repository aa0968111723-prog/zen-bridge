"""Tests for tools/session_resource_timeline.py (queue item 39).

Every input is a small fake typeperf / custom CSV or caption JSONL under
tmp_path, written with explicit encodings.  Nothing depends on a real typeperf,
the network, ports, POSIX permission bits, the local time zone or the clock.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "session_resource_timeline.py"

sys.path.insert(0, str(ROOT / "tools"))
import session_resource_timeline as srt  # noqa: E402

TPE = timezone(timedelta(hours=8))
BASE = datetime(2026, 10, 17, 19, 0, 0)          # naive Taipei wall clock (typeperf style)
EPOCH0 = BASE.replace(tzinfo=TPE).timestamp()     # same instant as epoch seconds
HOST = "\\\\DESKTOP-P8RGA3A"
PDH = '(PDH-CSV 4.0) (Taipei Standard Time)(-480)'
CPU_EN = f"{HOST}\\Processor(_Total)\\% Processor Time"
RAM_EN = f"{HOST}\\Memory\\Available MBytes"
WS_EN = f"{HOST}\\Process(python)\\Working Set"
REPORT_KEYS = {
    "tool", "schema_version", "status", "exit_code", "correlated", "rising", "settings",
    "display_utc_offset", "offset_source", "inputs", "timeline", "resources", "captions", "pressure",
    "comparison", "correlation", "warnings", "notes", "suggestions", "summary_zh",
}


# --------------------------------------------------------------------------- builders
def _q(cell: str) -> str:
    return '"' + cell.replace('"', '""') + '"'


def typeperf_text(rows: list[list[str]], headers: list[str], first: str = PDH) -> str:
    out = [",".join(_q(h) for h in [first, *headers])]
    for r in rows:
        out.append(",".join(_q(c) for c in r))
    return "\r\n".join(out) + "\r\n"


def tp_time(i: float) -> str:
    t = BASE + timedelta(seconds=i)
    return t.strftime("%m/%d/%Y %H:%M:%S.") + f"{t.microsecond // 1000:03d}"


def perf_rows(n: int = 120, cpu=None, ram=None, ws=None) -> list[list[str]]:
    rows = []
    for i in range(n):
        row = [tp_time(i)]
        if cpu is not None:
            row.append(cpu(i))
        if ram is not None:
            row.append(ram(i))
        if ws is not None:
            row.append(ws(i))
        rows.append(row)
    return rows


def write_perf(tmp_path: Path, *, n=120, cpu_hot=(40, 60), ram_low=(80, 100), name="perf.csv",
               encoding="utf-16", blank_first=True) -> Path:
    def cpu(i):
        if blank_first and i == 0:
            return " "
        return "95.5" if cpu_hot[0] <= i < cpu_hot[1] else "40.0"

    def ram(i):
        return "800" if ram_low[0] <= i < ram_low[1] else "3000"

    def ws(i):
        return str((1500 + i) * 1024 * 1024)

    text = typeperf_text(perf_rows(n, cpu, ram, ws), [CPU_EN, RAM_EN, WS_EN])
    p = tmp_path / name
    p.write_text(text, encoding=encoding, newline="")
    return p


def caption_events(n=120, step=2, hot=((40, 60), (80, 100)), hot_ms=3000, cold_ms=1200, draft_ms=900):
    events = []
    for i in range(0, n, step):
        lat = hot_ms if any(a <= i < b for a, b in hot) else cold_ms
        events.append({"at_ms": int((EPOCH0 + i) * 1000), "type": "final", "latency_ms": lat})
        events.append({"at_ms": int((EPOCH0 + i + 0.5) * 1000), "type": "draft", "latency_ms": draft_ms})
    return events


def write_jsonl(tmp_path: Path, events, name="live.jsonl", extra_lines=()) -> Path:
    p = tmp_path / name
    lines = [json.dumps(e, ensure_ascii=False) if not isinstance(e, str) else e for e in events]
    lines.extend(extra_lines)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def run(tmp_path: Path, csvs, caps, *extra: str) -> tuple[int, dict]:
    out = tmp_path / f"report-{len(list(tmp_path.glob('report-*.json')))}.json"
    argv = []
    for c in csvs:
        argv += ["--csv", str(c)]
    for c in caps:
        argv += ["--captions", str(c)]
    code = srt.main([*argv, "--json", str(out), *extra])
    report = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    return code, report


def warn_codes(report: dict) -> set[str]:
    return {w["code"] for w in report["warnings"]}


# --------------------------------------------------------------------------- helpers
def test_percentile_nearest_rank():
    assert srt.percentile([], 0.95) is None
    assert srt.percentile([5], 0.5) == 5
    vals = list(range(1, 101))
    assert srt.percentile(vals, 0.95) == 95
    assert srt.percentile(vals, 0.5) == 50
    assert srt.percentile([3, 1, 2], 1.0) == 3


@pytest.mark.parametrize("text,minutes", [
    ("+08:00", 480), ("+0800", 480), ("-05:30", -330), ("UTC+8", 480), ("+9", 540), ("-00:00", 0),
])
def test_parse_offset_ok(text, minutes):
    assert srt.parse_offset(text) == minutes


@pytest.mark.parametrize("text", ["", "8", "+15:00", "+08:75", "Taipei", "++08"])
def test_parse_offset_bad(text):
    with pytest.raises(ValueError):
        srt.parse_offset(text)


def test_fmt_offset_roundtrip():
    for m in (480, -330, 0, 540):
        assert srt.parse_offset(srt.fmt_offset(m)) == m


def test_parse_time_typeperf_and_iso_and_epoch():
    assert srt.parse_time("10/17/2026 19:00:00.000", 480) == EPOCH0
    assert srt.parse_time('"10/17/2026 19:00:01.500"', 480) == EPOCH0 + 1.5
    assert srt.parse_time("10/17/2026 20:00:00", 540) == EPOCH0  # same instant, UTC+9
    assert srt.parse_time("2026-10-17T11:00:00Z", 0) == EPOCH0
    assert srt.parse_time("2026-10-17T19:00:00+08:00", -300) == EPOCH0  # explicit offset wins
    assert srt.parse_time("2026-10-17 19:00:00", 480) == EPOCH0
    assert srt.parse_time(EPOCH0, 0) == EPOCH0
    assert srt.parse_time(EPOCH0 * 1000, 0) == EPOCH0
    assert srt.parse_time(str(int(EPOCH0)), 0) == int(EPOCH0)
    for bad in ("", " ", "abc", None, True, 12.5, "13/45/2026 99:00:00"):
        assert srt.parse_time(bad, 480) is None


def test_parse_number_tolerates_blanks():
    assert srt.parse_number(" ") is None
    assert srt.parse_number("") is None
    assert srt.parse_number('"12.5"') == 12.5
    assert srt.parse_number("1,234.5") == 1234.5
    assert srt.parse_number("nan") is None
    assert srt.parse_number(True) is None
    assert srt.parse_number(7) == 7.0


def test_classify_processor_is_not_process():
    c = srt.classify_column(1, CPU_EN, ())
    assert c is not None and c.metric == "cpu" and c.is_total
    assert srt.classify_column(2, f"{HOST}\\Process(python)\\% Processor Time", ()) is None
    ws = srt.classify_column(3, WS_EN, ())
    assert ws.metric == "ws" and ws.scale == pytest.approx(1 / (1024 * 1024))


@pytest.mark.parametrize("header,scale", [
    (f"{HOST}\\Memory\\Available MBytes", 1.0),
    (f"{HOST}\\Memory\\Available KBytes", 1 / 1024),
    (f"{HOST}\\Memory\\Available Bytes", 1 / (1024 * 1024)),
    (f"{HOST}\\記憶體\\可用 MB", 1.0),
    (f"{HOST}\\記憶體\\可用位元組", 1 / (1024 * 1024)),
    ("available_ram_mb", 1.0),
    ("available_ram_gb", 1024.0),
    ("available_ram_bytes", 1 / (1024 * 1024)),
    ("avail_kb", 1 / 1024),
])
def test_ram_unit_scaling(header, scale):
    c = srt.classify_column(1, header, ())
    assert c is not None and c.metric == "ram"
    assert c.scale == pytest.approx(scale)


# --------------------------------------------------------------------------- CSV parsing
@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-8-sig", "utf-8"])
def test_typeperf_encodings(tmp_path, encoding):
    p = write_perf(tmp_path, encoding=encoding)
    res = srt.read_resource_csv(p, offset_override=None, processes=())
    assert res.fmt == "typeperf"
    assert res.offset_min == 480 and res.offset_source == "pdh_header"
    assert res.pdh_timezone == "Taipei Standard Time"
    assert len(res.series["cpu"]) == 119  # first CPU cell is blank " "
    assert len(res.series["ram"]) == 120
    assert res.blank_cells == 1
    assert res.series["ram"][0] == (EPOCH0, 3000.0)
    assert res.series["ws"][0][1] == pytest.approx(1500.0)


def test_pdh_bias_other_zone(tmp_path):
    text = typeperf_text([["10/17/2026 20:00:00.000", "50", "2000"]], [CPU_EN, RAM_EN],
                         first="(PDH-CSV 4.0) (Tokyo Standard Time)(-540)")
    p = tmp_path / "jp.csv"
    p.write_text(text, encoding="utf-8")
    res = srt.read_resource_csv(p, offset_override=None, processes=())
    assert res.offset_min == 540
    assert res.series["cpu"][0][0] == EPOCH0


def test_utc_offset_override_beats_header(tmp_path):
    p = write_perf(tmp_path, n=3)
    res = srt.read_resource_csv(p, offset_override=0, processes=())
    assert res.offset_source == "--utc-offset"
    assert res.series["ram"][0][0] == EPOCH0 + 8 * 3600


def test_zh_tw_localized_counters_cp950(tmp_path):
    headers = [f"{HOST}\\處理器(_Total)\\% 處理器時間", f"{HOST}\\記憶體\\可用 MB",
               f"{HOST}\\處理程序(python)\\工作集"]
    rows = [[tp_time(i), "50", "2048", str(1024 * 1024 * 1024)] for i in range(5)]
    p = tmp_path / "zh.csv"
    p.write_bytes(typeperf_text(rows, headers, first="(PDH-CSV 4.0) (台北標準時間)(-480)").encode("cp950"))
    res = srt.read_resource_csv(p, offset_override=None, processes=())
    assert res.pdh_timezone == "台北標準時間"
    assert res.columns["cpu"] == [headers[0]] and res.columns["ram"] == [headers[1]]
    assert res.series["ws"][0][1] == pytest.approx(1024.0)
    assert len(res.series["cpu"]) == 5


def test_ja_localized_counters(tmp_path):
    headers = [f"{HOST}\\Processor(_Total)\\% プロセッサ時間", f"{HOST}\\Memory\\利用可能 MB",
               f"{HOST}\\プロセス(python)\\ワーキング セット"]
    rows = [[tp_time(i), "70", "1500", str(512 * 1024 * 1024)] for i in range(4)]
    p = tmp_path / "ja.csv"
    p.write_text(typeperf_text(rows, headers), encoding="utf-16")
    res = srt.read_resource_csv(p, offset_override=None, processes=())
    assert [len(res.series[k]) for k in ("cpu", "ram", "ws")] == [4, 4, 4]
    assert res.series["ws"][0][1] == pytest.approx(512.0)


def test_per_core_cpu_averaged_when_no_total(tmp_path):
    headers = [f"{HOST}\\Processor(0)\\% Processor Time", f"{HOST}\\Processor(1)\\% Processor Time", RAM_EN]
    rows = [[tp_time(0), "100", "50", "4000"], [tp_time(1), "80", "60", "4000"]]
    p = tmp_path / "cores.csv"
    p.write_text(typeperf_text(rows, headers), encoding="utf-8")
    res = srt.read_resource_csv(p, offset_override=None, processes=())
    assert [v for _, v in res.series["cpu"]] == [75.0, 70.0]


def test_total_cpu_preferred_over_cores(tmp_path):
    headers = [f"{HOST}\\Processor(0)\\% Processor Time", CPU_EN, RAM_EN]
    rows = [[tp_time(0), "100", "33", "4000"]]
    p = tmp_path / "t.csv"
    p.write_text(typeperf_text(rows, headers), encoding="utf-8")
    res = srt.read_resource_csv(p, offset_override=None, processes=())
    assert res.columns["cpu"] == [CPU_EN]
    assert res.series["cpu"][0][1] == 33.0


def test_working_set_summed_and_process_filter(tmp_path):
    headers = [CPU_EN, RAM_EN, f"{HOST}\\Process(python)\\Working Set", f"{HOST}\\Process(python#1)\\Working Set",
               f"{HOST}\\Process(chrome)\\Working Set", f"{HOST}\\Process(_Total)\\Working Set",
               f"{HOST}\\Process(python)\\Working Set Peak"]
    mb = 1024 * 1024
    rows = [[tp_time(0), "10", "4000", str(100 * mb), str(50 * mb), str(400 * mb), str(9000 * mb), str(999 * mb)]]
    p = tmp_path / "ws.csv"
    p.write_text(typeperf_text(rows, headers), encoding="utf-8")
    res = srt.read_resource_csv(p, offset_override=None, processes=())
    assert res.series["ws"][0][1] == pytest.approx(550.0)  # python + python#1 + chrome, no _Total / peak
    res = srt.read_resource_csv(p, offset_override=None, processes=("python",))
    assert res.series["ws"][0][1] == pytest.approx(150.0)


def test_custom_csv_iso_and_epoch(tmp_path):
    p = tmp_path / "custom.csv"
    p.write_text("timestamp,cpu_pct,available_ram_mb,working_set_mb\n"
                 "2026-10-17T19:00:00+08:00,12.5,3000,800\n"
                 "2026-10-17 19:00:01,95,900,810\n"
                 f"{int(EPOCH0) + 2},40,,820\n", encoding="utf-8")
    res = srt.read_resource_csv(p, offset_override=None, processes=())
    assert res.fmt == "custom" and res.offset_source == "default" and res.offset_min == 480
    assert res.series["cpu"] == [(EPOCH0, 12.5), (EPOCH0 + 1, 95.0), (EPOCH0 + 2, 40.0)]
    assert res.series["ram"] == [(EPOCH0, 3000.0), (EPOCH0 + 1, 900.0)]
    assert res.blank_cells == 1
    assert res.series["ws"][-1][1] == 820.0


def test_custom_csv_units(tmp_path):
    p = tmp_path / "u.csv"
    p.write_text("time,cpu,available_ram_gb,rss_bytes\n"
                 f"{int(EPOCH0 * 1000)},50,0.5,{256 * 1024 * 1024}\n", encoding="utf-8")
    res = srt.read_resource_csv(p, offset_override=None, processes=())
    assert res.series["ram"] == [(EPOCH0, 512.0)]
    assert res.series["ws"][0][1] == pytest.approx(256.0)


def test_typeperf_status_lines_are_not_bad_rows(tmp_path):
    rows = perf_rows(5, cpu=lambda i: "50", ram=lambda i: "3000")
    text = typeperf_text(rows, [CPU_EN, RAM_EN]) + "\r\nExiting, please wait...\r\nThe command completed successfully.\r\n"
    p = tmp_path / "tp.csv"
    p.write_text(text, encoding="utf-8")
    res = srt.read_resource_csv(p, offset_override=None, processes=())
    assert res.message_lines == 2 and res.bad_rows == 0 and res.rows == 5


# --------------------------------------------------------------------------- caption parsing
def test_caption_field_variants(tmp_path):
    t = EPOCH0 + 10
    events = [
        {"ts_ms": t * 1000, "draft_latency_ms": 800, "final_latency_ms": 2000},
        {"at": t, "type": "final", "latency_s": 1.5},
        {"at": "2026-10-17T19:00:10+08:00", "status": "draft", "delay_ms": 700},
        {"at": "2026-10-17 19:00:10", "kind": "partial", "lag_ms": 650},
        {"wall_ms": t * 1000, "type": "caption", "t1_wall_ms": t * 1000 - 2500},
        {"at_ms": t * 1000, "speech_start_ms": 1000, "first_token_ms": 2200},
        {"updated_at": t, "end_to_end_ms": 3000},
        {"type": "start"},
        {"at": t, "type": "pause"},
        {"at": t, "latency_ms": -5},
        {"at": t, "latency_ms": "abc"},
        {"at": t, "latency_ms": 10_000_000},
        {"at": 12.5, "latency_ms": 100},
    ]
    p = write_jsonl(tmp_path, events)
    res, samples = srt.read_captions(p, 480)
    got = [(s.kind, s.latency_ms, s.source) for s in samples]
    assert got == [
        ("draft", 800.0, "draft_latency_ms"), ("final", 2000.0, "final_latency_ms"),
        ("final", 1500.0, "latency_s"), ("draft", 700.0, "delay_ms"), ("draft", 650.0, "lag_ms"),
        ("final", 2500.0, "wall-t1_wall_ms"), ("draft", 1200.0, "first_token_ms-speech_start_ms"),
        ("other", 3000.0, "end_to_end_ms"),
    ]
    assert all(s.t == pytest.approx(t) for s in samples)
    assert res.no_timestamp == 2 and res.no_latency == 1 and res.invalid_latency == 3
    assert res.bad_lines == 0 and res.samples == 8


def test_caption_bad_lines_counted_bom_ok(tmp_path):
    p = tmp_path / "bom.jsonl"
    body = json.dumps({"at": EPOCH0, "latency_ms": 100}) + "\n{broken\n[1,2]\n\n"
    p.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    res, samples = srt.read_captions(p, 480)
    assert len(samples) == 1 and res.bad_lines == 2 and res.lines == 3


def test_caption_non_utf8_is_input_error(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_bytes('{"at": 1, "text": "禪"}'.encode("cp950") + b"\xff\xfe\xfa\n"[2:])
    with pytest.raises(srt.InputError):
        srt.read_captions(p, 480)


# --------------------------------------------------------------------------- end to end
def test_correlated_cpu_and_ram_exit_1(tmp_path):
    code, r = run(tmp_path, [write_perf(tmp_path)], [write_jsonl(tmp_path, caption_events())])
    assert code == 1 == r["exit_code"] and r["status"] == "correlated" and r["correlated"]
    assert set(r) == REPORT_KEYS
    assert {"reason": "ram", "kind": "final"} in r["rising"]
    assert {"reason": "cpu", "kind": "final"} in r["rising"]
    assert all(x["kind"] != "draft" for x in r["rising"])
    final_any = r["comparison"]["any"]["final"]
    assert final_any["inside"]["median_ms"] == 3000 and final_any["outside"]["median_ms"] == 1200
    assert final_any["median_ratio"] == 2.5 and final_any["rises"]
    assert r["comparison"]["any"]["draft"]["rises"] is False
    assert r["pressure"]["window_count"] == 2
    assert r["pressure"]["ram_windows"] == 1 and r["pressure"]["cpu_windows"] == 1
    cpu_w, ram_w = r["pressure"]["windows"]
    assert cpu_w["reasons"] == ["cpu"] and cpu_w["cpu_max_pct"] == 95.5
    assert ram_w["reasons"] == ["ram"] and ram_w["ram_min_mb"] == 800.0
    assert cpu_w["start"] == "2026-10-17T19:00:39.500+08:00" and cpu_w["duration_s"] == 20.0
    assert ram_w["by_kind"]["final"] == {"n": 10, "median_ms": 3000.0}
    assert r["captions"]["covered"] == 120 and r["captions"]["under_pressure"] == 40
    assert r["warnings"] == []
    assert r["resources"]["working_set_mb"]["growth"] == 119.0
    assert r["display_utc_offset"] == "+08:00" and r["offset_source"] == "pdh_header"
    assert "資源壓力時字幕延遲同步上升" in r["summary_zh"] and "RAM 不足／定稿" in r["summary_zh"]
    assert any("高效能" in s for s in r["suggestions"]) and any("1 GB" in s for s in r["suggestions"])


def test_correlated_wins_over_warnings(tmp_path):
    caps = write_jsonl(tmp_path, caption_events(), extra_lines=["not json"])
    code, r = run(tmp_path, [write_perf(tmp_path)], [caps])
    assert code == 1 and "bad_jsonl_lines" in warn_codes(r)


def test_pressure_without_latency_rise_exit_0(tmp_path):
    caps = write_jsonl(tmp_path, caption_events(hot_ms=1250))
    code, r = run(tmp_path, [write_perf(tmp_path)], [caps])
    assert code == 0 and r["status"] == "ok" and not r["correlated"]
    assert r["pressure"]["window_count"] == 2
    assert "字幕延遲沒有明顯同步上升" in r["summary_zh"]
    assert r["suggestions"] == []


def test_no_pressure_exit_0(tmp_path):
    perf = write_perf(tmp_path, cpu_hot=(0, 0), ram_low=(0, 0))
    code, r = run(tmp_path, [perf], [write_jsonl(tmp_path, caption_events())])
    assert code == 0 and r["pressure"]["window_count"] == 0 and r["pressure"]["total_s"] == 0
    assert "期間沒有資源壓力" in r["summary_zh"]


def test_thresholds_are_strict_and_configurable(tmp_path):
    perf = write_perf(tmp_path, cpu_hot=(0, 0), ram_low=(0, 0))  # cpu 40, ram 3000
    caps = write_jsonl(tmp_path, caption_events(hot=()))
    code, r = run(tmp_path, [perf], [caps], "--cpu-high", "40", "--ram-low-mb", "3000")
    assert r["pressure"]["window_count"] == 0  # 40 is not > 40, 3000 is not < 3000
    code, r = run(tmp_path, [perf], [caps], "--ram-low-mb", "3001")
    assert r["pressure"]["ram_windows"] == 1 and r["captions"]["under_pressure"] == 120
    assert code == 2 and "insufficient_samples" in warn_codes(r)  # nothing left outside pressure


def test_insufficient_samples_exit_2(tmp_path):
    events = [e for e in caption_events() if not (40 <= (e["at_ms"] / 1000 - EPOCH0) < 100)]
    events += [{"at_ms": int((EPOCH0 + 50) * 1000), "type": "final", "latency_ms": 9000}]
    code, r = run(tmp_path, [write_perf(tmp_path)], [write_jsonl(tmp_path, events)])
    assert code == 2 and r["status"] == "warn"
    assert "insufficient_samples" in warn_codes(r)
    assert r["comparison"]["any"]["final"]["sufficient"] is False
    assert "樣本不足" in r["summary_zh"]


def test_min_samples_option(tmp_path):
    events = [e for e in caption_events() if not (44 <= (e["at_ms"] / 1000 - EPOCH0) < 100)]
    code, r = run(tmp_path, [write_perf(tmp_path)], [write_jsonl(tmp_path, events)])
    assert r["comparison"]["cpu"]["final"]["inside"]["n"] == 2 and code == 2
    code, r = run(tmp_path, [write_perf(tmp_path)], [write_jsonl(tmp_path, events)], "--min-samples", "2")
    assert code == 1


def test_rise_ratio_and_min_ms_options(tmp_path):
    perf, caps = write_perf(tmp_path), write_jsonl(tmp_path, caption_events())
    assert run(tmp_path, [perf], [caps], "--rise-ratio", "3")[0] == 0
    assert run(tmp_path, [perf], [caps], "--rise-min-ms", "2000")[0] == 0
    assert run(tmp_path, [perf], [caps], "--rise-ratio", "2.5", "--rise-min-ms", "1800")[0] == 1


def test_ram_only_csv_warns_missing_cpu(tmp_path):
    rows = perf_rows(120, ram=lambda i: "800" if 80 <= i < 100 else "3000")
    p = tmp_path / "ram.csv"
    p.write_text(typeperf_text(rows, [RAM_EN]), encoding="utf-8")
    code, r = run(tmp_path, [p], [write_jsonl(tmp_path, caption_events(hot=((80, 100),)))])
    assert code == 1 and "missing_cpu" in warn_codes(r) and "cpu" not in r["comparison"]
    caps2 = write_jsonl(tmp_path, caption_events(hot=()), name="flat.jsonl")
    code, r = run(tmp_path, [p], [caps2])
    assert code == 2 and r["status"] == "warn" and warn_codes(r) == {"missing_cpu"}


def test_separate_cpu_and_ram_files_merge(tmp_path):
    cpu_rows = perf_rows(120, cpu=lambda i: "97" if 40 <= i < 60 else "30")
    ram_rows = perf_rows(120, ram=lambda i: "3000")
    a, b = tmp_path / "cpu.csv", tmp_path / "ram.csv"
    a.write_text(typeperf_text(cpu_rows, [CPU_EN]), encoding="utf-8")
    b.write_text(typeperf_text(ram_rows, [RAM_EN]), encoding="utf-16")
    code, r = run(tmp_path, [a, b], [write_jsonl(tmp_path, caption_events(hot=((40, 60),)))])
    assert code == 1 and r["warnings"] == []
    assert r["rising"] == [{"reason": "any", "kind": "final"}, {"reason": "cpu", "kind": "final"}]


def test_wrong_timezone_low_overlap_then_fixed(tmp_path):
    p = tmp_path / "custom.csv"
    lines = ["timestamp,cpu_pct,available_ram_mb"]
    for i in range(120):
        t = BASE + timedelta(seconds=i)
        lines.append(f"{t:%Y-%m-%d %H:%M:%S},{95 if 40 <= i < 60 else 30},3000")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    caps = write_jsonl(tmp_path, caption_events(hot=((40, 60),)))
    code, r = run(tmp_path, [p], [caps], "--utc-offset", "+00:00")
    assert code == 2 and "low_overlap" in warn_codes(r) and r["captions"]["covered"] == 0
    assert any("--utc-offset" in s for s in r["suggestions"])
    code, r = run(tmp_path, [p], [caps])  # default +08:00, noted
    assert code == 1 and r["offset_source"] == "default"
    assert any("+08:00" in n for n in r["notes"])
    code, r = run(tmp_path, [p], [caps], "--utc-offset", "+08:00")
    assert code == 1 and r["offset_source"] == "--utc-offset"


def test_naive_caption_times_use_csv_offset(tmp_path):
    perf = write_perf(tmp_path)
    events = []
    for i in range(0, 120, 2):
        t = BASE + timedelta(seconds=i)
        lat = 3000 if (40 <= i < 60 or 80 <= i < 100) else 1200
        events.append({"at": t.strftime("%Y-%m-%dT%H:%M:%S"), "type": "final", "latency_ms": lat})
    code, r = run(tmp_path, [perf], [write_jsonl(tmp_path, events)])
    assert code == 1 and r["captions"]["covered"] == 60


def test_lag_and_max_skew(tmp_path):
    perf = write_perf(tmp_path)
    # captions arrive 10 s after the pressure they suffered from
    events = caption_events(hot=((50, 70), (90, 110)))
    caps = write_jsonl(tmp_path, events)
    code, r = run(tmp_path, [perf], [caps])
    weak = r["comparison"]["any"]["final"]["median_ratio"]
    code2, r2 = run(tmp_path, [perf], [caps], "--lag-s", "10")
    assert code2 == 1 and r2["comparison"]["any"]["final"]["median_ratio"] == 2.5
    assert weak < 2.5
    # a caption far beyond the CSV end is uncovered
    far = write_jsonl(tmp_path, caption_events() + [{"at": EPOCH0 + 500, "latency_ms": 1}], name="far.jsonl")
    code, r = run(tmp_path, [perf], [far])
    assert r["captions"]["uncovered"] == 1
    code, r = run(tmp_path, [perf], [far], "--max-skew-s", "1000")
    assert r["captions"]["uncovered"] == 0


def test_window_split_on_sample_gap(tmp_path):
    rows = [[tp_time(i), "99", "3000"] for i in (0, 1, 2, 30, 31, 32)]
    p = tmp_path / "gap.csv"
    p.write_text(typeperf_text(rows, [CPU_EN, RAM_EN]), encoding="utf-8")
    code, r = run(tmp_path, [p], [write_jsonl(tmp_path, caption_events(n=40))])
    assert r["pressure"]["cpu_windows"] == 2


def test_merge_overlapping_windows():
    merged = srt.merge_windows([
        {"reason": "cpu", "start_t": 0, "end_t": 10, "peak": 95.0},
        {"reason": "ram", "start_t": 5, "end_t": 20, "peak": 700.0},
        {"reason": "cpu", "start_t": 30, "end_t": 31, "peak": 92.0},
    ])
    assert merged[0]["reasons"] == ["cpu", "ram"] and merged[0]["end_t"] == 20
    assert merged[0]["cpu_max"] == 95.0 and merged[0]["ram_min_mb"] == 700.0
    assert len(merged) == 2 and merged[1]["ram_min_mb"] is None


def test_pearson_correlation_info(tmp_path):
    code, r = run(tmp_path, [write_perf(tmp_path)], [write_jsonl(tmp_path, caption_events())])
    assert r["correlation"]["final"]["latency_vs_cpu"] > 0
    assert r["correlation"]["final"]["latency_vs_available_ram"] < 0
    assert r["correlation"]["draft"]["latency_vs_cpu"] is None  # constant latency
    assert srt.pearson([1, 2], [3, 4]) is None


def test_bad_csv_rows_warn(tmp_path):
    rows = perf_rows(120, cpu=lambda i: "40", ram=lambda i: "3000")
    rows.insert(5, ["garbage-time", "40", "3000"])
    p = tmp_path / "bad.csv"
    p.write_text(typeperf_text(rows, [CPU_EN, RAM_EN]), encoding="utf-8")
    code, r = run(tmp_path, [p], [write_jsonl(tmp_path, caption_events())])
    assert code == 2 and warn_codes(r) == {"bad_csv_rows"}
    assert r["inputs"]["csv"][0]["bad_rows"] == 1


def test_missing_caption_fields_tolerated(tmp_path):
    events = caption_events(hot_ms=1200) + [{"type": "start"}, {"at": EPOCH0, "text": "沒有延遲"}]
    code, r = run(tmp_path, [write_perf(tmp_path, cpu_hot=(0, 0), ram_low=(0, 0))], [write_jsonl(tmp_path, events)])
    assert code == 0
    assert r["captions"]["skipped"] == {"no_timestamp": 1, "no_latency": 1, "invalid_latency": 0}


def test_inputs_untouched_and_deterministic(tmp_path):
    perf, caps = write_perf(tmp_path), write_jsonl(tmp_path, caption_events())
    before = (perf.read_bytes(), caps.read_bytes())
    o1, o2 = tmp_path / "a.json", tmp_path / "b.json"
    assert srt.main(["--csv", str(perf), "--captions", str(caps), "--json", str(o1)]) == 1
    assert srt.main(["--csv", str(perf), "--captions", str(caps), "--json", str(o2)]) == 1
    assert o1.read_bytes() == o2.read_bytes()
    assert (perf.read_bytes(), caps.read_bytes()) == before


def test_human_summary_printed(tmp_path, capsys):
    perf, caps = write_perf(tmp_path), write_jsonl(tmp_path, caption_events())
    assert srt.main(["--csv", str(perf), "--captions", str(caps)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("資源壓力 × 字幕延遲時間軸：")
    assert "壓力區間：2 段" in out and "判定：" in out and "JSON 報告" not in out


# --------------------------------------------------------------------------- errors
def test_json_never_overwritten(tmp_path, capsys):
    perf, caps = write_perf(tmp_path), write_jsonl(tmp_path, caption_events())
    out = tmp_path / "exists.json"
    out.write_text("keep", encoding="utf-8")
    assert srt.main(["--csv", str(perf), "--captions", str(caps), "--json", str(out)]) == 4
    assert out.read_text(encoding="utf-8") == "keep"
    assert "拒絕覆蓋" in capsys.readouterr().err


def test_json_unwritable_exit_4(tmp_path):
    perf, caps = write_perf(tmp_path), write_jsonl(tmp_path, caption_events())
    out = tmp_path / "no-such-dir" / "r.json"
    assert srt.main(["--csv", str(perf), "--captions", str(caps), "--json", str(out)]) == 4


def _bad_inputs(tmp_path: Path) -> dict[str, tuple[Path, Path]]:
    good_csv, good_caps = write_perf(tmp_path), write_jsonl(tmp_path, caption_events())
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    blank = tmp_path / "blank.csv"
    blank.write_text("\n\n  \n", encoding="utf-8")
    no_ts = tmp_path / "nots.csv"
    no_ts.write_text("cpu_pct,available_ram_mb\n10,2000\n", encoding="utf-8")
    no_metric = tmp_path / "nometric.csv"
    no_metric.write_text("timestamp,temperature\n2026-10-17 19:00:00,50\n", encoding="utf-8")
    no_rows = tmp_path / "norows.csv"
    no_rows.write_text(typeperf_text([], [CPU_EN, RAM_EN]), encoding="utf-8")
    all_blank = tmp_path / "allblank.csv"
    all_blank.write_text(typeperf_text([[tp_time(0), " ", " "]], [CPU_EN, RAM_EN]), encoding="utf-8")
    binary = tmp_path / "bin.csv"
    binary.write_bytes(b"\x81\xff\xfe\x00\x81" * 3 + b"\x81")
    no_lat = write_jsonl(tmp_path, [{"at": EPOCH0, "text": "x"}, {"type": "start"}], name="nolat.jsonl")
    only_bad = write_jsonl(tmp_path, ["{oops", "[]"], name="onlybad.jsonl")
    return {
        "missing_csv": (tmp_path / "nope.csv", good_caps),
        "empty_csv": (empty, good_caps),
        "blank_csv": (blank, good_caps),
        "no_timestamp_col": (no_ts, good_caps),
        "no_cpu_ram_col": (no_metric, good_caps),
        "no_rows": (no_rows, good_caps),
        "all_blank_values": (all_blank, good_caps),
        "undecodable_csv": (binary, good_caps),
        "csv_is_dir": (tmp_path, good_caps),
        "missing_captions": (good_csv, tmp_path / "nope.jsonl"),
        "no_latency_events": (good_csv, no_lat),
        "only_bad_lines": (good_csv, only_bad),
    }


@pytest.mark.parametrize("case", [
    "missing_csv", "empty_csv", "blank_csv", "no_timestamp_col", "no_cpu_ram_col", "no_rows",
    "all_blank_values", "undecodable_csv", "csv_is_dir", "missing_captions", "no_latency_events",
    "only_bad_lines",
])
def test_input_errors_exit_3(tmp_path, case, capsys):
    csv_p, caps_p = _bad_inputs(tmp_path)[case]
    out = tmp_path / "r.json"
    assert srt.main(["--csv", str(csv_p), "--captions", str(caps_p), "--json", str(out)]) == 3
    assert not out.exists()
    assert "輸入錯誤" in capsys.readouterr().err


@pytest.mark.parametrize("extra", [
    [], ["--captions", "x.jsonl"], ["--csv", "x.csv"],
    ["--csv", "x.csv", "--captions", "y.jsonl", "--utc-offset", "Taipei"],
    ["--csv", "x.csv", "--captions", "y.jsonl", "--ram-low-mb", "-1"],
    ["--csv", "x.csv", "--captions", "y.jsonl", "--ram-low-mb", "nan"],
    ["--csv", "x.csv", "--captions", "y.jsonl", "--cpu-high", "101"],
    ["--csv", "x.csv", "--captions", "y.jsonl", "--cpu-high", "0"],
    ["--csv", "x.csv", "--captions", "y.jsonl", "--rise-ratio", "0.9"],
    ["--csv", "x.csv", "--captions", "y.jsonl", "--rise-min-ms", "-1"],
    ["--csv", "x.csv", "--captions", "y.jsonl", "--min-samples", "0"],
    ["--csv", "x.csv", "--captions", "y.jsonl", "--lag-s", "-2"],
    ["--csv", "x.csv", "--captions", "y.jsonl", "--max-skew-s", "0"],
    ["--csv", "x.csv", "--captions", "y.jsonl", "--min-samples", "abc"],
    ["--csv", "x.csv", "--captions", "y.jsonl", "--bogus"],
    ["--csv", "x.csv", "--captions", "y.jsonl", "--json", "x.csv"],
])
def test_argparse_errors_exit_3(extra, capsys):
    with pytest.raises(SystemExit) as exc:
        srt.main(extra)
    assert exc.value.code == 3
    assert capsys.readouterr().err


# --------------------------------------------------------------------------- CLI
def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                          encoding="utf-8", timeout=60, env={**os.environ, "PYTHONIOENCODING": "utf-8"})


def test_subprocess_cli_utf8_output(tmp_path):
    perf, caps = write_perf(tmp_path), write_jsonl(tmp_path, caption_events())
    out = tmp_path / "cli.json"
    proc = _cli("--csv", str(perf), "--captions", str(caps), "--json", str(out))
    assert proc.returncode == 1, proc.stderr
    assert "資源壓力時字幕延遲同步上升" in proc.stdout
    assert json.loads(out.read_text(encoding="utf-8"))["exit_code"] == 1


def test_subprocess_argparse_error_exit_3():
    assert _cli("--csv", "only.csv").returncode == 3


# --------------------------------------------------------------------------- hygiene
def test_stdlib_only_no_network_no_clock():
    src = TOOL.read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    stdlib = set(sys.stdlib_module_names)
    assert mods <= stdlib | {"__future__"}, mods - stdlib
    assert not mods & {"socket", "urllib", "http", "requests", "httpx", "aiohttp", "ftplib", "ssl",
                       "subprocess", "asyncio", "smtplib", "xmlrpc", "app", "time"}
    assert "8645" not in src
    for word in ("urlopen", "http://", "https://", ".now(", "utcnow", "localtime", "time.time"):
        assert word not in src


def test_shebang_and_docstring_document_exit_codes():
    src = TOOL.read_text(encoding="utf-8")
    assert src.startswith("#!/usr/bin/env python3")
    doc = ast.get_docstring(ast.parse(src)) or ""
    for code in ("``ok``", "``correlated``", "``warn``", "(input)", "(output)"):
        assert code in doc
    assert (srt.EXIT_OK, srt.EXIT_CORRELATED, srt.EXIT_WARN, srt.EXIT_INPUT, srt.EXIT_OUTPUT) == (0, 1, 2, 3, 4)
