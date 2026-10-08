import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.asr import AsrResult, CliAsr
from app.server import create_app
from app.settings import Settings
from app.translate import TranslateResult, Translator


class FakeAsr:
    def __init__(self, delay_for=None):
        self.calls = 0
        self.delay_for = delay_for or {}
        self.seen = []

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        text = wav.read_bytes().decode()
        self.seen.append(text)
        import time
        time.sleep(self.delay_for.get(text, 0))
        self.calls += 1
        if text == "fail":
            return AsrResult(ok=False, text="partial", error="辨識程序失敗")
        return AsrResult(ok=True, text=f"中文{text}")


class BoomTranslator(Translator):
    def translate(self, zh: str) -> TranslateResult:
        self.calls += 1
        raise RuntimeError("英譯爆炸")


def decoder(src: Path, work: Path) -> Path:
    wav = work / "audio.wav"
    wav.write_bytes(src.read_bytes())
    return wav


def make_app(asr=None, translator=None):
    app = create_app(
        Settings(allow_testclient=True, max_audio_bytes=32),
        asr=asr or FakeAsr(),
        translator=translator or Translator(enabled=True, key=""),
        decoder=decoder,
    )
    return app


async def token_of(app, client):
    resp = await client.get("/api/host-token")
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"] == app.state.token
    return body["token"]


def auth(token):
    return {"authorization": f"Bearer {token}", "origin": "http://127.0.0.1"}


@pytest.mark.anyio
async def test_push_requires_host_and_hides_token(monkeypatch):
    from app import share
    monkeypatch.setattr(share, "lan_ip", lambda: None)
    app = make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8780") as client:
        denied = await client.post("/api/push", data={"room_id": "class", "session_id": "s", "seq": "1"}, files={"audio": ("a.webm", b"1234", "audio/webm")})
        assert denied.status_code == 401
        setup = await client.get("/api/setup", params={"room_id": "class"})
        assert setup.json()["host_token"] is None
        assert app.state.token not in setup.text
        qr = await client.get("/api/qr", params={"room_id": "class"})
        assert qr.status_code == 409
        assert "尚無可供其他裝置使用的連結" in qr.text
        assert app.state.token not in qr.text


@pytest.mark.anyio
async def test_translate_failure_keeps_chinese():
    asr = FakeAsr()
    app = make_app(asr=asr, translator=BoomTranslator(enabled=True, key="secret"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8780") as client:
        token = await token_of(app, client)
        resp = await client.post(
            "/api/push",
            data={"room_id": "class", "session_id": "s", "seq": "1"},
            files={"audio": ("a.webm", b"1", "audio/webm")},
            headers=auth(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["zh"] == "中文1"
        assert body["en"] == ""
        assert body["status"] == "translate_failed"
        assert asr.calls == 1


@pytest.mark.anyio
async def test_retry_after_cancel_is_processed():
    import threading

    class GateAsr:
        def __init__(self):
            self.calls = 0
            self.entered = threading.Event()
            self.release = threading.Event()

        def transcribe(self, wav: Path, prompt: str) -> AsrResult:
            del prompt
            self.entered.set()
            assert self.release.wait(2), "cancel test did not release ASR"
            self.calls += 1
            return AsrResult(ok=True, text=wav.read_bytes().decode() or "中文")

    asr = GateAsr()
    app = make_app(asr=asr)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
        try:
            token = await token_of(app, client)
            headers = {**auth(token), "content-type": "application/json"}

            async def send(retry: bool):
                extra = {"x-breeze-retry": "1"} if retry else {}
                return await client.post(
                    "/api/push",
                    data={"room_id": "class", "session_id": "s", "seq": "1", **({"retry": "1"} if retry else {})},
                    files={"audio": ("a.webm", b"1", "audio/webm")},
                    headers={**auth(token), **extra},
                )

            first = asyncio.create_task(send(False))
            assert await asyncio.to_thread(asr.entered.wait, 2)
            cancelled = await client.post(
                "/api/segment/cancel",
                json={"room_id": "class", "session_id": "s", "seq": 1},
                headers=headers,
            )
            assert cancelled.status_code == 200
            asr.release.set()
            done = await asyncio.wait_for(first, 2)
            assert done.status_code == 409
            assert done.json()["status"] == "cancelled"
            again = await asyncio.wait_for(send(True), 2)
            body = again.json()
            assert again.status_code == 200, again.text
            assert body["status"] != "cancelled"
            assert body["zh"]
            assert asr.calls == 2
        finally:
            asr.release.set()


@pytest.mark.anyio
async def test_retry_does_not_transcribe_twice():
    asr = FakeAsr()
    app = make_app(asr=asr)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8780") as client:
        token = await token_of(app, client)
        headers = auth(token)
        first = await client.post("/api/push", data={"room_id": "class", "session_id": "s", "seq": "1"}, files={"audio": ("a.webm", b"1", "audio/webm")}, headers=headers)
        second = await client.post("/api/push", data={"room_id": "class", "session_id": "s", "seq": "1"}, files={"audio": ("a.webm", b"1", "audio/webm")}, headers=headers)
        assert first.json()["id"] == second.json()["id"]
        assert asr.calls == 1


@pytest.mark.anyio
async def test_out_of_order_asr_still_broadcasts_by_seq():
    asr = FakeAsr(delay_for={"1": 0.2})
    app = make_app(asr=asr)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8780") as client:
        token = await token_of(app, client)
        headers = auth(token)

        async def send(seq, payload):
            return await client.post(
                "/api/push",
                data={"room_id": "class", "session_id": "s", "seq": str(seq)},
                files={"audio": ("a.webm", payload, "audio/webm")},
                headers=headers,
            )

        await asyncio.gather(send(1, b"1"), send(2, b"2"))
        seqs = [item["seq"] for item in app.state.pipeline.broadcasts if item["status"] in {"zh_ready", "ready", "translate_failed"}]
        firsts = []
        seen = set()
        for seq in seqs:
            if seq not in seen:
                firsts.append(seq)
                seen.add(seq)
        assert firsts == [1, 2]


@pytest.mark.anyio
async def test_room_id_not_truncated_and_invalid_rejected():
    app = make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8780") as client:
        token = await token_of(app, client)
        long_a = "a" * 40
        long_b = "a" * 32 + "b" * 8
        headers = auth(token)
        await client.post("/api/push", data={"room_id": long_a, "session_id": "s", "seq": "1"}, files={"audio": ("a.webm", b"1", "audio/webm")}, headers=headers)
        await client.post("/api/push", data={"room_id": long_b, "session_id": "s", "seq": "1"}, files={"audio": ("a.webm", b"2", "audio/webm")}, headers=headers)
        assert long_a in app.state.rooms
        assert long_b in app.state.rooms
        bad = await client.post("/api/push", data={"room_id": "中文房", "session_id": "s", "seq": "1"}, files={"audio": ("a.webm", b"1", "audio/webm")}, headers=headers)
        assert bad.status_code == 400


@pytest.mark.anyio
async def test_oversize_rejected():
    app = make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8780") as client:
        token = await token_of(app, client)
        resp = await client.post(
            "/api/push",
            data={"room_id": "class", "session_id": "s", "seq": "1"},
            files={"audio": ("a.webm", b"x" * 40, "audio/webm")},
            headers=auth(token),
        )
        assert resp.status_code == 413


def test_cli_nonzero_is_failure(tmp_path: Path):
    whisper = tmp_path / "whisper-cli.exe"
    model = tmp_path / "model.bin"
    whisper.write_text("x")
    model.write_text("x")

    class Proc:
        returncode = 1
        stdout = "partial text"
        stderr = "boom"

    asr = CliAsr(whisper, model, runner=lambda *args, **kwargs: Proc())
    result = asr.transcribe(tmp_path / "a.wav", "prompt")
    assert result.ok is False
    assert result.loaded_once is False


def test_translation_prompt_does_not_answer():
    from app.translate import SYSTEM
    assert "Do not answer" in SYSTEM
    assert "questions" in SYSTEM
