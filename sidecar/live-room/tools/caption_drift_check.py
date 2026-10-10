#!/usr/bin/env python3
"""Offline caption analysis; stdlib only, without importing the application.

  python tools/caption_drift_check.py --db PATH [--session ID] [--json OUT]

CaptionStore (app/store.py) overwrites captions by id. Its zh_raw is raw ASR,
not a previous draft; t0_ms/t1_ms are audio offsets. The optional zen.sqlite3
ledger has sessions/segments/translations (app/admin/schema.sql), but saves
changed texts without draft/final markers (app/ledger.py). Consequently live
draft-to-final metrics are unavailable in both stores.

For the ledger, saved_revision_proxy compares the earliest saved translation
to its current version, including human/post-edit revisions. This is NOT live
caption drift or latency. Rates are the mean per-segment Unicode character
Levenshtein distance / max(text lengths, 1); percentiles use linear rank.
All stored segments, including failed/deleted ones, count toward segments.
Proxy sample counts identify segments with usable saved translations.

JSON is printed to stdout; --json also saves it to a NEW file (never overwrites).
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys


LANGUAGES = ("en", "ja")
DRIFT_REASON = (
    "No explicit first-draft/final text history is stored; raw ASR and saved "
    "translation revisions are not draft/final pairs."
)
LATENCY_REASON = (
    "No first-draft/final event timestamps are stored; audio offsets, latest "
    "updated_at, and revision created_at are not live draft-to-final latency."
)


def levenshtein(left: str, right: str) -> int:
    """Character edit distance, O(min(m, n)) memory; no byte/token splitting."""
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for i, char in enumerate(left, 1):
        current = [i]
        for j, other in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (char != other)))
        previous = current
    return previous[-1]


def normalized_distance(left: str, right: str) -> float:
    return levenshtein(left, right) / max(len(left), len(right), 1)


def latency_distribution(values: list[float]) -> dict:
    ordered = sorted(values)

    def percentile(p: float) -> float | None:
        if not ordered:
            return None
        rank = (len(ordered) - 1) * p
        low = int(rank)
        fraction = rank - low
        return ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * fraction

    return {"p50": percentile(0.50), "p90": percentile(0.90),
            "p99": percentile(0.99), "max": max(ordered) if ordered else None}


def _summary(rows: list[dict], schema: str) -> dict:
    pairs = [row["pair"] for row in rows if row["pair"] is not None]
    spans = [pair[2] for pair in pairs if pair[2] is not None]
    return {
        "segments": len(rows),
        "rewrite_rate": None,
        "share_changed": None,
        "latency_ms": latency_distribution([]),
        "missing_metrics": {
            "rewrite_rate": DRIFT_REASON,
            "share_changed": DRIFT_REASON,
            "latency_ms": LATENCY_REASON,
        },
        "saved_revision_proxy": {
            "samples": len(pairs),
            "rewrite_rate": sum(normalized_distance(p[0], p[1]) for p in pairs) / len(pairs) if pairs else None,
            "share_changed": sum(p[0] != p[1] for p in pairs) / len(pairs) if pairs else None,
            "elapsed_samples": len(spans),
            "elapsed_ms": latency_distribution(spans),
            "reason": (
                "Earliest saved translation to current translation, including post-session "
                "edits; timestamps measure persistence, not live draft/final events."
                if schema == "ledger" else "CaptionStore overwrites versions; no saved revision history."
            ),
        },
    }


def _breakdown(rows: list[dict], schema: str) -> dict:
    result = _summary(rows, schema)
    result["by_target_language"] = {
        lang: _summary([row for row in rows if row["lang"] == lang], schema)
        for lang in LANGUAGES
    }
    return result


def _read(conn: sqlite3.Connection, session: str | None) -> tuple[str, list[dict], list[tuple]]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    params = () if session is None else (session,)
    if "captions" in tables:
        query = "SELECT room_id, session_id FROM captions"
        if session is not None:
            query += " WHERE session_id=?"
        rows = [{"room": room, "session": sess, "lang": "en", "pair": None}
                for room, sess in conn.execute(query, params)]
        return "captions", rows, sorted({(r["room"], r["session"]) for r in rows})
    if {"sessions", "segments", "translations"} <= tables:
        query = "SELECT room_id, id FROM sessions"
        if session is not None:
            query += " WHERE id=?"
        sessions = list(conn.execute(query + " ORDER BY room_id, id", params))
        query = """
            SELECT s.room_id, s.session_id, s.id, se.tgt_lang,
                   t.text, t.created_at, t.is_current
            FROM segments s JOIN sessions se ON se.id=s.session_id
            LEFT JOIN translations t ON t.segment_id=s.id AND t.tgt_lang=se.tgt_lang
        """
        if session is not None:
            query += " WHERE s.session_id=?"
        query += " ORDER BY s.room_id, s.session_id, s.id, t.version"
        grouped: dict[str, dict] = {}
        for room, sess, segment, lang, text, created, current in conn.execute(query, params):
            row = grouped.setdefault(segment, {
                "room": room, "session": sess, "lang": lang, "pair": None, "first": None,
            })
            if text is None:
                continue
            if row["first"] is None:
                row["first"] = (text, created)
            if current:
                first_text, first_created = row["first"]
                elapsed = None
                if isinstance(created, (int, float)) and isinstance(first_created, (int, float)):
                    if created >= first_created:
                        elapsed = (created - first_created) * 1000
                row["pair"] = (first_text, text, elapsed)
        return "ledger", list(grouped.values()), sessions
    if not tables:
        return "empty", [], []
    raise ValueError("unsupported captions database schema (expected captions or ledger tables)")


def analyze(db: str | Path, session: str | None = None) -> dict:
    path = Path(db).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        schema, rows, sessions = _read(conn, session)
    return {
        "schema": schema,
        "notes": [
            "Live draft-to-final metrics are unavailable; see missing_metrics.",
            "CaptionStore has only an en target column; ja cannot be inferred from text."
            if schema == "captions" else "Language groups use sessions.tgt_lang.",
        ],
        "overall": _breakdown(rows, schema),
        "sessions": [
            {"room_id": room, "session_id": sess,
             **_breakdown([r for r in rows if (r["room"], r["session"]) == (room, sess)], schema)}
            for room, sess in sessions
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--session")
    parser.add_argument("--json", type=Path, metavar="OUT", help="also save JSON to a new file")
    args = parser.parse_args(argv)
    try:
        report = analyze(args.db, args.session)
        payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.json is not None:
            db = args.db.resolve()
            protected = {db, *(Path(str(db) + suffix) for suffix in ("-wal", "-shm", "-journal"))}
            if args.json.resolve() in protected:
                raise ValueError("--json must not refer to the database or its SQLite sidecars")
            with args.json.open("x", encoding="utf-8") as output:
                output.write(payload)
        print(payload, end="")
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"caption_drift_check: cannot analyze {args.db}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
