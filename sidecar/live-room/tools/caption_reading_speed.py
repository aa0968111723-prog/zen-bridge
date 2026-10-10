#!/usr/bin/env python3
"""Offline reading-speed estimates for captions.sqlite3 (en) or the ledger (en/ja).

Usage: python tools/caption_reading_speed.py --db PATH [--session ID] [--json OUT]

Final captions are CaptionStore's ready rows or current translations of translated
ledger segments in the session's target language. t0_ms/t1_ms are audio offsets,
so duration is a segmentation proxy, not measured time on viewers' screens.
Missing/non-positive spans use the next final caption's start in the same
room/session/language, then --default-duration (seconds, default 3).
Text is stripped at both ends. Japanese CJK/kana/full-width characters count as
1, ASCII and other narrow characters as 0.5. Percentiles use linear rank.
--json also saves stdout's report to a new file; existing files are never replaced.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import sys
import unicodedata


LANGUAGES = ("en", "ja")


def character_count(text: str, lang: str) -> float:
    text = text.strip()
    if lang == "en":
        return len(text)
    return sum(
        1 if unicodedata.east_asian_width(char) in ("W", "F") else 0.5
        for char in text
    )


def _positive(value: str | float) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("durations and thresholds must be finite and positive")
    return number


def _read(conn: sqlite3.Connection, session: str | None) -> tuple[str, list]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    params = () if session is None else (session,)
    if "captions" in tables:
        schema = "captions"
        query = """
            SELECT id, room_id, session_id, 'en' AS lang, en AS text, t0_ms, t1_ms, seq
            FROM captions WHERE status='ready'
        """
        if session is not None:
            query += " AND session_id=?"
        query += " ORDER BY room_id, session_id, t0_ms, seq, id"
    elif {"sessions", "segments", "translations"} <= tables:
        schema = "ledger"
        query = """
            SELECT s.id, s.room_id, s.session_id, t.tgt_lang AS lang,
                   t.text, s.t0_ms, s.t1_ms, s.seq
            FROM segments s
            JOIN sessions se ON se.id=s.session_id
            JOIN translations t ON t.segment_id=s.id AND t.tgt_lang=se.tgt_lang
            WHERE s.status='translated' AND t.is_current=1
                  AND t.status IN ('ok', 'merged') AND t.tgt_lang IN ('en', 'ja')
        """
        if session is not None:
            query += " AND s.session_id=?"
        query += " ORDER BY s.room_id, s.session_id, t.tgt_lang, s.t0_ms, s.seq, s.id"
    else:
        raise ValueError("unsupported captions database schema (expected captions or ledger tables)")
    return schema, list(conn.execute(query, params))


def _distribution(values: list[float]) -> dict:
    ordered = sorted(values)

    def percentile(p: float) -> float | None:
        if not ordered:
            return None
        rank = (len(ordered) - 1) * p
        low = int(rank)
        fraction = rank - low
        return ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * fraction

    return {"p50_cps": percentile(0.5), "p90_cps": percentile(0.9),
            "max_cps": ordered[-1] if ordered else None}


def analyze(db: str | Path, session: str | None = None, *,
            default_duration: float = 3, en_cps: float = 17, ja_cps: float = 8) -> dict:
    default_duration = _positive(default_duration)
    thresholds = {"en": _positive(en_cps), "ja": _positive(ja_cps)}
    path = Path(db).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        schema, rows = _read(conn, session)
    speeds: dict[str, list[float]] = {lang: [] for lang in LANGUAGES}
    flagged = []
    for index, row in enumerate(rows):
        text = (row["text"] or "").strip()
        if not text:
            continue
        start, end = row["t0_ms"], row["t1_ms"]
        duration = default_duration
        if start is not None:
            if end is not None and end > start:
                duration = (end - start) / 1000
            elif index + 1 < len(rows):
                next_row = rows[index + 1]
                group = ("room_id", "session_id", "lang")
                next_start = next_row["t0_ms"]
                if (all(row[key] == next_row[key] for key in group)
                        and next_start is not None and next_start > start):
                    duration = (next_start - start) / 1000
        lang = row["lang"]
        cps = character_count(text, lang) / duration
        speeds[lang].append(cps)
        if cps > thresholds[lang]:
            flagged.append({"id": row["id"], "session": row["session_id"],
                            "lang": lang, "cps": cps, "text": text[:80]})
    return {
        "schema": schema,
        "notes": [
            "Durations use audio offsets, not measured screen-display times.",
            "CaptionStore has only an en target column; ja requires the ledger's language tags."
            if schema == "captions" else "Only current translations in each session's target language are measured.",
        ],
        "by_language": {
            lang: {"count": len(speeds[lang]), **_distribution(speeds[lang]),
                   "threshold_cps": thresholds[lang],
                   "flagged_count": sum(item["lang"] == lang for item in flagged)}
            for lang in LANGUAGES
        },
        "flagged_count": len(flagged),
        "flagged": flagged,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--session")
    parser.add_argument("--default-duration", type=float, default=3, metavar="SECONDS")
    parser.add_argument("--en-cps", type=float, default=17)
    parser.add_argument("--ja-cps", type=float, default=8)
    parser.add_argument("--json", type=Path, metavar="OUT", help="also save JSON to a new file")
    args = parser.parse_args(argv)
    try:
        if args.json is not None:
            db = args.db.resolve()
            protected = {db, *(Path(str(db) + suffix) for suffix in ("-wal", "-shm", "-journal"))}
            if args.json.resolve() in protected:
                raise ValueError("--json must not refer to the database or its SQLite sidecars")
        report = analyze(args.db, args.session, default_duration=args.default_duration,
                         en_cps=args.en_cps, ja_cps=args.ja_cps)
        payload = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if args.json is not None:
            with args.json.open("x", encoding="utf-8") as output:
                output.write(payload)
        print(payload, end="")
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"caption_reading_speed: cannot analyze {args.db}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
