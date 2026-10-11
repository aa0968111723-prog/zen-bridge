#!/usr/bin/env python3
"""Suggest glossary gaps from final Chinese captions; stdlib only, read-only.

Usage: python tools/glossary_gap_candidates.py --db PATH --glossary PATH
       [--session ID] [--min-count 3] [--out NEW.csv] [--excel]

Uses CaptionStore's ready rows and zh (not raw ASR). Each occurrence of a
contiguous Han-character 2–4 gram counts, including overlapping occurrences.
Punctuation, whitespace, digits and Latin/kana characters break runs. sessions
is the number of distinct session IDs containing the term. Results are sorted
by descending count, then term; the first caption by ID supplies the example.

The deliberately small exact stoplist covers pronouns, conjunctions, discourse
fillers and common grammatical phrases in simplified/traditional Chinese:
我們/我们, 你們/你们, 他們/他们, 這個/这个, 那個/那个, 這些/这些,
那些, 然後/然后, 所以, 因為/因为, 但是, 就是, 不是, 一個/一个,
的話/的话, 什麼/什么. It does not remove these characters inside other terms.

Glossary formats and active-term loading match glossary_term_stats.py (UTF-8
JSON list / {"terms": [...]} or CSV, or SQLite). Chinese entries from both en
and ja are excluded, even when an export's target is blank. A candidate that
is a substring of a glossary term is also excluded. Shorter candidates wholly
contained in a longer remaining candidate with the same count are suppressed.
Inputs are never written; --out creates a NEW file and never replaces files.
--excel adds a UTF-8 BOM, including when writing to stdout.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
from pathlib import Path
import re
import sqlite3
import sys

if __package__:
    from .glossary_term_stats import _load_terms, _open_readonly, _tables
else:
    from glossary_term_stats import _load_terms, _open_readonly, _tables


STOPLIST = frozenset(
    "我們 我们 你們 你们 他們 他们 這個 这个 那個 那个 這些 这些 那些 "
    "然後 然后 所以 因為 因为 但是 就是 不是 一個 一个 的話 的话 什麼 什么".split()
)
HAN_RUN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\U00020000-\U0002fa1f\U00030000-\U0003347f]+"
)
FIELDS = ("term", "count", "sessions", "example_caption_id", "example_text")


def collect_candidates(
    db_path: str | Path,
    glossary_path: str | Path,
    min_count: int = 3,
    session: str | None = None,
) -> list[dict]:
    if min_count < 1:
        raise ValueError("min-count must be a positive integer")
    glossary = set()
    for lang in ("en", "ja"):
        for entry in _load_terms(Path(glossary_path), lang):
            term = entry.get("zh")
            if not isinstance(term, str) or not term.strip():
                raise ValueError("glossary zh must be a nonempty string")
            glossary.add(term.strip())
    candidates = {}
    with closing(_open_readonly(Path(db_path))) as conn:
        conn.execute("BEGIN")
        if "captions" not in _tables(conn):
            raise ValueError("unsupported database: captions table not found")
        query = "SELECT id, session_id, zh FROM captions WHERE status='ready'"
        params = []
        if session is not None:
            query += " AND session_id=?"
            params.append(session)
        for row in conn.execute(query + " ORDER BY id", params):
            text = row["zh"] or ""
            if not isinstance(text, str):
                raise ValueError("caption zh must be text")
            for run in HAN_RUN.findall(text):
                for size in range(2, 5):
                    for start in range(len(run) - size + 1):
                        term = run[start:start + size]
                        item = candidates.setdefault(term, {
                            "term": term, "count": 0, "sessions": set(),
                            "example_caption_id": row["id"], "example_text": text[:60],
                        })
                        item["count"] += 1
                        item["sessions"].add(row["session_id"])
    candidates = {
        term: item for term, item in candidates.items()
        if item["count"] >= min_count and term not in STOPLIST
        and not any(term in known for known in glossary)
    }
    suppressed = set()
    for term, item in candidates.items():
        for size in range(2, len(term)):
            for start in range(len(term) - size + 1):
                shorter = term[start:start + size]
                if shorter in candidates and candidates[shorter]["count"] == item["count"]:
                    suppressed.add(shorter)
    results = [
        {**item, "sessions": len(item["sessions"])}
        for term, item in candidates.items() if term not in suppressed
    ]
    return sorted(results, key=lambda item: (-item["count"], item["term"]))


def _write_csv(output, rows: list[dict], excel: bool) -> None:
    if excel:
        output.write("\ufeff")
    writer = csv.DictWriter(output, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--glossary", type=Path, required=True)
    parser.add_argument("--session")
    parser.add_argument("--min-count", type=int, default=3)
    parser.add_argument("--out", type=Path, help="CSV destination (must not exist); default stdout")
    parser.add_argument("--excel", action="store_true", help="add a UTF-8 BOM for Windows Excel")
    args = parser.parse_args(argv)
    try:
        rows = collect_candidates(args.db, args.glossary, args.min_count, args.session)
        if args.out is not None:
            protected = {
                Path(str(path.resolve()) + suffix)
                for path in (args.db, args.glossary)
                for suffix in ("", "-wal", "-shm", "-journal")
            }
            if args.out.resolve() in protected:
                raise ValueError("--out must not refer to an input or its SQLite sidecars")
            with args.out.open("x", encoding="utf-8", newline="") as output:
                _write_csv(output, rows, args.excel)
        else:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            _write_csv(sys.stdout, rows, args.excel)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"glossary_gap_candidates: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
