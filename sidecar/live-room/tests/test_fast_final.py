"""grok/fast-draft-asr: BREEZE_FAST_FINAL. Endpoint-cut lines are answered by a fast offline model,
then Breeze re-hears them in the background and replaces the line (zh + translation) if it differs.
Off by default; 6 s slices and every non-endpoint caller still reach Breeze directly."""
import asyncio
import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app import fast_final
from app.asr import AsrResult
from tests.test_draft_finalize import English, WavAsr, _app, _drain, _send_pcm
from tests.test_endpointer import silence, tone
from tests.test_round2 import Socket, auth, open_room, stop, token_of


class FakeRecognizer:
    def __init__(self, text="快稿"):
        self.text = text
        self.calls = 0

    def create_stream(self):
        rec = self

        class S:
            result = SimpleNamespace(text="")

            def accept_waveform(self, rate, samples):
                assert rate == 16000 and samples

        s = S()

        def decode(_s=s):
            rec.calls += 1
            s.result = SimpleNamespace(text=rec.text)
        s.decode = decode
        return s

    def decode_stream(self, s):
        s.decode()


def _fast(text="快稿"):
    return fast_final.SherpaOfflineFinal("sense_voice", None, recognizer=FakeRecognizer(text), convert=lambda t: t)


def _wav(tmp_path: Path, seconds=1.0) -> Path:
    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(tone(seconds))
    return p


def test_routing_only_inside_the_fast_context(tmp_path):
    breeze = WavAsr()
    asr = fast_final.FastFinalAsr(breeze, _fast())
    wav = _wav(tmp_path)
    assert asr.transcribe(wav, "p").text == "定稿1", "default route is Breeze"
    tok = fast_final.ROUTE.set("fast")
    try:
        assert asr.transcribe(wav, "p").text == "快稿"
    finally:
        fast_final.ROUTE.reset(tok)
    assert breeze.calls == [1000]


def test_fast_failure_falls_back_to_breeze(tmp_path):
    class Broken(FakeRecognizer):
        def decode_stream(self, s):
            raise RuntimeError("onnx")
    breeze = WavAsr()
    asr = fast_final.FastFinalAsr(breeze, fast_final.SherpaOfflineFinal(
        "paraformer", None, recognizer=Broken(), convert=lambda t: t))
    tok = fast_final.ROUTE.set("fast")
    try:
        assert asr.transcribe(_wav(tmp_path), "p").text == "定稿1"
    finally:
        fast_final.ROUTE.reset(tok)


def test_env_off_by_default_and_fails_soft(tmp_path):
    assert fast_final.fast_final_from_env({}) is None
    assert fast_final.fast_final_from_env({"BREEZE_FAST_FINAL": "sense_voice",
                                           "BREEZE_FAST_FINAL_MODEL_DIR": str(tmp_path)}) is None
    with pytest.raises(ValueError):
        fast_final.SherpaOfflineFinal("qwen", None, recognizer=FakeRecognizer(), convert=str)


@pytest.mark.anyio
async def test_corrector_keeps_same_text_replaces_different_and_skips_backlog(tmp_path):
    applied = []

    class Breeze:
        def transcribe(self, wav, prompt):
            return AsrResult(ok=True, text="今天，我們" if "-1." in Path(wav).name else "行禪")

    async def apply(*a):
        applied.append(a)

    c = fast_final.Corrector(Breeze(), "p", tmp_path, apply, max_pending=2)
    wav = _wav(tmp_path).read_bytes()

    async def settle():
        for _ in range(100):
            if c._task and c._task.done():
                return
            await asyncio.sleep(0.01)

    c.submit("r", "s", 1, wav, "今天我們")       # same once punctuation is ignored -> kept
    await settle()
    c.submit("r", "s", 2, wav, "行程")           # backlog of 3 > 2 while Breeze is busy:
    c.submit("r", "s", 3, wav, "行程")           # the oldest pending line keeps its fast text
    c.submit("r", "s", 4, wav, "行程")
    await settle()
    assert c.stats() == {"pending": 0, "corrected": 2, "kept": 1, "skipped": 1}
    assert [a[2] for a in applied] == [3, 4] and all(a[3] == "行禪" for a in applied)
    assert not list(tmp_path.glob("correct-*")), "temp wavs are removed"


@pytest.mark.anyio
async def test_fast_final_end_to_end_then_breeze_correction(monkeypatch):
    monkeypatch.setenv("BREEZE_ENDPOINT_FINAL", "1")
    monkeypatch.setattr(fast_final, "fast_final_from_env", lambda env=None: _fast())
    asr, en = WavAsr(), English()
    app = _app(asr, en)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            setup = (await client.get("/api/setup?room_id=class", headers=auth(token))).json()
            assert setup["fast_final"] == "sense_voice"
            async with Socket(app, "/ws/listen?room_id=class") as listener:
                await _drain(listener, {"hello"})
                async with Socket(app, "/ws/draft?room_id=class&session_id=s1", with_key=False) as host:
                    await host.inc.put({"type": "websocket.receive",
                                        "text": json.dumps({"token": token, "finalize": True})})
                    await host.recv()
                    await _send_pcm(host, silence(0.3) + tone(1.2) + silence(0.6))
                    first = await _drain(listener, {"caption"})
                    assert first["id"] == "class:s1:1" and first["zh"].startswith("快稿")
                    fixed = None
                    for _ in range(40):
                        msg = await listener.recv(timeout=5)
                        if msg.get("type") == "caption" and (msg.get("zh") or "").startswith("定稿") \
                                and msg.get("en"):
                            fixed = msg
                            break
                    assert fixed and fixed["id"] == "class:s1:1" and fixed["en"].startswith("EN 定稿1")
            assert asr.calls and len(asr.calls) == 1, "Breeze heard the line once, in the corrector"
    finally:
        await stop(app)
