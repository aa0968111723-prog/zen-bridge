"""社課現場收錄：主持人逐場開啟，且只收有「現場收錄」同意的講者。

live-room 的 pipeline 在每段 Breeze 定稿後呼叫 ``capture``（在 worker thread）。沒開啟收錄、
沒有 training.sqlite3、或講者同意失效時什麼都不做。收下的片段一律 ``unverified``，進審核佇列。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from app.training import audioq, recordings, registry
from app.training import db as tdb

log = logging.getLogger("zen.training")
DEFAULT_HOURS = 4.0
WARN_IDLE_S = 240      # 4 分鐘沒有辨識結果 → 後台警告
STOP_IDLE_S = 300      # 5 分鐘 → 自動停止並寫出完整逐字稿
_cache = {"at": 0.0, "active": False}
_lock = threading.Lock()


def enable(c, *, room_id: str, speaker_id: str, enabled_by: str, session_id: str | None = None,
           hours: float = DEFAULT_HOURS) -> dict:
    if not registry.has_consent(c, speaker_id, "asr", live=True):
        raise registry.RegistryError("這位講者沒有「社課現場收錄」的有效同意", code="no_consent", status=403)
    if not isinstance(room_id, str) or not room_id.strip() or len(room_id) > 64:
        raise registry.RegistryError("room_id 不正確")
    hours = max(0.25, min(float(hours or DEFAULT_HOURS), 12.0))
    now = time.time()
    c.execute("UPDATE harvest_optins SET stopped_at=? WHERE room_id=? AND stopped_at IS NULL", (now, room_id))
    c.execute("INSERT INTO harvest_optins(room_id, session_id, speaker_id, enabled_by, enabled_at, expires_at) "
              "VALUES (?,?,?,?,?,?)", (room_id, session_id or None, speaker_id, enabled_by[:40], now, now + hours * 3600))
    oid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    tdb.audit(c, "harvest_enable", speaker_id=speaker_id, actor=enabled_by, room_id=room_id)
    _cache["at"] = 0.0
    return optin(c, oid)


def optin(c, oid: int) -> dict | None:
    r = c.execute("SELECT * FROM harvest_optins WHERE id=?", (oid,)).fetchone()
    return dict(r) if r else None


def list_optins(c, active_only: bool = False) -> list[dict]:
    q = "SELECT * FROM harvest_optins"
    if active_only:
        q += " WHERE stopped_at IS NULL AND expires_at > ?"
        rows = [dict(r) for r in c.execute(q + " ORDER BY enabled_at DESC", (time.time(),))]
    else:
        rows = [dict(r) for r in c.execute(q + " ORDER BY enabled_at DESC LIMIT 50")]
    for o in rows:
        if o["stopped_at"] is None:
            o.update(idle_state(c, o))
    return rows


def stop(c, root: Path, oid: int, *, reason: str = "host") -> dict | None:
    o = optin(c, oid)
    if not o:
        return None
    if o["stopped_at"] is None:
        c.execute("UPDATE harvest_optins SET stopped_at=? WHERE id=?", (time.time(), oid))
    if o["recording_id"]:
        recordings.finish(c, root, o["recording_id"])
    _cache["at"] = 0.0
    return optin(c, oid)


def maybe_active(root: Path | None = None) -> bool:
    """Cheap check for the live loop: cached 3 s. False when no training DB exists at all."""
    if (os.environ.get("ZEN_TRAINING_HARVEST") or "1").strip() == "0":
        return False
    now = time.monotonic()
    if now - _cache["at"] < 3.0:
        return _cache["active"]
    with _lock:
        active = False
        try:
            path = tdb.db_path(root)
            if path.exists():
                c = tdb.connect(root)
                try:
                    active = bool(c.execute("SELECT 1 FROM harvest_optins WHERE stopped_at IS NULL AND expires_at > ? "
                                            "LIMIT 1", (time.time(),)).fetchone())
                finally:
                    c.close()
        except Exception:
            log.debug("harvest check failed", exc_info=True)
        _cache.update(at=now, active=active)
    if active and now - _cache.get("swept", 0.0) > 30.0:
        _cache["swept"] = now
        threading.Thread(target=_sweep_bg, args=(root,), daemon=True, name="zen-harvest-sweep").start()
    return active


def _sweep_bg(root):
    try:
        c = tdb.connect(root)
        try:
            c.execute("BEGIN IMMEDIATE")
            sweep(c, Path(root) if root else tdb.training_dir())
            c.execute("COMMIT")
        finally:
            c.close()
    except Exception:
        log.debug("harvest sweep failed", exc_info=True)


def idle_state(c, o: dict, now: float | None = None) -> dict:
    """Seconds since the last recognised segment of this opt-in (or since it was enabled)."""
    now = time.time() if now is None else now
    last = None
    if o.get("recording_id"):
        last = c.execute("SELECT MAX(created_at) FROM clips WHERE recording_id=?", (o["recording_id"],)).fetchone()[0]
    idle = now - (last or o["enabled_at"])
    return {"idle_s": round(idle, 1), "warn": idle >= WARN_IDLE_S, "will_stop_in_s": max(0, round(STOP_IDLE_S - idle))}


def sweep(c, root: Path, now: float | None = None) -> list[int]:
    """Auto-stop opt-ins that expired or had no recognition result for STOP_IDLE_S. Returns stopped ids."""
    now = time.time() if now is None else now
    stopped = []
    for o in c.execute("SELECT * FROM harvest_optins WHERE stopped_at IS NULL").fetchall():
        o = dict(o)
        if o["expires_at"] <= now or idle_state(c, o, now)["idle_s"] >= STOP_IDLE_S:
            stop(c, root, o["id"], reason="idle")
            stopped.append(o["id"])
    return stopped


def _optin_for(c, root: Path, room_id: str, session_id: str):
    now = time.time()
    rows = c.execute("SELECT * FROM harvest_optins WHERE room_id=? AND stopped_at IS NULL", (room_id,)).fetchall()
    for o in rows:
        if o["expires_at"] <= now:
            stop(c, root, o["id"], reason="expired")
            continue
        if o["session_id"] is None:
            c.execute("UPDATE harvest_optins SET session_id=? WHERE id=?", (session_id, o["id"]))
            return optin(c, o["id"])
        if o["session_id"] == session_id:
            return dict(o)
        # the room moved on to a new session: this opt-in was for one session only
        stop(c, root, o["id"], reason="new_session")
    return None


def capture(*, room_id: str, session_id: str, seq: int, wav_path, zh_raw: str, zh: str, t0_ms=None, t1_ms=None,
            tgt_lang: str | None = None, root: Path | None = None) -> str | None:
    """Store one final caption segment + its audio. Returns clip id or None. Never raises."""
    try:
        root = Path(root) if root else tdb.training_dir()
        if not tdb.db_path(root).exists():
            return None
        c = tdb.connect(root)
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                cid = _capture(c, root, room_id, session_id, seq, Path(wav_path), zh_raw, zh, t0_ms, t1_ms, tgt_lang)
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            return cid
        finally:
            c.close()
    except Exception:
        log.warning("training harvest skipped one segment", exc_info=True)
        return None


def _capture(c, root, room_id, session_id, seq, wav_path, zh_raw, zh, t0_ms, t1_ms, tgt_lang):
    o = _optin_for(c, root, room_id, session_id)
    if not o or not registry.has_consent(c, o["speaker_id"], "asr", live=True):
        return None
    seg_id = f"{room_id}:{session_id}:{seq}"
    pcm = audioq.read_pcm(wav_path)
    rec = recordings.get(c, o["recording_id"]) if o["recording_id"] else None
    if rec is None:
        rec = recordings.start(c, root, kind="harvest", speaker_id=o["speaker_id"], room_id=room_id,
                               session_id=session_id, title=f"{room_id} 社課收錄")
        c.execute("UPDATE harvest_optins SET recording_id=? WHERE id=?", (rec["id"], o["id"]))
    old = c.execute("SELECT id, audio_path FROM clips WHERE segment_id=?", (seg_id,)).fetchone()
    if old:          # a retry of the same segment replaces the earlier take
        from app.training.studio import delete_clip
        delete_clip(c, root, old["id"], rewrite=False)
    clip = recordings.add_clip(c, root, rec, pcm, speaker_id=o["speaker_id"], origin="harvest", asr_text=zh_raw,
                               text=zh, segment_id=seg_id, t0_ms=t0_ms, t1_ms=t1_ms, tgt_lang=tgt_lang,
                               status="unverified", align="native")
    return clip["id"]


def note_draft(*, room_id: str, session_id: str, text: str, t0_ms=None, t1_ms=None, root: Path | None = None) -> None:
    """Hook for the streaming draft ASR (PR #49): draft captions go into live.jsonl next to the clips."""
    try:
        root = Path(root) if root else tdb.training_dir()
        if not tdb.db_path(root).exists():
            return
        c = tdb.connect(root)
        try:
            o = c.execute("SELECT recording_id FROM harvest_optins WHERE room_id=? AND session_id=? AND stopped_at IS "
                          "NULL AND recording_id IS NOT NULL", (room_id, session_id)).fetchone()
            rec = o and recordings.get(c, o[0])
            if rec:
                recordings.append_event(root, rec, {"type": "draft", "t0_ms": t0_ms, "t1_ms": t1_ms, "text": text})
        finally:
            c.close()
    except Exception:
        log.debug("draft note skipped", exc_info=True)
