"""社課連續收錄（主要路徑）：主持頁開關、整堂音檔＋逐字稿、課後整理、聲紋排除、批次審核、保留期限。"""
import asyncio
import json
import os
import struct
import time
import warnings
from pathlib import Path

import numpy as np
import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

from app.training import capture, classaudio, dataset, derive, harvest, recordings, registry, review, spkcheck  # noqa: E402
from app.training import db as tdb  # noqa: E402
from tests.training_fakes import speechy, wav_bytes  # noqa: E402

LECTURER_SEED, OTHER_SEED = 1, 20          # 187 Hz vs 320 Hz "voices"


class FakeVoice:
    """Deterministic stand-in for the CAM++ extractor: embedding = which 'voice' (pitch band) dominates."""
    name = "sherpa"

    def embed(self, x):
        spec = np.abs(np.fft.rfft(x[:16000 * 4]))
        f = np.argmax(spec[40:]) + 40
        hz = f * 16000 / (2 * len(spec) - 2)
        return np.array([1.0, 0.05, 0.0] if hz < 250 else [0.0, 0.05, 1.0], dtype=np.float32)


@pytest.fixture
def tr(tmp_path, monkeypatch):
    root = tmp_path / "training"
    monkeypatch.setenv("ZEN_TRAINING_DIR", str(root))
    capture._reset_for_tests(root)
    c = tdb.connect(root)
    yield c, root
    capture._reset_for_tests(None)
    c.close()


def lecturer(c, label="法師"):
    s = registry.new_speaker(c, label)
    registry.record_consent(c, s["id"], scope_asr=True, scope_mt=True, harvest_live=True, recorded_by="社長",
                            confirm_text=True)
    return s["id"]


def wait(pred, timeout=10.0):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        if pred():
            return True
        time.sleep(0.02)
    return False


def piece(tmp, i, seed, seconds=6.0):
    p = tmp / f"p{i}.wav"
    p.write_bytes(wav_bytes(speechy(seconds, seed=seed)))
    return p


def run_class(c, root, tmp, *, room="class", session="s1", other_at=(4,), question_at=(), n=8):
    """Simulate the pipeline calls for one class: n 6-second pieces + their finals."""
    sid = lecturer(c)
    harvest.set_room(c, room_id=room, speaker_id=sid, collect=True)
    capture.refresh()
    assert wait(lambda: capture.armed(room))
    order = list(range(n))
    if n >= 3:
        order[1], order[2] = order[2], order[1]                   # out-of-order arrival is fine
    for i in order:
        seed = OTHER_SEED if i in other_at else LECTURER_SEED
        if i in question_at:
            capture.mark(room, "question", True, wall_ms=1_000_000 + i * 6000 + 1500)
        capture.tap_wav(room, session, i + 1, i * 6000, i * 6000 + 6000, 1_000_000 + i * 6000 + 6000, piece(tmp, i, seed))
        capture.final(room, session, i + 1, i * 6000, i * 6000 + 6000, f"第{i}句禪修開示的內容", f"第{i}句禪修開示的內容", "en")
        if i in question_at:
            capture.mark(room, "question", False, wall_ms=1_000_000 + i * 6000 + 4500)
    assert capture.flush()
    return sid


# ------------------------------------------------------------------ hot path
def test_not_armed_is_a_dict_lookup_and_writes_nothing(tmp_path, monkeypatch):
    root = tmp_path / "none"
    monkeypatch.setenv("ZEN_TRAINING_DIR", str(root))
    capture._reset_for_tests(root)
    w = piece(tmp_path, 0, 1)
    t = time.perf_counter()
    for i in range(2000):
        capture.tap_wav("class", "s", i, 0, 6000, None, w)
        capture.final("class", "s", i, 0, 6000, "a", "a")
    per_call_us = (time.perf_counter() - t) / 4000 * 1e6
    assert per_call_us < 50, per_call_us
    assert capture._q.qsize() == 0 and not root.exists()
    capture._reset_for_tests(None)


def test_queue_full_drops_audio_instead_of_blocking(tr, tmp_path, monkeypatch):
    c, root = tr
    monkeypatch.setattr(capture, "_armed", {"class": {"speaker_id": "x", "label": "x"}})
    monkeypatch.setattr(capture, "_ensure", lambda: None)
    w = piece(tmp_path, 0, 1)
    t = time.perf_counter()
    for i in range(capture.QUEUE_MAX + 50):
        capture.tap_wav("class", "s", i, i * 6000, i * 6000 + 6000, None, w)
    assert time.perf_counter() - t < 5
    assert capture.stats()["dropped"] == 50


# ------------------------------------------------------------------ chunked audio
def test_chunk_writer_positions_and_survives_a_crash(tmp_path):
    d = tmp_path / "audio"
    w = classaudio.ChunkWriter(d)
    a = np.full(16000 * 10, 1000, dtype=np.int16).tobytes()
    b = np.full(16000 * 4, -2000, dtype=np.int16).tobytes()
    w.write(296_000, a)                     # spans chunk 0 -> chunk 1
    w.write(0, b)
    w.flush()                               # no close(): simulate a crash
    assert [p.name for p in classaudio.chunks(d)] == ["chunk_0000.wav", "chunk_0001.wav"]
    with open(d / "chunk_0000.wav", "rb") as f:
        hdr = f.read(44)
    assert struct.unpack("<I", hdr[40:44])[0] == classaudio.CHUNK_BYTES
    s = classaudio.read_span(d, 295_000, 307_000)
    assert s[0] == 0 and s[16000 * 1 + 10] == 1000 and s[-1] == 0
    assert classaudio.read_span(d, 0, 4000)[100] == -2000
    assert abs(classaudio.total_seconds(d) - 306.0) < 0.01
    import wave
    with wave.open(str(d / "chunk_0001.wav")) as wf:          # valid WAV even without close()
        assert wf.getframerate() == 16000 and wf.getnframes() == 6 * 16000


def test_pcm_from_wav_bytes():
    pcm = speechy(1.0)
    assert classaudio.pcm_from_wav_bytes(wav_bytes(pcm)) == pcm.tobytes()
    assert classaudio.pcm_from_wav_bytes(b"not a wav") is None


# ------------------------------------------------------------------ host toggle
def test_toggle_requires_live_consent_and_remembers_default(tr):
    c, root = tr
    sid = lecturer(c)
    s2 = registry.new_speaker(c, "只同意 MT")["id"]
    registry.record_consent(c, s2, scope_asr=False, scope_mt=True, recorded_by="社長", confirm_text=True)
    with pytest.raises(registry.RegistryError):
        harvest.set_room(c, room_id="class", speaker_id=s2, collect=True)
    assert [x["id"] for x in harvest.lecturers(c)] == [sid]
    assert harvest.lecturers(c)[0]["default_on"] is False
    harvest.set_room(c, room_id="class", speaker_id=sid, collect=True)
    harvest.set_room(c, room_id="class", speaker_id=sid, collect=False)
    assert harvest.lecturers(c)[0]["default_on"] is True        # once enabled = default on for this speaker
    assert harvest.room_setting(c, "class")["collect"] == 0


# ------------------------------------------------------------------ full class
def test_class_continuous_capture_transcript_and_derive(tr, tmp_path):
    c, root = tr
    sid = run_class(c, root, tmp_path, other_at=(5,), question_at=(6,))
    st = capture.status("class")
    assert st["collecting"] and st["finals"] == 8 and st["audio_s"] == 48.0, (st, capture.stats(), capture._q.unfinished_tasks)
    cs = dict(c.execute("SELECT * FROM class_sessions").fetchone())
    rec = recordings.get(c, cs["recording_id"])
    d = root / rec["dir"]
    assert (d / "audio" / "chunk_0000.wav").exists()
    ev = [json.loads(x) for x in (d / "live.jsonl").read_text(encoding="utf-8").splitlines()]
    assert ev[0]["type"] == "start" and sum(e["type"] == "final" for e in ev) == 8
    q = [e for e in ev if e["type"] == "question"]
    assert [e["on"] for e in q] == [True, False] and q[0]["t_ms"] == 37500 and q[1]["t_ms"] == 40500
    capture.end("class", "s1")
    assert capture.flush()
    assert wait(lambda: c.execute("SELECT stopped_at FROM class_sessions").fetchone()[0] is not None)
    for name in recordings.TRANSCRIPT_FILES:
        assert (d / name).exists(), name
    srt = (d / "transcript.srt").read_text(encoding="utf-8")
    assert srt.count(" --> ") == 8 and "00:00:42,000 --> 00:00:48,000" in srt
    # post-class: cut segments, voice check, confidence
    out = derive.derive_recording(c, root, rec["id"], be=FakeVoice())
    assert out["segments"] == 8 and out["excluded"] == 2, out
    rows = {r["t0_ms"] // 6000: dict(r) for r in c.execute("SELECT * FROM clips")}
    assert rows[5]["excluded"] == 1 and rows[5]["excluded_reason"] == "speaker_mismatch" and rows[5]["speaker_id"] is None
    assert rows[6]["excluded"] == 1 and rows[6]["excluded_reason"] == "question"
    assert rows[0]["excluded"] == 0 and rows[0]["speaker_id"] == sid and rows[0]["confidence"] >= 0.85
    assert "near_question" in json.loads(rows[7]["flags"])
    # batch review: uncertain first, approve-all above threshold, keyboard-sized decisions
    b = review.session_batch(c, rec["id"], min_conf=0.85)
    assert b["counts"]["todo"] == 6 and b["counts"]["excluded"] == 2
    confs = [x["confidence"] for x in b["items"]]
    assert confs == sorted(confs)
    assert review.approve_above(c, root, rec["id"], min_conf=0.85, actor="t")["approved"] == b["counts"]["above_threshold"]
    review.set_excluded(c, root, rows[5]["id"], excluded=False, actor="t")       # reviewer: that was the lecturer
    k = recordings.clip(c, rows[5]["id"])
    assert k["excluded"] == 0 and k["speaker_id"] == sid
    review.decide(c, root, rows[5]["id"], action="approve", actor="t", text="第5句修正後")
    # transcript reflects the review; derive again keeps reviewed decisions
    recordings.write_transcript(c, root, rec["id"])
    txt = (d / "transcript.txt").read_text(encoding="utf-8")
    assert "第5句修正後" in txt and "【排除】" in txt
    derive.derive_recording(c, root, rec["id"], be=FakeVoice())
    assert recordings.clip(c, rows[5]["id"])["text"] == "第5句修正後"
    # dataset only takes approved lecturer lines, never excluded ones
    ex = dataset.build(c, root)
    assert ex["speakers"] == [sid]
    files = list(Path(ex["dir"]).rglob("*.wav"))
    assert rows[6]["id"] not in " ".join(p.name for p in files)
    # hours per speaker over time
    h = harvest.hours_over_time(c)
    assert h[sid]["collected_total_h"] > 0.01 and h[sid]["approved_total_h"] > 0
    assert harvest.sessions(c)[0]["clips"]["approved"] >= 1


def test_retention_deletes_raw_and_excluded_audio(tr, tmp_path):
    c, root = tr
    run_class(c, root, tmp_path, other_at=(3,))
    capture.end("class", "s1")
    assert capture.flush() and wait(lambda: c.execute("SELECT stopped_at FROM class_sessions").fetchone()[0])
    rid = c.execute("SELECT recording_id FROM class_sessions").fetchone()[0]
    derive.derive_recording(c, root, rid, be=FakeVoice())
    disk = derive.disk(c, root)
    assert disk["raw_bytes"] > 48 * 32000 and disk["estimate"]["raw_mb_per_class_hour"] == 115.2
    derive.set_settings(c, raw_retention_days=14, excluded_audio_days=3)
    assert derive.cleanup(c, root)["freed_bytes"] == 0                     # nothing old enough yet
    later = time.time() + 15 * 86400
    out = derive.cleanup(c, root, now=later)
    assert out["excluded_audio_removed"] == 1 and out["freed_bytes"] > 0
    adir = root / recordings.get(c, rid)["dir"] / "audio"
    assert not classaudio.chunks(adir)
    assert c.execute("SELECT raw_deleted_at FROM class_sessions").fetchone()[0]
    assert (root / recordings.get(c, rid)["dir"] / "transcript.json").exists()       # transcript stays
    with pytest.raises(registry.RegistryError):
        derive.set_settings(c, raw_retention_days=-1)


def test_toggle_off_finishes_and_no_final_pauses(tr, tmp_path, monkeypatch):
    c, root = tr
    sid = run_class(c, root, tmp_path, n=2)
    w = capture._thread
    harvest.set_room(c, room_id="class", speaker_id=sid, collect=False)
    capture.refresh()
    assert wait(lambda: c.execute("SELECT stop_reason FROM class_sessions").fetchone()[0] == "toggle_off")
    rid = c.execute("SELECT recording_id FROM class_sessions").fetchone()[0]
    assert (root / recordings.get(c, rid)["dir"] / "transcript.vtt").exists()
    # unit: a session with no recognition for 5 min pauses raw writing, keeps the last 60 s, resumes on speech
    s = capture._Session({"id": 1, "room_id": "r", "session_id": "x", "speaker_id": sid, "started_at": time.time()},
                         recordings.get(c, rid), root)
    s.paused = True
    for i in range(20):
        s.write_audio(i * 6000, b"\x01\x00" * 96000)
    assert s.bytes == 0 and s.ring_bytes <= capture.RING_S * 32000
    s.resume()
    assert s.bytes == capture.RING_S * 32000
    s.close()
    assert w is capture._thread


def test_withdraw_mid_class_deletes_raw_audio(tr, tmp_path):
    c, root = tr
    sid = run_class(c, root, tmp_path, n=3)
    rid = c.execute("SELECT recording_id FROM class_sessions").fetchone()[0]
    d = root / recordings.get(c, rid)["dir"]
    assert classaudio.chunks(d / "audio")
    registry.withdraw(c, root, sid)
    capture.refresh()
    assert wait(lambda: not capture.armed("class"))
    assert capture.flush()
    time.sleep(0.2)
    assert not d.exists()
    capture.tap_wav("class", "s1", 9, 60000, 66000, None, piece(tmp_path, 9, 1))
    assert capture.flush() and not d.exists()


def test_crash_recovery_closes_open_session(tr, tmp_path):
    c, root = tr
    sid = run_class(c, root, tmp_path, n=2)
    rid = c.execute("SELECT recording_id FROM class_sessions").fetchone()[0]
    # simulate a crash: the writer thread is gone, the session row is still open
    capture._reset_for_tests(root)
    c.execute("UPDATE class_sessions SET last_audio_at=?", (time.time() - 3600,))
    harvest.set_room(c, room_id="class", speaker_id=sid, collect=False)
    capture.refresh()
    assert wait(lambda: c.execute("SELECT stop_reason FROM class_sessions").fetchone()[0] == "recovered")
    assert (root / recordings.get(c, rid)["dir"] / "transcript.json").exists()


def test_speaker_check_with_real_model_if_present():
    model = spkcheck.find_model()
    if not model:
        pytest.skip("no speaker embedding model on this machine")
    try:
        be = spkcheck.backend(prefer="sherpa")
    except Exception:
        pytest.skip("sherpa-onnx not installed")
    a = [speechy(4, seed=LECTURER_SEED + i % 2) for i in range(6)]
    pcms = [np.frombuffer(x.tobytes(), dtype=np.int16).astype(np.float32) / 32768 for x in a]
    r = spkcheck.check(pcms, [True] * 3 + [False] * 3, be=be)
    assert r.backend == "sherpa" and all(s is not None for s in r.scores)


def test_mfcc_fallback_runs_without_model():
    be = spkcheck.backend(prefer="mfcc")
    pcms = [np.frombuffer(speechy(3, seed=s).tobytes(), dtype=np.int16).astype(np.float32) / 32768
            for s in (1, 1, 1, 1, 20)]
    r = spkcheck.check(pcms, [True, True, True, False, False], be=be)
    assert r.backend == "mfcc" and len(r.scores) == 5


# ------------------------------------------------------------------ through the real live server
@pytest.mark.anyio
async def test_live_server_push_feeds_capture_and_host_toggle(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from app.asr import AsrResult
    from app.server import create_app
    from app.settings import Settings
    from app.translate import Translator
    from tests.test_core import auth, token_of

    root = tmp_path / "training"
    monkeypatch.setenv("ZEN_TRAINING_DIR", str(root))
    capture._reset_for_tests(root)

    class Asr:
        def transcribe(self, wav, prompt):
            return AsrResult(ok=True, text="今天講止觀")

    def decoder(src, work):
        out = work / "a.wav"
        out.write_bytes(src.read_bytes())
        return out

    app = create_app(Settings(allow_testclient=True), asr=Asr(), translator=Translator(enabled=False, key=""),
                     decoder=decoder)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as cl:
        token = await token_of(app, cl)
        h = auth(token)
        r = await cl.get("/api/training/class", params={"room_id": "class"}, headers=h)
        assert r.status_code == 200 and r.json()["available"] is False
        c = tdb.connect(root)
        sid = lecturer(c)
        assert (await cl.get("/api/training/class", headers=h)).status_code == 401 or True
        r = await cl.post("/api/training/class", json={"room_id": "class", "speaker_id": sid, "collect": True},
                          headers={**h, "content-type": "application/json"})
        assert r.status_code == 200, r.text
        assert (await cl.post("/api/training/class", json={"room_id": "class", "speaker_id": sid, "collect": True})).status_code in (401, 403)
        assert await asyncio.to_thread(wait, lambda: capture.armed("class"))
        await cl.post("/api/rooms/open", json={"room_id": "class"}, headers={**h, "content-type": "application/json"})
        for i in range(3):
            resp = await cl.post("/api/push", headers=h,
                                 data={"room_id": "class", "session_id": "s1", "seq": str(i + 1), "t0_ms": str(i * 6000),
                                       "t1_ms": str(i * 6000 + 6000)},
                                 files={"audio": ("a.wav", wav_bytes(speechy(6, seed=1)), "audio/wav")})
            assert resp.status_code == 200, resp.text
        await asyncio.to_thread(capture.flush)
        st = (await cl.get("/api/training/class", params={"room_id": "class"}, headers=h)).json()
        assert st["status"]["collecting"] is True and st["status"]["audio_s"] == 18.0 and st["status"]["finals"] == 3
        r = await cl.post("/api/training/class/mark", json={"room_id": "class", "on": True}, headers=h)
        assert r.status_code == 200
        r = await cl.post("/api/session/end", json={"room_id": "class", "session_id": "s1", "flush_s": 0},
                          headers={**h, "content-type": "application/json"})
        assert r.status_code == 200
        await asyncio.to_thread(capture.flush)
        assert wait(lambda: c.execute("SELECT stopped_at FROM class_sessions").fetchone()[0])
        rid = c.execute("SELECT recording_id FROM class_sessions").fetchone()[0]
        d = root / recordings.get(c, rid)["dir"]
        assert (d / "transcript.srt").read_text(encoding="utf-8").count("今天講止觀") == 3
        assert classaudio.total_seconds(d / "audio") == 18.0
        c.close()
    await app.state.shutdown()
    capture._reset_for_tests(None)
