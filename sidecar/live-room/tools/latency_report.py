#!/usr/bin/env python3
"""Offline, read-only latency report from saved /api/metrics snapshots.

  python tools/latency_report.py metrics.jsonl -o report.html [--json]
  python tools/latency_report.py snap1.json snap2.json -o report.html

Each INPUT is a UTF-8 JSON file (one snapshot object, or a list of them) or a
JSONL file (one snapshot per line). A snapshot is the /api/metrics response:
pipeline.stats() plus rss_bytes, listeners, rooms and store_errors. Missing,
None, negative, non-numeric and unknown fields are skipped. Bad JSONL lines are
skipped and counted. An input that cannot be opened, decoded or parsed at all
exits 2.

Stage samples are one value per snapshot, taken from the first field present:

  A2 audio_capture  audio_capture_ms, oldest_wait_ms (slice waiting for ASR)
  A3 asr_draft      asr_draft_ms
  A4 asr_final      asr_final_ms, process_ms (decode plus recognition)
  A5 mt             mt_ms
  A6 broadcast      broadcast_ms
  A7 end_to_end     end_to_end_ms

asr_wall (last_process_ms, slice start until ASR returns, including the wait)
is reported next to the stages. The current /api/metrics publishes only A2 and
A4 directly; the other stages are shown only when the snapshots carry them.
A value that repeats across polls is counted once per snapshot. RTF uses
asr_rtf_last per snapshot. Percentiles use linear rank, like app/rtf.py.

The HTML is self-contained: inline CSS and inline SVG, no scripts and no
external references. Standard library only; nothing is sent over the network.
"""
from __future__ import annotations

import argparse
import html
import json
import math
from pathlib import Path
import sys

STAGES = (
    ("A2", "audio_capture", "音訊擷取（等待辨識）", ("audio_capture_ms", "oldest_wait_ms")),
    ("A3", "asr_draft", "ASR 草稿", ("asr_draft_ms",)),
    ("A4", "asr_final", "ASR 定稿（解碼＋辨識）", ("asr_final_ms", "process_ms")),
    ("A2–A4", "asr_wall", "切片開始至辨識完成（含等待）", ("last_process_ms",)),
    ("A5", "mt", "機器翻譯", ("mt_ms",)),
    ("A6", "broadcast", "廣播", ("broadcast_ms",)),
    ("A7", "end_to_end", "端到端", ("end_to_end_ms",)),
)


class InputError(Exception):
    """An input file that cannot be read as JSON or JSONL."""


def percentile(values: list[float], p: float) -> float | None:
    """Linear rank between closest ranks. None for empty input."""
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _clean(value: float | None) -> int | float | None:
    if value is None:
        return None
    number = round(value, 3)
    if number == int(number) and abs(number) < 10**15:
        return int(number)
    return number


def summarize(values: list[float]) -> dict:
    return {
        "count": len(values),
        "p50": _clean(percentile(values, 0.50)),
        "p95": _clean(percentile(values, 0.95)),
        "max": _clean(max(values)) if values else None,
    }


def _items(parsed: object) -> tuple[list[dict], int]:
    rows = parsed if isinstance(parsed, list) else [parsed]
    snapshots = [row for row in rows if isinstance(row, dict)]
    return snapshots, len(rows) - len(snapshots)


def load_file(path: Path) -> tuple[list[dict], int]:
    """Return (snapshots, skipped). Raise InputError if the file is unreadable."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"cannot read input {str(path)!r}: {exc}") from exc
    if not text.strip():
        return [], 0
    try:
        return _items(json.loads(text))
    except (ValueError, RecursionError):
        pass
    snapshots: list[dict] = []
    skipped = 0
    parsed_any = False
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except (ValueError, RecursionError):
            skipped += 1
            continue
        parsed_any = True
        rows, bad = _items(parsed)
        snapshots.extend(rows)
        skipped += bad
    if not parsed_any:
        raise InputError(f"input {str(path)!r} is not JSON or JSONL")
    return snapshots, skipped


def load_snapshots(paths: list[Path]) -> tuple[list[dict], int]:
    snapshots: list[dict] = []
    skipped = 0
    for path in paths:
        rows, bad = load_file(path)
        snapshots.extend(rows)
        skipped += bad
    return snapshots, skipped


def _first(snapshot: dict, fields: tuple[str, ...]) -> float | None:
    for field in fields:
        value = _number(snapshot.get(field))
        if value is not None:
            return value
    return None


def _column(snapshots: list[dict], field: str) -> list[float]:
    values = (_number(snap.get(field)) for snap in snapshots)
    return [value for value in values if value is not None]


def build_summary(snapshots: list[dict], sources: list[str] | None = None, skipped: int = 0) -> dict:
    stages = []
    for stage, key, label, fields in STAGES:
        values = [v for v in (_first(snap, fields) for snap in snapshots) if v is not None]
        row = {"stage": stage, "key": key, "label": label, "fields": list(fields)}
        row.update(summarize(values))
        stages.append(row)
    rtf = summarize(_column(snapshots, "asr_rtf_last"))
    window_p95 = _column(snapshots, "asr_rtf_p95")
    rtf["window_p95_max"] = _clean(max(window_p95)) if window_p95 else None
    rss = _column(snapshots, "rss_bytes")
    rss_series = [_clean(v) for v in rss]
    return {
        "snapshots": len(snapshots),
        "skipped": skipped,
        "sources": list(sources or []),
        "stages": stages,
        "rtf": rtf,
        "rss_bytes": {
            "count": len(rss),
            "min": _clean(min(rss)) if rss else None,
            "max": _clean(max(rss)) if rss else None,
            "first": rss_series[0] if rss_series else None,
            "last": rss_series[-1] if rss_series else None,
            "series": rss_series,
        },
        "listeners_max": _peak(snapshots, "listeners"),
        "rooms_max": _peak(snapshots, "rooms"),
        "store_errors_max": _peak(snapshots, "store_errors"),
    }


def _peak(snapshots: list[dict], field: str) -> int | float | None:
    values = _column(snapshots, field)
    return _clean(max(values)) if values else None


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _fmt(value: object, unit: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, int):
        return f"{value:,}{unit}"
    return f"{value:,.3f}".rstrip("0").rstrip(".") + unit


def _mib(value: object) -> str:
    if value is None:
        return "—"
    return f"{value / (1024 * 1024):,.1f} MiB"


def _stage_chart(stages: list[dict]) -> str:
    present = [row for row in stages if row["count"]]
    if not present:
        return '<p class="empty">無資料（no stage latency samples）</p>'
    top = max(float(row["max"] or 0) for row in present) or 1.0
    label_w, bar_w, row_h = 220, 380, 54
    height = row_h * len(present) + 10
    parts = [f'<svg class="chart" width="{label_w + bar_w + 110}" height="{height}" role="img" '
             f'aria-label="{_e("各階段延遲（ms）")}">']
    y = 5
    for row in present:
        parts.append(f'<text x="0" y="{y + 16}" class="lbl">{_e(row["stage"])} {_e(row["key"])}</text>')
        parts.append(f'<text x="0" y="{y + 34}" class="sub">{_e(row["label"])}</text>')
        for i, (name, cls) in enumerate((("p50", "b50"), ("p95", "b95"), ("max", "bmax"))):
            value = float(row[name] or 0)
            width = max(1.0, bar_w * value / top)
            by = y + i * 15
            parts.append(f'<rect x="{label_w}" y="{by}" width="{width:.1f}" height="12" class="{cls}"/>')
            parts.append(f'<text x="{label_w + width + 4:.1f}" y="{by + 10}" class="val">'
                         f'{_e(name)} {_e(_fmt(row[name]))}</text>')
        y += row_h
    parts.append("</svg>")
    return "".join(parts)


def _rss_chart(series: list) -> str:
    if len(series) < 2:
        return ""
    width, height = 600, 120
    low, high = min(series), max(series)
    span = (high - low) or 1
    step = width / (len(series) - 1)
    points = " ".join(f"{i * step:.1f},{height - 5 - (v - low) / span * (height - 10):.1f}"
                      for i, v in enumerate(series))
    return (f'<svg class="chart" width="{width}" height="{height}" role="img" '
            f'aria-label="{_e("RSS 隨時間變化")}"><polyline points="{_e(points)}" class="line"/></svg>')


CSS = """
body{font-family:"Microsoft JhengHei","PingFang TC","Noto Sans TC",sans-serif;margin:24px;color:#222;background:#fff}
h1{font-size:22px}h2{font-size:18px;margin-top:28px}
table{border-collapse:collapse;margin:8px 0}th,td{border:1px solid #ccc;padding:4px 10px;text-align:right}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{background:#f3f3f3}.empty{color:#888}.note{color:#555;font-size:13px}
.chart text{font-size:12px;fill:#222}.chart .sub{fill:#666;font-size:11px}
.b50{fill:#4c8bd6}.b95{fill:#e3a33b}.bmax{fill:#d65151}
.line{fill:none;stroke:#4c8bd6;stroke-width:2}
"""


def render_html(summary: dict) -> str:
    rows = []
    for row in summary["stages"]:
        rows.append(
            "<tr>"
            f"<td>{_e(row['stage'])} <code>{_e(row['key'])}</code></td>"
            f"<td>{_e(row['label'])}<br><span class=\"note\">{_e(', '.join(row['fields']))}</span></td>"
            f"<td>{_e(row['count'])}</td>"
            f"<td>{_e(_fmt(row['p50'], ' ms'))}</td>"
            f"<td>{_e(_fmt(row['p95'], ' ms'))}</td>"
            f"<td>{_e(_fmt(row['max'], ' ms'))}</td>"
            "</tr>"
        )
    rtf = summary["rtf"]
    rss = summary["rss_bytes"]
    sources = ", ".join(summary["sources"]) or "—"
    return "".join([
        "<!DOCTYPE html>\n<html lang=\"zh-Hant\"><head><meta charset=\"utf-8\">",
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">",
        "<title>即時字幕延遲報告</title>",
        f"<style>{CSS}</style></head><body>",
        "<h1>即時字幕延遲報告（latency report）</h1>",
        f"<p>快照數（snapshots）：{_e(summary['snapshots'])}　略過（skipped）：{_e(summary['skipped'])}</p>",
        f"<p>來源檔案（sources）：{_e(sources)}</p>",
        "<h2>各階段延遲（stage latency）</h2>",
        "<table><tr><th>階段</th><th>說明（欄位）</th><th>樣本數</th><th>p50</th><th>p95</th><th>最大值</th></tr>",
        "".join(rows),
        "</table>",
        _stage_chart(summary["stages"]),
        "<p class=\"note\">每個快照取一個值；無資料的階段表示快照未提供該欄位。</p>",
        "<h2>即時率（RTF, asr_rtf_last）</h2>",
        "<table><tr><th>項目</th><th>欄位</th><th>樣本數</th><th>p50</th><th>p95</th><th>最大值</th></tr>",
        f"<tr><td>RTF</td><td>asr_rtf_last</td><td>{_e(rtf['count'])}</td><td>{_e(_fmt(rtf['p50']))}</td>"
        f"<td>{_e(_fmt(rtf['p95']))}</td><td>{_e(_fmt(rtf['max']))}</td></tr></table>",
        f"<p>視窗 p95 最大值（asr_rtf_p95 max）：{_e(_fmt(rtf['window_p95_max']))}</p>",
        "<h2>記憶體（RSS, rss_bytes）</h2>",
        "<table><tr><th>樣本數</th><th>最小值</th><th>最大值</th><th>第一筆</th><th>最後一筆</th></tr>",
        f"<tr><td>{_e(rss['count'])}</td><td>{_e(_mib(rss['min']))}</td><td>{_e(_mib(rss['max']))}</td>"
        f"<td>{_e(_mib(rss['first']))}</td><td>{_e(_mib(rss['last']))}</td></tr></table>",
        _rss_chart(rss["series"]),
        "<h2>其他（other）</h2>",
        "<table><tr><th>項目</th><th>欄位</th><th>最大值</th></tr>",
        f"<tr><td>聽眾</td><td>listeners</td><td>{_e(_fmt(summary['listeners_max']))}</td></tr>",
        f"<tr><td>房間</td><td>rooms</td><td>{_e(_fmt(summary['rooms_max']))}</td></tr>",
        f"<tr><td>儲存錯誤</td><td>store_errors</td><td>{_e(_fmt(summary['store_errors_max']))}</td></tr>",
        "</table></body></html>\n",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="+", type=Path, metavar="INPUT", help="JSON or JSONL snapshot file")
    parser.add_argument("-o", "--output", required=True, type=Path, help="HTML report to write")
    parser.add_argument("--json", action="store_true", help="also print the summary as JSON to stdout")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if any(path.resolve() == output for path in args.inputs):
        print(f"latency_report: error: output {str(args.output)!r} is also an input", file=sys.stderr)
        return 2
    try:
        snapshots, skipped = load_snapshots(args.inputs)
    except InputError as exc:
        print(f"latency_report: error: {exc}", file=sys.stderr)
        return 2
    summary = build_summary(snapshots, [path.name for path in args.inputs], skipped)
    try:
        args.output.write_text(render_html(summary), encoding="utf-8")
    except OSError as exc:
        print(f"latency_report: error: cannot write {str(args.output)!r}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
