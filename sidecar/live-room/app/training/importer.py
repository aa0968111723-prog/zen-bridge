"""匯入既有錄音＋逐字稿（音檔 MP3/M4A/MP4/WAV… + TXT/SRT/VTT/JSON）。只讀本機檔案，不連任何雲端。

* 音檔用本機 ffmpeg 轉成 16 kHz 單聲道，放在這次匯入的隔離區（source.wav，可能含其他同學的聲音）。
* 逐字稿解析出時間與講者標籤。沒有時間：有設定 ZEN_TRAINING_ALIGNER（whisper.cpp CLI）就用 Breeze
  的時間戳強制對齊；沒有就依字數比例估計。兩者都標記 align='forced' 並一律 needs_review。
* 講者標籤要對應到名冊裡有同意的講者才會切出片段（unverified）；沒對應或沒同意的 = needs_review、
  不切音檔、不會進資料集。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.training import audioq, recordings, registry, textnorm
from app.training import db as tdb

AUDIO_EXT = {".mp3", ".m4a", ".mp4", ".wav", ".aac", ".flac", ".ogg", ".opus", ".webm", ".mov", ".mkv", ".wma"}
TEXT_EXT = {".txt", ".srt", ".vtt", ".json"}
MAX_SEG_MS = 30000


@dataclass
class Seg:
    text: str
    start_ms: int | None = None
    end_ms: int | None = None
    speaker: str | None = None


_TS = r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?"
_CUE = re.compile(rf"^\s*{_TS}\s*-->\s*{_TS}")
_LINE_TS = re.compile(rf"^\s*[\[(]?{_TS}[\])]?\s*(?:[-–]\s*{_TS}[\])]?)?\s*(.*)$")
_SPK = re.compile(r"^\s*([^:：\[\]]{1,24})[:：]\s*(.+)$")
_VOICE = re.compile(r"^<v\s+([^>]+)>(.*?)(?:</v>)?$")


def _ms(h, m, s, frac) -> int:
    frac = (frac or "0").ljust(3, "0")[:3]
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac)


def _split_speaker(text: str) -> tuple[str | None, str]:
    v = _VOICE.match(text.strip())
    if v:
        return v.group(1).strip(), v.group(2).strip()
    m = _SPK.match(text)
    if m and not re.search(r"https?$", m.group(1)):
        return m.group(1).strip(), m.group(2).strip()
    return None, text.strip()


def parse_cues(raw: str) -> list[Seg]:
    out, cur = [], None
    for line in raw.splitlines() + [""]:
        m = _CUE.match(line)
        if m:
            g = m.groups()
            cur = Seg("", _ms(*g[0:4]), _ms(*g[4:8]))
            continue
        if cur is None:
            continue
        if not line.strip():
            if cur.text:
                cur.speaker, cur.text = _split_speaker(cur.text)
                out.append(cur)
            cur = None
            continue
        piece = line.strip()
        if not piece.startswith("<v"):
            piece = re.sub(r"<[^>]+>", "", piece)
        cur.text = (cur.text + " " + piece).strip()
    return out


def parse_txt(raw: str) -> list[Seg]:
    """Lines like "[00:01:02] 講者: 文字", "00:01:02 講者" (header) + text lines, or plain paragraphs."""
    out: list[Seg] = []
    pending: Seg | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        m = _LINE_TS.match(line)
        if m and m.group(2) is not None:
            g = m.groups()
            start = _ms(*g[0:4])
            end = _ms(*g[4:8]) if g[5] is not None else None
            rest = (g[8] or "").strip()
            spk, text = _split_speaker(rest) if rest else (None, "")
            if text and spk is None and len(rest) <= 24 and not re.search(r"[。，,.!?？！]", rest):
                spk, text = rest, ""          # "00:01:02 小明" header line, text follows
            pending = Seg(text, start, end, spk)
            out.append(pending)
            continue
        spk, text = _split_speaker(line)
        if pending is not None and spk is None:
            pending.text = (pending.text + " " + text).strip()
        else:
            pending = Seg(text, None, None, spk)
            out.append(pending)
    return [s for s in out if s.text]


def parse_json(raw: str) -> list[Seg]:
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("segments") or data.get("transcription") or data.get("items") or []
    out = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()
        if not text:
            continue
        start, end = it.get("start_ms"), it.get("end_ms")
        if start is None and "start" in it:
            start, end = it.get("start"), it.get("end")
            start = None if start is None else int(float(start) * 1000)
            end = None if end is None else int(float(end) * 1000)
        if start is None and isinstance(it.get("offsets"), dict):          # whisper.cpp -oj
            start, end = it["offsets"].get("from"), it["offsets"].get("to")
        out.append(Seg(text, start, end, it.get("speaker") or it.get("speaker_tag")))
    return out


def parse_transcript(path: Path) -> list[Seg]:
    raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    ext = Path(path).suffix.lower()
    if ext in (".srt", ".vtt") or _CUE.search(raw[:2000] or ""):
        return parse_cues(raw)
    if ext == ".json":
        return parse_json(raw)
    return parse_txt(raw)


def _aligner_segments(wav: Path) -> list[Seg] | None:
    """Run the local whisper.cpp CLI configured in ZEN_TRAINING_ALIGNER, e.g.
    ``whisper-cli -m C:\\...\\breeze-ggml.bin -l zh -oj -of {out} -f {wav}``. Returns timed segments."""
    cmd = (os.environ.get("ZEN_TRAINING_ALIGNER") or "").strip()
    if not cmd:
        return None
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "align"
        args = [a.replace("{wav}", str(wav)).replace("{out}", str(out)) for a in shlex.split(cmd, posix=os.name != "nt")]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=3600)
        js = out.with_suffix(".json")
        if proc.returncode != 0 or not js.exists():
            return None
        return parse_json(js.read_text(encoding="utf-8", errors="replace"))


def align(segs: list[Seg], duration_ms: int, wav: Path | None = None) -> tuple[list[Seg], str]:
    """Give every segment a time span. Returns (segments, align_kind)."""
    if segs and all(s.start_ms is not None for s in segs):
        for i, s in enumerate(segs):
            if s.end_ms is None:
                s.end_ms = segs[i + 1].start_ms if i + 1 < len(segs) else duration_ms
        return segs, "file"
    ref = [textnorm.norm(s.text) for s in segs]
    total = sum(len(r) for r in ref) or 1
    timed = _aligner_segments(wav) if wav else None
    if timed:
        # map cumulative character position of the transcript onto the ASR's timed character stream
        stream = []
        for t in timed:
            n = max(1, len(textnorm.norm(t.text)))
            a, b = t.start_ms or 0, t.end_ms or t.start_ms or 0
            stream += [a + (b - a) * k / n for k in range(n)]
        stream.append(timed[-1].end_ms or duration_ms)

        def at(pos):
            return int(stream[min(len(stream) - 1, int(pos / total * (len(stream) - 1)))])
    else:
        def at(pos):
            return int(pos / total * duration_ms)
    pos = 0
    for s, r in zip(segs, ref):
        s.start_ms, s.end_ms = at(pos), at(pos + len(r))
        pos += len(r)
    return segs, "forced"


def import_recording(c, root: Path, audio: Path, transcript: Path, *, title=None, speaker_map: dict | None = None,
                     actor: str = "admin") -> dict:
    root = Path(root)
    if Path(audio).suffix.lower() not in AUDIO_EXT:
        raise registry.RegistryError("音檔格式不支援")
    if Path(transcript).suffix.lower() not in TEXT_EXT:
        raise registry.RegistryError("逐字稿需為 TXT／SRT／VTT／JSON")
    segs = parse_transcript(transcript)
    if not segs:
        raise registry.RegistryError("逐字稿裡沒有讀到任何文字")
    rec = recordings.start(c, root, kind="import", speaker_id=None, title=title or Path(audio).stem)
    src_rel = f"{rec['dir']}/source.wav"
    audioq.convert_any(Path(audio), root / src_rel)
    c.execute("UPDATE recordings SET source_audio=?, status='ended', ended_at=started_at WHERE id=?",
              (src_rel, rec["id"]))
    pcm = audioq.read_pcm(root / src_rel)
    dur_ms = int(len(pcm) * 1000 / audioq.RATE)
    segs, kind = align(segs, dur_ms, root / src_rel)
    for i, s in enumerate(segs):
        c.execute("INSERT INTO clips(id, recording_id, speaker_id, speaker_tag, origin, ord, t0_ms, t1_ms, text, "
                  "status, align, created_at) VALUES (?,?,NULL,?,'import',?,?,?,?, 'needs_review', ?, ?)",
                  (recordings.new_id("c"), rec["id"], s.speaker or "（未標）", i, s.start_ms, s.end_ms, s.text, kind,
                   rec["started_at"]))
    tdb.audit(c, "import", actor=actor, recording=rec["id"], segments=len(segs), align=kind)
    for tag, sid in (speaker_map or {}).items():
        if sid:
            map_speaker(c, root, rec["id"], tag, sid, actor=actor, pcm=pcm)
    recordings.write_transcript(c, root, rec["id"])
    return summary(c, rec["id"])


def map_speaker(c, root: Path, rid: str, tag: str, speaker_id: str, *, actor: str = "admin", pcm=None) -> dict:
    """Cut clips for every segment labelled ``tag`` — only if the speaker has ASR consent."""
    root = Path(root)
    rec = recordings.get(c, rid)
    if not rec or rec["kind"] != "import":
        raise registry.RegistryError("找不到這筆匯入", code="not_found", status=404)
    if not registry.has_consent(c, speaker_id, "asr"):
        raise registry.RegistryError("這位講者沒有有效的 ASR 同意；這些片段維持排除", code="no_consent", status=403)
    if not rec["source_audio"]:
        raise registry.RegistryError("原始音檔已清除，無法再切片段", code="source_gone", status=409)
    if pcm is None:
        pcm = audioq.read_pcm(root / rec["source_audio"])
    rows = c.execute("SELECT * FROM clips WHERE recording_id=? AND speaker_tag=? AND audio_path IS NULL "
                     "AND status!='rejected'", (rid, tag)).fetchall()
    for r in rows:
        piece = audioq.cut(pcm, r["t0_ms"] or 0, min(r["t1_ms"] or 0, (r["t0_ms"] or 0) + MAX_SEG_MS))
        rel = f"{rec['dir']}/clips/{r['id']}.wav"
        sha = audioq.write_wav(root / rel, piece)
        q = audioq.quality(piece, min_s=0, max_s=1e9)
        status = "needs_review" if r["align"] == "forced" else "unverified"
        c.execute("UPDATE clips SET speaker_id=?, audio_path=?, sha256=?, duration_s=?, rms_dbfs=?, peak_dbfs=?, "
                  "clip_ratio=?, silence_ratio=?, snr_db=?, status=? WHERE id=?",
                  (speaker_id, rel, sha, q.duration_s, q.rms_dbfs, q.peak_dbfs, q.clip_ratio, q.silence_ratio, q.snr_db,
                   status, r["id"]))
    tdb.audit(c, "import_map", speaker_id=speaker_id, actor=actor, recording=rid, clips=len(rows))
    recordings.write_transcript(c, root, rid)
    return summary(c, rid)


def purge_source(c, root: Path, rid: str) -> dict:
    rec = recordings.get(c, rid)
    if rec and rec["source_audio"]:
        p = tdb.absolute(root, rec["source_audio"])
        if p.exists():
            p.unlink()
        c.execute("UPDATE recordings SET source_audio=NULL WHERE id=?", (rid,))
    return summary(c, rid)


def summary(c, rid: str) -> dict:
    rec = recordings.get(c, rid)
    tags = c.execute("SELECT speaker_tag, COUNT(*), SUM(audio_path IS NOT NULL), MAX(speaker_id) FROM clips "
                     "WHERE recording_id=? GROUP BY speaker_tag", (rid,)).fetchall()
    return {**(rec or {}), "speakers": [{"tag": t, "segments": n, "cut": int(k or 0), "speaker_id": s}
                                        for t, n, k, s in tags]}


def scan_inbox(c, root: Path, *, actor: str = "cli") -> list[dict]:
    """Import every audio file in training\\import\\inbox that has a transcript with the same stem.
    The inbox copies are deleted after a successful import (put copies there, not originals)."""
    inbox = Path(root) / "import" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    done = []
    for a in sorted(inbox.iterdir()):
        if a.suffix.lower() not in AUDIO_EXT:
            continue
        t = next((a.with_suffix(e) for e in (".srt", ".vtt", ".json", ".txt") if a.with_suffix(e).exists()), None)
        if not t:
            continue
        done.append(import_recording(c, root, a, t, actor=actor))
        for f in (a, t):          # 匯入後刪除收件匣的複本：原檔可能有沒同意的人的聲音
            f.unlink()
    return done
