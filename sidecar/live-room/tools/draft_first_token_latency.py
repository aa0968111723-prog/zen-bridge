#!/usr/bin/env python3
"""Offline draft first-token latency report; stdlib only, read-only.

  python tools/draft_first_token_latency.py --db PATH [--session ID] [--json OUT]
  python tools/draft_first_token_latency.py --jsonl samples.jsonl [--json OUT]

Measures 「開口→第一個草稿字」 (speech_start → first draft token) for streaming
draft budgets aligned with Ofcom-style live-caption delay and the 1–2 s draft
stream target used elsewhere in zen-bridge.

Input (exactly one of, never modified):
* --db     SQLite opened with URI mode=ro (plus immutable=1 when no WAL).
           Accepts ``latency_samples`` / ``caption_latency`` / captions-style
           tables that expose speech_start + first-token (or draft) wall clocks
           and target_lang.
* --jsonl  UTF-8 JSON Lines. Each object may use
           {id, speech_start_ms, first_token_ms, target_lang} with aliases
           t0/t0_ms/opening_ms, draft_ms/t_draft/t_first_token, lang/tgt_lang.

Percentiles use nearest-rank (stdlib only). Metrics are reported for en, ja,
and overall. Default budget: first-token p95 ≤ 2000 ms (override with
--budget-p95-ms). JSON includes Traditional Chinese summary lines.

Exit: 0 budget passes (or no samples judged), 1 budget fails, 2 unreadable
input / bad options / no usable samples. No network, no app imports, no port.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import sys


TOOL = "draft_first_token_latency"
LANGUAGES = ("en", "ja")
DEFAULT_BUDGET_P95_MS = 2000.0  # Ofcom-aligned / draft-stream 1–2 s target

FIELD_ALIASES = {
    "speech_start_ms": ("speech_start_ms", "speech_start", "opening_ms", "t0_ms", "t0"),
    "first_token_ms": (
        "first_token_ms",
        "first_draft_ms",
        "t_first_token",
        "t_first_token_ms",
        "draft_ms",
        "draft",
        "t_draft",
        "t_draft_ms",
    ),
    "target_lang": ("target_lang", "lang", "tgt_lang"),
    "id": ("id",),
    "session_id": ("session_id", "session"),
}
TABLE_CANDIDATES = ("latency_samples", "caption_latency", "captions")
REQUIRED = ("speech_start_ms", "first_token_ms")


def nearest_rank_percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile for p in (0, 1]; None when empty."""
    if not values:
        return None
    if not (0 < p <= 1):
        raise ValueError("percentile p must be in (0, 1]")
    ordered = sorted(values)
    rank = max(1, math.ceil(p * len(ordered)))
    return float(ordered[rank - 1])


def _clean(value: float | None) -> int | float | None:
    if value is None:
        return None
    number = round(float(value), 3)
    if number == int(number) and abs(number) < 10**15:
        return int(number)
    return number


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _positive_ms(name: str, value) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return number


def _pick(row: dict, field: str):
    for alias in FIELD_ALIASES[field]:
        if alias in row and row[alias] is not None:
            return row[alias]
    return None


def _normalize_lang(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("target_lang must be a string")
    lang = value.strip().lower()
    if not lang:
        return None
    if lang in ("en", "eng", "english"):
        return "en"
    if lang in ("ja", "jp", "jpn", "japanese", "日本語"):
        return "ja"
    return lang


def first_token_delta_ms(row: dict) -> float | None:
    """speech_start → first draft token (ms). Missing/reversed → None."""
    speech = _number(_pick(row, "speech_start_ms"))
    token = _number(_pick(row, "first_token_ms"))
    if speech is None or token is None:
        return None
    if token < speech:
        return None
    return token - speech


def summarize(values: list[float], missing: int, budget_p95_ms: float) -> dict:
    p95 = nearest_rank_percentile(values, 0.95)
    passed = True if not values else (p95 is not None and p95 <= budget_p95_ms)
    return {
        "count": len(values),
        "missing": missing,
        "p50": _clean(nearest_rank_percentile(values, 0.50)),
        "p90": _clean(nearest_rank_percentile(values, 0.90)),
        "p95": _clean(p95),
        "max": _clean(max(values)) if values else None,
        "budget_p95_ms": _clean(budget_p95_ms),
        "pass": passed,
    }


def _summary_zh(info: dict) -> str:
    if not info["count"]:
        return "沒有可用的開口→第一個草稿字樣本，無法判定預算。"
    mark = "通過" if info["pass"] else "未通過"
    return (
        f"開口→第一個草稿字 p50={info['p50']}ms、p90={info['p90']}ms、"
        f"p95={info['p95']}ms、max={info['max']}ms"
        f"（預算 p95≤{info['budget_p95_ms']}ms）{mark}。"
    )


def _group_metrics(rows: list[dict], budget_p95_ms: float) -> dict:
    values: list[float] = []
    missing = 0
    for row in rows:
        delta = first_token_delta_ms(row)
        if delta is None:
            missing += 1
        else:
            values.append(delta)
    info = summarize(values, missing, budget_p95_ms)
    return {
        "rows": len(rows),
        "first_token": info,
        "pass": info["pass"] if info["count"] else False,
        "summary_zh": _summary_zh(info),
    }


def _open_ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError(f"database not found: {path}")
    uri = path.as_uri() + "?mode=ro"
    if not Path(str(path) + "-wal").exists():
        uri += "&immutable=1"
    elif not Path(str(path) + "-shm").exists():
        raise ValueError("WAL without SHM: checkpoint the database before offline analysis")
    return sqlite3.connect(uri, uri=True)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _resolve_table(conn: sqlite3.Connection) -> tuple[str, set[str]]:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }

    def score(columns: set[str]) -> int:
        has_lang = bool(columns & set(FIELD_ALIASES["target_lang"]))
        timing = sum(1 for field in REQUIRED if columns & set(FIELD_ALIASES[field]))
        if not has_lang or timing < 2:
            return -1
        return timing

    for name in TABLE_CANDIDATES:
        if name not in tables:
            continue
        columns = _table_columns(conn, name)
        if score(columns) >= 0:
            return name, columns
    for name in sorted(tables):
        columns = _table_columns(conn, name)
        if score(columns) >= 0:
            return name, columns
    raise ValueError(
        "unsupported database schema (expected speech_start_ms + first_token_ms/"
        "draft_ms + target_lang)"
    )


def _read_db(db: Path, session: str | None) -> tuple[str, list[dict]]:
    path = Path(db).resolve()
    with closing(_open_ro(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        table, columns = _resolve_table(conn)
        select_cols = sorted(columns)
        query = f'SELECT {", ".join(select_cols)} FROM "{table}"'
        params: tuple = ()
        session_col = next((c for c in ("session_id", "session") if c in columns), None)
        if session is not None:
            if session_col is None:
                raise ValueError("database has no session_id column for --session filter")
            query += f' WHERE "{session_col}"=?'
            params = (session,)
        query += " ORDER BY rowid"
        rows = [dict(row) for row in conn.execute(query, params)]
    return f"sqlite:{table}", rows


def _read_jsonl(path: Path, session: str | None) -> tuple[str, list[dict]]:
    rows: list[dict] = []
    with Path(path).open(encoding="utf-8-sig") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {number}: invalid JSON ({exc.msg})") from None
            if not isinstance(value, dict):
                raise ValueError(f"line {number}: expected a JSON object")
            row = dict(value)
            row.setdefault("id", number)
            if session is not None:
                sid = _pick(row, "session_id")
                if sid != session:
                    continue
            lang = _normalize_lang(_pick(row, "target_lang"))
            if lang is not None:
                row["target_lang"] = lang
            for key in REQUIRED:
                item = _pick(row, key)
                if item is None:
                    continue
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    raise ValueError(f"line {number}: {key} must be a number")
                if not math.isfinite(float(item)):
                    raise ValueError(f"line {number}: {key} must be finite")
            rows.append(row)
    return "jsonl", rows


def _usable(rows: list[dict]) -> bool:
    return any(first_token_delta_ms(row) is not None for row in rows)


def analyze(
    *,
    db: Path | None = None,
    jsonl: Path | None = None,
    session: str | None = None,
    budget_p95_ms: float = DEFAULT_BUDGET_P95_MS,
) -> dict:
    if (db is None) == (jsonl is None):
        raise ValueError("provide exactly one of --db or --jsonl")
    budget = _positive_ms("budget_p95_ms", budget_p95_ms)
    schema, rows = (
        _read_db(Path(db), session) if db is not None else _read_jsonl(Path(jsonl), session)
    )
    normalized: list[dict] = []
    for row in rows:
        item = dict(row)
        lang = _normalize_lang(_pick(item, "target_lang"))
        item["target_lang"] = lang
        normalized.append(item)
    if not _usable(normalized):
        raise ValueError("no usable first-token latency samples")
    by_lang = {
        lang: _group_metrics(
            [r for r in normalized if r.get("target_lang") == lang], budget
        )
        for lang in LANGUAGES
    }
    overall = _group_metrics(normalized, budget)
    judged_pass = overall["pass"] and all(
        by_lang[lang]["pass"]
        for lang in LANGUAGES
        if by_lang[lang]["first_token"]["count"]
    )
    unknown = sum(1 for r in normalized if r.get("target_lang") not in LANGUAGES)
    return {
        "tool": TOOL,
        "schema": schema,
        "session": session,
        "metric": "opening_to_first_draft_token_ms",
        "budget_p95_ms": _clean(budget),
        "percentile": "nearest-rank",
        "overall": overall,
        "by_target_language": by_lang,
        "unknown_language_rows": unknown,
        "pass": judged_pass,
        "summary_zh": _overall_summary_zh(overall, by_lang, judged_pass),
    }


def _overall_summary_zh(overall: dict, by_lang: dict, judged_pass: bool) -> str:
    bits = [f"整體：{overall['summary_zh']}"]
    for lang in LANGUAGES:
        label = "英語" if lang == "en" else "日語"
        bits.append(f"{label}：{by_lang[lang]['summary_zh']}")
    bits.append("綜合判定：通過。" if judged_pass else "綜合判定：未通過。")
    return " ".join(bits)


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", type=Path, help="SQLite with latency samples (read-only)")
    source.add_argument("--jsonl", type=Path, help="JSON Lines of first-token wall-clock samples")
    parser.add_argument("--session", help="only analyse this session_id")
    parser.add_argument(
        "--budget-p95-ms",
        type=float,
        default=DEFAULT_BUDGET_P95_MS,
        help="first-token p95 budget in ms (default 2000)",
    )
    parser.add_argument("--json", type=Path, metavar="OUT", help="also save JSON to a NEW file")
    parser.add_argument("--out", type=Path, metavar="OUT", dest="out", help="alias for --json")
    args = parser.parse_args(argv)
    out_path = args.json if args.json is not None else args.out
    label = args.db if args.db is not None else args.jsonl
    try:
        if out_path is not None:
            if args.db is not None:
                db = args.db.resolve()
                protected = {
                    db,
                    *(Path(str(db) + suffix) for suffix in ("-wal", "-shm", "-journal")),
                }
            else:
                protected = {args.jsonl.resolve()}
            if out_path.resolve() in protected:
                raise ValueError("--json/--out must not refer to the input or its SQLite sidecars")
            if out_path.exists() or out_path.is_symlink():
                raise ValueError(f"refusing to overwrite existing file {out_path}")
        report = analyze(
            db=args.db,
            jsonl=args.jsonl,
            session=args.session,
            budget_p95_ms=args.budget_p95_ms,
        )
        payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if out_path is not None:
            with out_path.open("x", encoding="utf-8") as output:
                output.write(payload)
    except (OSError, sqlite3.Error, ValueError, UnicodeDecodeError) as exc:
        print(f"{TOOL}: cannot analyze {label}: {exc}", file=sys.stderr)
        return 2
    print(payload, end="")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
