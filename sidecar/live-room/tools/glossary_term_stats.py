#!/usr/bin/env python3
"""Offline, read-only terminology statistics for captions.sqlite3 or zen.sqlite3.

By default the glossary comes from room_glossary or the ledger glossary tables
in --db (the app has no built-in glossary file). --glossary accepts another
SQLite database or a UTF-8 JSON list / {"terms": [...]} or CSV export with zh
and en/ja columns. Only active, nonempty target terms are scored.

Each canonical zh substring counts once per segment, regardless of repetitions;
aliases are not counted. Targets use NFKC substring matching, casefolded for en.
No Chinese-residual heuristic is used for Japanese. Missing translations count
as omissions. Ledger reports use current versions and Chinese transcripts,
excluding deleted segments, for sessions targeting --lang or having a current
translation in that language. The legacy captions table only supports en.

--top limits terms by omission count (descending); overall totals remain uncut.
Hit rates are fractions of term/segment occurrences, or null if none occurred.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
import json
from pathlib import Path
import sqlite3
import sys
import unicodedata


def _open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _database_terms(conn: sqlite3.Connection, lang: str) -> list[dict]:
    tables = _tables(conn)
    if {"glossaries", "glossary_terms"} <= tables:
        if "glossary_term_targets" in tables:
            rows = conn.execute(
                """SELECT t.zh, gt.text AS target, g.room_id, g.session_id
                   FROM glossary_terms t JOIN glossaries g ON g.id=t.glossary_id
                   JOIN glossary_term_targets gt ON gt.term_id=t.id
                   WHERE t.status='active' AND gt.status='active' AND gt.tgt_lang=?""",
                (lang,),
            )
        elif "admin_glossary_lang" in tables:
            rows = conn.execute(
                """SELECT t.zh, t.en AS target, g.room_id, g.session_id
                   FROM glossary_terms t JOIN glossaries g ON g.id=t.glossary_id
                   LEFT JOIN admin_glossary_lang l ON l.glossary_id=g.id
                   WHERE t.status='active' AND COALESCE(l.tgt_lang,'en')=?""",
                (lang,),
            )
        elif lang == "en":
            rows = conn.execute(
                """SELECT t.zh, t.en AS target, g.room_id, g.session_id
                   FROM glossary_terms t JOIN glossaries g ON g.id=t.glossary_id
                   WHERE t.status='active'"""
            )
        else:
            return []
        return [dict(row) for row in rows]
    if "room_glossary" in tables:
        terms = []
        for row in conn.execute("SELECT room_id, terms_json FROM room_glossary"):
            for term in _export_terms(json.loads(row["terms_json"]), lang):
                terms.append({**term, "room_id": row["room_id"]})
        return terms
    raise ValueError("glossary tables not found; supply --glossary with an exported glossary")


def _export_terms(raw, lang: str) -> list[dict]:
    if isinstance(raw, dict):
        raw = raw.get("terms")
    if not isinstance(raw, list) or any(not isinstance(term, dict) for term in raw):
        raise ValueError("glossary must be a list of term objects or an object with terms")
    return [
        {"zh": term.get("zh"), "target": term.get(lang)}
        for term in raw if term.get("status", "active") == "active"
    ]


def _load_terms(path: Path, lang: str) -> list[dict]:
    if path.suffix.lower() in {".json", ".csv"}:
        with path.open(encoding="utf-8-sig", newline="") as source:
            raw = list(csv.DictReader(source)) if path.suffix.lower() == ".csv" else json.load(source)
        return _export_terms(raw, lang)
    with closing(_open_readonly(path)) as conn:
        return _database_terms(conn, lang)


def _segments(conn: sqlite3.Connection, lang: str, session: str | None):
    tables = _tables(conn)
    if {"segments", "sessions", "transcripts", "translations"} <= tables:
        sql = """SELECT s.room_id, s.session_id, t.text AS zh, tr.text AS target
                 FROM segments s JOIN sessions se ON se.id=s.session_id
                 JOIN transcripts t ON t.segment_id=s.id AND t.is_current=1
                 LEFT JOIN translations tr ON tr.segment_id=s.id
                     AND tr.is_current=1 AND tr.tgt_lang=?
                     AND (tr.transcript_id IS NULL OR tr.transcript_id=t.id)
                 WHERE s.status<>'deleted' AND (t.lang='zh' OR t.lang LIKE 'zh-%')
                     AND (se.tgt_lang=? OR tr.id IS NOT NULL)"""
        params = [lang, lang]
    elif "captions" in tables:
        if lang != "en":
            raise ValueError("captions has only an en column; use a language-labelled ledger for ja")
        sql = "SELECT room_id, session_id, zh, en AS target FROM captions WHERE 1=1"
        params = []
    else:
        raise ValueError("unsupported database: caption/ledger tables not found")
    if session is not None:
        sql += " AND " + ("s.session_id=?" if "segments" in tables else "session_id=?")
        params.append(session)
    return conn.execute(sql, params)


def _key(text: str, lang: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return text.casefold() if lang == "en" else text


def collect_stats(
    db_path: str | Path,
    glossary_path: str | Path | None = None,
    lang: str = "en",
    session: str | None = None,
    top: int | None = None,
) -> dict:
    """Read inputs without importing app startup, migrations, or writable helpers."""
    if lang not in {"en", "ja"}:
        raise ValueError("lang must be en or ja")
    if top is not None and top < 1:
        raise ValueError("top must be a positive integer")
    with closing(_open_readonly(Path(db_path))) as conn:
        conn.execute("BEGIN")
        raw = _load_terms(Path(glossary_path), lang) if glossary_path is not None else _database_terms(conn, lang)
        terms = {}
        for term in raw:
            zh, target = term.get("zh"), term.get("target")
            if not isinstance(zh, str) or not zh.strip():
                raise ValueError("glossary zh must be a nonempty string")
            if target is None or target == "":
                continue
            if not isinstance(target, str) or not target.strip():
                raise ValueError("glossary target must be a nonempty string")
            pair = (zh.strip(), target.strip())
            item = terms.setdefault(pair, {
                "zh": pair[0], "target": pair[1], "source_segments": 0, "hits": 0, "omissions": 0,
                "scopes": set(),
            })
            item["scopes"].add((term.get("room_id"), term.get("session_id")))
        for row in _segments(conn, lang, session):
            source = _key(row["zh"] or "", "zh")
            target = _key(row["target"] or "", lang)
            for item in terms.values():
                applies = any(
                    (room is None or room == row["room_id"])
                    and (sid is None or sid == row["session_id"])
                    for room, sid in item["scopes"]
                )
                if applies and _key(item["zh"], "zh") in source:
                    item["source_segments"] += 1
                    item["hits" if _key(item["target"], lang) in target else "omissions"] += 1
    results = []
    for item in terms.values():
        item.pop("scopes")
        item["hit_rate"] = item["hits"] / item["source_segments"] if item["source_segments"] else None
        results.append(item)
    results.sort(key=lambda item: (-item["omissions"], item["zh"], item["target"]))
    total = sum(item["source_segments"] for item in results)
    hits = sum(item["hits"] for item in results)
    return {
        "lang": lang, "session": session,
        "overall": {"source_segments": total, "hits": hits, "omissions": total - hits,
                    "hit_rate": hits / total if total else None},
        "terms": results if top is None else results[:top],
    }


def _text_report(report: dict) -> str:
    overall = report["overall"]
    rate = f"{overall['hit_rate']:.1%}" if overall["hit_rate"] is not None else "n/a"
    lines = [f"{report['lang']}: {overall['hits']}/{overall['source_segments']} hits ({rate})",
             "zh\ttarget\tsource_segments\thits\tomissions\thit_rate"]
    for term in report["terms"]:
        rate = f"{term['hit_rate']:.1%}" if term["hit_rate"] is not None else "n/a"
        fields = [term["zh"], term["target"], term["source_segments"], term["hits"], term["omissions"], rate]
        lines.append("\t".join(str(field).replace("\t", " ").replace("\r", " ").replace("\n", " ")
                               for field in fields))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--glossary", type=Path, help="Default: glossary tables in --db")
    parser.add_argument("--lang", choices=("en", "ja"), default="en")
    parser.add_argument("--session")
    parser.add_argument("--top", type=int)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args(argv)
    try:
        report = collect_stats(args.db, args.glossary, args.lang, args.session, args.top)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json" else _text_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
