"""審核佇列：未校對片段 → 人工核准／修正／退回。修正後的字幕與譯文也成為 MT 配對，並連到翻譯記憶。"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from app.training import recordings, registry, textnorm
from app.training import db as tdb

ACTIONS = ("approve", "reject")
STATUSES = ("unverified", "needs_review", "approved", "rejected")


def queue(c: sqlite3.Connection, *, status: str = "unverified", speaker_id=None, origin=None, limit: int = 50,
          before: float | None = None) -> list[dict]:
    if status not in STATUSES + ("open",):
        raise registry.RegistryError("status 不正確")
    q = "SELECT k.*, s.label AS speaker_label FROM clips k LEFT JOIN speakers s ON s.id=k.speaker_id WHERE "
    args: list = []
    if status == "open":
        q += "k.status IN ('unverified','needs_review')"
    else:
        q += "k.status=?"
        args.append(status)
    for col, v in (("k.speaker_id", speaker_id), ("k.origin", origin)):
        if v:
            q += f" AND {col}=?"
            args.append(v)
    if before:
        q += " AND k.created_at < ?"
        args.append(before)
    q += " ORDER BY k.created_at DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    out = []
    for r in c.execute(q, args).fetchall():
        d = dict(r)
        d["cer"] = textnorm.cer(d["asr_text"], d["text"]) if d["asr_text"] else None
        out.append(d)
    return out


def ledger_lookup(zen_db: Path | None, segment_id: str | None) -> dict:
    """Current transcript/translation of a live segment in zen.sqlite3 (read-only), to prefill review."""
    if not zen_db or not segment_id or not Path(zen_db).exists():
        return {}
    try:
        c = sqlite3.connect(f"file:{Path(zen_db).as_posix()}?mode=ro", uri=True, timeout=5)
        c.execute("PRAGMA query_only=ON")
        try:
            t = c.execute("SELECT text, origin FROM transcripts WHERE segment_id=? AND is_current=1",
                          (segment_id,)).fetchone()
            tr = c.execute("SELECT tgt_lang, text, origin FROM translations WHERE segment_id=? AND is_current=1",
                           (segment_id,)).fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return {}
    return {"transcript": t and {"text": t[0], "origin": t[1]},
            "translations": [{"tgt_lang": a, "text": b, "origin": o} for a, b, o in tr]}


def decide(c: sqlite3.Connection, root: Path, cid: str, *, action: str, actor: str, text: str | None = None,
           mt_text: str | None = None, tgt_lang: str | None = None) -> dict:
    """approve (optionally with corrected text / translation) or reject (audio deleted). Returns the clip
    plus ``mt_pair`` when a translation pair was created."""
    if action not in ACTIONS:
        raise registry.RegistryError("action 需為 approve 或 reject")
    r = c.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    if not r:
        raise registry.RegistryError("找不到這個片段", code="not_found", status=404)
    now = time.time()
    pair = None
    if action == "reject":
        if r["audio_path"]:
            try:
                p = tdb.absolute(root, r["audio_path"])
                if p.exists():
                    p.unlink()
            except ValueError:
                pass
        c.execute("UPDATE clips SET status='rejected', audio_path=NULL, reviewed_by=?, reviewed_at=? WHERE id=?",
                  (actor[:40], now, cid))
    else:
        if not r["audio_path"] or not r["speaker_id"]:
            raise registry.RegistryError("這段還沒對應到有同意的講者，不能核准", code="needs_speaker", status=409)
        if not registry.has_consent(c, r["speaker_id"], "asr"):
            raise registry.RegistryError("講者同意已失效", code="no_consent", status=403)
        new_text = " ".join((text if text is not None else (r["text"] or r["asr_text"] or "")).split())
        if not new_text or len(new_text) > 500:
            raise registry.RegistryError("逐字稿需 1–500 字")
        c.execute("UPDATE clips SET status='approved', text=?, reviewed_by=?, reviewed_at=? WHERE id=?",
                  (new_text, actor[:40], now, cid))
        lang = tgt_lang or r["tgt_lang"]
        tgt = " ".join((mt_text or "").split())
        if tgt and lang in ("en", "ja"):
            if len(tgt) > 1000:
                raise registry.RegistryError("譯文最多 1000 字")
            c.execute("UPDATE clips SET mt_text=?, tgt_lang=? WHERE id=?", (tgt, lang, cid))
            if registry.has_consent(c, r["speaker_id"], "mt"):
                rec = recordings.get(c, r["recording_id"]) or {}
                c.execute("INSERT INTO mt_pairs(src_text, tgt_lang, tgt_text, origin, clip_id, speaker_id, session_id, "
                          "segment_id, created_at) VALUES (?,?,?,?,?,?,?,?,?) "
                          "ON CONFLICT(src_text, tgt_lang, tgt_text) DO NOTHING",
                          (new_text, lang, tgt, "review", cid, r["speaker_id"], rec.get("session_id") or rec.get("id"),
                           r["segment_id"], now))
                pr = c.execute("SELECT * FROM mt_pairs WHERE src_text=? AND tgt_lang=? AND tgt_text=?",
                               (new_text, lang, tgt)).fetchone()
                pair = dict(pr) if pr else None
    tdb.audit(c, f"review_{action}", speaker_id=r["speaker_id"], actor=actor, clip=cid)
    rec = recordings.get(c, r["recording_id"])
    if rec and rec["status"] == "ended":
        recordings.write_transcript(c, root, rec["id"])
    out = recordings.clip(c, cid)
    out["mt_pair"] = pair
    return out


def link_staging(c: sqlite3.Connection, pair_id: int, staging_id: int | None, tm_unit_id: int | None = None) -> None:
    c.execute("UPDATE mt_pairs SET staging_id=COALESCE(?, staging_id), tm_unit_id=COALESCE(?, tm_unit_id) WHERE id=?",
              (staging_id, tm_unit_id, pair_id))
