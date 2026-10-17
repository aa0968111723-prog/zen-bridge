"""課後整理：從整堂課音檔＋定稿逐字稿切出訓練片段、做聲紋檢查、算信心分數；以及保留期限清理。

收錄當下完全不切片段（所以片段失敗不會影響任何東西）。這裡在上課結束、所有收錄都停了 60 秒後，
於背景低優先順序執行；後台「重新整理」或 ``python -m app.training derive`` 也可以手動跑。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import numpy as np

from app.training import audioq, classaudio, recordings, registry, spkcheck
from app.training import db as tdb
from app.training.textnorm import cer, norm as normalize

log = logging.getLogger("zen.training")

ENROLL_S = 300              # 主講者聲紋：這堂課前 5 分鐘（排除提問中）
MIN_S, MAX_S = 1.0, 30.0
QUESTION_PAD_MS = 1000
DEFAULTS = {"raw_retention_days": "14", "excluded_audio_days": "3", "auto_approve_min": "0.85"}
_REPEAT = re.compile(r"(.{1,4})\1{3,}")
MB_PER_HOUR_RAW = classaudio.RATE * classaudio.BPS * 3600 / 1e6     # 115.2 MB


def lower_priority() -> None:
    """Background derive thread: below-normal priority so a class starting right after is not slowed."""
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            k.SetThreadPriority(k.GetCurrentThread(), -1)          # THREAD_PRIORITY_BELOW_NORMAL
        else:
            os.nice(5)
    except Exception:
        pass


def get_settings(c) -> dict:
    out = dict(DEFAULTS)
    for r in c.execute("SELECT key, value FROM settings"):
        out[r["key"]] = r["value"]
    return {"raw_retention_days": float(out["raw_retention_days"]),
            "excluded_audio_days": float(out["excluded_audio_days"]),
            "auto_approve_min": float(out["auto_approve_min"])}


def set_settings(c, **kw) -> dict:
    lim = {"raw_retention_days": (0, 365), "excluded_audio_days": (0, 90), "auto_approve_min": (0.5, 1.0)}
    for k, v in kw.items():
        if k not in lim or v is None:
            continue
        lo, hi = lim[k]
        v = float(v)
        if not lo <= v <= hi:
            raise registry.RegistryError(f"{k} 需介於 {lo} 和 {hi}")
        c.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (k, str(v)))
    return get_settings(c)


def _drafts(d: Path) -> list[tuple[int, int, str]]:
    p = d / "live.jsonl"
    out = []
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "draft" and ev.get("text"):
                out.append((int(ev.get("t0_ms") or 0), int(ev.get("t1_ms") or 0), ev["text"]))
    return out


def _overlaps(a0, a1, spans, pad=0) -> bool:
    return any(a0 < b1 + pad and b0 - pad < a1 for b0, b1 in spans)


def confidence(*, spk: float | None, confident_at: float, q, text: str, dur: float, draft: str | None,
               near_question: bool) -> tuple[float, list[str]]:
    c, flags = 1.0, []
    if spk is None:
        c -= 0.3
        flags.append("speaker_unknown")
    elif spk < confident_at:
        c -= 0.4
        flags.append("low_speaker")
    for p, label in (("clipping", "clipping"), ("low_snr", "noisy"), ("mostly_silence", "mostly_silence"),
                     ("too_quiet", "too_quiet")):
        if p in q.problems:
            c -= 0.3
            flags.append(label)
    n = len(normalize(text))
    rate = n / dur if dur else 0
    if n < 2:
        c -= 0.3
        flags.append("short_text")
    elif rate < 1.0 or rate > 9.0:
        c -= 0.3
        flags.append("odd_rate")
    if _REPEAT.search(normalize(text)):
        c -= 0.4
        flags.append("repeat")
    if draft:
        e = cer(draft, text)
        if e is not None and e > 0.35:
            c -= 0.2
            flags.append("draft_disagree")
    if near_question:
        c -= 0.2
        flags.append("near_question")
    return round(max(0.0, min(1.0, c)), 3), flags


def derive_recording(c, root: Path, rid: str, *, be=None) -> dict:
    """(Re)build the segment pairs of one class recording. Reviewed clips are kept as they are."""
    root = Path(root)
    rec = recordings.get(c, rid)
    if not rec or rec["kind"] != "harvest":
        return {"recording_id": rid, "skipped": "not a class recording"}
    d = root / rec["dir"]
    adir = d / "audio"
    cs = c.execute("SELECT * FROM class_sessions WHERE recording_id=?", (rid,)).fetchone()
    lecturer = rec["speaker_id"]
    if not lecturer or not registry.has_consent(c, lecturer, "asr", live=True):
        return {"recording_id": rid, "skipped": "no consent"}
    if not classaudio.chunks(adir):
        return {"recording_id": rid, "skipped": "raw audio already removed"}
    finals = recordings.class_finals(d)
    marks = recordings.class_marks(d)
    drafts = _drafts(d)
    room, session = rec["room_id"], rec["session_id"]
    reviewed = {r["segment_id"] for r in c.execute(
        "SELECT segment_id FROM clips WHERE recording_id=? AND (reviewed_at IS NOT NULL OR status IN "
        "('approved','rejected'))", (rid,))}
    c.execute("BEGIN IMMEDIATE")
    try:
        for r in c.execute("SELECT id, audio_path FROM clips WHERE recording_id=? AND reviewed_at IS NULL AND status "
                           "NOT IN ('approved','rejected')", (rid,)).fetchall():
            if r["audio_path"]:
                p = root / r["audio_path"]
                if p.exists():
                    p.unlink()
        c.execute("DELETE FROM clips WHERE recording_id=? AND reviewed_at IS NULL AND status NOT IN "
                  "('approved','rejected')", (rid,))
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    todo = []
    for f in finals:
        seg_id = f"{room}:{session}:{f['seq']}"
        t0, t1 = int(f["t0_ms"] or 0), int(f["t1_ms"] or 0)
        dur = (t1 - t0) / 1000.0
        if seg_id in reviewed or not (MIN_S <= dur <= MAX_S):
            continue
        todo.append((seg_id, f, t0, t1, dur, classaudio.read_span(adir, t0, t1)))
    pcms = [np.frombuffer(p.tobytes(), dtype=np.int16).astype(np.float32) / 32768.0 for *_, p in todo]
    enroll = [t0 < ENROLL_S * 1000 and not _overlaps(t0, t1, marks, QUESTION_PAD_MS) for _, _, t0, t1, _, _ in todo]
    prof_row = c.execute("SELECT * FROM speaker_profiles WHERE speaker_id=? AND backend='sherpa'", (lecturer,)).fetchone()
    profile = np.frombuffer(prof_row["emb"], dtype=np.float32) if prof_row else None
    t_spk = time.time()
    res = spkcheck.check(pcms, enroll, be=be or spkcheck.backend(root), profile=profile) if todo else None
    t_spk = time.time() - t_spk
    counts = {"segments": len(todo), "excluded": 0, "needs_review": 0, "confident": 0}
    c.execute("BEGIN IMMEDIATE")
    try:
        for k, (seg_id, f, t0, t1, dur, pcm) in enumerate(todo):
            q = audioq.quality(pcm, min_s=MIN_S, max_s=MAX_S)
            text = f.get("text") or f.get("asr_text") or ""
            in_q = _overlaps(t0, t1, marks, QUESTION_PAD_MS)
            near_q = not in_q and _overlaps(t0, t1, marks, 3000)
            ds = [t for a, b, t in drafts if a < t1 and b > t0]
            draft = max(ds, key=len) if ds else None
            spk = res.scores[k] if res else None
            conf, flags = confidence(spk=spk, confident_at=res.confident_at if res else 1.0, q=q, text=text, dur=dur,
                                     draft=draft, near_question=near_q)
            excluded, reason = 0, None
            if in_q:
                excluded, reason = 1, "question"
                flags.append("question")
            elif spk is not None and spk < res.threshold:
                excluded, reason = 1, "speaker_mismatch"
            if excluded:
                conf = 0.0
            extra = {"confidence": conf, "spk_score": None if spk is None else round(spk, 4),
                     "flags": json.dumps(flags), "excluded": excluded, "excluded_reason": reason}
            recordings.add_clip(c, root, rec, pcm, speaker_id=None if excluded else lecturer, origin="harvest",
                                asr_text=f.get("asr_text"), text=text, segment_id=seg_id, t0_ms=t0, t1_ms=t1,
                                tgt_lang=f.get("tgt_lang"), status="needs_review" if excluded else "unverified",
                                align="native", speaker_tag="Speaker 1" if excluded else recordings.DEFAULT_TAG,
                                draft_text=draft, log_event=False, extra=extra)
            counts["excluded"] += excluded
            counts["confident"] += int(not excluded and conf >= 0.85)
            counts["needs_review"] += int(not excluded and conf < 0.85)
        if res is not None and res.centroid is not None and res.enrolled >= 3:
            n_old = prof_row["n"] if prof_row else 0
            emb = res.centroid if profile is None else (profile * n_old + res.centroid * res.enrolled) / (n_old + res.enrolled)
            c.execute("INSERT INTO speaker_profiles(speaker_id, backend, dim, emb, n, updated_at) VALUES (?,?,?,?,?,?) "
                      "ON CONFLICT(speaker_id, backend) DO UPDATE SET emb=excluded.emb, n=excluded.n, dim=excluded.dim, "
                      "updated_at=excluded.updated_at", (lecturer, "sherpa", len(emb), np.asarray(emb, np.float32).tobytes(),
                                                         n_old + res.enrolled, time.time()))
        note = {"backend": res.backend if res else None, "threshold": res.threshold if res else None,
                "backend_error": spkcheck.LAST_ERROR["sherpa"] if res and res.backend != "sherpa" else None,
                "enrolled": res.enrolled if res else 0, "speaker_check_s": round(t_spk, 2), **counts}
        if cs:
            c.execute("UPDATE class_sessions SET derived_at=?, derive_note=? WHERE id=?",
                      (time.time(), json.dumps(note), cs["id"]))
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    recordings.write_transcript(c, root, rid)
    return {"recording_id": rid, **note}


def pending(c) -> list[str]:
    return [r[0] for r in c.execute("SELECT recording_id FROM class_sessions WHERE stopped_at IS NOT NULL AND "
                                    "derived_at IS NULL AND raw_deleted_at IS NULL AND recording_id IS NOT NULL")]


def delete_raw(c, root: Path, cs_id: int, reason: str = "retention") -> int:
    r = c.execute("SELECT cs.*, r.dir FROM class_sessions cs JOIN recordings r ON r.id = cs.recording_id "
                  "WHERE cs.id=?", (cs_id,)).fetchone()
    if not r or r["raw_deleted_at"] or r["stopped_at"] is None:
        return 0
    adir = Path(root) / r["dir"] / "audio"
    n = 0
    for p in classaudio.chunks(adir):
        n += p.stat().st_size
        p.unlink()
    c.execute("UPDATE class_sessions SET raw_deleted_at=? WHERE id=?", (time.time(), cs_id))
    tdb.audit(c, "class_raw_deleted", speaker_id=r["speaker_id"], reason=reason, bytes=n)
    return n


def cleanup(c, root: Path, now: float | None = None) -> dict:
    """Retention: whole-class raw audio after N days (only once derived), excluded segments' audio after M days."""
    now = time.time() if now is None else now
    st = get_settings(c)
    freed = 0
    for r in c.execute("SELECT id FROM class_sessions WHERE stopped_at IS NOT NULL AND derived_at IS NOT NULL AND "
                       "raw_deleted_at IS NULL AND stopped_at <= ?",
                       (now - st["raw_retention_days"] * 86400,)).fetchall():
        freed += delete_raw(c, root, r["id"])
    gone = 0
    for r in c.execute("SELECT id, audio_path FROM clips WHERE excluded=1 AND status != 'approved' AND audio_path IS NOT "
                       "NULL AND created_at <= ?", (now - st["excluded_audio_days"] * 86400,)).fetchall():
        p = Path(root) / r["audio_path"]
        if p.exists():
            freed += p.stat().st_size
            p.unlink()
        c.execute("UPDATE clips SET audio_path=NULL WHERE id=?", (r["id"],))
        gone += 1
    return {"freed_bytes": freed, "excluded_audio_removed": gone}


def disk(c, root: Path) -> dict:
    root = Path(root)
    raw = sum(classaudio.disk_bytes(root / r["dir"] / "audio") for r in c.execute(
        "SELECT r.dir FROM class_sessions cs JOIN recordings r ON r.id = cs.recording_id WHERE cs.raw_deleted_at IS NULL"))
    clips = 0
    for r in c.execute("SELECT audio_path, excluded FROM clips WHERE audio_path IS NOT NULL"):
        p = root / r["audio_path"]
        if p.exists():
            clips += p.stat().st_size
    hours = (c.execute("SELECT COALESCE(SUM(audio_s), 0) FROM class_sessions").fetchone()[0] or 0) / 3600
    return {"raw_bytes": raw, "clip_bytes": clips, "class_hours": round(hours, 2),
            "estimate": {"raw_mb_per_class_hour": round(MB_PER_HOUR_RAW, 1),
                         "clips_mb_per_class_hour": round(MB_PER_HOUR_RAW * 0.75, 1),
                         "note": "原始整堂 16 kHz 單聲道約 0.115 GB／小時；切出的片段約再 0.08 GB／小時"
                                 "（講話時間比例 ≈ 70–80%）。原始檔過保留期限刪除後，只剩片段。"}}
