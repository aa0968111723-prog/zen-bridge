"""一次錄音（錄音室／社課收錄／匯入）＝ 音檔 + 逐字稿，永遠一起存。

錄音進行中：每個片段的 WAV 寫進 clips/，同時把字幕事件（草稿／定稿，含時間）append 到
``live.jsonl``。結束（或之後人工修正）時寫出完整逐字稿：``recording.wav``（片段依序串接；
匯入則是原始檔）＋ ``transcript.json``／``.srt``／``.vtt``／``.txt``，時間軸對齊 recording.wav。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

from app.training import audioq
from app.training import db as tdb

TRANSCRIPT_FILES = ("transcript.json", "transcript.srt", "transcript.vtt", "transcript.txt")
DEFAULT_TAG = "Speaker 0"
_TAG_RE = re.compile(r"^[^\n\r<>:：]{1,24}$")


def new_id(prefix: str) -> str:
    return f"{prefix}_{time.strftime('%Y%m%d')}_{secrets.token_hex(4)}"


def start(c: sqlite3.Connection, root: Path, *, kind: str, speaker_id: str | None, room_id=None, session_id=None,
          title=None) -> dict:
    rid = new_id({"studio": "rs", "harvest": "rh", "import": "ri"}[kind])
    d = f"recordings/{kind}/{speaker_id or 'mixed'}/{rid}"
    (Path(root) / d / "clips").mkdir(parents=True, exist_ok=True)
    c.execute("INSERT INTO recordings(id, kind, speaker_id, room_id, session_id, title, dir, started_at) "
              "VALUES (?,?,?,?,?,?,?,?)", (rid, kind, speaker_id, room_id, session_id,
                                           (title or None) and str(title)[:80], d, time.time()))
    return get(c, rid)


def get(c: sqlite3.Connection, rid: str) -> dict | None:
    r = c.execute("SELECT * FROM recordings WHERE id=?", (rid,)).fetchone()
    return dict(r) if r else None


def append_event(root: Path, rec: dict, event: dict) -> None:
    """Live transcript, written as it happens (one JSON line per draft/final caption)."""
    p = Path(root) / rec["dir"] / "live.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": round(time.time(), 3), **event}, ensure_ascii=False)
    with open(p, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def add_clip(c: sqlite3.Connection, root: Path, rec: dict, pcm, *, speaker_id: str | None, origin: str,
             text=None, asr_text=None, prompt_id=None, segment_id=None, t0_ms=None, t1_ms=None, tgt_lang=None,
             mt_text=None, status="unverified", align="native", speaker_tag=None, draft_text=None,
             log_event: bool = True, extra: dict | None = None) -> dict:
    cid = new_id("c")
    speaker_tag = speaker_tag or DEFAULT_TAG
    rel = f"{rec['dir']}/clips/{cid}.wav"
    sha = audioq.write_wav(Path(root) / rel, pcm)
    q = audioq.quality(pcm, min_s=0.0, max_s=1e9)
    ord_ = c.execute("SELECT COALESCE(MAX(ord), -1) + 1 FROM clips WHERE recording_id=?", (rec["id"],)).fetchone()[0]
    if t0_ms is None:
        prev = c.execute("SELECT MAX(t1_ms) FROM clips WHERE recording_id=?", (rec["id"],)).fetchone()[0] or 0
        t0_ms = int(prev) + (300 if prev else 0)
    if t1_ms is None:
        t1_ms = int(t0_ms + q.duration_s * 1000)
    c.execute("""INSERT INTO clips(id, recording_id, speaker_id, speaker_tag, origin, prompt_id, segment_id, ord, t0_ms,
                 t1_ms, audio_path, sha256, duration_s, rms_dbfs, peak_dbfs, clip_ratio, silence_ratio, snr_db,
                 draft_text, asr_text, text, tgt_lang, mt_text, status, align, created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (cid, rec["id"], speaker_id, speaker_tag, origin, prompt_id, segment_id, ord_, t0_ms, t1_ms, rel, sha,
               q.duration_s, q.rms_dbfs, q.peak_dbfs, q.clip_ratio, q.silence_ratio, q.snr_db, draft_text, asr_text,
               text, tgt_lang, mt_text, status, align, time.time()))
    if extra:
        cols = ", ".join(f"{k}=?" for k in extra)
        c.execute(f"UPDATE clips SET {cols} WHERE id=?", (*extra.values(), cid))
    if not log_event:
        return clip(c, cid)
    append_event(root, rec, {"type": "final", "clip_id": cid, "speaker": speaker_tag, "t0_ms": t0_ms, "t1_ms": t1_ms,
                             "asr_text": asr_text, "text": text, "tgt_lang": tgt_lang, "mt_text": mt_text})
    return clip(c, cid)


def clip(c: sqlite3.Connection, cid: str) -> dict | None:
    r = c.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    return dict(r) if r else None


def _ts(ms: int, sep: str) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_transcript(c: sqlite3.Connection, root: Path, rid: str) -> dict | None:
    """(Re)write recording.wav + transcript.{json,srt,vtt,txt}. Safe to call after every correction."""
    rec = get(c, rid)
    if not rec:
        return None
    root = Path(root)
    d = root / rec["dir"]
    d.mkdir(parents=True, exist_ok=True)
    rows = [dict(r) for r in c.execute("SELECT * FROM clips WHERE recording_id=? ORDER BY ord, t0_ms", (rid,))]
    segs = []
    if rec["kind"] == "harvest":
        # 社課：時間軸 = 直播時間軸，音檔 = audio/chunk_*.wav（整堂課）。課後整理前用 live.jsonl 的定稿。
        audio_name = "audio/" if (d / "audio").exists() and any((d / "audio").glob("chunk_*.wav")) else None
        by_seg = {r["segment_id"]: r for r in rows if r.get("segment_id")}
        merged = []
        for f in class_finals(d):          # every final line; derived clips (with review state) replace theirs
            merged.append(by_seg.pop(f"{rec['room_id']}:{rec['session_id']}:{f['seq']}", f))
        rows = sorted(merged + list(by_seg.values()), key=lambda r: r["t0_ms"] or 0)
        for r in rows:
            segs.append((r, r["t0_ms"] or 0, r["t1_ms"] or 0))
    elif rec["kind"] == "import":
        audio_name = rec["source_audio"] and Path(rec["source_audio"]).name
        for r in rows:
            segs.append((r, r["t0_ms"] or 0, r["t1_ms"] or 0))
    else:
        keep = [r for r in rows if r["audio_path"] and r["status"] != "rejected"
                and (root / r["audio_path"]).exists()]
        audio_name = None
        if keep:
            pcm, offsets = audioq.concat([root / r["audio_path"] for r in keep])
            audioq.write_wav(d / "recording.wav", pcm)
            audio_name = "recording.wav"
            for r, off in zip(keep, offsets):
                segs.append((r, off, off + int((r["duration_s"] or 0) * 1000)))
        elif (d / "recording.wav").exists():
            (d / "recording.wav").unlink()
    items = []
    for i, (r, a, b) in enumerate(segs, 1):
        items.append({"index": i, "clip_id": r["id"], "start_ms": a, "end_ms": b,
                      "live_t0_ms": r["t0_ms"], "live_t1_ms": r["t1_ms"], "speaker_id": r["speaker_id"],
                      "speaker_tag": r["speaker_tag"], "text": r["text"] or r["asr_text"] or "",
                      "asr_text": r["asr_text"], "draft_text": r["draft_text"], "status": r["status"],
                      "align": r["align"], "tgt_lang": r["tgt_lang"], "mt_text": r["mt_text"],
                      "excluded": bool(r.get("excluded")), "confidence": r.get("confidence"),
                      "audio_file": r.get("audio_path") and Path(r["audio_path"]).relative_to(rec["dir"]).as_posix()})
    doc = {"recording_id": rid, "kind": rec["kind"], "speaker_id": rec["speaker_id"], "room_id": rec["room_id"],
           "session_id": rec["session_id"], "title": rec["title"], "audio": audio_name,
           "started_at": rec["started_at"], "ended_at": rec["ended_at"], "segments": items,
           "note": "時間軸對齊 audio（recording.wav、匯入原始檔，或社課 audio/chunk_*.wav 每 5 分鐘一檔）；"
                   "status=unverified 代表尚未人工校對；excluded=true 是非主講者（不進資料集）"}
    _atomic(d / "transcript.json", json.dumps(doc, ensure_ascii=False, indent=1))
    srt, vtt, txt = [], ["WEBVTT", ""], []
    for it in items:
        mark = ("【排除】" if it["excluded"] else "" if it["status"] == "approved" else "【未校對】"
                if it["status"] == "unverified" else f"【{it['status']}】")
        tag = it["speaker_tag"] or DEFAULT_TAG
        srt += [str(it["index"]), f"{_ts(it['start_ms'], ',')} --> {_ts(it['end_ms'], ',')}", f"{tag}: {it['text']}", ""]
        vtt += [f"{_ts(it['start_ms'], '.')} --> {_ts(it['end_ms'], '.')}", f"<v {tag}>{it['text']}", ""]
        txt.append(f"[{_ts(it['start_ms'], '.')}] {tag}: {mark}{it['text']}")
    _atomic(d / "transcript.srt", "\n".join(srt))
    _atomic(d / "transcript.vtt", "\n".join(vtt))
    _atomic(d / "transcript.txt", "\n".join(txt) + ("\n" if txt else ""))
    c.execute("UPDATE recordings SET transcript_at=? WHERE id=?", (time.time(), rid))
    return doc


def class_finals(d: Path) -> list[dict]:
    """Final captions from a class live.jsonl (last version of each seq wins), in timeline order."""
    p = Path(d) / "live.jsonl"
    if not p.exists():
        return []
    by_seq: dict = {}
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue                                   # a line cut by a crash
        if ev.get("type") == "final" and (ev.get("text") or ev.get("asr_text")):
            by_seq[ev.get("seq", len(by_seq))] = ev
    out = []
    for i, ev in enumerate(sorted(by_seq.values(), key=lambda e: (e.get("t0_ms") or 0))):
        out.append({"id": None, "seq": ev.get("seq"), "t0_ms": ev.get("t0_ms") or 0, "t1_ms": ev.get("t1_ms") or 0,
                    "speaker_id": None, "speaker_tag": DEFAULT_TAG, "text": ev.get("text"), "asr_text": ev.get("asr_text"),
                    "draft_text": None, "status": "unverified", "align": "native", "tgt_lang": ev.get("tgt_lang"),
                    "mt_text": None, "excluded": 0, "confidence": None, "audio_path": None})
    return out


def class_marks(d: Path) -> list[tuple[int, int]]:
    """Host 'question in progress' intervals (ms on the class timeline)."""
    p = Path(d) / "live.jsonl"
    spans, start = [], None
    if not p.exists():
        return spans
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "question":
            if ev.get("on") and start is None:
                start = int(ev.get("t_ms") or 0)
            elif not ev.get("on") and start is not None:
                spans.append((start, int(ev.get("t_ms") or start)))
                start = None
    if start is not None:
        spans.append((start, 10 ** 12))
    return spans


def finish(c: sqlite3.Connection, root: Path, rid: str) -> dict | None:
    rec = get(c, rid)
    if not rec:
        return None
    if rec["status"] != "ended":
        c.execute("UPDATE recordings SET status='ended', ended_at=? WHERE id=?", (time.time(), rid))
        if rec["kind"] != "harvest":                  # 社課的 end 事件由收錄執行緒寫
            append_event(root, rec, {"type": "end"})
    write_transcript(c, root, rid)
    return get(c, rid)


def rename_tag(c: sqlite3.Connection, root: Path, rid: str, old: str, new: str) -> dict:
    """Rename a speaker label (e.g. "Speaker 1" -> "法師") across the whole recording + transcript."""
    new = " ".join((new or "").split())
    if not _TAG_RE.match(new):
        raise ValueError("講者標籤需 1–24 字，不可含冒號或角括號")
    n = c.execute("UPDATE clips SET speaker_tag=? WHERE recording_id=? AND speaker_tag=?", (new, rid, old)).rowcount
    rec = get(c, rid)
    if rec:
        append_event(root, rec, {"type": "rename", "from": old, "to": new})
        write_transcript(c, root, rid)
    return {"renamed": n}


def set_clip_tag(c: sqlite3.Connection, root: Path, cid: str, tag: str) -> dict | None:
    tag = " ".join((tag or "").split())
    if not _TAG_RE.match(tag):
        raise ValueError("講者標籤需 1–24 字，不可含冒號或角括號")
    k = clip(c, cid)
    if not k:
        return None
    c.execute("UPDATE clips SET speaker_tag=? WHERE id=?", (tag, cid))
    write_transcript(c, root, k["recording_id"])
    return clip(c, cid)


def map_tag(c: sqlite3.Connection, root: Path, rid: str, tag: str, speaker_id: str | None) -> dict:
    """Apply a label to the consent registry for the whole recording.

    speaker_id=None excludes the label: its audio is deleted and the lines stay needs_review.
    A speaker without ASR consent is refused (the lines stay excluded)."""
    from app.training import registry
    rec = get(c, rid)
    if not rec:
        raise registry.RegistryError("找不到這次錄音", code="not_found", status=404)
    if rec["kind"] == "import" and speaker_id:
        from app.training import importer
        return importer.map_speaker(c, root, rid, tag, speaker_id)
    rows = c.execute("SELECT id, audio_path FROM clips WHERE recording_id=? AND speaker_tag=?", (rid, tag)).fetchall()
    if speaker_id is None:
        for r in rows:
            if r["audio_path"]:
                p = tdb.absolute(root, r["audio_path"])
                if p.exists():
                    p.unlink()
        c.execute("UPDATE clips SET speaker_id=NULL, audio_path=NULL, status='needs_review', excluded=1, "
                  "excluded_reason='reviewer' WHERE recording_id=? AND speaker_tag=?", (rid, tag))
    else:
        if not registry.has_consent(c, speaker_id, "asr"):
            raise registry.RegistryError("這位講者沒有有效的 ASR 同意", code="no_consent", status=403)
        c.execute("UPDATE clips SET speaker_id=? WHERE recording_id=? AND speaker_tag=? AND audio_path IS NOT NULL",
                  (speaker_id, rid, tag))
    write_transcript(c, root, rid)
    return {"recording_id": rid, "tag": tag, "speaker_id": speaker_id, "clips": len(rows)}


def bundle(c: sqlite3.Connection, root: Path, rid: str, out_path: Path | None = None):
    """One-click export: transcript.{txt,srt,vtt,json} + audio (+ live.jsonl) as a zip.

    Class recordings include the whole-class chunks (audio/chunk_*.wav) while they are retained.
    With ``out_path`` the zip is written to disk (large classes) and the path is returned."""
    import io
    import zipfile
    rec = get(c, rid)
    if not rec:
        raise FileNotFoundError(rid)
    write_transcript(c, root, rid)
    d = Path(root) / rec["dir"]
    target = open(out_path, "wb") if out_path else io.BytesIO()
    with target as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as z:
        for name in TRANSCRIPT_FILES + ("live.jsonl",):
            if (d / name).exists():
                z.write(d / name, f"{rid}/{name}")
        audio = [d / "recording.wav"]
        if rec["kind"] == "import" and rec["source_audio"]:
            audio = [Path(root) / rec["source_audio"]]
        if rec["kind"] == "harvest":
            audio = sorted((d / "audio").glob("chunk_*.wav"))
        for a in audio:
            if a.exists():
                arc = f"{rid}/audio/{a.name}" if a.parent.name == "audio" else f"{rid}/{a.name}"
                z.write(a, arc, compress_type=zipfile.ZIP_STORED)
        if not out_path:
            z.close()
            return fh.getvalue()
    return Path(out_path)
