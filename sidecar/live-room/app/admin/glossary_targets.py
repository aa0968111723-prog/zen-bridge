"""Per-target-language glossary reads (round2 T7, schema v3 glossary_term_targets).

glossary_terms.en stays the write path this version (triggers keep the child table in sync);
readers that need a language use this so a ja session never gets the English list.
"""
from __future__ import annotations

import sqlite3

from app.mt_backend import validate_tgt_lang


def terms_for(c: sqlite3.Connection, tgt_lang: str, *, glossary_id: int | None = None,
              status: str = "active") -> list[dict]:
    lang = validate_tgt_lang(tgt_lang)
    sql = ("SELECT t.zh, gt.text AS tgt, gt.reading, gt.locked, t.aliases, t.glossary_id, t.id AS term_id "
           "FROM glossary_term_targets gt JOIN glossary_terms t ON t.id = gt.term_id "
           "WHERE gt.tgt_lang = ? AND gt.status = ?")
    args: list = [lang, status]
    if glossary_id is not None:
        sql += " AND t.glossary_id = ?"
        args.append(glossary_id)
    sql += " ORDER BY gt.locked DESC, length(t.zh) DESC, t.id"
    out = []
    for r in c.execute(sql, args):
        d = dict(r) if isinstance(r, sqlite3.Row) else dict(zip(("zh", "tgt", "reading", "locked", "aliases", "glossary_id", "term_id"), r))
        d[lang] = d["tgt"]                  # mt_backend.build_messages reads t[lang] or t["tgt"]
        out.append(d)
    return out
