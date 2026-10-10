"""Segment search with length routing (database-review §4.2).

- >= 3 characters: trigram FTS (transcripts_fts), phrase-quoted.
- 1-2 characters: unigram FTS (transcripts_fts_uni, unicode61 over the space-separated
  text_uni); CJK as an adjacent phrase "因 緣", other tokens as a prefix "AI"*.
  Rows written before text_uni existed are covered by an instr() fallback that only scans
  the partial index transcripts_cur_nouni.
- English (translations_fts, porter unicode61): phrase-quoted.
Results are keyset-paginated on (score, id); bm25 is negative, lower = better.
"""
from __future__ import annotations

import re
import sqlite3

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003134f]")


def phrase(q: str) -> str:
    return '"' + q.replace('"', '""') + '"'


def unigram_query(q: str) -> str:
    q = q.strip()
    if _CJK.search(q):
        return phrase(" ".join(ch for ch in q if not ch.isspace()))
    return phrase(q) + "*"


def route(q: str) -> str:
    n = len("".join(q.split()))
    return "trigram" if n >= 3 else "unigram"


def search_transcripts(c: sqlite3.Connection, q: str, *, limit: int = 20, after: tuple | None = None,
                       session_id: str | None = None) -> tuple[list[dict], str]:
    mode = route(q)
    if mode == "trigram":
        hits = ("SELECT t.id AS owner_id, t.segment_id, bm25(transcripts_fts) AS score, "
                "snippet(transcripts_fts, 0, '[', ']', '…', 16) AS snippet "
                "FROM transcripts_fts JOIN transcripts t ON t.id = transcripts_fts.rowid "
                "WHERE transcripts_fts MATCH ? AND t.is_current = 1")
        args: list = [phrase(q.strip())]
    else:
        needle = "".join(q.split())
        hits = ("SELECT t.id AS owner_id, t.segment_id, bm25(transcripts_fts_uni) AS score, t.text AS snippet "
                "FROM transcripts_fts_uni JOIN transcripts t ON t.id = transcripts_fts_uni.rowid "
                "WHERE transcripts_fts_uni MATCH ? AND t.is_current = 1 "
                "UNION ALL "
                "SELECT t.id, t.segment_id, 0.0, t.text FROM transcripts t INDEXED BY transcripts_cur_nouni "
                "WHERE t.is_current = 1 AND t.text_uni IS NULL AND instr(t.text, ?) > 0")
        args = [unigram_query(q), needle]
        mode = "unigram+instr"
    return _page(c, hits, args, "transcript", limit, after, session_id), mode


def search_translations(c: sqlite3.Connection, q: str, *, limit: int = 20, after: tuple | None = None,
                        session_id: str | None = None) -> tuple[list[dict], str]:
    hits = ("SELECT t.id AS owner_id, t.segment_id, bm25(translations_fts) AS score, "
            "snippet(translations_fts, 0, '[', ']', '…', 16) AS snippet "
            "FROM translations_fts JOIN translations t ON t.id = translations_fts.rowid "
            "WHERE translations_fts MATCH ? AND t.is_current = 1")
    try:
        return _page(c, hits, [phrase(q.strip())], "translation", limit, after, session_id), "porter"
    except sqlite3.OperationalError:
        return [], "porter"


def _page(c, hits_sql: str, args: list, owner_type: str, limit: int, after, session_id) -> list[dict]:
    sql = (f"WITH hits AS ({hits_sql}) SELECT '{owner_type}' AS owner_type, h.owner_id, h.segment_id, h.score, "
           "h.snippet, g.session_id, g.t0_ms FROM hits h JOIN segments g ON g.id = h.segment_id WHERE 1=1")
    if after is not None:
        sql += " AND (h.score > ? OR (h.score = ? AND h.owner_id > ?))"
        args = args + [after[0], after[0], after[1]]
    if session_id:
        sql += " AND g.session_id = ?"
        args = args + [session_id]
    sql += " ORDER BY h.score, h.owner_id LIMIT ?"
    return [dict(r) for r in c.execute(sql, args + [limit])]
