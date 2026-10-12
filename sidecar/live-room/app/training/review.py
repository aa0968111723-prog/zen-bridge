"""審核佇列：未校對片段 → 人工核准／修正／退回。修正後的字幕與譯文也成為 MT 配對，並連到翻譯記憶。"""
from __future__ import annotations

import json
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
    if rec and rec["status"] == "ended" and rec["kind"] != "harvest":
        recordings.write_transcript(c, root, rec["id"])      # 社課整份逐字稿在批次結束／下載時重寫
    out = recordings.clip(c, cid)
    out["mt_pair"] = pair
    return out


def link_staging(c: sqlite3.Connection, pair_id: int, staging_id: int | None, tm_unit_id: int | None = None) -> None:
    c.execute("UPDATE mt_pairs SET staging_id=COALESCE(?, staging_id), tm_unit_id=COALESCE(?, tm_unit_id) WHERE id=?",
              (staging_id, tm_unit_id, pair_id))


# ---------------------------------------------------------------- class sessions: fast batch review
SORTS = {"confidence": "k.excluded ASC, COALESCE(k.confidence, 0) ASC, k.t0_ms", "time": "k.t0_ms",
         "speaker": "COALESCE(k.spk_score, -1) ASC, k.t0_ms"}


def session_batch(c: sqlite3.Connection, rid: str, *, sort: str = "confidence", show: str = "todo",
                  min_conf: float | None = None) -> dict:
    rec = recordings.get(c, rid)
    if not rec:
        raise registry.RegistryError("找不到這堂課", code="not_found", status=404)
    where = {"todo": "k.status IN ('unverified','needs_review') AND k.excluded=0",
             "excluded": "k.excluded=1 AND k.status != 'rejected'",
             "done": "k.status IN ('approved','rejected')", "all": "1=1"}.get(show)
    if where is None:
        raise registry.RegistryError("show 不正確")
    rows = c.execute(f"SELECT k.* FROM clips k WHERE k.recording_id=? AND {where} ORDER BY {SORTS.get(sort, SORTS['confidence'])}",
                     (rid,)).fetchall()
    items = []
    for r in rows:
        d = {k: r[k] for k in ("id", "t0_ms", "t1_ms", "duration_s", "text", "asr_text", "draft_text", "status",
                               "confidence", "spk_score", "excluded", "excluded_reason", "snr_db", "tgt_lang",
                               "mt_text", "speaker_tag")}
        d["flags"] = json.loads(r["flags"] or "[]")
        d["has_audio"] = bool(r["audio_path"])
        items.append(d)
    k = c.execute("SELECT COUNT(*), SUM(status='approved'), SUM(status IN ('unverified','needs_review') AND excluded=0), "
                  "SUM(excluded=1 AND status!='rejected'), SUM(status IN ('unverified','needs_review') AND excluded=0 AND "
                  "confidence >= ?) FROM clips WHERE recording_id=?", (min_conf if min_conf is not None else 2, rid)).fetchone()
    return {"recording": rec, "items": items,
            "counts": {"total": k[0] or 0, "approved": k[1] or 0, "todo": k[2] or 0, "excluded": k[3] or 0,
                       "above_threshold": k[4] or 0}}


def approve_above(c: sqlite3.Connection, root: Path, rid: str, *, min_conf: float, actor: str) -> dict:
    """One click: approve every unreviewed lecturer segment with confidence >= min_conf (text as recognised)."""
    rec = recordings.get(c, rid)
    if not rec:
        raise registry.RegistryError("找不到這堂課", code="not_found", status=404)
    if not 0.5 <= float(min_conf) <= 1.0:
        raise registry.RegistryError("門檻需介於 0.5 和 1")
    if not rec["speaker_id"] or not registry.has_consent(c, rec["speaker_id"], "asr"):
        raise registry.RegistryError("講者同意已失效", code="no_consent", status=403)
    now = time.time()
    n = c.execute("UPDATE clips SET status='approved', reviewed_by=?, reviewed_at=? WHERE recording_id=? AND "
                  "status='unverified' AND excluded=0 AND speaker_id IS NOT NULL AND audio_path IS NOT NULL AND "
                  "confidence >= ?", (f"auto≥{min_conf}:{actor}"[:40], now, rid, float(min_conf))).rowcount
    tdb.audit(c, "review_approve_above", speaker_id=rec["speaker_id"], actor=actor, n=n, min_conf=float(min_conf))
    recordings.write_transcript(c, root, rid)
    return {"approved": n}


def set_excluded(c: sqlite3.Connection, root: Path, cid: str, *, excluded: bool, actor: str) -> dict:
    """Reviewer override: this line IS the lecturer (include) / is someone else (exclude)."""
    r = c.execute("SELECT k.*, r.speaker_id AS lecturer FROM clips k JOIN recordings r ON r.id = k.recording_id "
                  "WHERE k.id=?", (cid,)).fetchone()
    if not r:
        raise registry.RegistryError("找不到這個片段", code="not_found", status=404)
    now = time.time()
    if excluded:
        c.execute("UPDATE clips SET excluded=1, excluded_reason='reviewer', speaker_id=NULL, speaker_tag='Speaker 1', "
                  "status='needs_review', reviewed_by=?, reviewed_at=? WHERE id=?", (actor[:40], now, cid))
    else:
        if not r["audio_path"]:
            raise registry.RegistryError("這段的音檔已依保留期限刪除，無法再收回", code="no_audio", status=409)
        if not r["lecturer"] or not registry.has_consent(c, r["lecturer"], "asr", live=True):
            raise registry.RegistryError("主講者同意已失效", code="no_consent", status=403)
        c.execute("UPDATE clips SET excluded=0, excluded_reason=NULL, speaker_id=?, speaker_tag=?, status='unverified', "
                  "reviewed_by=?, reviewed_at=? WHERE id=?", (r["lecturer"], recordings.DEFAULT_TAG, actor[:40], now, cid))
    tdb.audit(c, "review_exclude" if excluded else "review_include", speaker_id=r["lecturer"], actor=actor, clip=cid)
    return recordings.clip(c, cid)
