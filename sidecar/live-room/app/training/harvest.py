"""社課收錄設定（主持頁一個開關）與每堂課的收錄紀錄。

* 每個房間記住「主講者＋要不要收錄」（room_collect）。下次上課同一個房間、同一位主講者自動沿用。
* 主持人對某位講者開過一次收錄，他的 ``collect_default`` 就是 1：之後在主持頁選他，開關預設是開。
* 真正的收錄在 ``capture.py``（背景執行緒）；課後整理在 ``derive.py``。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from app.training import capture, recordings, registry
from app.training import db as tdb


def room_setting(c, room_id: str) -> dict | None:
    r = c.execute("SELECT * FROM room_collect WHERE room_id=?", (room_id,)).fetchone()
    return dict(r) if r else None


def lecturers(c) -> list[dict]:
    """Speakers the host may pick: not withdrawn, valid ASR consent that allows live class recording."""
    out = []
    for s in c.execute("SELECT id, label, collect_default FROM speakers WHERE withdrawn_at IS NULL ORDER BY label"):
        if registry.has_consent(c, s["id"], "asr", live=True):
            out.append({"id": s["id"], "label": s["label"], "default_on": bool(s["collect_default"])})
    return out


def set_room(c, *, room_id: str, speaker_id: str | None, collect: bool, by: str = "host") -> dict:
    if not isinstance(room_id, str) or not room_id.strip() or len(room_id) > 64:
        raise registry.RegistryError("room_id 不正確")
    if collect:
        if not speaker_id:
            raise registry.RegistryError("要先選主講者")
        if not registry.has_consent(c, speaker_id, "asr", live=True):
            raise registry.RegistryError("這位講者沒有「社課現場收錄」的有效同意", code="no_consent", status=403)
    elif speaker_id and not registry.get_speaker(c, speaker_id):
        speaker_id = None
    now = time.time()
    c.execute("INSERT INTO room_collect(room_id, speaker_id, collect, updated_by, updated_at) VALUES (?,?,?,?,?) "
              "ON CONFLICT(room_id) DO UPDATE SET speaker_id=excluded.speaker_id, collect=excluded.collect, "
              "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
              (room_id, speaker_id, int(bool(collect)), by[:40], now))
    if speaker_id and collect:          # 開過一次 → 之後選他預設開（關掉只影響這個房間，不改預設）
        c.execute("UPDATE speakers SET collect_default=1 WHERE id=?", (speaker_id,))
    tdb.audit(c, "class_collect_on" if collect else "class_collect_off", speaker_id=speaker_id, actor=by, room_id=room_id)
    capture.refresh()
    return room_setting(c, room_id)


def sessions(c, *, limit: int = 50, speaker_id: str | None = None) -> list[dict]:
    q = ("SELECT cs.*, s.label AS speaker_label, r.title FROM class_sessions cs LEFT JOIN speakers s ON s.id = cs.speaker_id "
         "LEFT JOIN recordings r ON r.id = cs.recording_id")
    args: list = []
    if speaker_id:
        q += " WHERE cs.speaker_id=?"
        args.append(speaker_id)
    rows = [dict(r) for r in c.execute(q + " ORDER BY cs.started_at DESC LIMIT ?", (*args, limit))]
    for r in rows:
        r["derive_note"] = json.loads(r["derive_note"]) if r["derive_note"] else None
        k = c.execute("SELECT COUNT(*), SUM(status='approved'), SUM(status='unverified' AND excluded=0), SUM(excluded), "
                      "SUM(CASE WHEN status='approved' THEN duration_s ELSE 0 END) FROM clips WHERE recording_id=?",
                      (r["recording_id"],)).fetchone()
        r["clips"] = {"total": k[0] or 0, "approved": k[1] or 0, "to_review": k[2] or 0, "excluded": k[3] or 0,
                      "approved_s": round(k[4] or 0, 1)}
    return rows


def stop_session(c, root: Path, cs_id: int, *, by: str = "admin") -> dict | None:
    """Admin stop of a running class collection (the writer thread notices within REFRESH_S)."""
    r = c.execute("SELECT * FROM class_sessions WHERE id=?", (cs_id,)).fetchone()
    if not r:
        return None
    if r["stopped_at"] is None:
        c.execute("UPDATE class_sessions SET stopped_at=?, stop_reason='admin' WHERE id=?", (time.time(), cs_id))
        c.execute("UPDATE room_collect SET collect=0, updated_by=?, updated_at=? WHERE room_id=?",
                  (by[:40], time.time(), r["room_id"]))
        if r["recording_id"]:
            recordings.finish(c, root, r["recording_id"])
        capture.refresh()
    return dict(c.execute("SELECT * FROM class_sessions WHERE id=?", (cs_id,)).fetchone())


def hours_over_time(c, *, bucket: str = "week") -> dict:
    """Per speaker: collected class hours and approved training hours by ISO week (Asia/Taipei)."""
    fmt = "%Y-W%W" if bucket == "week" else "%Y-%m"
    out: dict = {}
    for r in c.execute("SELECT cs.speaker_id, s.label, strftime(?, cs.started_at + 8*3600, 'unixepoch') AS b, "
                       "SUM(cs.audio_s) FROM class_sessions cs JOIN speakers s ON s.id = cs.speaker_id "
                       "WHERE s.withdrawn_at IS NULL GROUP BY 1, 3", (fmt,)):
        out.setdefault(r[0], {"label": r[1], "buckets": {}})["buckets"].setdefault(r[2], {"collected_h": 0, "approved_h": 0})
        out[r[0]]["buckets"][r[2]]["collected_h"] = round((r[3] or 0) / 3600, 3)
    for r in c.execute("SELECT k.speaker_id, s.label, strftime(?, k.created_at + 8*3600, 'unixepoch') AS b, "
                       "SUM(k.duration_s) FROM clips k JOIN speakers s ON s.id = k.speaker_id WHERE k.status='approved' "
                       "AND s.withdrawn_at IS NULL GROUP BY 1, 3", (fmt,)):
        out.setdefault(r[0], {"label": r[1], "buckets": {}})["buckets"].setdefault(r[2], {"collected_h": 0, "approved_h": 0})
        out[r[0]]["buckets"][r[2]]["approved_h"] = round((r[3] or 0) / 3600, 3)
    for v in out.values():
        v["series"] = [{"bucket": k, **b} for k, b in sorted(v.pop("buckets").items())]
        v["approved_total_h"] = round(sum(x["approved_h"] for x in v["series"]), 3)
        v["collected_total_h"] = round(sum(x["collected_h"] for x in v["series"]), 3)
    return out
