"""Synthetic 16 kHz speech-like audio for the training-data tests (bursts of tone over a quiet floor)."""
import array
import io
import math
import random
import wave

RATE = 16000


def speechy(seconds: float = 3.0, amp: int = 8000, seed: int = 1, clip: bool = False) -> array.array:
    rnd = random.Random(seed)
    n = int(seconds * RATE)
    out = array.array("h")
    f = 180 + seed * 7
    for i in range(n):
        t = i / RATE
        on = (int(t * 4) % 3) != 2 and 0.2 < t < seconds - 0.2
        v = rnd.randint(-60, 60)
        if on:
            v += int(amp * math.sin(2 * math.pi * f * t) * (0.6 + 0.4 * math.sin(2 * math.pi * 3 * t)))
        if clip:
            v *= 6
        out.append(max(-32768, min(32767, v)))
    return out


def wav_bytes(pcm: array.array) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def class_clip(c, root, *, room_id, session_id, seq, wav_path, zh_raw, zh, speaker_id, tgt_lang=None, t0_ms=None,
               t1_ms=None):
    """A lecturer segment as the post-class derive step would leave it (unverified, in a class recording)."""
    import time as _t
    from app.training import audioq, recordings
    row = c.execute("SELECT recording_id FROM class_sessions WHERE room_id=? AND session_id=?",
                    (room_id, session_id)).fetchone()
    rec = recordings.get(c, row[0]) if row else None
    if rec is None:
        rec = recordings.start(c, root, kind="harvest", speaker_id=speaker_id, room_id=room_id, session_id=session_id)
        c.execute("INSERT INTO class_sessions(room_id, session_id, speaker_id, recording_id, started_at) VALUES (?,?,?,?,?)",
                  (room_id, session_id, speaker_id, rec["id"], _t.time()))
    seg = f"{room_id}:{session_id}:{seq}"
    old = c.execute("SELECT id FROM clips WHERE segment_id=?", (seg,)).fetchone()
    if old:
        from app.training.studio import delete_clip
        delete_clip(c, root, old[0], rewrite=False)
    k = recordings.add_clip(c, root, rec, audioq.read_pcm(wav_path), speaker_id=speaker_id, origin="harvest",
                            asr_text=zh_raw, text=zh, segment_id=seg, t0_ms=t0_ms, t1_ms=t1_ms, tgt_lang=tgt_lang,
                            status="unverified", align="native", extra={"confidence": 0.9})
    return k["id"]


class HarvestShim:
    """Old per-segment test helper API on top of class_clip (host toggle + consent checked on each call)."""

    def __init__(self, c, root):
        self.c, self.root, self.on = c, root, {}

    def enable(self, c, *, room_id, speaker_id, enabled_by="host"):
        from app.training import harvest
        harvest.set_room(c, room_id=room_id, speaker_id=speaker_id, collect=True, by=enabled_by)
        self.on[room_id] = speaker_id

    def capture(self, *, room_id, session_id, seq, wav_path, zh_raw, zh, t0_ms=None, t1_ms=None, tgt_lang=None):
        from app.training import registry
        sid = self.on.get(room_id)
        if not sid or not registry.has_consent(self.c, sid, "asr", live=True):
            return None
        return class_clip(self.c, self.root, room_id=room_id, session_id=session_id, seq=seq, wav_path=wav_path,
                          zh_raw=zh_raw, zh=zh, speaker_id=sid, tgt_lang=tgt_lang, t0_ms=t0_ms, t1_ms=t1_ms)

    def stop(self, *a, **k):
        from app.training import recordings
        for (rid,) in self.c.execute("SELECT recording_id FROM class_sessions WHERE stopped_at IS NULL").fetchall():
            self.c.execute("UPDATE class_sessions SET stopped_at=? WHERE recording_id=?", (__import__("time").time(), rid))
            recordings.finish(self.c, self.root, rid)
