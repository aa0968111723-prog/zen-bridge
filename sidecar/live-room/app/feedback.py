"""Corrections -> translation memory / glossary proposals (the feedback loop).

- A translation fix becomes a new current ``translations`` version (origin human) and, by
  default, a TM unit (quality 4). The next identical source line is then served from TM.
- A transcript (ASR) fix is recorded as a new transcript version only; it never feeds TM.
- ``propose_term`` creates a glossary term with status 'proposed'. It is not used for
  translation until a human approves it in the admin backend.
Callers own the transaction.
"""
from __future__ import annotations

import json
import re
import sqlite3

from app.admin.db import to_uni
from app.tm import add_unit

GLOBAL_GLOSSARY = "global"
TARGET_TYPES = ("translation", "transcript")
REASONS = ("asr_misheard", "term", "fluency", "meaning", "other")


class FeedbackError(ValueError):
    def __init__(self, detail: str, code: str = "invalid", status: int = 422):
        super().__init__(detail)
        self.detail, self.code, self.status = detail, code, status


def _current(c: sqlite3.Connection, table: str, seg: str, tgt: str | None = None):
    if table == "transcripts":
        return c.execute("SELECT id, version, text FROM transcripts WHERE segment_id=? AND is_current=1", (seg,)).fetchone()
    return c.execute(
        "SELECT id, version, text FROM translations WHERE segment_id=? AND tgt_lang=? AND is_current=1", (seg, tgt)
    ).fetchone()


def record_correction(c: sqlite3.Connection, *, segment_id: str, target_type: str, text: str,
                      reason: str | None = None, tgt_lang: str = "en", author_id: int | None = None) -> dict:
    text = (text or "").strip()
    if target_type not in TARGET_TYPES:
        raise FeedbackError("target_type 只能是 translation 或 transcript")
    if not text or len(text) > 2000:
        raise FeedbackError("修正文字不能空白，也不能超過 2000 字")
    if reason is not None and reason not in REASONS:
        raise FeedbackError("reason 不在允許清單")
    seg = c.execute("SELECT id, room_id FROM segments WHERE id=?", (segment_id,)).fetchone()
    if not seg:
        raise FeedbackError("找不到這個段落", code="not_found", status=404)
    _validate_text(c, target_type, segment_id, text)
    if target_type == "translation":
        cur = _current(c, "translations", segment_id, tgt_lang)
        nxt = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM translations WHERE segment_id=? AND tgt_lang=?",
                        (segment_id, tgt_lang)).fetchone()[0]
        tr = _current(c, "transcripts", segment_id)
        c.execute(
            """INSERT INTO translations(segment_id, transcript_id, tgt_lang, version, text, origin, engine, status)
               VALUES (?,?,?,?,?, 'human', 'admin', 'ok')""",
            (segment_id, tr[0] if tr else None, tgt_lang, nxt, text),
        )
    else:
        cur = _current(c, "transcripts", segment_id)
        nxt = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM transcripts WHERE segment_id=?",
                        (segment_id,)).fetchone()[0]
        c.execute(
            "INSERT INTO transcripts(segment_id, version, text_raw, text, text_uni, origin) VALUES (?,?,?,?,?, 'human')",
            (segment_id, nxt, text, text, to_uni(text)),
        )
    new_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute(
        """INSERT INTO corrections(target_type, target_id, segment_id, before_text, after_text, reason, author_id,
                                   status, applied_at)
           VALUES (?,?,?,?,?,?,?, 'applied', unixepoch('subsec'))""",
        (target_type, str(new_id), segment_id, cur[2] if cur else None, text, reason, author_id),
    )
    corr_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"correction_id": corr_id, "version": nxt, "target_id": new_id, "room_id": seg[1]}


_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]|</?think\b|<\|", re.IGNORECASE)


def _validate_text(c: sqlite3.Connection, target_type: str, segment_id: str, text: str) -> None:
    """QA AI代理 P1-4: a correction is published as a caption, so it passes the same gate as MT."""
    if target_type == "translation":
        from app.translate import validate_caption_en
        tr = _current(c, "transcripts", segment_id)
        if validate_caption_en(text, zh=tr[2] if tr else "") is None:
            raise FeedbackError("英譯修正必須是單行純英文（不能含中文、多段、控制字元或模型標記）")
    elif _CTRL.search(text) or "\n\n" in text:
        raise FeedbackError("逐字稿修正不能含控制字元、模型標記或多段")


# Quality ladder for TM units: >=3 is served live; 2 = waiting for review; 1 = disabled.
TM_PENDING, TM_LIVE, TM_DISABLED = 2, 4, 1


def _global_glossary(c: sqlite3.Connection) -> int:
    row = c.execute("SELECT id FROM glossaries WHERE scope='global' AND name=?", (GLOBAL_GLOSSARY,)).fetchone()
    if row:
        return int(row[0])
    c.execute("INSERT INTO glossaries(name, scope) VALUES (?, 'global')", (GLOBAL_GLOSSARY,))
    return int(c.execute("SELECT last_insert_rowid()").fetchone()[0])


def propose_term(c: sqlite3.Connection, zh: str, en: str, *, note: str | None = None) -> int:
    zh, en = (zh or "").strip(), (en or "").strip()
    if not (1 <= len(zh) <= 20) or not en or len(en) > 80:
        raise FeedbackError("建議詞：zh 需 1–20 字、en 需 1–80 字")
    gid = _global_glossary(c)
    row = c.execute("SELECT id, status FROM glossary_terms WHERE glossary_id=? AND zh=?", (gid, zh)).fetchone()
    if row:
        if row[1] == "proposed":
            c.execute(
                """UPDATE glossary_terms SET en=?, hit_count=hit_count+1, rev=rev+1,
                   updated_at=unixepoch('subsec') WHERE id=?""", (en, row[0]))
        elif row[1] == "rejected":
            # QA AI代理 P2-5: a rejected term stays rejected (en untouched); only the demand is counted.
            c.execute("UPDATE glossary_terms SET hit_count=hit_count+1, updated_at=unixepoch('subsec') WHERE id=?",
                      (row[0],))
        return int(row[0])
    c.execute(
        """INSERT INTO glossary_terms(glossary_id, zh, en, aliases, locked, source, status, note, hit_count)
           VALUES (?,?,?, '[]', 0, 'correction', 'proposed', ?, 1)""",
        (gid, zh, en, note),
    )
    return int(c.execute("SELECT last_insert_rowid()").fetchone()[0])


def promote_correction(c: sqlite3.Connection, corr_id: int, *, to_tm: bool = True, propose: dict | None = None,
                       reviewed: bool = False) -> dict:
    """Feed an applied translation fix into TM and/or propose a term. Idempotent per target.

    QA AI代理 P1-4: the TM unit starts at quality 2 (not served) until review_tm_unit approves
    it, unless the caller is already a reviewer (reviewed=True)."""
    row = c.execute(
        "SELECT target_type, segment_id, after_text, status, promoted_tm_id, promoted_term_id FROM corrections WHERE id=?",
        (corr_id,)).fetchone()
    if not row:
        raise FeedbackError("找不到這筆修正", code="not_found", status=404)
    ttype, seg, after, status, tm_id, term_id = row
    if status == "rejected":
        raise FeedbackError("這筆修正已被駁回", code="conflict", status=409)
    if ttype == "translation" and to_tm and tm_id is None:
        zh_row = _current(c, "transcripts", seg) if seg else None
        # 全站 D4: a fix made on a merged MT line may still cover the earlier zh; review it first.
        merged = bool(seg and c.execute("SELECT 1 FROM translations WHERE segment_id=? AND status='merged' LIMIT 1",
                                        (seg,)).fetchone())
        if zh_row and zh_row[2]:
            room = c.execute("SELECT room_id FROM segments WHERE id=?", (seg,)).fetchone()
            tm_id = add_unit(c, zh_row[2], after, origin="correction",
                             quality=TM_LIVE if reviewed and not merged else TM_PENDING,
                             room_id=room[0] if room else None, segment_id=seg)
    if propose and term_id is None:
        term_id = propose_term(c, str(propose.get("zh") or ""), str(propose.get("en") or ""),
                               note=f"from correction {corr_id}")
    c.execute("UPDATE corrections SET status='applied', applied_at=COALESCE(applied_at, unixepoch('subsec')), "
              "promoted_tm_id=?, promoted_term_id=? WHERE id=?", (tm_id, term_id, corr_id))
    return {"status": "applied", "tm_id": tm_id, "term_id": term_id}


def apply_correction(c: sqlite3.Connection, corr_id: int, *, to_tm: bool = True, propose: dict | None = None) -> dict:
    """Backward-compatible name for promote_correction."""
    return promote_correction(c, corr_id, to_tm=to_tm, propose=propose)


def reject_correction(c: sqlite3.Connection, corr_id: int) -> dict:
    row = c.execute("SELECT status, promoted_tm_id FROM corrections WHERE id=?", (corr_id,)).fetchone()
    if not row:
        raise FeedbackError("找不到這筆修正", code="not_found", status=404)
    if row[1] is not None:
        # QA AI代理 P1-4 (4): a promoted fix can still be withdrawn; its TM unit stops serving.
        c.execute("UPDATE tm_units SET quality=?, updated_at=unixepoch('subsec') WHERE id=?", (TM_DISABLED, row[1]))
    c.execute("UPDATE corrections SET status='rejected' WHERE id=?", (corr_id,))
    return {"status": "rejected", "tm_disabled": row[1]}


def review_tm_unit(c: sqlite3.Connection, tm_id: int, approve: bool) -> dict:
    row = c.execute("SELECT id, src_text, tgt_text, quality FROM tm_units WHERE id=?", (tm_id,)).fetchone()
    if not row:
        raise FeedbackError("找不到這筆翻譯記憶", code="not_found", status=404)
    if approve:
        from app.translate import validate_caption_en
        if validate_caption_en(row[2], zh=row[1]) is None:
            raise FeedbackError("這筆譯文不是單行純英文，不能核准")
        q = max(TM_LIVE, int(row[3]))
    else:
        q = TM_DISABLED
    c.execute("UPDATE tm_units SET quality=?, updated_at=unixepoch('subsec') WHERE id=?", (q, tm_id))
    return {"tm_id": tm_id, "quality": q, "served": q >= 3}


def decide_term(c: sqlite3.Connection, term_id: int, approve: bool) -> dict:
    row = c.execute("SELECT id, glossary_id, status FROM glossary_terms WHERE id=?", (term_id,)).fetchone()
    if not row:
        raise FeedbackError("找不到這個建議詞", code="not_found", status=404)
    if row[2] != "proposed":
        raise FeedbackError(f"這個詞目前是 {row[2]}，不是待審狀態", code="conflict", status=409)
    new = "active" if approve else "rejected"
    c.execute("UPDATE glossary_terms SET status=?, rev=rev+1, updated_at=unixepoch('subsec') WHERE id=?", (new, term_id))
    c.execute("UPDATE glossaries SET version=version+1, updated_at=unixepoch('subsec') WHERE id=?", (row[1],))
    version = c.execute("SELECT version FROM glossaries WHERE id=?", (row[1],)).fetchone()[0]
    c.execute("INSERT INTO events(kind, payload) VALUES (?, ?)",
              (f"glossary.{new}", json.dumps({"term_id": term_id, "glossary_version": version})))
    return {"term_id": term_id, "status": new, "glossary_version": version}
