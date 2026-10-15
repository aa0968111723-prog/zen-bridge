#!/usr/bin/env python3
"""Offline caption latency SLO check; stdlib only, read-only.

  python tools/latency_slo_check.py --db PATH [--session ID] [--json OUT]
  python tools/latency_slo_check.py --jsonl samples.jsonl [--json OUT]

Input (exactly one of, never modified):
* --db     SQLite opened with URI mode=ro (plus immutable=1 when no WAL file
           exists). Expected table ``latency_samples`` with columns
           id, speech_start_ms, draft_ms, final_ms, translated_ms, target_lang
           (session_id optional). A captions-style table that already carries
           those timing columns is also accepted. CaptionStore's stock
           captions DDL (t0_ms/t1_ms only) has no live draft/final/translation
           wall clocks — use JSONL or a latency_samples export for those.
* --jsonl  UTF-8 JSON Lines. Each object may use
           {id, speech_start_ms, draft_ms, final_ms, translated_ms, target_lang}
           with aliases t0/t0_ms/opening_ms, lang/tgt_lang, translation_ms.

Stages (absolute wall-clock ms → deltas; negative/non-finite deltas discarded):
  draft        speech_start → first draft   (開口→草稿)
  final        draft → final                (草稿→定稿)
  translation  final → translation shown    (定稿→譯文)

Percentiles use nearest-rank (stdlib only). Metrics and SLO pass/fail are
reported for en, ja, and overall. Default SLOs (override with CLI flags):
  draft p95 ≤ 2000 ms, final p95 ≤ 6000 ms, translation p95 ≤ 1500 ms.

JSON (Traditional Chinese summary lines) goes to stdout; --json/--out also
saves to a NEW file and never overwrites the input or SQLite sidecars.
Exit: 0 all judged SLOs pass, 1 any fails, 2 unreadable input / bad options /
no usable timing samples. No network, no app imports, no listening port.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import sys


TOOL = "latency_slo_check"
LANGUAGES = ("en", "ja")
STAGES = ("draft", "final", "translation")
STAGE_LABELS_ZH = {
    "draft": "開口→草稿",
    "final": "草稿→定稿",
    "translation": "定稿→譯文",
}
DEFAULT_SLO_MS = {
    "draft": 2000.0,
    "final": 6000.0,
    "translation": 1500.0,
}
# Absolute timestamp field names accepted on each row (first hit wins).
FIELD_ALIASES = {
    "speech_start_ms": ("speech_start_ms", "speech_start", "opening_ms", "t0_ms", "t0"),
    "draft_ms": ("draft_ms", "draft", "t_draft", "t_draft_ms"),
    "final_ms": ("final_ms", "final", "t_final", "t_final_ms"),
    "translated_ms": ("translated_ms", "translation_ms", "translated", "t_translated",
                      "t_mt_done", "t_mt1"),
    "target_lang": ("target_lang", "lang", "tgt_lang"),
    "id": ("id",),
    "session_id": ("session_id", "session"),
}
LATENCY_TABLE_CANDIDATES = ("latency_samples", "caption_latency", "captions")
REQUIRED_TIMING = ("speech_start_ms", "draft_ms", "final_ms", "translated_ms")


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


def _slos(overrides: dict | None) -> dict[str, float]:
    values = dict(DEFAULT_SLO_MS)
    for key, value in (overrides or {}).items():
        if key not in DEFAULT_SLO_MS:
            raise ValueError(f"unknown SLO {key}")
        if value is not None:
            values[key] = value
    return {key: _positive_ms(f"{key}_p95_ms", values[key]) for key in STAGES}


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


def row_stages(row: dict) -> dict[str, float | None]:
    """Compute stage deltas (ms). Missing inputs → None; bad deltas → None."""
    speech = _number(_pick(row, "speech_start_ms"))
    draft = _number(_pick(row, "draft_ms"))
    final = _number(_pick(row, "final_ms"))
    translated = _number(_pick(row, "translated_ms"))
    out: dict[str, float | None] = {"draft": None, "final": None, "translation": None}
    if speech is not None and draft is not None and draft >= speech:
        out["draft"] = draft - speech
    if draft is not None and final is not None and final >= draft:
        out["final"] = final - draft
    if final is not None and translated is not None and translated >= final:
        out["translation"] = translated - final
    return out


def summarize_stage(values: list[float], missing: int, slo_ms: float) -> dict:
    p95 = nearest_rank_percentile(values, 0.95)
    passed = True if not values else (p95 is not None and p95 <= slo_ms)
    return {
        "count": len(values),
        "missing": missing,
        "p50": _clean(nearest_rank_percentile(values, 0.50)),
        "p90": _clean(nearest_rank_percentile(values, 0.90)),
        "p95": _clean(p95),
        "max": _clean(max(values)) if values else None,
        "slo_p95_ms": _clean(slo_ms),
        "pass": passed,
    }


def _group_metrics(rows: list[dict], slos: dict[str, float]) -> dict:
    buckets = {stage: [] for stage in STAGES}
    missing = {stage: 0 for stage in STAGES}
    for row in rows:
        stages = row_stages(row)
        for stage in STAGES:
            value = stages[stage]
            if value is None:
                missing[stage] += 1
            else:
                buckets[stage].append(value)
    stages_out = {stage: summarize_stage(buckets[stage], missing[stage], slos[stage])
                  for stage in STAGES}
    judged = [stages_out[stage] for stage in STAGES if stages_out[stage]["count"]]
    all_pass = all(item["pass"] for item in judged) if judged else False
    return {
        "rows": len(rows),
        "stages": stages_out,
        "pass": all_pass,
        "summary_zh": _summary_zh(stages_out, all_pass, judged),
    }


def _summary_zh(stages_out: dict, all_pass: bool, judged: list) -> str:
    if not judged:
        return "沒有可用的延遲樣本，無法判定 SLO。"
    parts = []
    for stage in STAGES:
        info = stages_out[stage]
        if not info["count"]:
            parts.append(f"{STAGE_LABELS_ZH[stage]}：無樣本")
            continue
        mark = "通過" if info["pass"] else "未通過"
        parts.append(
            f"{STAGE_LABELS_ZH[stage]} p95={info['p95']}ms（門檻 {info['slo_p95_ms']}ms）{mark}"
        )
    verdict = "全部 SLO 通過。" if all_pass else "有 SLO 未通過，請檢查延遲。"
    return "；".join(parts) + "。" + verdict


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


def _resolve_latency_table(conn: sqlite3.Connection) -> tuple[str, set[str]]:
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    for name in LATENCY_TABLE_CANDIDATES:
        if name not in tables:
            continue
        columns = _table_columns(conn, name)
        # Need target_lang (or lang) plus at least two timing columns to be useful.
        has_lang = bool(columns & set(FIELD_ALIASES["target_lang"]))
        timing_hits = sum(1 for field in REQUIRED_TIMING
                          if columns & set(FIELD_ALIASES[field]))
        if has_lang and timing_hits >= 2:
            return name, columns
    # Fall back: any table with the timing + lang columns.
    for name in sorted(tables):
        columns = _table_columns(conn, name)
        has_lang = bool(columns & set(FIELD_ALIASES["target_lang"]))
        timing_hits = sum(1 for field in REQUIRED_TIMING
                          if columns & set(FIELD_ALIASES[field]))
        if has_lang and timing_hits >= 2:
            return name, columns
    raise ValueError(
        "unsupported database schema (expected latency_samples with "
        "speech_start_ms/draft_ms/final_ms/translated_ms/target_lang)"
    )


def _read_db(db: Path, session: str | None) -> tuple[str, list[dict]]:
    path = Path(db).resolve()
    with closing(_open_ro(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        table, columns = _resolve_latency_table(conn)
        select_cols = sorted(columns)
        query = f'SELECT {", ".join(select_cols)} FROM "{table}"'
        params: tuple = ()
        # session filter if column present
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
            for key in REQUIRED_TIMING:
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
    for row in rows:
        stages = row_stages(row)
        if any(stages[stage] is not None for stage in STAGES):
            return True
    return False


def analyze(*, db: Path | None = None, jsonl: Path | None = None,
            session: str | None = None, thresholds: dict | None = None) -> dict:
    if (db is None) == (jsonl is None):
        raise ValueError("provide exactly one of --db or --jsonl")
    slos = _slos(thresholds)
    schema, rows = _read_db(Path(db), session) if db is not None else _read_jsonl(Path(jsonl), session)
    # Normalize language on DB rows too.
    normalized: list[dict] = []
    for row in rows:
        item = dict(row)
        lang = _normalize_lang(_pick(item, "target_lang"))
        item["target_lang"] = lang
        normalized.append(item)
    if not _usable(normalized):
        raise ValueError("no usable timing samples")
    by_lang = {
        lang: _group_metrics([r for r in normalized if r.get("target_lang") == lang], slos)
        for lang in LANGUAGES
    }
    overall = _group_metrics(normalized, slos)
    # Languages with samples must pass; overall must pass.
    judged_pass = overall["pass"] and all(
        by_lang[lang]["pass"] for lang in LANGUAGES if by_lang[lang]["stages"]
        and any(by_lang[lang]["stages"][s]["count"] for s in STAGES)
    )
    unknown = sum(1 for r in normalized if r.get("target_lang") not in LANGUAGES)
    return {
        "tool": TOOL,
        "schema": schema,
        "session": session,
        "slos_ms": {key: _clean(slos[key]) for key in STAGES},
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
    source.add_argument("--db", type=Path, help="SQLite with latency_samples (read-only)")
    source.add_argument("--jsonl", type=Path, help="JSON Lines of latency wall-clock samples")
    parser.add_argument("--session", help="only analyse this session_id")
    parser.add_argument("--draft-p95-ms", type=float, default=DEFAULT_SLO_MS["draft"],
                        help="draft (opening→draft) p95 SLO in ms")
    parser.add_argument("--final-p95-ms", type=float, default=DEFAULT_SLO_MS["final"],
                        help="final (draft→final) p95 SLO in ms")
    parser.add_argument("--translation-p95-ms", type=float, default=DEFAULT_SLO_MS["translation"],
                        help="translation (final→translation) p95 SLO in ms")
    parser.add_argument("--json", type=Path, metavar="OUT", help="also save JSON to a NEW file")
    parser.add_argument("--out", type=Path, metavar="OUT", dest="out",
                        help="alias for --json")
    args = parser.parse_args(argv)
    out_path = args.json if args.json is not None else args.out
    label = args.db if args.db is not None else args.jsonl
    try:
        if out_path is not None:
            if args.db is not None:
                db = args.db.resolve()
                protected = {db, *(Path(str(db) + suffix) for suffix in ("-wal", "-shm", "-journal"))}
            else:
                protected = {args.jsonl.resolve()}
            if out_path.resolve() in protected:
                raise ValueError("--json/--out must not refer to the input or its SQLite sidecars")
            if out_path.exists() or out_path.is_symlink():
                raise ValueError(f"refusing to overwrite existing file {out_path}")
        report = analyze(
            db=args.db, jsonl=args.jsonl, session=args.session,
            thresholds={"draft": args.draft_p95_ms, "final": args.final_p95_ms,
                        "translation": args.translation_p95_ms},
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