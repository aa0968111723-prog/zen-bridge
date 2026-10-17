"""錄音室：提示句朗讀。每次坐下錄 = 一個 studio recording；每句一個 take（可重錄）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.training import audioq, recordings, registry
from app.training import db as tdb

HARD = {"empty", "too_short", "too_long", "mostly_silence"}      # 這些不存，直接請重錄
MAX_TAKE_BYTES = 30 * audioq.RATE * 2 + 4096 + 64 * 1024


def start(c: sqlite3.Connection, root: Path, speaker_id: str, title=None) -> dict:
    if not registry.has_consent(c, speaker_id, "asr"):
        raise registry.RegistryError("這位講者沒有有效的語音辨識（ASR）同意，不能錄音", code="no_consent", status=403)
    return recordings.start(c, root, kind="studio", speaker_id=speaker_id, title=title)


def take(c: sqlite3.Connection, root: Path, rid: str, wav: bytes, *, prompt_id: int | None, replace: str | None = None,
         text: str | None = None) -> dict:
    rec = recordings.get(c, rid)
    if not rec or rec["kind"] != "studio":
        raise registry.RegistryError("找不到這次錄音", code="not_found", status=404)
    if rec["status"] != "recording":
        raise registry.RegistryError("這次錄音已結束，請重新開始", code="ended", status=409)
    if not registry.has_consent(c, rec["speaker_id"], "asr"):
        raise registry.RegistryError("講者同意已失效", code="no_consent", status=403)
    try:
        pcm = audioq.read_pcm(wav)
    except audioq.AudioFormatError as exc:
        raise registry.RegistryError(str(exc), code="bad_audio")
    q = audioq.quality(pcm)
    hard = [p for p in q.problems if p in HARD]
    if hard:
        return {"saved": False, "quality": q.as_dict(), "problems": [audioq.PROBLEM_TEXT[p] for p in q.problems]}
    if prompt_id is not None:
        p = c.execute("SELECT text FROM prompts WHERE id=?", (prompt_id,)).fetchone()
        if not p:
            raise registry.RegistryError("找不到提示句", code="not_found", status=404)
        text = p[0]
    if not text:
        raise registry.RegistryError("需要 prompt_id 或文字")
    if replace:
        delete_clip(c, root, replace, rewrite=False)
    status = "approved" if not q.problems else "unverified"
    clip = recordings.add_clip(c, root, rec, pcm, speaker_id=rec["speaker_id"], origin="studio", text=text,
                               prompt_id=prompt_id, status=status, align="native")
    return {"saved": True, "clip": clip, "quality": q.as_dict(),
            "problems": [audioq.PROBLEM_TEXT[p] for p in q.problems]}


def delete_clip(c: sqlite3.Connection, root: Path, cid: str, *, rewrite: bool = True) -> bool:
    r = c.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    if not r:
        return False
    if r["audio_path"]:
        try:
            p = tdb.absolute(root, r["audio_path"])
            if p.exists():
                p.unlink()
        except ValueError:
            pass
    c.execute("DELETE FROM clips WHERE id=?", (cid,))
    rec = recordings.get(c, r["recording_id"])
    if rec:
        recordings.append_event(root, rec, {"type": "delete", "clip_id": cid})
        if rewrite and rec["status"] == "ended":
            recordings.write_transcript(c, root, rec["id"])
    return True
