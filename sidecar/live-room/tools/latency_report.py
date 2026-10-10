#!/usr/bin/env python3
"""Offline, read-only pipeline latency report as one static HTML file.

  python tools/latency_report.py --db data/zen.sqlite3 --session S --lang en --out report.html
  python tools/latency_report.py --metrics metrics.jsonl --title "Tuesday class"

Input is either a SQLite database opened strictly read-only (zen.sqlite3 with
the admin ``metrics(ts, name, value, room_id, session_id, labels)`` table; a
captions.sqlite3 has no latency rows and yields a "no data" page) or a JSON /
JSONL export. JSON may be the admin ``/metrics/export?format=json`` shape
``{"columns": [...], "rows": [...]}``, a list of objects, or
``{"records": [...]}``. Each object is either a long metric row with ``name``
and ``value`` or a wide record whose keys are metric names (``asr_ms``,
``a5_ms``, ...). Malformed JSONL lines are skipped and counted.

Stages A2-A7 are per-stage durations in milliseconds. A stage takes the first
metric name in its list that has samples, so a record carrying both
``a4_ms`` and the existing RTF meter's ``asr_ms`` is not counted twice.
End-to-end uses an explicit ``e2e_ms`` metric when present, otherwise the
per-segment sum of A2, A4, A5, A6 and A7 that have data (only segments with
every such stage). A3 (draft) runs beside the final path and is not added. ``--lang`` keeps samples tagged with that language (``labels.lang``,
the record's ``lang``, or zen.sqlite3 ``sessions.tgt_lang``); untagged samples
such as ASR timings are language-neutral and kept.

The page has inline CSS and inline SVG only: no scripts, fonts or links.
Standard library only; no network, no subprocess.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import html
import json
import math
from pathlib import Path
import re
import sqlite3
import sys

STAGES = (
    ("A2", "Audio capture \u2192 VAD end", ("a2", "a2_ms", "a2_vad_end_ms", "vad_end_ms")),
    ("A3", "ASR draft", ("a3", "a3_ms", "a3_asr_draft_ms", "asr_draft_ms", "draft_asr_ms")),
    ("A4", "ASR final", ("a4", "a4_ms", "a4_asr_final_ms", "asr_final_ms", "asr_ms")),
    ("A5", "MT", ("a5", "a5_ms", "a5_mt_ms", "mt_ms", "translate_ms")),
    ("A6", "Publish to room", ("a6", "a6_ms", "a6_publish_ms", "publish_ms")),
    ("A7", "Client display", ("a7", "a7_ms", "a7_display_ms", "display_ms")),
)
E2E_STAGES = ("A2", "A4", "A5", "A6", "A7")
E2E_NAMES = ("e2e_ms", "end_to_end_ms", "a2_a7_ms")
RTF_NAMES = ("rtf", "asr_rtf")
KNOWN_NAMES = frozenset(n for _, _, names in STAGES for n in names) | set(E2E_NAMES) | set(RTF_NAMES)
LANG_ALIASES = {"jp": "ja", "jpn": "ja", "eng": "en"}
MAX_FILE_BYTES = 512 * 1024 * 1024


def percentile(values: list[float], p: float) -> float:
    """Linear rank between closest ranks; same shape as app.rtf.percentile."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of empty data")
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def summarize(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "count": len(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": float(max(values)),
    }


def _lang(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    base = re.split(r"[-_]", value.strip().lower(), maxsplit=1)[0]
    return LANG_ALIASES.get(base, base)


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _labels(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _text(value) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)


def _sample(name, value, session, room, lang, segment) -> dict | None:
    if not isinstance(name, str):
        return None
    key = name.strip().lower()
    number = _number(value)
    if key not in KNOWN_NAMES or number is None:
        return None
    return {"name": key, "value": number, "session_id": _text(session), "room_id": _text(room),
            "lang": _lang(lang), "segment": _text(segment)}


def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def load_db(path: Path) -> tuple[list[dict], list[str]]:
    if not path.is_file():
        raise ValueError(f"database not found: {path}")
    notes: list[str] = []
    with closing(_open_readonly(path)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "metrics" not in tables:
            notes.append("This database has no metrics table (no latency records).")
            return [], notes
        session_lang: dict[str, str] = {}
        if "sessions" in tables:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
            if {"id", "tgt_lang"} <= columns:
                session_lang = {str(r[0]): r[1] for r in conn.execute("SELECT id, tgt_lang FROM sessions")}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(metrics)")}
        labels_col = "labels" if "labels" in columns else "NULL"
        session_col = "session_id" if "session_id" in columns else "NULL"
        room_col = "room_id" if "room_id" in columns else "NULL"
        names = sorted(KNOWN_NAMES)
        rows = conn.execute(
            f"SELECT name, value, {session_col}, {room_col}, {labels_col} FROM metrics "
            f"WHERE lower(name) IN ({','.join('?' * len(names))}) ORDER BY rowid",
            names,
        )
        samples = []
        for name, value, session, room, raw_labels in rows:
            labels = _labels(raw_labels)
            lang = labels.get("lang") or labels.get("tgt_lang") or session_lang.get(str(session))
            sample = _sample(name, value, session, room, lang, labels.get("segment_id") or labels.get("segment"))
            if sample:
                samples.append(sample)
    return samples, notes


def _record_samples(obj: dict) -> list[dict]:
    labels = _labels(obj.get("labels"))
    session = obj.get("session_id", obj.get("session", labels.get("session_id")))
    room = obj.get("room_id", obj.get("room", labels.get("room_id")))
    lang = obj.get("lang") or obj.get("tgt_lang") or labels.get("lang") or labels.get("tgt_lang")
    segment = obj.get("segment_id", obj.get("segment", labels.get("segment_id")))
    if "name" in obj and "value" in obj:
        sample = _sample(obj.get("name"), obj.get("value"), session, room, lang, segment)
        return [sample] if sample else []
    out = []
    for key, value in obj.items():
        sample = _sample(key, value, session, room, lang, segment)
        if sample:
            out.append(sample)
    return out


def _json_records(data) -> list:
    if isinstance(data, dict):
        if isinstance(data.get("columns"), list) and isinstance(data.get("rows"), list):
            cols = [str(c) for c in data["columns"]]
            return [dict(zip(cols, row)) for row in data["rows"] if isinstance(row, list)]
        for key in ("records", "metrics", "rows", "samples"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    if isinstance(data, list):
        return data
    return []


def load_metrics(path: Path) -> tuple[list[dict], list[str]]:
    if not path.is_file():
        raise ValueError(f"metrics file not found: {path}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"metrics file is larger than {MAX_FILE_BYTES} bytes")
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    notes: list[str] = []
    try:
        records = _json_records(json.loads(text)) if text.strip() else []
    except ValueError:
        records, bad = [], 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                bad += 1
        if bad:
            notes.append(f"Skipped {bad} malformed line(s).")
    samples = []
    for obj in records:
        if isinstance(obj, dict):
            samples.extend(_record_samples(obj))
    return samples, notes


def build_report(samples: list[dict], session: str | None = None, lang: str | None = None) -> dict:
    wanted = _lang(lang)
    kept = [s for s in samples
            if (session is None or s["session_id"] == session)
            and (wanted is None or s["lang"] is None or s["lang"] == wanted)]
    by_name: dict[str, list[dict]] = {}
    for s in kept:
        by_name.setdefault(s["name"], []).append(s)

    def first(names):
        for name in names:
            if by_name.get(name):
                return name, by_name[name]
        return None, []

    stages, chosen = [], {}
    for sid, label, names in STAGES:
        source, rows = first(names)
        chosen[sid] = rows
        stages.append({"id": sid, "label": label, "source": source,
                       **summarize([r["value"] for r in rows])})
    e2e_source, e2e_rows = first(E2E_NAMES)
    e2e_values = [r["value"] for r in e2e_rows]
    if not e2e_rows:
        present = [sid for sid, rows in chosen.items() if rows and sid in E2E_STAGES]
        per_segment: dict[tuple, dict[str, float]] = {}
        for sid in present:
            for r in chosen[sid]:
                if r["segment"] is not None:
                    per_segment.setdefault((r["session_id"], r["segment"]), {})[sid] = r["value"]
        e2e_values = [sum(v.values()) for v in per_segment.values() if present and len(v) == len(present)]
        if e2e_values:
            e2e_source = "sum of " + ", ".join(present) + " per segment"
    rtf_source, rtf_rows = first(RTF_NAMES)
    return {
        "session": session,
        "lang": wanted,
        "samples": len(kept),
        "stages": stages,
        "e2e": {"source": e2e_source, **summarize(e2e_values)},
        "rtf": {"source": rtf_source, **summarize([r["value"] for r in rtf_rows])},
    }


_HTTP = re.compile(r"(?i)h(?=ttp)")


def esc(value) -> str:
    """HTML-escape data text; also break any 'http' so the page never carries a URL."""
    return _HTTP.sub("&#104;", html.escape(str(value), quote=True))


def _ms(value) -> str:
    if value is None:
        return "n/a"
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}"


def _chart(stages: list[dict]) -> str:
    width, left, bar_h, gap = 640, 190, 12, 14
    rows = [s for s in stages if s["count"]]
    if not rows:
        return '<p class="muted">No stage data to chart.</p>'
    top = max(max(s["p95"], s["p50"]) for s in rows) or 1.0
    span = width - left - 70
    height = len(stages) * (2 * bar_h + gap) + 30
    parts = [f'<svg role="img" aria-label="Stage latency p50 and p95" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}">']
    y = 10
    for s in stages:
        label = esc(f"{s['id']} {s['label']}")
        parts.append(f'<text x="0" y="{y + bar_h + 4}" font-size="12" fill="#222">{label}</text>')
        if not s["count"]:
            parts.append(f'<text x="{left}" y="{y + bar_h + 4}" font-size="12" fill="#888">n/a</text>')
        else:
            for offset, key, color in ((0, "p50", "#4a7bd0"), (bar_h, "p95", "#e08a2c")):
                w = max(1.0, span * s[key] / top)
                parts.append(f'<rect x="{left}" y="{y + offset}" width="{w:.1f}" height="{bar_h - 1}" fill="{color}"/>')
                parts.append(f'<text x="{left + w + 4:.1f}" y="{y + offset + bar_h - 2}" font-size="10" '
                             f'fill="#333">{key} {esc(_ms(s[key]))}</text>')
        y += 2 * bar_h + gap
    parts.append(f'<text x="{left}" y="{height - 4}" font-size="10" fill="#555">'
                 f'blue = p50, orange = p95 (ms)</text></svg>')
    return "".join(parts)


def render_html(report: dict, title: str = "Latency report", source: str = "", notes=()) -> str:
    def row(cells, tag="td"):
        return "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"

    head = row(["Stage", "Description", "Count", "p50 ms", "p95 ms", "p99 ms", "max ms", "Metric"], "th")
    body = [row([esc(s["id"]), esc(s["label"]), s["count"], _ms(s["p50"]), _ms(s["p95"]), _ms(s["p99"]),
                 _ms(s["max"]), esc(s["source"] or "n/a")]) for s in report["stages"]]
    e2e, rtf = report["e2e"], report["rtf"]

    def rtf_cell(v):
        return "n/a" if v is None else f"{v:.3f}"

    meta = [("Source", source or "n/a"), ("Session", report["session"] or "all"),
            ("Language", report["lang"] or "all"), ("Samples", report["samples"])]
    empty = report["samples"] == 0
    out = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(title)}</title>",
        "<style>body{font-family:system-ui,'Segoe UI',sans-serif;margin:24px;color:#222;max-width:900px}"
        "table{border-collapse:collapse;margin:12px 0}th,td{border:1px solid #ccc;padding:4px 8px;"
        "text-align:right}th:nth-child(-n+2),td:nth-child(-n+2){text-align:left}th{background:#f2f2f2}"
        ".muted{color:#777}.nodata{padding:12px;background:#fff4e0;border:1px solid #e0b060}</style>",
        "</head><body>",
        f"<h1>{esc(title)}</h1>",
        "<dl>" + "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in meta) + "</dl>",
    ]
    out += [f'<p class="muted">{esc(n)}</p>' for n in notes]
    if empty:
        out.append('<p class="nodata"><strong>No data.</strong> No latency samples matched this input.</p>')
    out += [
        "<h2>Per-stage latency</h2>",
        f"<table>{head}{''.join(body)}</table>",
        "<h2>End-to-end</h2>",
        "<table>" + row(["Metric", "Count", "p50 ms", "p95 ms"], "th")
        + row([esc(e2e["source"] or "n/a"), e2e["count"], _ms(e2e["p50"]), _ms(e2e["p95"])]) + "</table>",
        "<h2>ASR real-time factor</h2>",
        "<table>" + row(["Metric", "Count", "p50", "p95", "p99", "max"], "th")
        + row([esc(rtf["source"] or "n/a"), rtf["count"], rtf_cell(rtf["p50"]), rtf_cell(rtf["p95"]),
               rtf_cell(rtf["p99"]), rtf_cell(rtf["max"])]) + "</table>",
        "<h2>Chart</h2>",
        _chart(report["stages"]),
        "</body></html>",
    ]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", type=Path, help="captions.sqlite3 or zen.sqlite3 (opened read-only)")
    source.add_argument("--metrics", type=Path, help="JSON or JSONL metrics export")
    parser.add_argument("--session")
    parser.add_argument("--lang", choices=("en", "ja"))
    parser.add_argument("--out", type=Path, help="Output HTML path (default: stdout)")
    parser.add_argument("--title", default="Latency report")
    args = parser.parse_args(argv)
    src = args.db or args.metrics
    if args.out is not None and args.out.resolve() == src.resolve():
        parser.error("--out must not be the input file")
    try:
        samples, notes = load_db(args.db) if args.db else load_metrics(args.metrics)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    page = render_html(build_report(samples, args.session, args.lang), args.title, src.name, notes)
    if args.out is None:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(page)
    else:
        try:
            args.out.write_text(page, encoding="utf-8")
        except OSError as exc:
            parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
