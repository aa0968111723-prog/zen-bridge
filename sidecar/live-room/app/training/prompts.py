"""錄音室提示句：社團用語、詞表（唯讀 zen.sqlite3）、開示常用句、中英夾雜句。"""
from __future__ import annotations

import json
import random
import sqlite3
import time
from pathlib import Path

DATA = Path(__file__).with_name("prompts_zh.json")
SOURCES = ("club", "glossary", "dharma", "mixed", "custom")


def builtin() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def glossary_terms(zen_db: Path | None, limit: int = 300) -> list[dict]:
    """Active glossary terms, read-only (mode=ro + query_only). Missing DB -> []."""
    if not zen_db or not Path(zen_db).exists():
        return []
    try:
        c = sqlite3.connect(f"file:{Path(zen_db).as_posix()}?mode=ro", uri=True, timeout=5)
        c.execute("PRAGMA query_only=ON")
        try:
            rows = c.execute("SELECT zh, en FROM glossary_terms WHERE status='active' ORDER BY hit_count DESC, id "
                             "LIMIT ?", (limit,)).fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return []
    return [{"zh": zh, "en": en} for zh, en in rows if zh]


def sync(c: sqlite3.Connection, zen_db: Path | None = None) -> int:
    """Insert built-in + glossary prompts that are not there yet. Returns rows added."""
    data = builtin()
    now = time.time()
    rows = [(src, t, None) for src in ("club", "dharma", "mixed") for t in data.get(src, [])]
    tpl = data.get("glossary_templates") or ["{zh}"]
    for i, term in enumerate(glossary_terms(zen_db)):
        rows.append(("glossary", tpl[i % len(tpl)].format(zh=term["zh"]), term["zh"]))
    added = 0
    for src, text, term in rows:
        cur = c.execute("INSERT OR IGNORE INTO prompts(source, text, term, created_at) VALUES (?,?,?,?)",
                        (src, text, term, now))
        added += cur.rowcount
    return added


def add_custom(c: sqlite3.Connection, text: str) -> dict:
    text = " ".join((text or "").split())
    if not (2 <= len(text) <= 200):
        raise ValueError("提示句需 2–200 字")
    c.execute("INSERT OR IGNORE INTO prompts(source, text, created_at) VALUES ('custom', ?, ?)", (text, time.time()))
    r = c.execute("SELECT * FROM prompts WHERE source='custom' AND text=?", (text,)).fetchone()
    return dict(r)


def next_prompts(c: sqlite3.Connection, speaker_id: str, *, source: str | None = None, n: int = 10,
                 seed: int | None = None) -> list[dict]:
    """Prompts this speaker has not recorded yet (approved/unverified take), shuffled, mixed sources."""
    q = ("SELECT * FROM prompts WHERE active=1 AND id NOT IN (SELECT prompt_id FROM clips WHERE speaker_id=? "
         "AND prompt_id IS NOT NULL AND status!='rejected')")
    args: list = [speaker_id]
    if source:
        q += " AND source=?"
        args.append(source)
    rows = [dict(r) for r in c.execute(q, args).fetchall()]
    random.Random(seed).shuffle(rows)
    return rows[:n]


def progress(c: sqlite3.Connection, speaker_id: str) -> dict:
    total = c.execute("SELECT source, COUNT(*) FROM prompts WHERE active=1 GROUP BY source").fetchall()
    done = c.execute("SELECT p.source, COUNT(DISTINCT p.id) FROM clips k JOIN prompts p ON p.id=k.prompt_id "
                     "WHERE k.speaker_id=? AND k.status!='rejected' GROUP BY p.source", (speaker_id,)).fetchall()
    d = dict(done)
    return {s: {"done": d.get(s, 0), "total": n} for s, n in total}
