"""The silence scan runs only when the gate is on, and never on the event loop."""

import array
import struct
import threading

import pytest
from httpx import ASGITransport, AsyncClient

from app.asr import AsrResult
from app.audio import wav_rms
from app.settings import Settings
from app.translate import Translator
from tests.test_round2 import app_for, auth, stop, token_of


def _pcm_wav(pcm: bytes, rate: int = 16000) -> bytes:
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    data = b"data" + struct.pack("<I", len(pcm)) + pcm
    return header + fmt + data


def _quiet_wav(seconds: float = 0.4) -> bytes:
    count = int(16000 * seconds)
    return _pcm_wav(b"\x00\x00" * count)


def _loud_wav(seconds: float = 0.4) -> bytes:
    count = int(16000 * seconds)
    samples = array.array("h", [8000]) * count
    return _pcm_wav(samples.tobytes())


class CountAsr:
    def __init__(self, fail_once: bool = False):
        self.calls = 0
        self.fail_once = fail_once

    def transcribe(self, wav, prompt: str = "") -> AsrResult:
        del wav, prompt
        self.calls += 1
        if self.fail_once and self.calls == 1:
            return AsrResult(ok=False, text="", error="辨識程序失敗")
        return AsrResult(ok=True, text="中文")


def _spy(monkeypatch):
    seen: list[tuple[int, bool]] = []
    main = threading.main_thread().ident

    def wrapped(path):
        try:
            asyncio_loop = __import__("asyncio").get_running_loop()
        except RuntimeError:
            asyncio_loop = None
        seen.append((threading.get_ident(), asyncio_loop is not None))
        return wav_rms(path)

    monkeypatch.setattr("app.pipeline.wav_rms", wrapped)
    return seen, main


async def _push(client, token, seq: int, payload: bytes, *, retry: bool = False):
    headers = auth(token)
    data = {"room_id": "class", "session_id": "s", "seq": str(seq)}
    if retry:
        data["retry"] = "1"
        headers["x-breeze-retry"] = "1"
    return await client.post(
        "/api/push",
        data=data,
        files={"audio": ("a.wav", payload, "audio/wav")},
        headers=headers,
    )


@pytest.mark.anyio
async def test_silence_gate_off_never_calls_wav_rms(monkeypatch):
    seen, _main = _spy(monkeypatch)
    asr = CountAsr()
    app = app_for(
        settings=Settings(allow_testclient=True, silence_rms=0),
        asr=asr,
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            resp = await _push(client, token, 1, _quiet_wav())
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["zh"] == "中文"
            assert body["status"] != "silent"
            assert asr.calls == 1
            assert seen == []
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_silence_gate_on_skips_quiet_audio_off_the_loop(monkeypatch):
    seen, main = _spy(monkeypatch)
    asr = CountAsr()
    app = app_for(
        settings=Settings(allow_testclient=True, silence_rms=500),
        asr=asr,
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            quiet = await _push(client, token, 1, _quiet_wav())
            assert quiet.status_code == 200, quiet.text
            body = quiet.json()
            assert body["status"] == "silent"
            assert body["error"] == "這段太安靜，沒有送去辨識"
            assert body["zh"] == ""
            assert asr.calls == 0
            assert len(seen) == 1
            thread, on_loop = seen[0]
            assert thread != main
            assert on_loop is False
            loud = await _push(client, token, 2, _loud_wav())
            assert loud.status_code == 200, loud.text
            assert loud.json()["zh"] == "中文"
            assert loud.json()["status"] != "silent"
            assert asr.calls == 1
            assert len(seen) == 2
            thread, on_loop = seen[1]
            assert thread != main
            assert on_loop is False
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_retry_scans_twice_only_when_the_gate_is_on(monkeypatch):
    seen, main = _spy(monkeypatch)
    asr = CountAsr(fail_once=True)
    loud = _loud_wav()
    app = app_for(
        settings=Settings(allow_testclient=True, silence_rms=500),
        asr=asr,
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = await _push(client, token, 1, loud)
            assert first.status_code == 422, first.text
            assert first.json()["status"] == "error"
            again = await _push(client, token, 1, loud, retry=True)
            assert again.status_code == 200, again.text
            assert again.json()["zh"] == "中文"
            assert asr.calls == 2
            assert len(seen) == 2
            for thread, on_loop in seen:
                assert thread != main
                assert on_loop is False
    finally:
        await stop(app)

    seen_off, _main = _spy(monkeypatch)
    asr_off = CountAsr(fail_once=True)
    app_off = app_for(
        settings=Settings(allow_testclient=True, silence_rms=0),
        asr=asr_off,
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app_off), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app_off, client)
            first = await _push(client, token, 1, loud)
            assert first.status_code == 422, first.text
            again = await _push(client, token, 1, loud, retry=True)
            assert again.status_code == 200, again.text
            assert asr_off.calls == 2
            assert seen_off == []
    finally:
        await stop(app_off)
