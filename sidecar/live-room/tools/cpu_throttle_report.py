#!/usr/bin/env python3
"""Offline typeperf / PerfMon CSV CPU throttle report (queue item 34).

  python tools/cpu_throttle_report.py --csv path/to/typeperf.csv
  python tools/cpu_throttle_report.py --csv path/to/typeperf.csv --json OUT.json
  python tools/cpu_throttle_report.py --csv path.csv --throttle-below 95 --high-util 80

Reads a Windows ``typeperf`` / Performance Monitor CSV (stdlib only, read-only,
no network). Typical columns:

* ``% Processor Performance`` — frequency % relative to base clock
  (<100 means the CPU is throttled / not at base).
* ``% Processor Time`` — utilization.

Chinese or English PDH headers are accepted. The first column is usually a
``(PDH-CSV 4.0)`` timestamp.

Contiguous intervals where frequency% is below ``--throttle-below`` (default
95) are recorded with start/end, duration, min/avg frequency%, and avg util%.

Baseline notes for AMD Ryzen 5 5600H (zen-bridge club laptop target):
base ~3.3 GHz, boost ~4.2 GHz. On battery / power-saver Windows often holds
``% Processor Performance`` well below 100.

==========  ==========================================================  ====
status      condition                                                    exit
==========  ==========================================================  ====
``ok``      no throttle intervals, or total throttle < 5% of session     0
``warn``    throttle present but moderate                                1
``fail``    throttle ≥20% of session, OR continuous ≥60s while util high 2
``unknown`` CSV unreadable / missing required columns                    3
(usage)     invalid options / missing --csv / bad paths                  4
==========  ==========================================================  ====

Human output is Traditional Chinese. ``--json OUT`` writes one JSON object to
a *new* file only (never overwrites an existing path) and also prints it.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

TOOL = "cpu_throttle_report"
SCHEMA_VERSION = 1

DEFAULT_THROTTLE_BELOW = 95.0
DEFAULT_HIGH_UTIL = 80.0
OK_THROTTLE_FRACTION = 0.05
FAIL_THROTTLE_FRACTION = 0.20
FAIL_CONTINUOUS_S = 60.0

EXIT_OK = 0
EXIT_WARN = 1
EXIT_FAIL = 2
EXIT_UNKNOWN = 3
EXIT_USAGE = 4

EXIT_CODES = {
    "ok": EXIT_OK,
    "warn": EXIT_WARN,
    "fail": EXIT_FAIL,
    "unknown": EXIT_UNKNOWN,
}

STATUS_ZH = {
    "ok": "正常",
    "warn": "需留意",
    "fail": "嚴重限速",
    "unknown": "無法判斷",
}

# AMD Ryzen 5 5600H — zen-bridge target laptop (DESKTOP-P8RGA3A).
BASELINE = {
    "cpu": "AMD Ryzen 5 5600H",
    "base_ghz": 3.3,
    "boost_ghz": 4.2,
    "cores": 6,
    "threads": 12,
    "notes_zh": (
        "Ryzen 5 5600H 基準約 3.3 GHz、Boost 約 4.2 GHz。"
        "筆記型電腦在電池／省電計畫下，Windows 常把 "
        "「% Processor Performance」壓在遠低於 100。"
    ),
}

ADVICE_ZH = [
    "改用「高效能」電源計畫（控制台 → 電源選項），避免「省電」或「平衡」在負載時降頻。",
    "插電（接上電源）再跑正式場次；電池模式常會強制限速。",
    "關閉 Turbo 限流／超頻監控軟體（例如某些筆電廠商的「靜音模式」、第三方限頻工具）。",
    "關閉 OBS、多餘瀏覽器分頁與背景同步，降低長時間高負載。",
    "確認機身溫度與風扇：過熱會觸發 thermal throttling，清潔進風口或墊高筆電。",
    "工作管理員 → 效能 → CPU：觀察「基準速度」與實際速度是否長期低於基準。",
]

# Column-name fragments (case-insensitive; Chinese + English PDH).
FREQ_HINTS = (
    "% processor performance",
    "processor performance",
    "% of maximum frequency",
    "processor frequency",
    "處理器效能",
    "處理器性能",
    "最大頻率的百分比",
    "處理器頻率",
)
UTIL_HINTS = (
    "% processor time",
    "processor time",
    "% 處理器時間",
    "處理器時間",
)
TIMESTAMP_HINTS = (
    "(pdh-csv",
    "timestamp",
    "時間",
    "date",
)


class ReportInputError(ValueError):
    """Raised for invalid options or an unusable input path."""


# --------------------------------------------------------------------------
# CSV parsing
# --------------------------------------------------------------------------


def _norm_header(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _is_timestamp_col(name: str) -> bool:
    n = _norm_header(name)
    if not n:
        return False
    return any(h in n for h in TIMESTAMP_HINTS) or n.startswith("(")


def _score_hints(name: str, hints: tuple[str, ...]) -> int:
    n = _norm_header(name)
    best = 0
    for h in hints:
        if h in n:
            best = max(best, len(h))
    return best


def detect_columns(fieldnames: list[str] | None) -> dict[str, str | None]:
    """Map logical roles → original header names.

    Returns ``{"timestamp", "frequency", "utilization"}`` (values may be None).
    Prefers the strongest hint match; skips the first column when it looks like
    a PDH timestamp for frequency/util scoring so a lone first-col never wins.
    """
    if not fieldnames:
        return {"timestamp": None, "frequency": None, "utilization": None}

    ts_col: str | None = None
    # Prefer the first column when it looks like a PDH timestamp.
    if fieldnames and _is_timestamp_col(fieldnames[0]):
        ts_col = fieldnames[0]
    else:
        for name in fieldnames:
            if _is_timestamp_col(name):
                ts_col = name
                break

    freq_col: str | None = None
    freq_score = 0
    util_col: str | None = None
    util_score = 0
    for name in fieldnames:
        if name == ts_col:
            continue
        fs = _score_hints(name, FREQ_HINTS)
        us = _score_hints(name, UTIL_HINTS)
        # Prefer frequency over util when both match the same header weakly.
        if fs > freq_score:
            freq_score = fs
            freq_col = name
        if us > util_score:
            util_score = us
            util_col = name

    # Avoid assigning the same physical column to both roles.
    if freq_col is not None and freq_col == util_col:
        if freq_score >= util_score:
            util_col = None
            util_score = 0
            for name in fieldnames:
                if name in (ts_col, freq_col):
                    continue
                us = _score_hints(name, UTIL_HINTS)
                if us > util_score:
                    util_score = us
                    util_col = name
        else:
            freq_col = None

    return {"timestamp": ts_col, "frequency": freq_col, "utilization": util_col}


_TS_FORMATS = (
    "%m/%d/%Y %H:%M:%S.%f",
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S.%f",
    "%d/%m/%Y %H:%M:%S",
)


def parse_timestamp(raw: str) -> datetime | None:
    """Parse a typeperf / PerfMon timestamp; return None if unparseable."""
    text = (raw or "").strip().strip('"')
    if not text:
        return None
    # ISO with optional Z / offset.
    iso = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        pass
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_number(raw: str) -> float | None:
    """Parse a PDH numeric cell (tolerates blanks, commas, quotes)."""
    text = (raw or "").strip().strip('"').replace(",", "")
    if not text or text in {".", "-", "--"}:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return value


def _open_csv_text(path: Path) -> str:
    """Read CSV bytes as text, trying utf-8-sig then utf-16 (typeperf default)."""
    data = path.read_bytes()
    if not data:
        raise ReportInputError(f"CSV is empty: {path}")
    # UTF-16 LE/BE BOM
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    # Heuristic: lots of NULs → UTF-16 without BOM (common on some exports).
    sample = data[:4096]
    if sample.count(b"\x00") > max(8, len(sample) // 8):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "utf-8", "cp950", "big5", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_typeperf_csv(path: str | Path) -> dict[str, Any]:
    """Load samples from a typeperf CSV.

    Returns ``{"columns", "samples", "error"}`` where each sample is
    ``{"timestamp", "frequency_pct", "utilization_pct", "index"}``.
    """
    path = Path(path)
    try:
        text = _open_csv_text(path)
    except OSError as exc:
        return {
            "columns": {"timestamp": None, "frequency": None, "utilization": None},
            "samples": [],
            "error": f"cannot read {path}: {exc}",
        }
    except ReportInputError as exc:
        return {
            "columns": {"timestamp": None, "frequency": None, "utilization": None},
            "samples": [],
            "error": str(exc),
        }

    # Strip blank leading lines typeperf sometimes emits.
    stream = io.StringIO(text.lstrip("\ufeff"))
    try:
        reader = csv.DictReader(stream)
        fieldnames = list(reader.fieldnames or [])
    except csv.Error as exc:
        return {
            "columns": {"timestamp": None, "frequency": None, "utilization": None},
            "samples": [],
            "error": f"CSV parse error: {exc}",
        }

    columns = detect_columns(fieldnames)
    if columns["frequency"] is None:
        return {
            "columns": columns,
            "samples": [],
            "fieldnames": fieldnames,
            "error": "missing frequency column (% Processor Performance / 處理器效能)",
        }

    samples: list[dict[str, Any]] = []
    ts_key = columns["timestamp"]
    freq_key = columns["frequency"]
    util_key = columns["utilization"]
    for idx, row in enumerate(reader):
        if not row:
            continue
        freq = parse_number(row.get(freq_key, "") if freq_key else "")
        if freq is None:
            continue
        util = None
        if util_key:
            util = parse_number(row.get(util_key, ""))
        ts = None
        if ts_key:
            ts = parse_timestamp(row.get(ts_key, ""))
        samples.append(
            {
                "index": idx,
                "timestamp": ts,
                "frequency_pct": freq,
                "utilization_pct": util,
            }
        )

    if not samples:
        return {
            "columns": columns,
            "samples": [],
            "fieldnames": fieldnames,
            "error": "no numeric frequency samples found",
        }
    return {
        "columns": columns,
        "samples": samples,
        "fieldnames": fieldnames,
        "error": None,
    }


# --------------------------------------------------------------------------
# Interval detection & classification
# --------------------------------------------------------------------------


def _sample_delta_s(samples: list[dict[str, Any]]) -> float:
    """Median inter-sample gap in seconds; default 1.0 when timestamps missing."""
    gaps: list[float] = []
    prev = None
    for s in samples:
        ts = s.get("timestamp")
        if ts is not None and prev is not None:
            gap = (ts - prev).total_seconds()
            if 0 < gap < 3600:
                gaps.append(gap)
        if ts is not None:
            prev = ts
    if len(gaps) >= 2:
        return float(statistics.median(gaps))
    if len(gaps) == 1:
        return float(gaps[0])
    return 1.0


def _fmt_ts(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.microsecond:
        return ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def find_throttle_intervals(
    samples: list[dict[str, Any]],
    *,
    throttle_below: float = DEFAULT_THROTTLE_BELOW,
    delta_s: float | None = None,
) -> list[dict[str, Any]]:
    """Find contiguous runs where frequency_pct < throttle_below."""
    if not samples:
        return []
    if delta_s is None:
        delta_s = _sample_delta_s(samples)
    delta_s = max(0.001, float(delta_s))

    intervals: list[dict[str, Any]] = []
    run_freqs: list[float] = []
    run_utils: list[float] = []
    run_start_ts: datetime | None = None
    run_end_ts: datetime | None = None
    run_start_idx: int | None = None
    run_end_idx: int | None = None

    def flush() -> None:
        nonlocal run_freqs, run_utils, run_start_ts, run_end_ts, run_start_idx, run_end_idx
        if not run_freqs:
            return
        n = len(run_freqs)
        # Duration: span of timestamps if available, else n * delta.
        if (
            run_start_ts is not None
            and run_end_ts is not None
            and run_end_ts >= run_start_ts
        ):
            # Include one sample width so a single-point run has duration ~delta.
            duration = (run_end_ts - run_start_ts).total_seconds() + delta_s
        else:
            duration = n * delta_s
        avg_util = (
            round(statistics.mean(run_utils), 3) if run_utils else None
        )
        intervals.append(
            {
                "start": _fmt_ts(run_start_ts),
                "end": _fmt_ts(run_end_ts),
                "start_index": run_start_idx,
                "end_index": run_end_idx,
                "samples": n,
                "duration_s": round(duration, 3),
                "min_frequency_pct": round(min(run_freqs), 3),
                "avg_frequency_pct": round(statistics.mean(run_freqs), 3),
                "avg_utilization_pct": avg_util,
            }
        )
        run_freqs = []
        run_utils = []
        run_start_ts = None
        run_end_ts = None
        run_start_idx = None
        run_end_idx = None

    for s in samples:
        freq = s["frequency_pct"]
        if freq < throttle_below:
            if not run_freqs:
                run_start_ts = s.get("timestamp")
                run_start_idx = s["index"]
            run_end_ts = s.get("timestamp")
            run_end_idx = s["index"]
            run_freqs.append(freq)
            util = s.get("utilization_pct")
            if util is not None:
                run_utils.append(util)
        else:
            flush()
    flush()
    return intervals


def session_duration_s(
    samples: list[dict[str, Any]], *, delta_s: float | None = None
) -> float:
    if not samples:
        return 0.0
    if delta_s is None:
        delta_s = _sample_delta_s(samples)
    delta_s = max(0.001, float(delta_s))
    first_ts = samples[0].get("timestamp")
    last_ts = samples[-1].get("timestamp")
    if first_ts is not None and last_ts is not None and last_ts >= first_ts:
        return max(delta_s, (last_ts - first_ts).total_seconds() + delta_s)
    return len(samples) * delta_s


def classify(
    intervals: list[dict[str, Any]],
    *,
    session_s: float,
    high_util: float = DEFAULT_HIGH_UTIL,
) -> str:
    """Return ``ok`` / ``warn`` / ``fail`` / ``unknown``."""
    if session_s <= 0:
        return "unknown"
    total_throttle = sum(float(i["duration_s"]) for i in intervals)
    fraction = total_throttle / session_s if session_s else 0.0

    long_high = False
    for iv in intervals:
        util = iv.get("avg_utilization_pct")
        if (
            float(iv["duration_s"]) >= FAIL_CONTINUOUS_S
            and util is not None
            and float(util) >= high_util
        ):
            long_high = True
            break

    if not intervals or fraction < OK_THROTTLE_FRACTION:
        return "ok"
    if fraction >= FAIL_THROTTLE_FRACTION or long_high:
        return "fail"
    return "warn"


def advice_zh(status: str) -> list[str]:
    if status == "ok":
        return [
            "本次取樣未見明顯 CPU 限速（或限速佔比很低），可維持目前電源與散熱設定。",
            "正式場次仍建議插電並使用高效能電源計畫。",
        ]
    if status == "unknown":
        return [
            "無法解析 typeperf CSV（缺欄位或檔案無法讀取）。",
            "請用類似指令重新取樣後再分析：",
            'typeperf "\\Processor Information(_Total)\\% Processor Performance" '
            '"\\Processor(_Total)\\% Processor Time" -si 1 -sc 120 -o cpu.csv',
        ]
    tips = list(ADVICE_ZH)
    if status == "fail":
        tips.insert(
            0,
            "限速嚴重：佔用場次比例偏高，或在高使用率下長時間降頻，即時字幕可能卡頓。",
        )
    else:
        tips.insert(0, "有偵測到中度限速區間；建議在正式場次前調整電源與散熱。")
    return tips


def _check_threshold(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportInputError(f"{label} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ReportInputError(f"{label} must be finite")
    return value


def evaluate(
    loaded: dict[str, Any],
    *,
    throttle_below: float = DEFAULT_THROTTLE_BELOW,
    high_util: float = DEFAULT_HIGH_UTIL,
) -> dict[str, Any]:
    """Build the JSON-able report from a loaded CSV."""
    throttle_below = _check_threshold(throttle_below, "throttle_below")
    high_util = _check_threshold(high_util, "high_util")
    if throttle_below <= 0 or throttle_below > 200:
        raise ReportInputError("throttle_below must be in (0, 200]")
    if high_util < 0 or high_util > 100:
        raise ReportInputError("high_util must be in [0, 100]")

    notes: list[str] = []
    if loaded.get("error"):
        notes.append(str(loaded["error"]))
        status = "unknown"
        intervals: list[dict[str, Any]] = []
        samples = loaded.get("samples") or []
        session_s = 0.0
        delta = 1.0
        freqs: list[float] = []
        utils: list[float] = []
    else:
        samples = loaded["samples"]
        delta = _sample_delta_s(samples)
        intervals = find_throttle_intervals(
            samples, throttle_below=throttle_below, delta_s=delta
        )
        session_s = session_duration_s(samples, delta_s=delta)
        status = classify(intervals, session_s=session_s, high_util=high_util)
        freqs = [s["frequency_pct"] for s in samples]
        utils = [
            s["utilization_pct"]
            for s in samples
            if s.get("utilization_pct") is not None
        ]

    total_throttle = sum(float(i["duration_s"]) for i in intervals)
    fraction = (total_throttle / session_s) if session_s > 0 else 0.0
    longest = max((float(i["duration_s"]) for i in intervals), default=0.0)

    summary = f"CPU 限速檢查：{STATUS_ZH[status]}（{status}）"
    if samples and status != "unknown":
        summary += (
            f"；場次約 {session_s:.1f}s，限速區間 {len(intervals)} 段"
            f"、合計 {total_throttle:.1f}s（{fraction:.1%}）"
        )
        if freqs:
            summary += (
                f"；頻率% 最小 {min(freqs):.1f}／平均 {statistics.mean(freqs):.1f}"
            )

    columns = loaded.get("columns") or {}
    return {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "exit_code": EXIT_CODES[status],
        "csv_columns": {
            "timestamp": columns.get("timestamp"),
            "frequency": columns.get("frequency"),
            "utilization": columns.get("utilization"),
        },
        "thresholds": {
            "throttle_below": throttle_below,
            "high_util": high_util,
            "ok_fraction": OK_THROTTLE_FRACTION,
            "fail_fraction": FAIL_THROTTLE_FRACTION,
            "fail_continuous_s": FAIL_CONTINUOUS_S,
        },
        "baseline": dict(BASELINE),
        "sample_count": len(samples),
        "sample_interval_s": round(delta, 3) if samples else None,
        "session_duration_s": round(session_s, 3),
        "throttle_interval_count": len(intervals),
        "throttle_total_s": round(total_throttle, 3),
        "throttle_fraction": round(fraction, 4),
        "longest_throttle_s": round(longest, 3),
        "frequency_pct": {
            "min": round(min(freqs), 3) if freqs else None,
            "max": round(max(freqs), 3) if freqs else None,
            "avg": round(statistics.mean(freqs), 3) if freqs else None,
        },
        "utilization_pct": {
            "min": round(min(utils), 3) if utils else None,
            "max": round(max(utils), 3) if utils else None,
            "avg": round(statistics.mean(utils), 3) if utils else None,
        },
        "intervals": intervals,
        "summary_zh": summary,
        "advice_zh": advice_zh(status),
        "notes": notes,
    }


def format_text(report: dict[str, Any]) -> str:
    lines = [report["summary_zh"]]
    base = report.get("baseline") or {}
    if base.get("notes_zh"):
        lines.append(f"基準：{base['notes_zh']}")
    if report.get("intervals"):
        lines.append("限速區間：")
        for i, iv in enumerate(report["intervals"][:20], 1):
            start = iv.get("start") or f"#{iv.get('start_index')}"
            end = iv.get("end") or f"#{iv.get('end_index')}"
            util = iv.get("avg_utilization_pct")
            util_s = f"，平均使用率 {util:.1f}%" if util is not None else ""
            lines.append(
                f"  {i}. {start} → {end}，{iv['duration_s']:.1f}s，"
                f"頻率% 最小 {iv['min_frequency_pct']:.1f}／"
                f"平均 {iv['avg_frequency_pct']:.1f}{util_s}"
            )
        if len(report["intervals"]) > 20:
            lines.append(f"  …另有 {len(report['intervals']) - 20} 段未列出")
    for tip in report["advice_zh"]:
        lines.append(f"- {tip}")
    for note in report.get("notes") or []:
        lines.append(f"（備註）{note}")
    lines.append(f"exit code: {report['exit_code']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=TOOL,
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--csv",
        required=True,
        metavar="PATH",
        help="typeperf / PerfMon CSV path (read-only)",
    )
    parser.add_argument(
        "--json",
        metavar="OUT",
        help="write one JSON object to a NEW file (never overwrite)",
    )
    parser.add_argument(
        "--throttle-below",
        type=float,
        default=DEFAULT_THROTTLE_BELOW,
        help=f"frequency %% below this counts as throttling (default {DEFAULT_THROTTLE_BELOW})",
    )
    parser.add_argument(
        "--high-util",
        type=float,
        default=DEFAULT_HIGH_UTIL,
        help=f"utilization %% treated as high during long throttle (default {DEFAULT_HIGH_UTIL})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        csv_path = Path(args.csv)
        if not csv_path.is_file():
            raise ReportInputError(f"CSV not found: {csv_path}")
        out_path: Path | None = None
        if args.json is not None:
            out_path = Path(args.json)
            if out_path.resolve() == csv_path.resolve():
                raise ReportInputError("--json must not refer to the input CSV")
            if out_path.exists() or out_path.is_symlink():
                raise ReportInputError(
                    f"refusing to overwrite existing file {out_path}"
                )
        loaded = read_typeperf_csv(csv_path)
        report = evaluate(
            loaded,
            throttle_below=args.throttle_below,
            high_util=args.high_util,
        )
        payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if out_path is not None:
            with out_path.open("x", encoding="utf-8") as fh:
                fh.write(payload)
            print(payload, end="")
        else:
            print(format_text(report))
    except ReportInputError as exc:
        print(f"{TOOL}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"{TOOL}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())