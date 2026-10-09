"""Recognition RTF on /api/metrics: real slice duration, bounded memory, same host auth."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.asr import AsrResult, ResidentAsr
from app.audio import riff_duration_seconds
from app.rtf import SESSION_LIMIT, SESSION_ROOM_CAP, RtfMeter, percentile
from app.server import create_app
from app.settings import Settings
from app.translate import Translator

EXISTING = {
    "pending",
    "inflight",
    "oldest_wait_ms",
    "last_process_ms",
    "rejected",
    "missing",
    "held",
    "results",
    "translate_queued",
    "translate_skipped",
    "listeners",
    "rooms",
    "rss_bytes",
    "tokens_used",
    "price",
    "store_errors",
    "storage_recovered",
}


def wave_bytes(seconds: float = 1.0, extra: bytes = b"") -> bytes:
    """16 kHz mono PCM. extra is a LIST chunk so a size guess is not the real duration."""
    rate = 16000
    frames = int(round(seconds * rate))
    pcm = b"\x00\x00" * frames
    fmt = b"fmt " + struct.pack("<I", 16) + struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    chunks = fmt
    if extra:
        pad = b"\x00" if len(extra) % 2 else b""
        chunks += b"LIST" + struct.pack("<I", len(extra)) + extra + pad
    chunks += b"data" + struct.pack("<I", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def copy_decoder(src: Path, work: Path) -> Path:
    wav = work / "audio.wav"
    wav.write_bytes(src.read_bytes())
    return wav


class TimedAsr:
    def __init__(self, delay: float):
        self.delay = delay
        self.calls = 0

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        del wav, prompt
        time.sleep(self.delay)
        self.calls += 1
        return AsrResult(ok=True, text="中文")


class GateAsr:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        del wav, prompt
        self.calls += 1
        self.started.set()
        assert self.release.wait(3), "RTF gate was not released"
        return AsrResult(ok=True, text="中文")


def app_for(asr, **over):
    fields = dict(allow_testclient=True, max_audio_bytes=2_000_000)
    fields.update(over)
    return create_app(
        Settings(**fields),
        asr=asr,
        translator=Translator(enabled=True, key=""),
        decoder=copy_decoder,
    )


def auth(token: str) -> dict:
    return {"authorization": f"Bearer {token}", "origin": "http://127.0.0.1"}


async def token_of(app, client) -> str:
    resp = await client.get("/api/host-token")
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


async def push(client, token, seq, payload, session="s", room="class"):
    return await client.post(
        "/api/push",
        params={"room_id": room, "session_id": session, "seq": str(seq)},
        data={
            "room_id": room,
            "session_id": session,
            "seq": str(seq),
            "wait_translation": "0",
        },
        files={"audio": ("a.wav", payload, "audio/wav")},
        headers={**auth(token), "x-breeze-async-translation": "1"},
    )


def _assert_active_age_within_elapsed(snap, t0, room=None):
    """In-flight age cannot outrun this test's own perf_counter.

    note_asr_active and _active_age both read time.perf_counter (app/rtf.py).
    t0 is taken before that stamp and t_after after this snapshot, so a correct
    age is at most (t_after - t0) aside from 6-decimal rounding. A floor well
    above the real elapsed fails the bound.
    """
    t_after = time.perf_counter()
    assert snap["asr_active_s"] <= (t_after - t0) + 0.02
    if room is not None:
        assert snap["asr_active_by_room"][room] <= (t_after - t0) + 0.02


def test_recognition_defaults_stay_put():
    fresh = Settings()
    from_env = Settings.from_env({})
    assert fresh.asr_mode == "cli" and from_env.asr_mode == "cli"
    assert fresh.silence_rms == 0.0 and from_env.silence_rms == 0.0
    assert fresh.asr_threads == 6 and from_env.asr_threads == 6
    assert fresh.asr_workers == 1 and from_env.asr_workers == 1
    assert fresh.model_path == "" and from_env.model_path == ""


def test_rtf_meter_percentiles_window_and_session_cap():
    assert SESSION_LIMIT >= 1000
    meter = RtfMeter()
    meter.record_ms(50, 0, ("room", "class"))
    assert meter.snapshot()["rtf"]["session"]["count"] == 0
    meter.note_waiting(("room", "class", 1), 1.25)
    meter.note_waiting(("room", "class", 2), 2.5)
    meter.note_decoded(("room", "class", 1), 1.25)
    meter.note_decoded(("room", "class", 2), 2.5)
    waiting = meter.snapshot()
    assert waiting["backlog_audio_s"] == 3.75
    assert waiting["backlog_s"] == 3.75
    assert waiting["backlog_estimated"] is False
    assert waiting["asr_active_s"] == 0
    meter.clear_waiting(("room", "class", 1))
    # Recognition starting drops only the not-yet-started queue. Decoded audio stays.
    started = meter.snapshot()
    assert started["backlog_s"] == 2.5
    assert started["backlog_audio_s"] == 3.75
    meter.clear_decoded(("room", "class", 1))
    assert meter.snapshot()["backlog_audio_s"] == 2.5
    assert meter.snapshot()["backlog_s"] == 2.5
    meter.clear_waiting(("room", "class", 2))
    meter.clear_decoded(("room", "class", 2))
    idle = meter.snapshot()
    assert idle["backlog_audio_s"] == 0
    assert idle["backlog_s"] == 0
    assert isinstance(idle["backlog_audio_s"], int)
    assert isinstance(idle["asr_active_s"], int)

    for value in (100, 200, 300, 400, 500):
        meter.record_ms(value, 1000, ("room", "class"))
    session = meter.snapshot()["rtf"]["session"]
    assert session["count"] == 5
    assert session["limit"] == SESSION_LIMIT
    assert session["asr_ms"] == {"p50": 300, "p95": 480, "max": 500}
    assert session["audio_ms"] == {"p50": 1000, "p95": 1000, "max": 1000}
    assert session["rtf"]["p50"] == pytest.approx(0.3)
    assert session["rtf"]["p95"] == pytest.approx(0.48)
    assert session["rtf"]["max"] == pytest.approx(0.5)
    assert session["asr_ms"]["p50"] == pytest.approx(percentile([100, 200, 300, 400, 500], 0.50))
    assert session["asr_ms"]["p95"] == pytest.approx(percentile([100, 200, 300, 400, 500], 0.95))

    meter.record_ms(900, 1000, ("room", "next"))
    reset = meter.snapshot()["rtf"]
    assert reset["session"]["count"] == 1
    assert reset["session"]["asr_ms"]["max"] == 900
    assert reset["window"]["count"] == 6
    assert reset["window"]["limit"] == 200

    full = RtfMeter()
    for index in range(1, 1001):
        full.record_ms(index, 6000, ("room", "class"))
    held = full.snapshot()["rtf"]
    assert held["session"]["count"] == 1000
    assert held["window"]["count"] == 200
    assert held["window"]["asr_ms"]["max"] == 1000
    # Last 200 of 1..1000 are 801..1000.
    assert held["window"]["asr_ms"]["p50"] == pytest.approx(percentile(list(range(801, 1001)), 0.50))

    capped = RtfMeter()
    for index in range(1, SESSION_LIMIT + 6):
        capped.record_ms(index, 1000, ("room", "long"))
    top = capped.snapshot()["rtf"]
    assert top["session"]["count"] == SESSION_LIMIT
    assert top["session"]["asr_ms"]["max"] == SESSION_LIMIT + 5
    assert top["window"]["count"] == 200
    assert top["window"]["asr_ms"]["max"] == SESSION_LIMIT + 5


def test_host_metrics_line_keeps_existing_labels_and_adds_rtf():
    text = Path("app/static/host.html").read_text(encoding="utf-8")
    line = text.split("metrics.textContent = ", 1)[1].split(";", 1)[0]
    for label in ("待處理 ", "最久等待 ", "上次辨識 ", "拒絕 ", "缺段 ", "聽眾 ", "記憶體 "):
        assert label in line
    assert "辨識即時率" not in line
    assert "辨識速度" not in line
    assert 'data.last_process_ms ?? "—"' in line
    assert 'data.process_ms ?? "—"' in line
    assert 'data.asr_active_s ?? "—"' in line
    assert "純辨識 " in line
    assert "正在辨識 " in line
    phase = text.index('id="phase"')
    speed = text.index('id="rtf-speed"')
    captions = text.index('id="caption-en"')
    assert phase < speed < captions
    assert "辨識速度：開始聽之後才有數字" in text
    assert "最近 200 段" in text
    assert "（低於 0.9 才跟得上）" in text
    assert "狀態：" in text
    assert "｜辨識速度：" in text
    assert "（數字暫停更新）" in text
    assert "setInterval(refreshMetrics, 2000)" in text
    stale = re.search(r"#rtf-speed\.speed-stale\s*\{([^}]+)\}", text)
    assert stale, "stale metrics need a visible indicator that does not replace the status colour"
    assert "outline" in stale.group(1)
    assert re.search(r"(?:^|;)\s*color\s*:", stale.group(1)) is None
    for word in ("跟得上", "接近上限", "跟不上，字幕會延遲"):
        assert word in text


@pytest.mark.anyio
async def test_metrics_rtf_uses_wave_duration_and_known_asr_speed(tmp_path):
    blob = wave_bytes(1.0, extra=b"INFO" + b"\x00" * 80)
    wav = tmp_path / "slice.wav"
    wav.write_bytes(blob)
    real = riff_duration_seconds(wav)
    assert real == pytest.approx(1.0)
    naive_ms = int(round(max(0, len(blob) - 44) / 32000 * 1000))
    assert naive_ms != 1000

    asr = TimedAsr(0.2)
    app = app_for(asr)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            before = (await client.get("/api/metrics", headers=auth(token))).json()
            assert EXISTING <= set(before)
            assert before["last_process_ms"] is None
            assert before["process_ms"] is None
            assert before["asr_active_s"] == 0
            assert before["pending"] == 0 and before["rejected"] == 0 and before["missing"] == 0
            assert before["rtf"]["window"]["count"] == 0
            assert before["rtf"]["session"]["count"] == 0
            assert before["backlog_audio_s"] == 0
            assert before["backlog_s"] == 0
            assert before["asr_timeouts"] == 0
            assert before["silent_skipped"] == 0
            assert before["asr_empty"] == 0
            assert before["asr_rtf_p95"] is None

            resp = await push(client, token, 1, blob)
            assert resp.status_code == 200, resp.text
            assert resp.json()["zh"] == "中文"
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert EXISTING <= set(body)
            assert body["pending"] == 0
            assert body["rejected"] == 0
            assert body["missing"] == 0
            assert body["backlog_audio_s"] == 0
            assert body["backlog_s"] == 0
            assert isinstance(body["last_process_ms"], int)
            session = body["rtf"]["session"]
            window = body["rtf"]["window"]
            assert session["count"] == 1 and window["count"] == 1
            assert window["limit"] == 200
            assert session["audio_ms"] == {"p50": 1000, "p95": 1000, "max": 1000}
            assert session["audio_ms"]["p50"] != naive_ms
            asr_ms = session["asr_ms"]["p50"]
            assert session["asr_ms"]["p95"] == asr_ms
            assert session["asr_ms"]["max"] == asr_ms
            assert 100 <= asr_ms <= 5000
            assert session["rtf"]["p50"] == pytest.approx(asr_ms / 1000)
            assert session["rtf"]["p95"] == pytest.approx(asr_ms / 1000)
            assert session["rtf"]["max"] == pytest.approx(asr_ms / 1000)
            assert window["rtf"]["p95"] == session["rtf"]["p95"]
            assert body["asr_rtf_p50"] == window["rtf"]["p50"]
            assert body["asr_rtf_p95"] == window["rtf"]["p95"]
            assert body["asr_samples"] == 1
            assert session["decode_ms"]["p50"] == window["decode_ms"]["p50"]
            assert session["asr_wait_ms"]["p95"] == window["asr_wait_ms"]["p95"]
            assert body["asr_timeouts"] == 0
            assert body["silent_skipped"] == 0
            assert body["asr_empty"] == 0
            # last_process_ms covers the whole slice, including the queue.
            # process_ms is decode plus recognition and is at least the RTF numerator.
            assert isinstance(body["process_ms"], int)
            assert body["process_ms"] + 50 >= asr_ms
            assert body["last_process_ms"] + 50 >= body["process_ms"]
            assert body["last_process_ms"] + 50 >= asr_ms
            assert body["asr_active_s"] == 0
            assert asr.calls == 1
    finally:
        await app.state.shutdown()


@pytest.mark.anyio
async def test_metrics_backlog_is_decoded_audio_still_waiting():
    asr = GateAsr()
    app = app_for(asr, asr_workers=1)
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = asyncio.create_task(push(client, token, 1, blob))
            assert await asyncio.to_thread(asr.started.wait, 2)
            second = asyncio.create_task(push(client, token, 2, blob))
            seen = None
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                seen = (await client.get("/api/metrics", headers=auth(token))).json()
                # One slice is inside ASR and one decoded slice is still waiting for the slot.
                if (
                    asr.calls == 1
                    and seen["pending"] == 2
                    and seen["backlog_audio_s"] >= 1.5
                    and 0.5 <= seen["backlog_s"] <= 1.01
                ):
                    break
                await asyncio.sleep(0.02)
            assert seen is not None
            assert asr.calls == 1
            assert seen["backlog_audio_s"] == pytest.approx(2.0, abs=0.001)
            assert seen["backlog_s"] == pytest.approx(1.0, abs=0.001)
            assert seen["backlog_estimated"] is False
            assert seen["backlog_by_room"] == {"class": pytest.approx(1.0, abs=0.001)}
            assert seen["asr_active_s"] > 0
            assert seen["pending"] == 2
            assert seen["rejected"] == 0
            assert seen["missing"] == 0
            assert isinstance(seen["oldest_wait_ms"], int) and seen["oldest_wait_ms"] >= 0
            asr.release.set()
            done = await asyncio.wait_for(asyncio.gather(first, second), 3)
            assert [item.status_code for item in done] == [200, 200]
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            assert after["backlog_audio_s"] == 0
            assert after["backlog_s"] == 0
            assert after["backlog_estimated"] is False
            assert after["pending"] == 0
            assert after["rtf"]["session"]["count"] == 2
            assert after["rtf"]["session"]["audio_ms"]["max"] == 1000
            assert asr.calls == 2
    finally:
        asr.release.set()
        await app.state.shutdown()


@pytest.mark.anyio
async def test_metrics_window_is_bounded_and_non_host_is_rejected():
    app = app_for(TimedAsr(0))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            meter = app.state.pipeline._rtf
            for index in range(1, 251):
                meter.record_ms(index, 1000, ("class", "s"))
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert EXISTING <= set(body)
            assert body["rtf"]["window"]["count"] == 200
            assert body["rtf"]["window"]["limit"] == 200
            assert body["rtf"]["window"]["asr_ms"]["max"] == 250
            assert body["rtf"]["session"]["count"] == 250
            assert body["rtf"]["session"]["limit"] >= 1000
            assert body["last_process_ms"] is None
            assert body["pending"] == 0 and body["rejected"] == 0 and body["missing"] == 0

            missing = await client.get("/api/metrics")
            assert missing.status_code == 401
            bad = await client.get("/api/metrics", headers={"authorization": "Bearer nope"})
            assert bad.status_code == 401
            evil = await client.get(
                "/api/metrics",
                headers={**auth(token), "origin": "http://evil.example"},
            )
            assert evil.status_code == 403
            assert token not in missing.text and token not in bad.text and token not in evil.text
    finally:
        await app.state.shutdown()


@pytest.mark.anyio
async def test_new_session_resets_rtf_and_non_wave_is_not_guessed():
    asr = TimedAsr(0)
    app = app_for(asr)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            blob = wave_bytes(1.0)
            first = await push(client, token, 1, blob, session="one")
            second = await push(client, token, 1, blob, session="two")
            assert first.status_code == 200 and second.status_code == 200
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert body["rtf"]["session"]["count"] == 1
            assert body["rtf"]["window"]["count"] == 2

            noise = await push(client, token, 2, b"x" * 200, session="two")
            assert noise.status_code == 200, noise.text
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            # 200 raw bytes are not a WAVE file. A size guess would be a few milliseconds.
            assert after["rtf"]["session"]["count"] == 1
            assert after["rtf"]["window"]["count"] == 2
            assert asr.calls == 3
    finally:
        await app.state.shutdown()


@pytest.mark.anyio
async def test_silence_skip_does_not_record_rtf():
    asr = TimedAsr(0)
    app = app_for(asr, silence_rms=1_000_000)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            resp = await push(client, token, 1, wave_bytes(1.0))
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "silent"
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert body["rtf"]["session"]["count"] == 0
            assert body["rtf"]["window"]["count"] == 0
            assert body["backlog_audio_s"] == 0
            assert body["backlog_s"] == 0
            assert body["asr_timeouts"] == 0
            assert body["asr_errors"] == 0
            assert body["silent_skipped"] == 1
            assert body["asr_empty"] == 0
            assert body["last_process_ms"] is None
            assert body["process_ms"] is None
            assert asr.calls == 0
    finally:
        await app.state.shutdown()


def _rows(snapshot: dict) -> dict[tuple[str, str], dict]:
    return {(row["room_id"], row["session_id"]): row for row in snapshot["rtf"]["sessions"]}


def test_timeout_does_not_enter_rtf_percentiles():
    meter = RtfMeter()
    meter.record_ms(200, 1000, ("room", "class"))
    meter.note_timeout(("room", "class"))
    meter.note_timeout(("room", "class"))
    snap = meter.snapshot()
    assert snap["asr_timeouts"] == 2
    assert snap["rtf"]["session"]["count"] == 1
    assert snap["rtf"]["window"]["count"] == 1
    assert snap["rtf"]["session"]["asr_ms"]["max"] == 200
    assert snap["rtf"]["session"]["rtf"]["p95"] == pytest.approx(0.2)
    assert _rows(snap)[("room", "class")]["asr_timeouts"] == 2
    # A new session drops that room's samples only. The timeout total stays.
    meter.note_timeout(("room", "next"))
    after = meter.snapshot()
    assert after["asr_timeouts"] == 3
    assert after["rtf"]["session"]["count"] == 0
    assert after["rtf"]["session"]["rtf"]["p95"] is None
    assert after["rtf"]["window"]["count"] == 1
    meter.drop_room("room")
    dropped = meter.snapshot()
    assert dropped["asr_timeouts"] == 3
    assert dropped["rtf"]["sessions"] == []
    assert dropped["rtf"]["window"]["count"] == 1


def test_alternating_rooms_do_not_wipe_session_rtf():
    meter = RtfMeter()
    for _ in range(40):
        meter.record_ms(100, 1000, ("east", "live"))
        meter.record_ms(900, 1000, ("west", "live"))
    rows = _rows(meter.snapshot())
    assert rows[("east", "live")]["count"] == 40
    assert rows[("west", "live")]["count"] == 40
    assert rows[("east", "live")]["rtf"]["p95"] == pytest.approx(0.1)
    assert rows[("west", "live")]["rtf"]["p95"] == pytest.approx(0.9)
    assert rows[("east", "live")]["asr_ms"]["max"] == 100
    assert rows[("west", "live")]["asr_ms"]["max"] == 900
    # Default snapshot follows the latest update, which still has every east sample.
    assert meter.snapshot()["rtf"]["session"]["count"] == 40
    assert meter.snapshot(("west", "live"))["rtf"]["session"]["count"] == 40
    meter.record_ms(50, 1000, ("east", "next"))
    rows = _rows(meter.snapshot())
    assert ("east", "live") not in rows
    assert rows[("east", "next")]["count"] == 1
    assert rows[("east", "next")]["asr_ms"]["max"] == 50
    assert rows[("west", "live")]["count"] == 40
    assert rows[("west", "live")]["rtf"]["p95"] == pytest.approx(0.9)
    assert meter.snapshot()["rtf"]["window"]["count"] == 81


def test_session_buckets_stay_bounded():
    meter = RtfMeter()
    total = SESSION_ROOM_CAP + 5
    for index in range(total):
        meter.record_ms(index + 1, 1000, (f"room{index}", "s"))
    rows = meter.snapshot()["rtf"]["sessions"]
    assert len(rows) == SESSION_ROOM_CAP
    ids = {row["room_id"] for row in rows}
    assert "room0" not in ids
    assert f"room{total - 1}" in ids
    assert meter.snapshot()["rtf"]["window"]["count"] == total
    assert meter.snapshot()["rtf"]["session"]["count"] == 1


def test_stop_path_does_not_block_on_the_rtf_meter():
    text = Path("app/pipeline.py").read_text(encoding="utf-8")
    end = text.split("async def end_session", 1)[1].split("\n    def fail_received", 1)[0]
    assert "_rtf" not in end
    before = text.split('self.fail_received(segment, "辨識逾時", status="timeout")', 1)[0]
    handler = before.rsplit("except asyncio.TimeoutError:", 1)[1]
    assert "note_timeout" in handler
    assert "record_s" not in handler
    finally_body = text.split("self._rtf.clear_waiting(segment.key)", 1)[1].split("self.last_process_s", 1)[0]
    assert "snapshot" not in finally_body
    assert "sleep" not in finally_body
    assert ".record(" in finally_body
    meter = Path("app/rtf.py").read_text(encoding="utf-8")
    for banned in ("time.sleep", "asyncio.", "subprocess", "threading", "Lock(", "socket.", "open("):
        assert banned not in meter, banned


def _contrast(foreground: str, background: str) -> float:
    def channel(value: int) -> float:
        scaled = value / 255
        if scaled <= 0.04045:
            return scaled / 12.92
        return ((scaled + 0.055) / 1.055) ** 2.4

    def luminance(color: str) -> float:
        color = color.lstrip("#")
        red, green, blue = (int(color[index:index + 2], 16) for index in (0, 2, 4))
        return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)

    lighter = max(luminance(foreground), luminance(background))
    darker = min(luminance(foreground), luminance(background))
    return (lighter + 0.05) / (darker + 0.05)


def test_host_speed_line_contrast_and_status_words():
    text = Path("app/static/host.html").read_text(encoding="utf-8")
    for selector in ("#rtf-speed.speed-empty", "#rtf-speed.speed-ok", "#rtf-speed.speed-warn", "#rtf-speed.speed-bad"):
        block = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", text)
        assert block, selector
        body = block.group(1)
        colors = {}
        for name in ("color", "background"):
            found = re.search(rf"(?:^|;)\s*{name}\s*:\s*(#[0-9a-fA-F]{{6}})", body)
            assert found, (selector, name, body)
            colors[name] = found.group(1)
        ratio = _contrast(colors["color"], colors["background"])
        assert ratio >= 4.5, (selector, colors, ratio)
    view = text.split("function recognitionSpeedView", 1)[1].split("function paintRecognitionSpeed", 1)[0]
    for word in ("跟得上", "接近上限", "跟不上，字幕會延遲"):
        assert word in view
    assert 'p95 >= 0.9' in view and 'p95 >= 0.7' in view


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}"
    start = source.index(marker)
    open_brace = source.index("{", start)
    depth = 0
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(name)


def _node_bin() -> str:
    found = shutil.which("node")
    if not found:
        pytest.skip("node is not installed")
    return found


def test_host_speed_line_wording_for_each_band():
    node = _node_bin()
    source = Path("app/static/host.html").read_text(encoding="utf-8")
    function = _extract_function(source, "recognitionSpeedView")
    session_key = _extract_function(source, "sessionKey")
    failure = _extract_function(source, "noteMetricsFailure")
    listening = {"roomId": "class", "sessionId": "live"}
    empty = "辨識速度：開始聽之後才有數字"

    def sample(p50, p95, count=5, limit=200, session="live"):
        return {
            "rtf": {
                "window": {"count": 200, "limit": limit, "rtf": {"p50": 9.9, "p95": 9.9}},
                "sessions": [
                    {
                        "room_id": "class",
                        "session_id": "previous",
                        "count": 80,
                        "recent": {"count": 80, "limit": 200, "rtf": {"p50": 0.99, "p95": 1.2}},
                    },
                    {
                        "room_id": "class",
                        "session_id": session,
                        "count": max(count, 1),
                        "limit": 4096,
                        "recent": {"count": count, "limit": limit, "rtf": {"p50": p50, "p95": p95}},
                    },
                ],
            }
        }

    def line(p50, p95, word, count=5):
        return f"{word}｜辨識速度：一般 {p50:.2f}、最慢 {p95:.2f}（低於 0.9 才跟得上）。最近 {count} 段"

    cases = [
        {"data": None, "listening": listening, "text": empty, "tone": "speed-empty"},
        {"data": {}, "listening": listening, "text": empty, "tone": "speed-empty"},
        {"data": sample(None, None, count=0), "listening": listening, "text": empty, "tone": "speed-empty", "absent": ["跟得上"]},
        {"data": sample(0, 0, count=0), "listening": listening, "text": empty, "tone": "speed-empty", "absent": ["跟得上", "0.00"]},
        {
            "data": {"rtf": {"sessions": [{"room_id": "class", "session_id": "live", "count": 0, "rtf": {"p50": 0, "p95": 0}}]}},
            "listening": listening,
            "text": empty,
            "tone": "speed-empty",
            "absent": ["跟得上"],
        },
        {"data": sample(0.42, 0.50), "listening": None, "text": empty, "tone": "speed-empty", "absent": ["0.42", "0.99"]},
        {"data": sample(0.42, 0.50), "listening": {"roomId": "class", "sessionId": "brand-new"}, "text": empty, "tone": "speed-empty", "absent": ["0.42", "0.99", "1.20"]},
        {"data": sample(0.42, 0.50), "listening": listening, "text": line(0.42, 0.50, "跟得上"), "tone": "speed-ok", "absent": ["接近上限", "跟不上", "最近 200"]},
        {"data": sample(0.69, 0.69), "listening": listening, "text": line(0.69, 0.69, "跟得上"), "tone": "speed-ok", "absent": ["接近上限", "跟不上"]},
        {"data": sample(0.70, 0.70), "listening": listening, "text": line(0.70, 0.70, "接近上限"), "tone": "speed-warn", "absent": ["跟不上"]},
        {"data": sample(0.80, 0.89), "listening": listening, "text": line(0.80, 0.89, "接近上限"), "tone": "speed-warn", "absent": ["跟不上"]},
        {"data": sample(0.90, 0.90), "listening": listening, "text": line(0.90, 0.90, "跟不上，字幕會延遲"), "tone": "speed-bad"},
        {"data": sample(1.20, 1.40), "listening": listening, "text": line(1.20, 1.40, "跟不上，字幕會延遲"), "tone": "speed-bad"},
        {"data": sample(0.40, 0.55, count=200), "listening": listening, "text": line(0.40, 0.55, "跟得上", 200), "tone": "speed-ok"},
    ]
    script = function + session_key + failure + """
const cases = JSON.parse(process.argv[1]);
let failed = 0;
for (const item of cases) {
  const view = recognitionSpeedView(item.data, item.listening);
  const problems = [];
  if (view.text !== item.text) problems.push("text " + JSON.stringify(view.text));
  if (view.tone !== item.tone) problems.push("tone " + view.tone);
  if (!String(view.title).includes("最近 200 段")) problems.push("title");
  if (!String(view.title).includes("上一場")) problems.push("title session");
  for (const word of item.absent || []) {
    if (view.text.includes(word)) problems.push("unexpected " + word);
  }
  if (problems.length) {
    console.log(JSON.stringify(item.text), problems.join("; "));
    failed++;
  }
}
var ctl = null;
const speed = {
  textContent: "跟得上｜辨識速度：一般 0.42、最慢 0.50（低於 0.9 才跟得上）。最近 5 段",
  classList: { added: [], add(name) { if (!this.added.includes(name)) this.added.push(name); } },
};
noteMetricsFailure(speed);
if (!speed.classList.added.includes("speed-stale")) {
  console.log("missing stale class");
  failed++;
}
if (!speed.textContent.includes("跟得上")) {
  console.log("status word dropped");
  failed++;
}
if (!speed.textContent.includes("（數字暫停更新）")) {
  console.log("missing stale mark");
  failed++;
}
noteMetricsFailure(speed);
const marks = speed.textContent.split("（數字暫停更新）").length - 1;
if (marks !== 1) {
  console.log("stale mark repeated " + marks);
  failed++;
}
ctl = { session: { roomId: "class", id: "aaa" } };
const kept = {
  textContent: "跟得上｜辨識速度：一般 0.42、最慢 0.50（低於 0.9 才跟得上）。最近 5 段",
  title: "old",
  metricsSession: "class\\naaa",
  classList: {
    added: ["speed-ok"],
    add(name) { if (!this.added.includes(name)) this.added.push(name); },
    remove(...names) { this.added = this.added.filter((name) => !names.includes(name)); },
  },
};
noteMetricsFailure(kept);
if (!kept.textContent.startsWith("跟得上")) {
  console.log("same session dropped the speed line " + kept.textContent);
  failed++;
}
if (!kept.textContent.includes("（數字暫停更新）")) {
  console.log("same session missing stale mark");
  failed++;
}
if ((kept.textContent.split("（數字暫停更新）").length - 1) !== 1) {
  console.log("same session repeated the mark");
  failed++;
}
ctl.session = { roomId: "class", id: "bbb" };
noteMetricsFailure(kept);
const reset = "辨識速度：開始聽之後才有數字（數字暫停更新）";
if (kept.textContent !== reset) {
  console.log("session change kept old text " + kept.textContent);
  failed++;
}
if (kept.textContent.includes("跟得上") || kept.textContent.includes("0.42")) {
  console.log("session change kept the previous speed");
  failed++;
}
if (kept.metricsSession !== "class\\nbbb") {
  console.log("metrics session not reset " + kept.metricsSession);
  failed++;
}
if (!kept.classList.added.includes("speed-empty") || !kept.classList.added.includes("speed-stale")) {
  console.log("session change classes " + kept.classList.added.join(","));
  failed++;
}
if (kept.classList.added.includes("speed-ok")) {
  console.log("old tone survived the session change");
  failed++;
}
if (!String(kept.title).includes("最近 200 段")) {
  console.log("title dropped");
  failed++;
}
if (failed) process.exit(1);
"""
    proc = subprocess.run(
        [node, "-e", script, json.dumps(cases)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


class TimeoutAsr:
    def __init__(self):
        self.calls = 0

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        del wav, prompt
        self.calls += 1
        time.sleep(0.25)
        return AsrResult(ok=True, text="太慢")


@pytest.mark.anyio
async def test_asr_timeout_is_not_an_rtf_sample():
    asr = TimeoutAsr()
    app = app_for(asr, asr_timeout_s=0.05, asr_workers=1)
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            timed = await push(client, token, 1, blob)
            assert timed.status_code == 408, timed.text
            assert timed.json()["status"] == "timeout"
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert EXISTING <= set(body)
            assert body["asr_timeouts"] == 1
            assert body["asr_errors"] == 0
            assert body["silent_skipped"] == 0
            assert body["asr_empty"] == 0
            assert body["last_process_ms"] is None
            assert body["process_ms"] is None
            assert body["asr_active_s"] == 0
            assert body["rtf"]["session"]["count"] == 0
            assert body["rtf"]["window"]["count"] == 0
            assert body["rtf"]["session"]["rtf"]["p95"] is None
            assert body["rtf"]["window"]["rtf"]["p95"] is None
            assert body["backlog_audio_s"] == 0
            assert body["rejected"] == 0
            asr.transcribe = lambda wav, prompt: AsrResult(ok=True, text="中文")
            done = await push(client, token, 2, blob)
            assert done.status_code == 200, done.text
            assert done.json()["zh"] == "中文"
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            assert after["asr_timeouts"] == 1
            assert after["silent_skipped"] == 0
            assert after["asr_empty"] == 0
            assert after["rtf"]["session"]["count"] == 1
            assert after["rtf"]["window"]["count"] == 1
            assert after["rtf"]["session"]["audio_ms"]["max"] == 1000
            assert after["rtf"]["session"]["rtf"]["p95"] is not None
            assert after["rtf"]["sessions"][0]["asr_timeouts"] == 1
    finally:
        await app.state.shutdown()


@pytest.mark.anyio
async def test_alternating_rooms_keep_their_own_rtf_session():
    asr = TimedAsr(0)
    app = app_for(asr)
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            east = await push(client, token, 1, blob, session="live", room="east")
            west = await push(client, token, 1, blob, session="live", room="west")
            again = await push(client, token, 2, blob, session="live", room="east")
            assert [item.status_code for item in (east, west, again)] == [200, 200, 200]
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            rows = _rows(body)
            assert rows[("east", "live")]["count"] == 2
            assert rows[("west", "live")]["count"] == 1
            assert body["rtf"]["session"]["count"] == 2
            assert body["rtf"]["window"]["count"] == 3
            assert body["asr_timeouts"] == 0
            nxt = await push(client, token, 1, blob, session="next", room="east")
            assert nxt.status_code == 200, nxt.text
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            rows = _rows(after)
            assert ("east", "live") not in rows
            assert rows[("east", "next")]["count"] == 1
            assert rows[("west", "live")]["count"] == 1
            assert after["rtf"]["session"]["count"] == 1
            assert after["rtf"]["window"]["count"] == 4
            assert asr.calls == 4
    finally:
        await app.state.shutdown()


def test_skip_counters_stay_out_of_rtf_and_do_not_share_a_tally():
    meter = RtfMeter()
    meter.note_silent_skip(("east", "a"))
    meter.note_empty(("west", "b"))
    meter.note_timeout(("west", "b"))
    snap = meter.snapshot()
    assert snap["silent_skipped"] == 1
    assert snap["asr_empty"] == 1
    assert snap["asr_timeouts"] == 1
    assert snap["rtf"]["window"]["count"] == 0
    assert snap["asr_rtf_p95"] is None
    rows = _rows(snap)
    east = rows[("east", "a")]
    assert east["count"] == 0
    assert east["silent_skipped"] == 1
    assert east["rtf"]["p95"] is None
    assert east["asr_empty"] == 0
    assert rows[("west", "b")]["count"] == 0
    assert rows[("west", "b")]["asr_timeouts"] == 1
    assert rows[("west", "b")]["asr_empty"] == 1
    assert rows[("west", "b")]["silent_skipped"] == 0
    meter.record_ms(100, 1000, ("east", "a"), decode_ms=12, wait_ms=34)
    rows = _rows(meter.snapshot())
    assert rows[("east", "a")]["silent_skipped"] == 1
    assert rows[("east", "a")]["asr_empty"] == 0
    assert rows[("east", "a")]["count"] == 1
    assert rows[("east", "a")]["decode_ms"]["p50"] == 12
    assert rows[("east", "a")]["asr_wait_ms"]["p50"] == 34
    assert rows[("west", "b")]["asr_empty"] == 1
    before = meter.snapshot()["asr_rtf_p95"]
    meter.note_timeout(("east", "a"))
    meter.note_silent_skip(("east", "a"))
    after = meter.snapshot()
    assert after["asr_timeouts"] == 2
    assert after["silent_skipped"] == 2
    assert after["asr_empty"] == 1
    assert after["asr_rtf_p95"] == before
    assert after["rtf"]["window"]["count"] == 1
    meter.drop_room("east")
    dropped = meter.snapshot()
    assert ("east", "a") not in _rows(dropped)
    assert dropped["silent_skipped"] == 2
    assert dropped["asr_timeouts"] == 2
    assert dropped["asr_empty"] == 1


def test_backlog_is_per_room_and_an_estimate_until_the_duration_is_known():
    meter = RtfMeter()
    meter.note_waiting(("east", "s", 1), 1.5)
    meter.note_waiting(("west", "s", 1), 2.5, estimated=True)
    snap = meter.snapshot()
    assert snap["backlog_audio_s"] == 0
    assert isinstance(snap["backlog_audio_s"], int)
    assert snap["backlog_s"] == 4.0
    assert snap["backlog_estimated"] is True
    assert snap["backlog_by_room"] == {"east": 1.5, "west": 2.5}
    meter.note_decoded(("east", "s", 1), 1.5)
    meter.note_decoded(("west", "s", 1), 2.5)
    decoded = meter.snapshot()
    assert decoded["backlog_audio_s"] == 4.0
    assert decoded["backlog_s"] == 4.0
    meter.clear_waiting(("east", "s", 1))
    cleared = meter.snapshot()
    assert cleared["backlog_s"] == 2.5
    assert cleared["backlog_by_room"] == {"west": 2.5}
    assert cleared["backlog_estimated"] is True
    assert cleared["backlog_audio_s"] == 4.0
    meter.clear_decoded(("east", "s", 1))
    meter.note_asr_active(("west", "s", 1), "west")
    meter.drop_room("west")
    gone = meter.snapshot()
    assert gone["backlog_audio_s"] == 0
    assert gone["backlog_s"] == 0
    assert gone["backlog_estimated"] is False
    assert gone["backlog_by_room"] == {}
    assert gone["asr_active_s"] == 0
    assert gone["asr_active_by_room"] == {}


def test_percentile_cache_recomputes_only_when_a_sample_arrives(monkeypatch):
    calls = {"n": 0}
    real = percentile

    def wrapped(values, p):
        calls["n"] += 1
        return real(values, p)

    monkeypatch.setattr("app.rtf.percentile", wrapped)
    meter = RtfMeter()
    meter.record_ms(100, 1000, ("room", "s"), decode_ms=10, wait_ms=20)
    meter.record_ms(300, 1000, ("room", "s"), decode_ms=30, wait_ms=40)
    first = meter.snapshot()
    used = calls["n"]
    assert used > 0
    assert first["asr_rtf_p50"] == first["rtf"]["window"]["rtf"]["p50"]
    assert first["decode_ms_p50"] == pytest.approx(percentile([10, 30], 0.50))
    assert first["asr_wait_ms_p95"] == pytest.approx(percentile([20, 40], 0.95))
    assert first["rtf"]["sessions"][0]["recent"]["count"] == 2
    meter.note_waiting(("room", "s", 1), 1.5, estimated=True)
    meter.note_silent_skip(("room", "s"))
    meter.note_timeout(("room", "s"))
    second = meter.snapshot()
    assert calls["n"] == used
    assert second["rtf"]["window"] == first["rtf"]["window"]
    assert second["rtf"]["window"] is not first["rtf"]["window"]
    assert second["rtf"]["session"] is not first["rtf"]["session"]
    saved_p95 = first["asr_rtf_p95"]
    saved_window = first["rtf"]["window"]["rtf"]["p95"]
    saved_recent = first["rtf"]["sessions"][0]["recent"]["rtf"]["p95"]
    second["rtf"]["window"]["rtf"]["p95"] = 999
    second["rtf"]["window"]["count"] = 0
    second["rtf"]["sessions"][0]["rtf"]["p95"] = 999
    second["rtf"]["sessions"][0]["recent"]["rtf"]["p95"] = 999
    second["rtf"]["session"]["rtf"]["p95"] = 999
    again = meter.snapshot()
    assert calls["n"] == used
    assert again["asr_rtf_p95"] == saved_p95
    assert again["rtf"]["window"]["count"] == 2
    assert again["rtf"]["window"]["rtf"]["p95"] == saved_window
    assert again["rtf"]["sessions"][0]["recent"]["rtf"]["p95"] == saved_recent
    assert again["rtf"]["session"]["rtf"]["p95"] == saved_window
    assert second["backlog_s"] == 1.5
    assert second["backlog_estimated"] is True
    assert second["silent_skipped"] == 1
    assert second["asr_timeouts"] == 1
    assert second["asr_rtf_p95"] == first["asr_rtf_p95"]
    meter.record_ms(900, 1000, ("room", "s"), decode_ms=50, wait_ms=60)
    third = meter.snapshot()
    assert calls["n"] > used
    assert third["rtf"]["window"] is not first["rtf"]["window"]
    assert third["asr_samples"] == 3
    assert third["asr_rtf_p95"] == third["rtf"]["window"]["rtf"]["p95"]
    assert third["backlog_s"] == 1.5
    assert third["silent_skipped"] == 1


class HoldAsr:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        del wav, prompt
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            assert self.release.wait(3), "first recognition was not released"
            return AsrResult(ok=True, text="先")
        return AsrResult(ok=True, text="後")


@pytest.mark.anyio
async def test_backlog_is_counted_from_upload_and_cleared_when_asr_starts():
    entered = threading.Event()
    release_decode = threading.Event()
    asr = GateAsr()

    def decoder(src: Path, work: Path) -> Path:
        entered.set()
        assert release_decode.wait(3), "decode was not released"
        return copy_decoder(src, work)

    app = create_app(
        Settings(allow_testclient=True, max_audio_bytes=2_000_000, segment_ms=6000),
        asr=asr,
        translator=Translator(enabled=True, key=""),
        decoder=decoder,
    )
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            t0 = time.perf_counter()
            task = asyncio.create_task(push(client, token, 1, blob))
            assert await asyncio.to_thread(entered.wait, 2)
            during = (await client.get("/api/metrics", headers=auth(token))).json()
            _assert_active_age_within_elapsed(during, t0)
            assert during["backlog_audio_s"] == 0
            assert during["backlog_s"] == pytest.approx(6.0)
            assert during["backlog_estimated"] is True
            assert during["backlog_by_room"].get("class") == pytest.approx(6.0)
            assert asr.calls == 0
            release_decode.set()
            assert await asyncio.to_thread(asr.started.wait, 2)
            first = (await client.get("/api/metrics", headers=auth(token))).json()
            _assert_active_age_within_elapsed(first, t0, "class")
            assert first["asr_active_s"] > 0
            assert first["asr_active_by_room"]["class"] > 0
            second = None
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                second = (await client.get("/api/metrics", headers=auth(token))).json()
                _assert_active_age_within_elapsed(second, t0, "class")
                if (
                    second["backlog_s"] == 0
                    and second["asr_active_s"] - first["asr_active_s"] >= 0.05
                    and second["asr_active_by_room"]["class"] - first["asr_active_by_room"]["class"] >= 0.05
                ):
                    break
                await asyncio.sleep(0.01)
            assert second is not None
            assert second["backlog_s"] == 0
            assert second["asr_active_s"] > 0
            assert second["asr_active_s"] - first["asr_active_s"] >= 0.05
            assert second["asr_active_by_room"]["class"] > 0
            assert second["asr_active_by_room"]["class"] - first["asr_active_by_room"]["class"] >= 0.05
            assert second["asr_active_by_room"]["class"] == pytest.approx(second["asr_active_s"], abs=0.05)
            assert second["backlog_audio_s"] == pytest.approx(1.0)
            assert second["backlog_estimated"] is False
            assert asr.calls == 1
            asr.release.set()
            done = await asyncio.wait_for(task, 3)
            assert done.status_code == 200, done.text
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            _assert_active_age_within_elapsed(after, t0)
            assert after["backlog_audio_s"] == 0
            assert after["rtf"]["session"]["audio_ms"]["max"] == 1000
    finally:
        release_decode.set()
        asr.release.set()
        await app.state.shutdown()


@pytest.mark.anyio
async def test_last_process_excludes_asr_slot_wait():
    """last_process_ms includes the slot wait. asr_ms, RTF, and process_ms do not."""
    asr = HoldAsr()

    def slow_decoder(src: Path, work: Path) -> Path:
        time.sleep(0.06)
        return copy_decoder(src, work)

    app = create_app(
        Settings(allow_testclient=True, max_audio_bytes=2_000_000, asr_workers=1),
        asr=asr,
        translator=Translator(enabled=True, key=""),
        decoder=slow_decoder,
    )
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = asyncio.create_task(push(client, token, 1, blob))
            assert await asyncio.to_thread(asr.started.wait, 2)
            second = asyncio.create_task(push(client, token, 2, blob))
            waiting = None
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                waiting = (await client.get("/api/metrics", headers=auth(token))).json()
                if asr.calls == 1 and waiting["pending"] == 2 and 0.5 <= waiting["backlog_s"] <= 1.01:
                    break
                await asyncio.sleep(0.02)
            assert waiting is not None
            assert waiting["backlog_s"] == pytest.approx(1.0, abs=0.001)
            await asyncio.sleep(0.45)
            asr.release.set()
            done = await asyncio.wait_for(asyncio.gather(first, second), 3)
            assert [item.status_code for item in done] == [200, 200]
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            samples = list(app.state.pipeline._rtf._window)
            assert len(samples) == 2
            waited = max(samples, key=lambda row: row[4])
            holder = min(samples, key=lambda row: row[4])
            waited_asr, waited_audio, waited_rtf, waited_decode, waited_wait = waited
            assert holder[0] >= 400
            assert waited_wait >= 400
            assert waited_asr < 200
            assert waited_asr < waited_wait / 2
            assert waited_decode >= 40
            assert waited_decode < waited_wait
            assert waited_audio == 1000
            assert waited_rtf == pytest.approx(waited_asr / waited_audio)
            assert waited_decode + waited_wait + waited_asr <= after["last_process_ms"] + 30
            assert after["last_process_ms"] + 50 >= waited_wait
            assert after["process_ms"] < waited_wait
            assert abs(after["process_ms"] - (waited_decode + waited_asr)) <= 50
            assert after["asr_wait_ms_p95"] >= 400
            assert after["decode_ms_p95"] is not None
            assert after["rtf"]["session"]["count"] == 2
            assert after["asr_rtf_p95"] == after["rtf"]["window"]["rtf"]["p95"]
            waits = [row[4] for row in samples]
            assert max(waits) >= 400
            assert min(waits) < 200
    finally:
        asr.release.set()
        await app.state.shutdown()


@pytest.mark.anyio
async def test_empty_asr_is_counted_apart_from_silence_and_timeouts():
    class Scripted:
        def __init__(self):
            self.calls = 0

        def transcribe(self, wav: Path, prompt: str) -> AsrResult:
            del wav, prompt
            self.calls += 1
            if self.calls == 1:
                return AsrResult(ok=True, text="   ")
            return AsrResult(ok=False, text="", error="辨識失敗")

    asr = Scripted()
    app = app_for(asr)
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            empty = await push(client, token, 1, blob)
            assert empty.status_code == 200, empty.text
            assert empty.json()["status"] == "silent"
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert body["asr_empty"] == 1
            assert body["asr_errors"] == 0
            assert body["silent_skipped"] == 0
            assert body["asr_timeouts"] == 0
            assert body["rtf"]["session"]["count"] == 1
            assert body["rtf"]["sessions"][0]["asr_empty"] == 1
            failed = await push(client, token, 2, blob)
            assert failed.status_code == 422, failed.text
            assert failed.json()["status"] == "error"
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            assert after["asr_empty"] == 1
            assert after["asr_errors"] == 1
            assert after["silent_skipped"] == 0
            assert after["asr_timeouts"] == 0
            assert after["asr_samples"] == 1
            assert after["rtf"]["session"]["count"] == 1
            assert after["rtf"]["window"]["count"] == 1
            assert after["rtf"]["sessions"][0]["count"] == 1
            assert after["rtf"]["sessions"][0]["asr_errors"] == 1
            assert after["rtf"]["sessions"][0]["asr_empty"] == 1
            assert asr.calls == 2
    finally:
        await app.state.shutdown()


def _rtf_check():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "tools" / "rtf_check.py"
    spec = importlib.util.spec_from_file_location("rtf_check_metrics_gate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_asr_errors_are_counted_apart_from_samples():
    meter = RtfMeter()
    meter.record_ms(200, 1000, ("room", "class"))
    before = meter.snapshot()["asr_rtf_p95"]
    meter.note_error(("room", "class"))
    meter.note_error(("room", "class"))
    snap = meter.snapshot()
    assert snap["asr_errors"] == 2
    assert snap["asr_timeouts"] == 0
    assert snap["asr_empty"] == 0
    assert snap["asr_samples"] == 1
    assert snap["asr_rtf_p95"] == before
    assert snap["rtf"]["window"]["count"] == 1
    assert _rows(snap)[("room", "class")]["asr_errors"] == 2
    assert _rows(snap)[("room", "class")]["count"] == 1
    meter.note_error(("room", "next"))
    after = meter.snapshot()
    assert after["asr_errors"] == 3
    assert after["rtf"]["session"]["count"] == 0
    assert after["rtf"]["window"]["count"] == 1
    assert after["asr_rtf_p95"] == before
    meter.drop_room("room")
    dropped = meter.snapshot()
    assert dropped["asr_errors"] == 3
    assert dropped["rtf"]["sessions"] == []
    text = Path("scripts/device_check.py").read_text(encoding="utf-8")
    assert '"asr_errors"' in text
    assert "asr_errors={row.get('asr_errors')}" in text
    assert '"backlog_audio_s": metrics.get("backlog_audio_s")' in text
    assert "backlog_audio_s={row.get('backlog_audio_s')}" in text
    assert "asr_active_s={row.get('asr_active_s')}" in text
    assert 'metrics.get("backlog_s", metrics.get("backlog_audio_s"))' not in text
    assert 'metrics.get("backlog_audio_s", metrics.get("backlog_s"))' not in text


@pytest.mark.anyio
async def test_all_asr_errors_leave_no_sample_and_fail_rtf_check():
    class AlwaysFail:
        def __init__(self):
            self.calls = 0

        def transcribe(self, wav: Path, prompt: str) -> AsrResult:
            del wav, prompt
            self.calls += 1
            return AsrResult(ok=False, text="", error="找不到 Breeze 模型")

    asr = AlwaysFail()
    app = app_for(asr)
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            for seq in range(1, 11):
                failed = await push(client, token, seq, blob)
                assert failed.status_code == 422, failed.text
                assert failed.json()["status"] == "error"
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert asr.calls == 10
            assert body["asr_errors"] == 10
            assert body["asr_samples"] == 0
            assert body["asr_rtf_p95"] is None
            assert body["asr_rtf_p50"] is None
            assert body["asr_timeouts"] == 0
            assert body["asr_empty"] == 0
            assert body["silent_skipped"] == 0
            assert body["rtf"]["window"]["count"] == 0
            assert body["rtf"]["session"]["count"] == 0
            assert body["rtf"]["session"]["rtf"]["p95"] is None
            assert body["backlog_s"] == 0
            row = _rows(body)[("class", "s")]
            assert row["count"] == 0
            assert row["asr_errors"] == 10
            assert row["recent"]["count"] == 0
            text, code = _rtf_check().render(body, "十段全錯")
            assert code != 0
            assert "結果：PASS" not in text
            assert "FAIL" in text
            assert "沒有 RTF 樣本" in text
            assert "辨識錯誤（不計入 RTF）：10" in text
    finally:
        await app.state.shutdown()


@pytest.mark.anyio
async def test_mixed_asr_results_record_only_successes():
    class Mixed:
        def __init__(self):
            self.calls = 0

        def transcribe(self, wav: Path, prompt: str) -> AsrResult:
            del wav, prompt
            self.calls += 1
            if self.calls % 2 == 0:
                return AsrResult(ok=False, text="沒算", error="辨識失敗")
            return AsrResult(ok=True, text="中文")

    asr = Mixed()
    app = app_for(asr)
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            codes = []
            for seq in range(1, 5):
                resp = await push(client, token, seq, blob)
                codes.append(resp.status_code)
            assert codes == [200, 422, 200, 422]
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert asr.calls == 4
            assert body["asr_errors"] == 2
            assert body["asr_empty"] == 0
            assert body["asr_samples"] == 2
            assert body["rtf"]["window"]["count"] == 2
            assert body["rtf"]["session"]["count"] == 2
            assert body["asr_rtf_p95"] is not None
            assert _rows(body)[("class", "s")]["asr_errors"] == 2
            assert _rows(body)[("class", "s")]["count"] == 2
            text, code = _rtf_check().render(body, "混合")
            assert code == 0
            assert "結果：PASS" in text
            assert "辨識錯誤（不計入 RTF）：2" in text
    finally:
        await app.state.shutdown()


@pytest.mark.anyio
async def test_asr_exception_is_counted_and_not_a_sample():
    class Boom:
        def __init__(self):
            self.calls = 0

        def transcribe(self, wav: Path, prompt: str) -> AsrResult:
            del wav, prompt
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("模型炸了")
            return AsrResult(ok=True, text="中文")

    asr = Boom()
    app = app_for(asr)
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            failed = await push(client, token, 1, blob)
            assert failed.status_code == 422, failed.text
            assert failed.json()["status"] == "error"
            assert "模型炸了" in failed.json()["detail"]
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert body["asr_errors"] == 1
            assert body["asr_timeouts"] == 0
            assert body["asr_empty"] == 0
            assert body["asr_samples"] == 0
            assert body["asr_rtf_p95"] is None
            assert body["rtf"]["window"]["count"] == 0
            assert body["rtf"]["session"]["count"] == 0
            assert body["backlog_s"] == 0
            assert _rows(body)[("class", "s")]["asr_errors"] == 1
            assert _rows(body)[("class", "s")]["count"] == 0
            text, code = _rtf_check().render(body, "例外")
            assert code != 0
            assert "結果：PASS" not in text
            done = await push(client, token, 2, blob)
            assert done.status_code == 200, done.text
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            assert asr.calls == 2
            assert after["asr_errors"] == 1
            assert after["asr_samples"] == 1
            assert after["rtf"]["window"]["count"] == 1
            assert after["rtf"]["session"]["count"] == 1
            assert after["asr_rtf_p95"] is not None
            assert _rows(after)[("class", "s")]["asr_errors"] == 1
            assert _rows(after)[("class", "s")]["count"] == 1
    finally:
        await app.state.shutdown()


def test_asr_active_age_grows_until_recognition_ends():
    meter = RtfMeter()
    idle = meter.snapshot()
    assert idle["asr_active_s"] == 0
    assert isinstance(idle["asr_active_s"], int)
    assert idle["asr_active_by_room"] == {}
    t0 = time.perf_counter()
    meter.note_asr_active(("room", "s", 1), "room")
    first = meter.snapshot()
    _assert_active_age_within_elapsed(first, t0, "room")
    assert first["asr_active_s"] > 0
    assert first["asr_active_by_room"]["room"] > 0
    for _ in range(100):
        sample = meter.snapshot()
        _assert_active_age_within_elapsed(sample, t0, "room")
        assert sample["asr_active_s"] > 0
    second = None
    deadline = time.perf_counter() + 1
    while time.perf_counter() < deadline:
        second = meter.snapshot()
        _assert_active_age_within_elapsed(second, t0, "room")
        if (
            second["asr_active_s"] - first["asr_active_s"] >= 0.05
            and second["asr_active_by_room"]["room"] - first["asr_active_by_room"]["room"] >= 0.05
        ):
            break
        time.sleep(0.001)
    assert second is not None
    assert second["asr_active_s"] > first["asr_active_s"]
    assert second["asr_active_s"] - first["asr_active_s"] >= 0.05
    assert second["asr_active_by_room"]["room"] > 0
    assert second["asr_active_by_room"]["room"] - first["asr_active_by_room"]["room"] >= 0.05
    assert second["asr_active_by_room"]["room"] == pytest.approx(second["asr_active_s"], abs=0.02)
    meter.clear_asr_active(("room", "s", 1))
    cleared = meter.snapshot()
    _assert_active_age_within_elapsed(cleared, t0)
    assert cleared["asr_active_s"] == 0
    assert isinstance(cleared["asr_active_s"], int)
    assert cleared["asr_active_by_room"] == {}


def test_silence_only_session_replaces_the_previous_room_and_stays_capped():
    meter = RtfMeter()
    meter.record_ms(1200, 1000, ("east", "old"))
    assert meter.snapshot()["rtf"]["session"]["rtf"]["p95"] == pytest.approx(1.2)
    meter.note_silent_skip(("east", "new"))
    snap = meter.snapshot()
    rows = _rows(snap)
    assert ("east", "old") not in rows
    assert rows[("east", "new")]["count"] == 0
    assert rows[("east", "new")]["silent_skipped"] == 1
    assert rows[("east", "new")]["rtf"]["p95"] is None
    checker = _rtf_check()
    assert checker.verdict_p95(snap) is None
    text, code = checker.render(snap, "靜音換場")
    assert code == 1
    assert "沒有 RTF 樣本" in text
    detail = text.split("本場以來", 1)[1].split("辨識逾時", 1)[0]
    assert "1.200" not in detail
    assert "1.200" not in text.split("結果：", 1)[1]

    same = RtfMeter()
    for index in range(10):
        same.note_silent_skip(("east", f"s{index}"))
    only = same.snapshot()["rtf"]["sessions"]
    assert len(only) == 1
    assert only[0]["session_id"] == "s9"
    assert only[0]["count"] == 0
    assert only[0]["silent_skipped"] == 1

    total = SESSION_ROOM_CAP + 5
    capped = RtfMeter()
    for index in range(total):
        capped.note_silent_skip((f"room{index}", "s"))
    held = capped.snapshot()["rtf"]["sessions"]
    assert len(held) == SESSION_ROOM_CAP
    assert len(capped._silent) <= SESSION_ROOM_CAP
    assert "room0" not in {row["room_id"] for row in held}
    assert f"room{total - 1}" in {row["room_id"] for row in held}

    emptied = RtfMeter()
    for index in range(total):
        emptied.note_empty((f"empty{index}", "s"))
    empty_rows = emptied.snapshot()["rtf"]["sessions"]
    assert len(empty_rows) == SESSION_ROOM_CAP
    assert len(emptied._empty) <= SESSION_ROOM_CAP


class _StuckAsr:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        del wav, prompt
        self.calls += 1
        self.started.set()
        self.release.wait(5)
        return AsrResult(ok=True, text="不該被採用")


@pytest.mark.anyio
async def test_stuck_recognition_grows_decoded_backlog_then_timeout_clears_it():
    asr = _StuckAsr()
    app = app_for(asr, asr_workers=1, asr_timeout_s=1.0)
    blob = wave_bytes(20.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            t0 = time.perf_counter()
            task = asyncio.create_task(push(client, token, 1, blob))
            assert await asyncio.to_thread(asr.started.wait, 2)
            first = (await client.get("/api/metrics", headers=auth(token))).json()
            _assert_active_age_within_elapsed(first, t0, "class")
            assert first["asr_active_s"] > 0
            assert first["asr_active_by_room"]["class"] > 0
            second = None
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                second = (await client.get("/api/metrics", headers=auth(token))).json()
                _assert_active_age_within_elapsed(second, t0, "class")
                if (
                    second["asr_active_s"] - first["asr_active_s"] >= 0.2
                    and second["asr_active_by_room"]["class"] - first["asr_active_by_room"]["class"] >= 0.2
                    and second["oldest_wait_ms"] > first["oldest_wait_ms"]
                ):
                    break
                await asyncio.sleep(0.01)
            assert second is not None
            assert first["backlog_audio_s"] == pytest.approx(20.0, abs=0.001)
            assert second["backlog_audio_s"] == pytest.approx(20.0, abs=0.001)
            assert first["backlog_s"] == 0 and second["backlog_s"] == 0
            assert first["inflight"] == 1 and second["inflight"] == 1
            assert second["oldest_wait_ms"] > first["oldest_wait_ms"]
            assert second["asr_active_s"] > first["asr_active_s"]
            assert second["asr_active_s"] - first["asr_active_s"] >= 0.2
            assert second["asr_active_by_room"]["class"] > 0
            assert second["asr_active_by_room"]["class"] - first["asr_active_by_room"]["class"] >= 0.2
            assert second["asr_active_by_room"]["class"] == pytest.approx(second["asr_active_s"], abs=0.05)
            done = await asyncio.wait_for(task, 3)
            assert done.status_code == 408, done.text
            assert done.json()["status"] == "timeout"
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            _assert_active_age_within_elapsed(body, t0)
            assert body["asr_timeouts"] == 1
            assert body["asr_errors"] == 0
            assert body["asr_samples"] == 0
            assert body["inflight"] == 0
            assert body["backlog_audio_s"] == 0
            assert body["backlog_s"] == 0
            assert body["asr_active_s"] == 0
            assert isinstance(body["backlog_audio_s"], int)
            assert isinstance(body["asr_active_s"], int)
            assert body["last_process_ms"] is None
            assert body["process_ms"] is None
            assert body["rtf"]["window"]["count"] == 0
            assert body["rtf"]["session"]["count"] == 0
            assert _rows(body)[("class", "s")]["count"] == 0
            assert _rows(body)[("class", "s")]["asr_timeouts"] == 1
    finally:
        asr.release.set()
        await app.state.shutdown()


@pytest.mark.anyio
async def test_upload_estimate_clears_after_decode_failure_or_cancel():
    entered = threading.Event()
    release_decode = threading.Event()

    def failing_decoder(src: Path, work: Path) -> Path:
        del src, work
        entered.set()
        assert release_decode.wait(3), "decode was not released"
        raise RuntimeError("解碼失敗了")

    app = create_app(
        Settings(allow_testclient=True, max_audio_bytes=2_000_000, segment_ms=6000),
        asr=TimedAsr(0),
        translator=Translator(enabled=True, key=""),
        decoder=failing_decoder,
    )
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            task = asyncio.create_task(push(client, token, 1, blob))
            assert await asyncio.to_thread(entered.wait, 2)
            during = (await client.get("/api/metrics", headers=auth(token))).json()
            assert during["backlog_audio_s"] == 0
            assert during["backlog_s"] == pytest.approx(6.0)
            assert during["backlog_estimated"] is True
            release_decode.set()
            failed = await asyncio.wait_for(task, 3)
            assert failed.status_code == 422, failed.text
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            assert after["backlog_s"] == 0
            assert after["backlog_audio_s"] == 0
            assert after["backlog_estimated"] is False
            assert isinstance(after["backlog_s"], int)
    finally:
        release_decode.set()
        await app.state.shutdown()

    entered_cancel = threading.Event()
    release_cancel = threading.Event()
    asr = TimedAsr(0)

    def held_decoder(src: Path, work: Path) -> Path:
        entered_cancel.set()
        assert release_cancel.wait(3), "cancel decode was not released"
        return copy_decoder(src, work)

    app = create_app(
        Settings(allow_testclient=True, max_audio_bytes=2_000_000, segment_ms=6000),
        asr=asr,
        translator=Translator(enabled=True, key=""),
        decoder=held_decoder,
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            task = asyncio.create_task(push(client, token, 1, blob))
            assert await asyncio.to_thread(entered_cancel.wait, 2)
            during = (await client.get("/api/metrics", headers=auth(token))).json()
            assert during["backlog_audio_s"] == 0
            assert during["backlog_s"] == pytest.approx(6.0)
            assert during["backlog_estimated"] is True
            cancelled = await client.post(
                "/api/segment/cancel",
                json={"room_id": "class", "session_id": "s", "seq": 1},
                headers=auth(token),
            )
            assert cancelled.status_code == 200, cancelled.text
            release_cancel.set()
            done = await asyncio.wait_for(task, 3)
            assert done.status_code == 409, done.text
            assert done.json()["status"] == "cancelled"
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            assert after["backlog_s"] == 0
            assert after["backlog_audio_s"] == 0
            assert after["backlog_estimated"] is False
            assert asr.calls == 0
    finally:
        release_cancel.set()
        await app.state.shutdown()


def test_resident_blank_result_is_not_an_error(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"not-read")
    asr = ResidentAsr("http://127.0.0.1:9", transport=lambda path, prompt: " \n ", server_bin=None)
    assert asr.start().ok is True
    result = asr.transcribe(wav, "提示")
    assert result.ok is True
    assert result.blank is True
    assert result.text == ""
    assert result.error == ""
    assert AsrResult(ok=True, text="").blank is False


@pytest.mark.anyio
async def test_resident_blank_counts_as_empty_and_not_a_speed_sample():
    asr = ResidentAsr("http://127.0.0.1:9", transport=lambda path, prompt: "   ", server_bin=None)
    assert asr.start().ok is True
    app = app_for(asr)
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            resp = await push(client, token, 1, blob)
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "silent"
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert asr.calls == 1
            assert body["asr_empty"] == 1
            assert body["asr_errors"] == 0
            assert body["asr_timeouts"] == 0
            assert body["silent_skipped"] == 0
            assert body["asr_samples"] == 0
            assert body["rtf"]["window"]["count"] == 0
            assert body["rtf"]["session"]["count"] == 0
            assert body["asr_rtf_p95"] is None
            row = _rows(body)[("class", "s")]
            assert row["count"] == 0
            assert row["asr_empty"] == 1
            assert row["asr_errors"] == 0
    finally:
        await app.state.shutdown()
