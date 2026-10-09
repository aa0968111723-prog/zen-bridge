import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.asr import AsrResult
from app.translate import TranslateResult, Translator
from tests.test_pipeline_repair import EchoAsr, Socket, app_for, auth, push, settings_with, stop, token_of


class SlowAsr(EchoAsr):
    def transcribe(self, wav, prompt: str) -> AsrResult:
        time.sleep(0.35)
        return super().transcribe(wav, prompt)


class SlowEnglish(Translator):
    def __init__(self, delay: float):
        super().__init__(enabled=True, key="k")
        self.delay = delay

    def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
        del glossary, context, deadline
        if cancel is not None and cancel.wait(self.delay):
            return TranslateResult("", "timeout", "英譯逾時，不假設沒有計費。中文仍保留")
        if cancel is None:
            time.sleep(self.delay)
        return TranslateResult("EN " + zh, "ok")


def _json_headers(token):
    return {**auth(token), "content-type": "application/json"}


@pytest.mark.anyio
async def test_stop_last_chunk_uploaded_after_end_is_not_lost(tmp_path):
    app = app_for(
        asr=SlowAsr(),
        translator=Translator(enabled=False),
        settings=settings_with(
            allow_testclient=True,
            gap_wait_s=30,
            stop_flush_s=2,
            data_path=str(tmp_path / "captions.sqlite3"),
        ),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            inflight = asyncio.create_task(push(client, token, "class", "live", 1, "尾段".encode(), t0_ms=0, t1_ms=400))
            for _ in range(200):
                if ("class", "live", 1) in app.state.pipeline._active:
                    break
                await asyncio.sleep(0.01)
            assert ("class", "live", 1) in app.state.pipeline._active
            ending = asyncio.create_task(client.post(
                "/api/session/end",
                json={"room_id": "class", "session_id": "live"},
                headers=_json_headers(token),
            ))
            for _ in range(100):
                if ("class", "live") in app.state.pipeline._flushing:
                    break
                await asyncio.sleep(0.01)
            assert ("class", "live") in app.state.pipeline._flushing
            arrived = await push(client, token, "class", "live", 2, "剛到".encode(), t0_ms=400, t1_ms=800)
            assert arrived.status_code == 200, arrived.text
            assert arrived.json()["zh"] == "剛到"
            assert arrived.json()["status"] != "missing"
            done_push, done_end = await asyncio.wait_for(asyncio.gather(inflight, ending), 3)
            assert done_end.status_code == 200, done_end.text
            assert done_push.status_code == 200, done_push.text
            assert done_push.json()["zh"] == "尾段"
            assert done_push.json()["status"] != "missing"
            await asyncio.to_thread(app.state.store.flush)
            stored = { (row["session_id"], row["seq"]): row for row in app.state.store.room_rows("class") }
            assert stored[("live", 1)]["zh"] == "尾段"
            assert stored[("live", 1)]["status"] != "missing"
            assert stored[("live", 2)]["zh"] == "剛到"
            assert stored[("live", 2)]["status"] != "missing"
            late = await push(client, token, "class", "live", 3, "太晚".encode())
            assert late.status_code == 409
            assert "已結束" in late.text
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_stop_flushes_pending_translations_to_store(tmp_path):
    app = app_for(
        translator=SlowEnglish(0.25),
        settings=settings_with(
            allow_testclient=True,
            stop_flush_s=2,
            translate_timeout_s=2,
            data_path=str(tmp_path / "captions.sqlite3"),
        ),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            resp = await push(
                client, token, "class", "s", 1, "留下".encode(),
                wait_translation="0", async_header=True, t0_ms=0, t1_ms=500,
            )
            assert resp.status_code == 200
            assert resp.json()["zh"] == "留下"
            assert resp.json()["en"] == ""
            started = time.monotonic()
            ending = await client.post(
                "/api/session/end",
                json={"room_id": "class", "session_id": "s"},
                headers=_json_headers(token),
            )
            assert ending.status_code == 200, ending.text
            assert time.monotonic() - started < 2
            await asyncio.to_thread(app.state.store.flush)
            rows = app.state.store.room_rows("class")
            assert rows and rows[0]["zh"] == "留下"
            assert rows[0]["en"] == "EN 留下"
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_close_room_closes_listener_sockets():
    app = app_for(settings=settings_with(allow_testclient=True, translate=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            opened = await client.post(
                "/api/rooms/open",
                json={"room_id": "class"},
                headers=_json_headers(token),
            )
            assert opened.status_code == 200
            async with Socket(app, "/ws/listen?room_id=class&cursor=0") as ws:
                hello = await ws.recv()
                assert hello["type"] == "hello"
                closed = await client.post(
                    "/api/rooms/close",
                    json={"room_id": "class"},
                    headers=_json_headers(token),
                )
                assert closed.status_code == 200
                seen = []
                for _ in range(6):
                    msg = await ws.recv(timeout=2)
                    seen.append(msg)
                    if msg.get("type") == "websocket.close":
                        break
                assert any(item.get("type") == "room_unavailable" for item in seen), seen
                assert any(item.get("type") == "websocket.close" and item.get("code") == 4404 for item in seen), seen
    finally:
        await stop(app)
